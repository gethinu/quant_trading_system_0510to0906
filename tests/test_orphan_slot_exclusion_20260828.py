# -*- coding: utf-8 -*-
"""risk.exclude_orphans_from_slots (2026-08-28) — 凍結 orphan の枠占有を止める検証。

背景
----
2026-08-28 の実 book (``results_csv/exit_orders_20260828_execution.json``) は long 29 件。
うち **CDTX / FOLD の 2 件は ``tradable: false`` / ``classification:
untradable_no_exit_possible``** = 上場廃止で broker から close できない。こちらから
決済できない以上、資金は broker の清算まで凍結され、**件数上限 (long 40) の枠だけを
永久に食い潰す**。実際に本番 cap report は ``held.long=29 -> allow.long=11`` で、
2 枠がこの 2 銘柄に持って行かれていた (``held_unmapped.long=2``)。

このファイルが守るのは 4 つ:

1. **OFF は現行と完全一致** — 件数も exposure も report キーも従来どおり。既定 OFF
   なので、これが崩れたら本番挙動が黙って変わる。
2. **ON で 2 枠が返る** — 実 book で ``held.long 29 -> 27`` / ``allow.long 11 -> 13``。
3. **枠は返すが資金は返さない** — 凍結分の市場価値は gross/net exposure 側で
   引き続き占有として数える。**枠が空いたことを理由に exposure 上限を破る新規は
   通らない**。ここが安全上の要。フラグは exposure を **締める方向にしか** 効かない。
4. **fail-closed** — 「帰属が無いだけで取引可能な保有」「取引可否を確認できなかった
   保有」「市場価値が読めない保有」は **従来どおり枠を占有**する。誤って枠を空けない。

参照: ``docs/ORPHAN_SLOT_EXCLUSION_20260828.md``
"""

from __future__ import annotations

from pathlib import Path
import sys

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.final_allocation import (  # noqa: E402
    _apply_portfolio_caps,
    _load_exclude_orphans_from_slots,
    _sort_final_frame,
    count_active_positions_by_system,
    count_frozen_orphans,
    count_positions_with_unmapped,
)

LONG_SYSTEMS = ["system1", "system3", "system4", "system5"]
SHORT_SYSTEMS = ["system2", "system6", "system7"]

# results_csv/today_signals_20260828.json portfolio.caps.equity_base_usd
EQUITY = 100259.95
CAPS = {
    "max_total_positions": 70,
    "max_long_positions": 40,
    "max_short_positions": 30,
    "max_gross_exposure_pct": 1.0,
    "max_net_exposure_pct": 0.5,  # config/config.yaml の本番値
}

# 2026-08-28 22:35 の broker read (results_csv/exit_orders_20260828_execution.json)。
# (symbol, side, system, market_value)。system=None は帰属できなかった保有で、
# CDTX / FOLD はそのうち ``tradable: false`` = 上場廃止 (凍結) と確認済み。
REAL_BOOK: list[tuple[str, str, str | None, float]] = [
    ("AEHR", "long", "system1", 283.91),
    ("AMCR", "long", "system1", 1114.53),
    ("AMIX", "long", "system1", 350.35),
    ("AON", "long", "system4", 947.44),
    ("ASST", "long", "system1", 2105.74),
    ("BNY", "long", "system1", 1377.60),
    ("BWIN", "long", "system5", 1091.53),
    ("CDT", "long", "system3", 1498.30),
    ("CDTX", "long", None, 2213.80),
    ("DE", "long", "system4", 836.87),
    ("ERAS", "long", "system1", 1026.47),
    ("FBRX", "long", "system1", 951.40),
    ("FOLD", "long", None, 2072.07),
    ("FTV", "long", "system4", 1077.86),
    ("GCT", "long", "system5", 895.80),
    ("IPW", "long", "system3", 1159.20),
    ("ITW", "long", "system4", 1161.51),
    ("JUNS", "long", "system5", 1019.98),
    ("PAVS", "long", "system5", 276.42),
    ("PAY", "long", "system5", 897.89),
    ("PCAR", "long", "system4", 697.32),
    ("RAL", "long", "system4", 3674.68),
    ("ROST", "long", "system4", 955.14),
    ("SAN", "long", "system4", 887.22),
    ("SLS", "long", "system1", 677.27),
    ("SNA", "long", "system4", 1632.62),
    ("TNON", "long", "system3", 663.04),
    ("TROW", "long", "system4", 1210.57),
    ("VOYG", "long", "system5", 1149.88),
]
FROZEN = ["CDTX", "FOLD"]
FROZEN_USD = 2213.80 + 2072.07  # = 4285.87


