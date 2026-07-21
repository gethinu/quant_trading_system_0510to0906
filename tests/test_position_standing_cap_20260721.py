"""P1 fix (2026-07-21): position-management standing-cap の回帰テスト。

対象 (docs/POSITION_MANAGEMENT_P1_STANDING_CAP_20260721.md):
  Fix 1 — reconcile fail-closed: 現保有 fetch 失敗を silent None 縮退でなく raise。
  Fix 2 — submit 境界 per-system standing cap: system の保有が上限のとき新規を弾く。
           (already_held は同一銘柄しか止めない → 別銘柄での積み増しを止める最終防波堤)
  Fix 3 — delisted/orphan を held に算入: 帰属できない実保有を total/side に数える。

既存の silent-fail 監視テスト (test_alpaca_positions_fetch_error_raises,
test_paper_order_execution_fallback) と同じ作法: fake client + skip_reason 検証で
「silent drop しない」ことを固定する。paper 限定・実発注なし (dry_run/fake client)。
"""

from __future__ import annotations

from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common.alpaca_trading import (  # noqa: E402
    count_held_positions_by_system,
    evaluate_standing_cap,
    signals_json_to_orders,
)
import common.broker_alpaca as ba  # noqa: E402
from core.final_allocation import (  # noqa: E402
    _apply_portfolio_caps,
    count_positions_with_unmapped,
)


# =========================================================================
# fake Alpaca client (self-contained)
# =========================================================================
class _Asset:
    def __init__(self, frac: bool) -> None:
        self.fractionable = frac


class _Order:
    def __init__(self, oid="oid", status="accepted", symbol=None, side=None, coid=None):
        self.id = oid
        self.status = status
        self.symbol = symbol
        self.side = side
        self.client_order_id = coid


class _Pos:
    def __init__(self, symbol, qty, side=None):
        self.symbol = symbol
        self.qty = qty
        self.side = side or ("long" if float(qty) >= 0 else "short")


class _Client:
    """held-position / order / asset を注入できる最小 fake。"""

    def __init__(self, *, positions=None, all_orders=None, frac=None):
        self._positions = positions or []
        self._orders = all_orders or []
        self._frac = frac or {}
        self.notional_submits: list = []

    def get_all_positions(self):
        return list(self._positions)

    def get_orders(self, filter=None):  # noqa: A002 (mirror alpaca kw)
        return list(self._orders)

    def get_asset(self, sym):
        return _Asset(self._frac.get(sym, True))

    def submit_order(self, order_data=None):
        self.notional_submits.append(order_data)
        return _Order(oid=f"nid-{getattr(order_data, 'symbol', '?')}")


@pytest.fixture
def paper_env(monkeypatch):
    monkeypatch.setenv("ALPACA_PAPER", "true")
    monkeypatch.setenv("ALPACA_API_BASE_URL", "https://paper-api.alpaca.markets")
    # 既定を明示 (他テストの env 汚染から隔離)
    monkeypatch.delenv("SUBMIT_ENFORCE_STANDING_CAP", raising=False)
    monkeypatch.delenv("SUBMIT_MAX_POSITIONS_PER_SYSTEM", raising=False)
    monkeypatch.delenv("SUBMIT_MAX_TOTAL_POSITIONS", raising=False)
    # standing-cap の held 集計を coid 経由に固定するため map は空にする
    monkeypatch.setattr("common.symbol_map.load_symbol_system_map", lambda *a, **k: {})


def _json(signals: list[dict], date="2026-07-02") -> dict:
    """sys{N} JSON を組み立てる (_flatten_json_signals が system{N} に正規化)。"""
    systems: dict = {}
    for s in signals:
        key = s["system"].replace("system", "sys")
        systems.setdefault(key, {"signals": []})["signals"].append(
            {
                "symbol": s["symbol"],
                "side": s["side"],
                "entry_price": s.get("entry_price", 100.0),
                "weight": s.get("weight", 1.0),
            }
        )
    return {"date": date, "systems": systems}


# =========================================================================
# A. evaluate_standing_cap — pure decision
# =========================================================================
def test_eval_under_caps_returns_none():
    assert (
        evaluate_standing_cap(
            system="system1",
            held_by_system={"system1": 3},
            total_held=3,
            batch_by_system={"system1": 2},
            batch_total=2,
            per_system_cap=10,
            total_cap=70,
        )
        is None
    )


