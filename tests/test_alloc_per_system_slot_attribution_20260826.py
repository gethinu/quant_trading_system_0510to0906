# -*- coding: utf-8 -*-
"""回帰テスト — 保有ポジションの system 帰属が配分段まで届くこと (2026-08-26)。

背景
----
``09956a5`` (2026-08-04) が ``_fetch_positions_and_symbol_map`` に entry coid
(``system{N}-SYM-YYYYMMDD``) 由来の帰属マージを入れたが、**呼び出し側**
``_resolve_positions_for_allocation`` は::

    if not symbol_system_map and fetched_map:
        symbol_system_map = fetched_map

と「static map が空のときだけ」取り込んでいた。``data/symbol_system_map.json``
は 84 銘柄入っていて常に truthy なので、coid 由来の帰属は **毎回丸ごと捨てられ**、
配分には stale な static map だけが渡っていた。

実測 (2026-08-26 22:35 の本番 run):

- ``logs/today_signals_20260826_2235.log``: coid map を **277 銘柄** 構築 → 破棄
- ``results_csv/today_signals_20260826.json``: ``held_unmapped == held``
  (long 27 / short 2 / total 29 が 1 件も system に帰属できていない)
- 結果 ``available_slots[s] = max_positions - 0`` = **全 system 10 に張り付き**
- system1 は保有 7 + 新規 9、system2 は保有 2 + 新規 10 で自身の枠 10 を超過。
  system2 の超過は発注境界の standing cap が
  ``standing_cap:system2_held=2+batch=8>=cap=10`` で 2 件落として救済していた
  (= 配分段では枠が効いていなかった証拠)。

本テストの契約
--------------
1. ``_resolve_positions_for_allocation`` が返す map で **保有が system に帰属する**
   (static が stale でも空振りしない)。旧実装ではここが空になる。
2. 現実的な保有 (2026-08-26 実データ形状) で
   **system 別 held + 新規 <= max_positions** が成り立つ。
3. その検査が空振りでないこと — stale map だけを渡すと同じ検査が **落ちる**。
4. long/short/合計プール上限 (40/30/70) の判定は本修正で **変わらない**。

参照: ``docs/SLOT_MODEL_AND_DASHBOARD_REDESIGN_20260826.md`` §3.1
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
import sys
import types

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.final_allocation import (  # noqa: E402
    count_active_positions_by_system,
    count_positions_with_unmapped,
    finalize_allocation,
)

MAX_POSITIONS = 10

# 2026-08-26 22:35 の実発注直前 broker read
# (results_csv/exit_orders_20260826_execution.json .positions)
# (symbol, side, その建玉を開いた system)。system=None は delisted/orphan。
REAL_BOOK: list[tuple[str, str, str | None]] = [
    ("AEHR", "long", "system4"),
    ("AON", "long", "system4"),
    ("BURL", "long", "system4"),
    ("DE", "long", "system4"),
    ("FTV", "long", "system4"),
    ("ITW", "long", "system4"),
    ("PCAR", "long", "system4"),
    ("ROST", "long", "system4"),
    ("SNA", "long", "system4"),
    ("TROW", "long", "system4"),
    ("BNY", "long", "system1"),
    ("IPST", "long", "system1"),
    ("SLS", "long", "system1"),
    ("ERAS", "long", "system1"),
    ("FBRX", "long", "system1"),
    ("AMCR", "long", "system1"),
    ("WETO", "long", "system1"),
    ("BWIN", "long", "system5"),
    ("FIGS", "long", "system5"),
    ("GCT", "long", "system5"),
    ("MRVI", "long", "system5"),
    ("NEWP", "long", "system5"),
    ("PAVS", "long", "system5"),
    ("SSRM", "long", "system5"),
    ("ZETA", "long", "system5"),
    ("PAY", "short", "system2"),
    ("SAN", "short", "system2"),
    ("CDTX", "long", None),  # delisted/orphan (帰属不能)
    ("FOLD", "long", None),  # delisted/orphan (帰属不能)
]

# 同 run の system 別候補数
# (results_csv/today_signals_20260826.json .systems[].n_candidates_input)
REAL_CANDIDATE_COUNTS = {
    "system1": 10,
    "system2": 10,
    "system3": 10,
    "system4": 10,
    "system5": 9,
    "system6": 0,
    "system7": 0,
}

# 実際に配分へ渡っていた stale map の縮図
# (data/symbol_system_map.json は 84 銘柄あるが現保有と重なりゼロ)
STALE_STATIC_MAP = {"aapl": "system1", "msft": "system4", "nvda": "system3"}

ALL_SYSTEMS = [f"system{i}" for i in range(1, 8)]


def _position(symbol: str, side: str):
    return types.SimpleNamespace(
        symbol=symbol, qty=(10 if side == "long" else -10), side=side
    )


def _real_positions():
    return [_position(sym, side) for sym, side, _sys in REAL_BOOK]


def _coid_map() -> dict[str, str]:
    """entry coid から作られる symbol -> system (大小両キー = 本番の merge 形)。"""
    out: dict[str, str] = {}
    for sym, _side, system in REAL_BOOK:
        if system:
            out[sym.upper()] = system
            out[sym.lower()] = system
    return out


class _FakeOrder:
    def __init__(self, symbol: str, coid: str):
        self.symbol = symbol
        self.client_order_id = coid
        self.submitted_at = 1


class _FakeClient:
    """entry coid つきの注文履歴と現保有を返す read-only fake。"""

    def __init__(self):
        self._orders = [
            _FakeOrder(sym, f"{system}-{sym}-20260801")
            for sym, _side, system in REAL_BOOK
            if system
        ]
        # entry ではない coid は帰属に使われない
        self._orders.append(_FakeOrder("CDTX", "manual-CDTX"))
        self._orders.append(_FakeOrder("FOLD", "exit-system1-FOLD-20260801"))

    def get_orders(self, req):
        if getattr(req, "until", None) is not None:
            return []
        return self._orders

    def get_all_positions(self):
        return _real_positions()


class _Strategy:
    def __init__(self, name: str, max_positions: int = MAX_POSITIONS):
        self.SYSTEM_NAME = name
        self.config = {
            "max_positions": max_positions,
            "risk_pct": 0.02,
            "max_pct": 0.10,
        }

    def calculate_position_size(
        self, capital, entry_price, stop_price, *, risk_pct, max_pct
    ):
        risk_per_share = abs(entry_price - stop_price)
        if risk_per_share <= 0:
            return 0
        return int(
            min(capital * risk_pct / risk_per_share, capital * max_pct / entry_price)
        )


def _candidates(system: str, n: int) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "symbol": f"{system.upper()}_C{i:02d}",
                "score": float(n - i),
                "entry_price": 100.0,
                "stop_price": 95.0,
                "system": system,
            }
            for i in range(n)
        ]
    )


def _allocate(symbol_system_map):
    per_system = {
        s: _candidates(s, n) for s, n in REAL_CANDIDATE_COUNTS.items() if n > 0
    }
    final_df, summary = finalize_allocation(
        per_system,
        strategies={s: _Strategy(s) for s in ALL_SYSTEMS},
        positions=_real_positions(),
        symbol_system_map=symbol_system_map,
        include_trade_management=False,
    )
    kept: Counter = Counter()
    if final_df is not None and not final_df.empty and "system" in final_df.columns:
        kept.update(str(v).strip().lower() for v in final_df["system"])
    return kept, summary


def _caps_diag(summary):
    return (summary.system_diagnostics or {}).get("portfolio_caps") or {}


# --------------------------------------------------------------------------
# 契約 1: 本番配線 (_resolve_positions_for_allocation) が coid 帰属を取り込む
# --------------------------------------------------------------------------
def _patch_broker(monkeypatch):
    import scripts.run_all_systems_today as rast

    monkeypatch.setenv("APCA_API_KEY_ID", "test-key")
    monkeypatch.setenv("APCA_API_SECRET_KEY", "test-secret")
    monkeypatch.setenv("ALLOCATION_RECONCILE_POSITIONS", "1")
    monkeypatch.setattr(
        rast, "load_symbol_system_map", lambda *a, **k: dict(STALE_STATIC_MAP)
    )
    monkeypatch.setattr(rast.ba, "get_client", lambda *a, **k: _FakeClient())
    return rast


def test_resolve_positions_for_allocation_keeps_coid_attribution(monkeypatch):
    """static map が stale (かつ非空) でも coid 由来の帰属が配分へ届くこと。

    旧実装 ``if not symbol_system_map and fetched_map:`` に戻すとこのテストは
    落ちる (static が非空 → coid map が捨てられ、帰属が全滅する)。
    """
    rast = _patch_broker(monkeypatch)

    positions, symbol_system_map = rast._resolve_positions_for_allocation()

    assert positions is not None and len(positions) == len(REAL_BOOK)

    per_system = count_active_positions_by_system(positions, symbol_system_map)
    assert (
        per_system
    ), "保有が 1 件も system に帰属できていない (coid map が捨てられている)"

    expected = Counter(sys_ for _s, _side, sys_ in REAL_BOOK if sys_)
    assert per_system == dict(expected)

    # stale な static map の中身も失われない (マージであって置換ではない)
    assert str(symbol_system_map.get("aapl", "")).lower() == "system1"


def test_resolve_positions_reports_orphans_not_total_blackout(monkeypatch):
    """帰属できないのは delisted/orphan の 2 件だけ (29 件全滅ではない)。"""
    rast = _patch_broker(monkeypatch)

    positions, symbol_system_map = rast._resolve_positions_for_allocation()
    _per_system, unmapped = count_positions_with_unmapped(positions, symbol_system_map)

    n_orphan = sum(1 for _s, _side, sys_ in REAL_BOOK if not sys_)
    assert unmapped["total"] == n_orphan
    assert unmapped["total"] < len(REAL_BOOK)


# --------------------------------------------------------------------------
# 契約 2/3: system 別 held + 新規 <= max_positions (かつ検査が空振りでない)
# --------------------------------------------------------------------------
def test_per_system_cap_holds_with_coid_attribution():
    """正しい帰属を渡せば、どの system も held + 新規 <= max_positions に収まる。"""
    ssm = dict(STALE_STATIC_MAP)
    ssm.update(_coid_map())

    held = count_active_positions_by_system(_real_positions(), ssm)
    assert held, "前提: 帰属が空でないこと"

    kept, summary = _allocate(ssm)

    for system in ALL_SYSTEMS:
        total = held.get(system, 0) + kept.get(system, 0)
        assert total <= MAX_POSITIONS, (
            f"{system}: held {held.get(system, 0)} + new {kept.get(system, 0)} "
            f"= {total} > max_positions {MAX_POSITIONS}"
        )

    # available_slots が保有を反映していること (全 system 10 に張り付いていない)
    slots = dict(summary.available_slots or {})
    assert slots.get("system4") == 0  # 10 保有で満杯
    assert slots.get("system1") == MAX_POSITIONS - held["system1"]
    assert slots.get("system5") == MAX_POSITIONS - held["system5"]


def test_stale_static_map_alone_violates_per_system_cap():
    """空振り検知 — stale map だけだと同じ検査が落ちる (= 本バグの再現)。"""
    true_held = count_active_positions_by_system(_real_positions(), _coid_map())
    kept, summary = _allocate(dict(STALE_STATIC_MAP))

    # 配分エンジンから見た held は空 = system 枠が素通り
    assert count_active_positions_by_system(_real_positions(), STALE_STATIC_MAP) == {}
    assert all(v == MAX_POSITIONS for v in (summary.available_slots or {}).values())

    violations = [
        system
        for system in ALL_SYSTEMS
        if true_held.get(system, 0) + kept.get(system, 0) > MAX_POSITIONS
    ]
    assert violations, "stale map で超過が出ないなら上のテストは空振りしている"


# --------------------------------------------------------------------------
# 契約 4: long/short/合計プール上限 (40/30/70) は本修正で変わらない
# --------------------------------------------------------------------------
def test_pool_caps_unchanged_by_attribution_fix():
    """帰属の修正は per-system 会計だけを直し、プール上限の算術には触れない。"""
    ssm = dict(STALE_STATIC_MAP)
    ssm.update(_coid_map())

    _kept_before, sum_before = _allocate(dict(STALE_STATIC_MAP))
    _kept_after, sum_after = _allocate(ssm)

    before, after = _caps_diag(sum_before), _caps_diag(sum_after)
    assert before.get("held") == after.get("held")
    assert before.get("allow") == after.get("allow")
    assert before.get("caps") == after.get("caps")
    # 変わるのは「system に帰属できなかった件数」だけ
    assert before.get("held_unmapped") != after.get("held_unmapped")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