class _Pos:
    """duck-typed Alpaca position."""

    def __init__(self, symbol, side, market_value, qty=None):
        self.symbol = symbol
        self.side = side
        self.qty = qty if qty is not None else (-10 if side == "short" else 10)
        self.market_value = market_value


def _real_book() -> tuple[list[_Pos], dict[str, list[str]]]:
    positions = [_Pos(sym, side, mv) for sym, side, _s, mv in REAL_BOOK]
    smap = {sym: [s] for sym, _side, s, _mv in REAL_BOOK if s}
    return positions, smap


def _candidates(n: int, pv: float, side: str = "long", system: str = "system1"):
    return _sort_final_frame(
        pd.DataFrame(
            [
                {
                    "symbol": f"NEW{i:03d}",
                    "system": system,
                    "side": side,
                    "score": 100.0 - i,
                    "position_value": float(pv),
                }
                for i in range(n)
            ]
        )
    )


def _apply(df, positions, smap, **kwargs):
    return _apply_portfolio_caps(
        df,
        caps=CAPS,
        active_positions=positions,
        symbol_system_map=smap,
        long_systems=LONG_SYSTEMS,
        short_systems=SHORT_SYSTEMS,
        equity=EQUITY,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# 1. 既定 OFF と OFF-parity
# ---------------------------------------------------------------------------


def test_flag_defaults_off() -> None:
    """フラグを触らない限り従来経路。既定が ON へ滑ったら本番が黙って変わる。"""
    assert _load_exclude_orphans_from_slots() is False


def test_off_is_identical_to_the_legacy_call() -> None:
    """新引数を渡さない呼び方と exclude_frozen_slots=False が完全一致すること。"""
    positions, smap = _real_book()
    df = _candidates(20, 2000.0)
    legacy_df, legacy_report = _apply(df, positions, smap)
    explicit_df, explicit_report = _apply(
        df, positions, smap, exclude_frozen_slots=False, frozen_symbols=FROZEN
    )
    pd.testing.assert_frame_equal(legacy_df, explicit_df)
    assert legacy_report == explicit_report


def test_off_report_has_no_frozen_key() -> None:
    """OFF の report は従来と byte 一致させる契約 (summary JSON に載るため)。"""
    positions, smap = _real_book()
    _out, report = _apply(_candidates(20, 2000.0), positions, smap)
    assert "frozen_orphans" not in report


def test_off_keeps_the_orphans_in_the_pool_count() -> None:
    """OFF の現状を回帰として固定: 実 book で allow.long=11 (本番 report と同値)。"""
    positions, smap = _real_book()
    _out, report = _apply(_candidates(20, 2000.0), positions, smap)
    assert report["held"]["long"] == 29
    assert report["held_unmapped"] == {"long": 2, "short": 0, "total": 2}
    assert report["allow"]["long"] == 11


# ---------------------------------------------------------------------------
# 2. ON — 2 枠が返る
# ---------------------------------------------------------------------------


def test_on_frees_exactly_the_two_frozen_long_slots() -> None:
    """本題: 上場廃止 2 件が long プールの枠を食うのをやめる。"""
    positions, smap = _real_book()
    df = _candidates(20, 2000.0)
    _off, rep_off = _apply(df, positions, smap)
    _on, rep_on = _apply(
        df, positions, smap, exclude_frozen_slots=True, frozen_symbols=FROZEN
    )
    assert (rep_off["held"]["long"], rep_on["held"]["long"]) == (29, 27)
    assert (rep_off["held"]["total"], rep_on["held"]["total"]) == (29, 27)
    assert (rep_off["allow"]["long"], rep_on["allow"]["long"]) == (11, 13)
    # 需要が枠を上回る日なので、返った枠はそのまま採用本数になる。
    assert (rep_off["kept"]["long"], rep_on["kept"]["long"]) == (11, 13)
    assert rep_on["frozen_orphans"]["count"] == {"long": 2, "short": 0, "total": 2}
    assert rep_on["frozen_orphans"]["symbols"] == ["CDTX", "FOLD"]


def test_on_frees_a_short_slot_for_a_frozen_short_orphan() -> None:
    """long 専用の細工になっていないこと (short プールでも同じ規約)。"""
    positions, smap = _real_book()
    positions.append(_Pos("DEADSHORT", "short", -1500.0, qty=-10))
    df = _candidates(40, 500.0, side="short", system="system2")
    _off, rep_off = _apply(df, positions, smap)
    _on, rep_on = _apply(
        df,
        positions,
        smap,
        exclude_frozen_slots=True,
        frozen_symbols=FROZEN + ["DEADSHORT"],
    )
    assert (rep_off["held"]["short"], rep_on["held"]["short"]) == (1, 0)
    assert (rep_off["allow"]["short"], rep_on["allow"]["short"]) == (29, 30)
    assert rep_on["frozen_orphans"]["count"]["short"] == 1


def test_per_system_slots_never_counted_the_orphans_anyway() -> None:
    """per-system 枠は元から未帰属を数えない (ダッシュの「system枠には数えられない」)。

    ここが変わっていたら二重に緩めていることになる。
    """
    positions, smap = _real_book()
    per_system = count_active_positions_by_system(positions, smap)
    assert sum(per_system.values()) == 27  # 29 保有 - 未帰属 2
    _per, unmapped = count_positions_with_unmapped(positions, smap)
    assert unmapped["total"] == 2


# ---------------------------------------------------------------------------
# 3. 枠は返すが資金は返さない (安全上の要)
# ---------------------------------------------------------------------------


def test_frozen_capital_is_charged_to_the_exposure_caps() -> None:
    """枠から外した建玉の市場価値は exposure 側に残る。落としたら over-leverage。"""
    positions, smap = _real_book()
    _on, rep = _apply(
        _candidates(20, 2000.0),
        positions,
        smap,
        exclude_frozen_slots=True,
        frozen_symbols=FROZEN,
    )
    assert rep["frozen_orphans"]["exposure_usd"]["long"] == pytest.approx(FROZEN_USD)
    assert rep["frozen_orphans"]["exposure_usd"]["gross"] == pytest.approx(FROZEN_USD)


def test_flag_does_not_move_the_exposure_cap_values() -> None:
    """上限「値」は不変。動くのは占有の数え方だけ。"""
    positions, smap = _real_book()
    df = _candidates(20, 2000.0)
    _off, rep_off = _apply(df, positions, smap)
    _on, rep_on = _apply(
        df, positions, smap, exclude_frozen_slots=True, frozen_symbols=FROZEN
    )
    assert rep_off["caps"]["gross_cap_usd"] == rep_on["caps"]["gross_cap_usd"]
    assert rep_off["caps"]["net_cap_usd"] == rep_on["caps"]["net_cap_usd"]


def test_freed_slots_cannot_breach_the_gross_cap() -> None:
    """枠が空いたことを理由に gross 上限超えの新規が通らないこと。

    net 上限を無効化して gross だけを束縛させ、返った枠が **資金側で止まる**ことを見る。
    """
    positions, smap = _real_book()
    caps = dict(CAPS, max_net_exposure_pct=0.0)
    df = _candidates(20, 8000.0)
    kwargs = dict(
        caps=caps,
        active_positions=positions,
        symbol_system_map=smap,
        long_systems=LONG_SYSTEMS,
        short_systems=SHORT_SYSTEMS,
        equity=EQUITY,
    )
    _off, rep_off = _apply_portfolio_caps(df, **kwargs)
    _on, rep_on = _apply_portfolio_caps(
        df, **kwargs, exclude_frozen_slots=True, frozen_symbols=FROZEN
    )
    gross_cap = rep_on["caps"]["gross_cap_usd"]
    new_gross_on = rep_on["new_long_usd"] + rep_on["new_short_usd"]
    # 枠は 11 -> 13 に増えている
    assert (rep_off["allow"]["long"], rep_on["allow"]["long"]) == (11, 13)
    # それでも「新規 + 凍結」は gross 上限を越えない
    assert new_gross_on + FROZEN_USD <= gross_cap
    # 超過分は件数ではなく exposure で止まっている
    assert rep_on["trimmed"].get("gross_exposure", 0) > 0
    # 凍結分を計上したぶん、ON の新規 gross は OFF 以下に収まる
    assert new_gross_on <= rep_off["new_long_usd"] + rep_off["new_short_usd"]


def test_flag_only_ever_tightens_the_net_cap() -> None:
    """net は符号付きなので、素朴に足すと逆に緩む日がある。緩む側へは倒さない。

    実 2026-08-28 の形 (凍結は long、新規は short 寄り) で、素朴な符号付き加算だと
    ``|net|`` が 7,857 -> 3,571 に *減って* short 余力が増えてしまう。ON が OFF より
    多く採用したらこの契約が壊れている。
    """
    positions, smap = _real_book()
    df = _candidates(20, 6000.0, side="short", system="system2")
    _off, rep_off = _apply(df, positions, smap)
    _on, rep_on = _apply(
        df, positions, smap, exclude_frozen_slots=True, frozen_symbols=FROZEN
    )
    assert rep_off["trimmed"].get("net_exposure", 0) > 0  # 検査が空振りでないこと
    assert rep_on["kept"]["short"] <= rep_off["kept"]["short"]
    assert rep_on["new_short_usd"] <= rep_off["new_short_usd"]


# ---------------------------------------------------------------------------
# 4. fail-closed — 誤って枠を空けない
# ---------------------------------------------------------------------------


def test_tradable_orphan_keeps_its_slot() -> None:
    """帰属が無いだけの **取引可能な** 保有は生きた建玉。枠を占有し続ける。"""
    positions, smap = _real_book()
    positions.append(_Pos("LIVE", "long", 1000.0))  # 未帰属だが取引可能
    _on, rep = _apply(
        _candidates(20, 2000.0),
        positions,
        smap,
        exclude_frozen_slots=True,
        frozen_symbols=FROZEN,  # LIVE は「取引不能」集合に入っていない
    )
    assert rep["held"]["long"] == 28  # 30 保有 - 凍結 2。LIVE は残る
    assert "LIVE" not in rep["frozen_orphans"]["symbols"]


def test_unknown_tradability_keeps_its_slot() -> None:
    """broker に届かず確認できなかった銘柄は「取引不能」と断定しない (枠は返さない)。"""
    positions, smap = _real_book()
    _on, rep = _apply(
        _candidates(20, 2000.0),
        positions,
        smap,
        exclude_frozen_slots=True,
        frozen_symbols=[],  # 照会が全滅した想定
    )
    _off, rep_off = _apply(_candidates(20, 2000.0), positions, smap)
    assert rep["held"] == rep_off["held"]
    assert rep["allow"] == rep_off["allow"]
    assert rep["frozen_orphans"]["count"]["total"] == 0


def test_unpriced_frozen_position_keeps_its_slot() -> None:
    """市場価値が読めない = exposure へ付け替えられない。枠だけ返すと穴になる。"""
    positions, smap = _real_book()
    for pos in positions:
        if pos.symbol == "FOLD":
            pos.market_value = None
    _on, rep = _apply(
        _candidates(20, 2000.0),
        positions,
        smap,
        exclude_frozen_slots=True,
        frozen_symbols=FROZEN,
    )
    assert rep["held"]["long"] == 28  # CDTX だけ枠が返る
    assert rep["frozen_orphans"]["unpriced_kept_in_slots"] == 1
    assert rep["frozen_orphans"]["symbols"] == ["CDTX"]


def test_mapped_position_never_frees_a_slot_even_if_untradable() -> None:
    """帰属済みの建玉は per-system 枠が既に数えている。ここで抜くと二重に緩む。"""
    positions, smap = _real_book()
    frozen = count_frozen_orphans(positions, smap, ["AEHR", "RAL"])  # どちらも帰属済み
    assert frozen["total"] == 0
    assert frozen["symbols"] == []


def test_count_frozen_orphans_is_a_noop_without_a_frozen_set() -> None:
    positions, smap = _real_book()
    assert count_frozen_orphans(positions, smap, None)["total"] == 0
    assert count_frozen_orphans(positions, smap, [])["total"] == 0
    assert count_frozen_orphans(None, smap, FROZEN)["total"] == 0


def test_zero_qty_frozen_position_is_ignored() -> None:
    """qty=0 は建玉ではない (従来の count 規約と同じ)。"""
    positions, smap = _real_book()
    positions.append(_Pos("GHOST", "long", 0.0, qty=0))
    frozen = count_frozen_orphans(positions, smap, FROZEN + ["GHOST"])
    assert frozen["total"] == 2
    assert "GHOST" not in frozen["symbols"]


# ---------------------------------------------------------------------------
# 5. 呼び出し側の配線 (broker 照会は read-only、OFF では一切叩かない)
# ---------------------------------------------------------------------------


def _run_all():
    """run_all_systems_today は重いので、必要なテストの中だけで import する。"""
    import scripts.run_all_systems_today as ras

    return ras


def test_resolver_does_not_touch_the_broker_when_the_flag_is_off(monkeypatch) -> None:
    """OFF (既定) では照会そのものを行わない = 完全後方互換。"""
    ras = _run_all()
    monkeypatch.setattr(
        ras,
        "probe_asset_tradable",
        lambda *_a, **_k: pytest.fail("flag OFF なのに broker を叩いた"),
    )
    positions, smap = _real_book()
    assert ras._resolve_frozen_orphan_symbols(positions, smap) == []


def test_resolver_only_returns_confirmed_untradable_orphans(monkeypatch) -> None:
    """ON: 未帰属だけを照会し、``tradable is False`` の銘柄だけを返す。

    - 帰属済みは照会しない (API 呼び出しの最小化 + 二重緩和の防止)
    - ``True`` (取引可能) も ``None`` (確認できず) も返さない = 枠は返さない
    """
    ras = _run_all()
    monkeypatch.setattr(
        "core.final_allocation._load_exclude_orphans_from_slots", lambda: True
    )
    probed: list[str] = []

    def _probe(symbol, client=None):
        probed.append(symbol)
        return {"CDTX": False, "FOLD": False, "LIVE": True}.get(symbol)

    monkeypatch.setattr(ras, "probe_asset_tradable", _probe)
    positions, smap = _real_book()
    positions.append(_Pos("LIVE", "long", 1000.0))  # 未帰属だが取引可能
    positions.append(_Pos("HUH", "long", 1000.0))  # 照会が None (確認できず)
    got = ras._resolve_frozen_orphan_symbols(positions, smap)
    assert got == ["CDTX", "FOLD"]
    assert sorted(probed) == ["CDTX", "FOLD", "HUH", "LIVE"]  # 帰属済みは照会しない


def test_resolver_never_places_orders(monkeypatch) -> None:
    """判定は read-only。発注 API に触れないこと。"""
    ras = _run_all()
    monkeypatch.setattr(
        "core.final_allocation._load_exclude_orphans_from_slots", lambda: True
    )

    class _Client:
        def get_asset(self, symbol):
            class _A:
                tradable = False

            return _A()

        def submit_order(self, *a, **k):  # pragma: no cover - 呼ばれたら失敗
            raise AssertionError("枠判定が発注 API を呼んだ")

    from common.alpaca_trading import probe_asset_tradable

    monkeypatch.setattr(
        ras,
        "probe_asset_tradable",
        lambda sym, client=None: probe_asset_tradable(sym, _Client()),
    )
    positions, smap = _real_book()
    assert ras._resolve_frozen_orphan_symbols(positions, smap) == ["CDTX", "FOLD"]