def test_eval_per_system_cap_blocks_when_held_plus_batch_reaches_cap():
    # held 8 + batch 2 = 10 >= cap 10 → 弾く
    reason = evaluate_standing_cap(
        system="system2",
        held_by_system={"system2": 8},
        total_held=8,
        batch_by_system={"system2": 2},
        batch_total=2,
        per_system_cap=10,
        total_cap=70,
    )
    assert reason and reason.startswith("standing_cap:system2_held=8+batch=2")


def test_eval_total_cap_takes_precedence():
    reason = evaluate_standing_cap(
        system="system1",
        held_by_system={"system1": 0},
        total_held=70,
        batch_by_system={},
        batch_total=0,
        per_system_cap=10,
        total_cap=70,
    )
    assert reason and "portfolio_total_held=70" in reason


def test_eval_cap_zero_disables_that_dimension():
    # per_system_cap=0 → per-system 判定なし。total_cap=0 → total 判定なし。
    assert (
        evaluate_standing_cap(
            system="system1",
            held_by_system={"system1": 99},
            total_held=99,
            batch_by_system={},
            batch_total=0,
            per_system_cap=0,
            total_cap=0,
        )
        is None
    )


# =========================================================================
# B. count_held_positions_by_system — 帰属 + delisted
# =========================================================================
def test_count_held_attributes_via_coid():
    client = _Client(
        positions=[_Pos("AAA", 10), _Pos("BBB", -3)],
        all_orders=[
            _Order(symbol="AAA", coid="system1-AAA-20260713"),
            _Order(symbol="BBB", coid="system2-BBB-20260713"),
        ],
    )
    held = count_held_positions_by_system(
        client, open_positions={"AAA": 10.0, "BBB": -3.0}, symbol_system_map={}
    )
    assert held.per_system == {"system1": 1, "system2": 1}
    assert held.total == 2
    assert held.long_total == 1 and held.short_total == 1
    assert held.unmapped == 0


def test_count_held_falls_back_to_symbol_system_map():
    # coid が取れない (orders 空) → symbol_system_map で帰属
    client = _Client(positions=[_Pos("CCC", 5)], all_orders=[])
    held = count_held_positions_by_system(
        client, open_positions={"CCC": 5.0}, symbol_system_map={"CCC": "system3"}
    )
    assert held.per_system == {"system3": 1}
    assert held.unmapped == 0


def test_count_held_delisted_orphan_is_unmapped_but_counts_total():
    # FOLD/CDTX 相当: coid も map も無い → unmapped だが total/side には算入 (Fix 3)
    client = _Client(positions=[_Pos("FOLD", 7), _Pos("CDTX", -2)], all_orders=[])
    held = count_held_positions_by_system(
        client,
        open_positions={"FOLD": 7.0, "CDTX": -2.0},
        symbol_system_map={},
    )
    assert held.per_system == {}
    assert held.unmapped == 2
    assert held.total == 2
    assert held.long_total == 1 and held.short_total == 1


def test_count_held_spy_short_attributes_to_system7():
    client = _Client(positions=[_Pos("SPY", -4)], all_orders=[])
    held = count_held_positions_by_system(
        client, open_positions={"SPY": -4.0}, symbol_system_map={}
    )
    assert held.per_system == {"system7": 1}
    assert held.unmapped == 0


# =========================================================================
# C. signals_json_to_orders — submit 境界の standing cap (end-to-end, fake client)
# =========================================================================
def test_per_system_cap_blocks_cross_day_accumulation(monkeypatch, paper_env):
    """system1 を既に 2 保有 (別銘柄) → cap=2 で新規 system1 は全 skip。

    これが 07-13 の 10 + 07-14 の別 10 = 20 を防ぐ核心 (already_held は別銘柄を
    止められない)。既存ポジションには一切触れない (新規を弾くだけ)。
    """
    monkeypatch.setenv("SUBMIT_MAX_POSITIONS_PER_SYSTEM", "2")
    client = _Client(
        positions=[_Pos("HELD0", 10), _Pos("HELD1", 10)],  # 既存 system1 保有 2
        all_orders=[
            _Order(symbol="HELD0", coid="system1-HELD0-20260713"),
            _Order(symbol="HELD1", coid="system1-HELD1-20260713"),
        ],
        frac={"NEWA": True, "NEWB": True},
    )
    orders = signals_json_to_orders(
        _json(
            [
                {"symbol": "NEWA", "side": "buy", "system": "system1"},
                {"symbol": "NEWB", "side": "buy", "system": "system1"},
            ]
        ),
        tier="small",
        dry_run=False,
        client=client,
        sizing_mode="fixed_tier",
    )
    assert len(orders) == 2
    for o in orders:
        assert o.order_id is None, f"{o.symbol} は発注されてはいけない"
        assert o.skip_reason and o.skip_reason.startswith("standing_cap:system1")
    assert client.notional_submits == []  # 実 submit 0


def test_batch_cap_within_single_call(monkeypatch, paper_env):
    """保有 0 でも 1 回の call 内で cap を超える分は弾く (batch カウント)。"""
    monkeypatch.setenv("SUBMIT_MAX_POSITIONS_PER_SYSTEM", "2")
    client = _Client(
        positions=[],
        all_orders=[],
        frac={s: True for s in ("A0", "A1", "A2", "A3")},
    )
    orders = signals_json_to_orders(
        _json(
            [
                {"symbol": "A0", "side": "buy", "system": "system1"},
                {"symbol": "A1", "side": "buy", "system": "system1"},
                {"symbol": "A2", "side": "buy", "system": "system1"},
                {"symbol": "A3", "side": "buy", "system": "system1"},
            ]
        ),
        tier="small",
        dry_run=False,
        client=client,
        sizing_mode="fixed_tier",
    )
    submitted = [o for o in orders if o.order_id]
    skipped = [o for o in orders if o.skip_reason and "standing_cap" in o.skip_reason]
    assert len(submitted) == 2  # 先着 2 のみ
    assert len(skipped) == 2  # 残りは理由付き skip (silent drop でない)


def test_total_cap_blocks_across_systems(monkeypatch, paper_env):
    """portfolio total cap は system 横断で効く (delisted 含む total 基準)。"""
    monkeypatch.setenv("SUBMIT_MAX_TOTAL_POSITIONS", "1")
    monkeypatch.setenv("SUBMIT_MAX_POSITIONS_PER_SYSTEM", "10")
    client = _Client(
        positions=[], all_orders=[], frac={s: True for s in ("X0", "Y0", "Z0")}
    )
    orders = signals_json_to_orders(
        _json(
            [
                {"symbol": "X0", "side": "buy", "system": "system1"},
                {"symbol": "Y0", "side": "buy", "system": "system3"},
                {"symbol": "Z0", "side": "buy", "system": "system4"},
            ]
        ),
        tier="small",
        dry_run=False,
        client=client,
        sizing_mode="fixed_tier",
    )
    submitted = [o for o in orders if o.order_id]
    blocked = [
        o for o in orders if o.skip_reason and "portfolio_total" in o.skip_reason
    ]
    assert len(submitted) == 1
    assert len(blocked) == 2


def test_delisted_orphans_consume_total_cap(monkeypatch, paper_env):
    """delisted 保有 (帰属不能) が total を圧迫し新規を弾く = Fix 3 が submit 境界で効く。"""
    monkeypatch.setenv("SUBMIT_MAX_TOTAL_POSITIONS", "2")
    monkeypatch.setenv("SUBMIT_MAX_POSITIONS_PER_SYSTEM", "10")
    client = _Client(
        positions=[_Pos("FOLD", 7), _Pos("CDTX", -2)],  # 帰属不能 orphan 2
        all_orders=[],
        frac={"NEWX": True},
    )
    orders = signals_json_to_orders(
        _json([{"symbol": "NEWX", "side": "buy", "system": "system1"}]),
        tier="small",
        dry_run=False,
        client=client,
        sizing_mode="fixed_tier",
    )
    assert len(orders) == 1
    assert orders[0].order_id is None
    assert "portfolio_total" in orders[0].skip_reason  # total=2 (delisted) >= cap 2


def test_enforce_flag_off_disables_cap(monkeypatch, paper_env):
    """SUBMIT_ENFORCE_STANDING_CAP=0 で従来挙動 (cap 無し)。"""
    monkeypatch.setenv("SUBMIT_ENFORCE_STANDING_CAP", "0")
    monkeypatch.setenv("SUBMIT_MAX_POSITIONS_PER_SYSTEM", "1")
    client = _Client(
        positions=[], all_orders=[], frac={s: True for s in ("B0", "B1", "B2")}
    )
    orders = signals_json_to_orders(
        _json(
            [
                {"symbol": "B0", "side": "buy", "system": "system1"},
                {"symbol": "B1", "side": "buy", "system": "system1"},
                {"symbol": "B2", "side": "buy", "system": "system1"},
            ]
        ),
        tier="small",
        dry_run=False,
        client=client,
        sizing_mode="fixed_tier",
    )
    # cap 無効 → standing_cap での skip は 0
    assert not any("standing_cap" in (o.skip_reason or "") for o in orders)


def test_no_silent_drop_capped_orders_have_skip_reason(monkeypatch, paper_env):
    """cap で弾いた注文も terminal state (skip_reason) を必ず持つ。"""
    monkeypatch.setenv("SUBMIT_MAX_POSITIONS_PER_SYSTEM", "1")
    client = _Client(positions=[], all_orders=[], frac={"C0": True, "C1": True})
    orders = signals_json_to_orders(
        _json(
            [
                {"symbol": "C0", "side": "buy", "system": "system1"},
                {"symbol": "C1", "side": "buy", "system": "system1"},
            ]
        ),
        tier="small",
        dry_run=False,
        client=client,
        sizing_mode="fixed_tier",
    )
    for o in orders:
        assert bool(o.order_id) or bool(o.error) or bool(o.skip_reason)


# =========================================================================
# D. count_positions_with_unmapped + _apply_portfolio_caps の delisted 算入 (Fix 3)
# =========================================================================
def test_count_positions_with_unmapped_tally():
    positions = [
        _Pos("AAA", 10, "long"),  # mapped system1
        _Pos("FOLD", 5, "long"),  # delisted long
        _Pos("CDTX", -3, "short"),  # delisted short
    ]
    sym_map = {"AAA": "system1"}
    per_system, unmapped = count_positions_with_unmapped(positions, sym_map)
    assert per_system.get("system1") == 1
    assert unmapped == {"long": 1, "short": 1, "total": 2}


def test_apply_portfolio_caps_counts_delisted_in_total():
    """delisted 2 (long) + max_total 5 → 新規は 3 までしか通らない (従来は 5 通していた)。"""
    import pandas as pd

    caps = {
        "max_total_positions": 5,
        "max_long_positions": 40,
        "max_short_positions": 30,
        "max_gross_exposure_pct": 1.0,
        "max_net_exposure_pct": 1.0,
    }
    positions = [_Pos("FOLD", 5, "long"), _Pos("XDEAD", 4, "long")]  # 帰属不能 2
    df = pd.DataFrame(
        [
            {
                "symbol": f"N{i}",
                "system": "system1",
                "side": "long",
                "position_value": 100.0,
            }
            for i in range(5)
        ]
    )
    out, report = _apply_portfolio_caps(
        df,
        caps=caps,
        active_positions=positions,
        symbol_system_map={},  # 帰属できない → unmapped
        long_systems=["system1"],
        short_systems=["system2"],
        equity=100000.0,
    )
    assert report["held"]["total"] == 2  # delisted 2 が held に算入された
    assert report["held_unmapped"] == {"long": 2, "short": 0, "total": 2}
    assert len(out) == 3  # allow_total = 5 - 2 = 3


# =========================================================================
# E. reconcile fail-closed 補完 (fetch primitive が raise すること)
# =========================================================================
def test_fetch_positions_and_symbol_map_raises_on_client_failure(monkeypatch):
    """P1 Fix 1: position fetch 失敗を silent [] でなく PositionReconcileError に。"""
    import scripts.run_all_systems_today as rast

    class _BadClient:
        def get_all_positions(self):
            raise RuntimeError("alpaca unreachable")

    monkeypatch.setattr(ba, "get_client", lambda *a, **k: _BadClient())
    with pytest.raises(rast.PositionReconcileError):
        rast._fetch_positions_and_symbol_map()
