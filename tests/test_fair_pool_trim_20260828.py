"""risk.fair_pool_trim (2026-08-28) — プール上限が効いたときの切り捨て順の検証。

背景と設計判断 (de Prado 精査、案 A-D の採否): docs/FAIR_POOL_TRIM_20260828.md。

このファイルが守るのは 3 つ:
  1. **OFF は現行と完全一致** — 切り捨て順・report ともに従来どおり。
     既定 OFF なので、これが崩れたら本番挙動が黙って変わる。
  2. **OFF の偏りを明文化** — long40/short30/total70 が束縛すると S5/S7 が
     構造的に最初の犠牲になる、という現状を回帰テストとして固定する。
     ON の効果はこの数字との差分でしか主張できない。
  3. **ON の公平性** — 同一 side で採用数差 <= 1 (G1)、7 系統すべて残る (G6)、
     rotation の churn は束縛プール 1 つあたり <= 1 枠 (G3)、同じ日付は同じ答え (G5)。
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
    _cap_slots_by_side,
    _fair_deal_order,
    _fair_trim_epoch,
    _load_fair_pool_trim_enabled,
    _sort_final_frame,
)

LONG_SYSTEMS = ["system1", "system3", "system4", "system5"]
SHORT_SYSTEMS = ["system2", "system6", "system7"]
ALL_SYSTEMS = LONG_SYSTEMS + SHORT_SYSTEMS

_CAPS = {
    "max_total_positions": 70,
    "max_long_positions": 40,
    "max_short_positions": 30,
    "max_gross_exposure_pct": 1.0,
    "max_net_exposure_pct": 1.0,
}


class _Pos:
    def __init__(self, symbol: str, side: str) -> None:
        self.symbol = symbol
        self.side = side
        self.qty = 10


def _book(per_system: int = 10, pv: float | None = None) -> pd.DataFrame:
    """本番と同じ提示順 (_sort_final_frame) の候補フレームを作る。"""
    rows = []
    for name in ALL_SYSTEMS:
        side = "long" if name in LONG_SYSTEMS else "short"
        for i in range(per_system):
            row = {
                "symbol": f"{name[-1]}X{i:03d}",
                "system": name,
                "side": side,
                "score": 100.0 - i,
            }
            if pv is not None:
                row["position_value"] = float(pv)
            rows.append(row)
    return _sort_final_frame(pd.DataFrame(rows))


def _held(n_long: int, n_short: int) -> list[_Pos]:
    return [_Pos(f"HL{i:03d}", "long") for i in range(n_long)] + [
        _Pos(f"HS{i:03d}", "short") for i in range(n_short)
    ]


def _apply(df: pd.DataFrame, held: list[_Pos] | None = None, **kwargs):
    return _apply_portfolio_caps(
        df,
        caps=_CAPS,
        active_positions=held,
        symbol_system_map=None,
        long_systems=LONG_SYSTEMS,
        short_systems=SHORT_SYSTEMS,
        equity=100_000.0,
        **kwargs,
    )


def _counts(df: pd.DataFrame) -> dict[str, int]:
    if df.empty:
        return {name: 0 for name in ALL_SYSTEMS}
    got = df.groupby("system").size().to_dict()
    return {name: int(got.get(name, 0)) for name in ALL_SYSTEMS}


# ---------------------------------------------------------------------------
# 1. 既定 OFF と OFF-parity
# ---------------------------------------------------------------------------


def test_flag_defaults_off() -> None:
    """フラグを触らない限り従来経路。既定が ON へ滑ったら本番が黙って変わる。"""
    assert _load_fair_pool_trim_enabled() is False


def test_off_is_identical_to_the_legacy_call() -> None:
    """新引数を渡さない呼び方と fair_trim=False が完全一致すること。"""
    df = _book()
    held = _held(18, 9)
    legacy_df, legacy_report = _apply(df, held)
    explicit_df, explicit_report = _apply(df, held, fair_trim=False)
    pd.testing.assert_frame_equal(legacy_df, explicit_df)
    assert legacy_report == explicit_report


def test_off_report_has_no_fair_trim_key() -> None:
    """OFF の report は従来と byte 一致させる契約 (summary JSON に載るため)。"""
    _out, report = _apply(_book(), _held(18, 9))
    assert "fair_trim" not in report


# ---------------------------------------------------------------------------
# 2. OFF の構造的偏りを固定 (ON の効果はこの差分でしか語れない)
# ---------------------------------------------------------------------------


def test_off_sacrifices_system5_and_system7_first() -> None:
    """現行: long プールは system4 で尽きて S5=0、total プールは S7 を削る。

    この期待値は 2026-08-28 の実測 (docs/FAIR_POOL_TRIM_20260828.md 1.2) をそのまま
    固定したもの。ここが変わったら OFF が現行挙動でなくなっている。
    """
    out, _report = _apply(_book(), _held(18, 9))
    counts = _counts(out)
    assert counts["system5"] == 0
    assert counts["system7"] == 1
    assert counts["system1"] == counts["system3"] == 10
    # 同一 side で 10 対 0 という極端な差が出るのが現行の偏り。
    long_counts = [counts[n] for n in LONG_SYSTEMS]
    assert max(long_counts) - min(long_counts) == 10


def test_off_sort_order_is_side_then_system_number() -> None:
    """偏りの出どころ: side 昇順 -> system 番号昇順で long ブロックが先頭を独占する。"""
    order = list(_book(per_system=2)["system"])
    assert order == [
        "system1",
        "system1",
        "system3",
        "system3",
        "system4",
        "system4",
        "system5",
        "system5",
        "system2",
        "system2",
        "system6",
        "system6",
        "system7",
        "system7",
    ]


# ---------------------------------------------------------------------------
# 3. ON の公平性 (G1 / G6)
# ---------------------------------------------------------------------------


def test_on_no_system_is_zeroed_when_the_pool_binds() -> None:
    """G6: 候補がある system は必ず 1 枠目を得てから他が 2 枠目を得る。"""
    out, _report = _apply(_book(), _held(18, 9), fair_trim=True, fair_trim_epoch=0)
    counts = _counts(out)
    assert all(counts[name] > 0 for name in ALL_SYSTEMS), counts


def test_on_same_side_counts_differ_by_at_most_one() -> None:
    """G1 (max-min fair): 同一 side・未消化候補ありの 2 system の差は <= 1。"""
    out, _report = _apply(_book(), _held(18, 9), fair_trim=True, fair_trim_epoch=0)
    counts = _counts(out)
    for side_systems in (LONG_SYSTEMS, SHORT_SYSTEMS):
        vals = [counts[n] for n in side_systems]
        assert max(vals) - min(vals) <= 1, (side_systems, counts)


def test_on_keeps_the_same_total_as_off() -> None:
    """公平化は枠を増やさない。上限値は不変で、配り方だけが変わる。"""
    df, held = _book(), _held(18, 9)
    off_out, _ = _apply(df, held)
    on_out, _ = _apply(df, held, fair_trim=True, fair_trim_epoch=0)
    assert len(on_out) == len(off_out)


def test_on_spreads_the_exposure_bound_book_too() -> None:
    """gross/net exposure が束縛する経路でも 7 系統に散ること。"""
    df = _book(per_system=12, pv=4000.0)
    off_out, _ = _apply(df)
    on_out, _ = _apply(df, fair_trim=True, fair_trim_epoch=0)
    off_counts, on_counts = _counts(off_out), _counts(on_out)
    assert len(on_out) == len(off_out)
    assert sum(1 for n in ALL_SYSTEMS if off_counts[n] == 0) >= 3
    assert all(on_counts[n] > 0 for n in ALL_SYSTEMS), on_counts


def test_on_preserves_intra_system_score_priority() -> None:
    """system 内の順位 (score) は情報なので触らない。捨てるのは下位から。"""
    out, _report = _apply(_book(), _held(18, 9), fair_trim=True, fair_trim_epoch=0)
    kept = out[out["system"] == "system5"]["symbol"].tolist()
    assert kept == sorted(kept), kept  # 5X000, 5X001, ... = score 上位から


def test_on_preserves_the_presentation_row_order() -> None:
    """判定順は round-robin でも、出力フレームの行順は従来の提示順のまま。"""
    out, _report = _apply(_book(), _held(18, 9), fair_trim=True, fair_trim_epoch=0)
    seen: list[str] = []
    for name in out["system"]:
        if name not in seen:
            seen.append(name)
    assert seen == [
        "system1",
        "system3",
        "system4",
        "system5",
        "system2",
        "system6",
        "system7",
    ]


# ---------------------------------------------------------------------------
# 4. rotation: 決定論 (G5) と churn の上限 (G3)
# ---------------------------------------------------------------------------


def test_on_is_deterministic_for_a_given_epoch() -> None:
    """G5: 同じ epoch は必ず同じ答え。replay が再現できること。"""
    df, held = _book(), _held(7, 4)
    first, _ = _apply(df, held, fair_trim=True, fair_trim_epoch=12345)
    second, _ = _apply(df, held, fair_trim=True, fair_trim_epoch=12345)
    pd.testing.assert_frame_equal(first, second)


def test_rotation_moves_the_residue_around_all_systems() -> None:
    """どの system も「構造的に最後」にならない: 7 epoch で犠牲が一巡する。"""
    df, held = _book(), _held(7, 4)
    losers: set[str] = set()
    for epoch in range(7):
        out, _ = _apply(df, held, fair_trim=True, fair_trim_epoch=epoch)
        counts = _counts(out)
        low = min(counts.values())
        losers.update(n for n in ALL_SYSTEMS if counts[n] == low)
    assert losers == set(ALL_SYSTEMS), losers


def test_rotation_churn_is_bounded_to_one_slot_per_step() -> None:
    """G3: epoch が 1 進んでも system 間を移る枠は最大 1。暴れないことの証拠。"""
    df, held = _book(), _held(7, 4)
    prev = None
    moves = []
    for epoch in range(14):
        out, _ = _apply(df, held, fair_trim=True, fair_trim_epoch=epoch)
        counts = _counts(out)
        if prev is not None:
            moves.append(sum(max(0, prev[n] - counts[n]) for n in ALL_SYSTEMS))
        prev = counts
    assert moves, "no steps compared"
    assert max(moves) <= 1, moves


def test_rotation_cycles_with_the_system_count() -> None:
    """epoch と epoch+7 は同じ配り方 (7 系統なので周期 7)。"""
    df, held = _book(), _held(7, 4)
    a, _ = _apply(df, held, fair_trim=True, fair_trim_epoch=3)
    b, _ = _apply(df, held, fair_trim=True, fair_trim_epoch=10)
    pd.testing.assert_frame_equal(a, b)


def test_missing_epoch_still_gives_fair_quotas() -> None:
    """日付が無くても quota の公平性 (G1) は落ちない。落ちるのは端数の回転だけ。"""
    out, _report = _apply(_book(), _held(18, 9), fair_trim=True, fair_trim_epoch=None)
    counts = _counts(out)
    assert all(counts[n] > 0 for n in ALL_SYSTEMS), counts
    for side_systems in (LONG_SYSTEMS, SHORT_SYSTEMS):
        vals = [counts[n] for n in side_systems]
        assert max(vals) - min(vals) <= 1


# ---------------------------------------------------------------------------
# 5. 監査記録
# ---------------------------------------------------------------------------


def test_on_report_records_who_gave_up_a_slot() -> None:
    """「なぜ今日 S5 が少ないのか」を artifact だけで答えられること。"""
    out, report = _apply(_book(), _held(18, 9), fair_trim=True, fair_trim_epoch=5)
    fair = report["fair_trim"]
    assert fair["enabled"] is True
    assert fair["epoch"] == 5
    assert fair["rotation"] == 5 % 7
    assert sorted(fair["deal_order"]) == sorted(ALL_SYSTEMS)
    assert fair["demand"] == {name: 10 for name in ALL_SYSTEMS}
    for name in ALL_SYSTEMS:
        assert fair["kept"][name] + fair["dropped"][name] == fair["demand"][name]
    assert sum(fair["kept"].values()) == len(out)


# ---------------------------------------------------------------------------
# 6. _fair_deal_order / _fair_trim_epoch の単体
# ---------------------------------------------------------------------------


def test_deal_order_is_round_robin_and_keeps_intra_system_order() -> None:
    df = _book(per_system=3)
    order = _fair_deal_order(df, rotation=0)
    assert sorted(order) == list(range(len(df)))
    systems = list(df["system"])
    # 最初の 7 件は 7 系統から 1 本ずつ
    assert len({systems[p] for p in order[:7]}) == 7
    # system 内の相対順序は不変
    for name in ALL_SYSTEMS:
        picked = [p for p in order if systems[p] == name]
        assert picked == sorted(picked)


def test_deal_order_rotation_shifts_who_opens_the_round() -> None:
    df = _book(per_system=3)
    systems = list(df["system"])
    openers = {systems[_fair_deal_order(df, rotation=r)[0]] for r in range(7)}
    assert openers == set(ALL_SYSTEMS)


def test_deal_order_handles_degenerate_frames() -> None:
    assert _fair_deal_order(pd.DataFrame(), rotation=3) == []
    no_system = pd.DataFrame([{"symbol": "A"}, {"symbol": "B"}])
    assert _fair_deal_order(no_system, rotation=3) == [0, 1]
    single = pd.DataFrame([{"system": "system1"}, {"system": "system1"}])
    assert _fair_deal_order(single, rotation=3) == [0, 1]


@pytest.mark.parametrize(
    "value, expected",
    [
        (None, None),
        ("not-a-date", None),
        (pd.NaT, None),
        (pd.Timestamp("2026-08-28"), pd.Timestamp("2026-08-28").toordinal()),
        ("2026-08-28", pd.Timestamp("2026-08-28").toordinal()),
    ],
)
def test_trim_epoch_is_a_pure_function_of_the_signal_date(value, expected) -> None:
    """G2/G5: 当日日付だけから決まる純関数。未来の情報も永続 state も使わない。"""
    assert _fair_trim_epoch(value) == expected


# ---------------------------------------------------------------------------
# 7. _cap_slots_by_side の端数 tie-break (slots_from_capital ON 経路)
# ---------------------------------------------------------------------------

_REQ_LONG = {name: 12 for name in LONG_SYSTEMS}
_RAW_TIE = {name: 3.25 for name in LONG_SYSTEMS}


def test_cap_slots_rotation_zero_is_the_legacy_tie_break() -> None:
    for cap in range(1, 60):
        legacy = _cap_slots_by_side(_REQ_LONG, _RAW_TIE, side_cap=cap, min_slots=1)
        explicit = _cap_slots_by_side(
            _REQ_LONG, _RAW_TIE, side_cap=cap, min_slots=1, rotation=0
        )
        assert legacy == explicit, cap


def test_cap_slots_legacy_tie_break_always_penalises_system5() -> None:
    """現行の端数は必ず名前順の最後 (long なら S5) が失う。"""
    result = _cap_slots_by_side(_REQ_LONG, _RAW_TIE, side_cap=39, min_slots=1)
    assert result["system5"] == 9
    assert result["system1"] == 10


def test_cap_slots_rotation_cycles_the_penalised_system() -> None:
    losers = set()
    for rotation in range(4):
        result = _cap_slots_by_side(
            _REQ_LONG, _RAW_TIE, side_cap=39, min_slots=1, rotation=rotation
        )
        low = min(result.values())
        losers.update(n for n, v in result.items() if v == low)
        assert sum(result.values()) == 39
    assert losers == set(LONG_SYSTEMS), losers


# ---------------------------------------------------------------------------
# 8. 配線 (finalize_allocation -> cap 層 / capital-slot 層)
# ---------------------------------------------------------------------------


class _Strat:
    config = {"max_positions": 10}


def _per_system(n: int = 10) -> dict[str, pd.DataFrame]:
    out = {}
    for name in ALL_SYSTEMS:
        side = "long" if name in LONG_SYSTEMS else "short"
        out[name] = pd.DataFrame(
            [
                {
                    "symbol": f"{name[-1]}X{i:03d}",
                    "system": name,
                    "side": side,
                    "score": 100.0 - i,
                    "entry_price": 50.0,
                    "stop_price": 45.0,
                    "shares": 10,
                }
                for i in range(n)
            ]
        )
    return out


def _finalize(signal_date, held: list[_Pos] | None = None):
    from core.final_allocation import finalize_allocation

    return finalize_allocation(
        _per_system(),
        strategies={name: _Strat() for name in ALL_SYSTEMS},
        positions=_held(18, 9) if held is None else held,
        symbol_system_map=None,
        default_capital=100_000.0,
        signal_date=signal_date,
        include_trade_management=False,
    )


def test_finalize_allocation_is_unchanged_while_the_flag_is_off() -> None:
    """配線が入っても OFF の finalize_allocation は現行の偏った結果のまま。"""
    final_df, summary = _finalize(pd.Timestamp("2026-08-28"))
    counts = _counts(final_df)
    assert counts["system5"] == 0
    assert counts["system7"] == 1
    caps = (summary.system_diagnostics or {}).get("portfolio_caps", {})
    assert "fair_trim" not in caps


def test_finalize_allocation_honours_the_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """フラグ ON が cap 層まで届いていること (helper 単体ではなく本番の入口で)。"""
    import core.final_allocation as allocation

    monkeypatch.setattr(allocation, "_load_fair_pool_trim_enabled", lambda: True)
    final_df, summary = _finalize(pd.Timestamp("2026-08-28"))
    counts = _counts(final_df)
    assert all(counts[name] > 0 for name in ALL_SYSTEMS), counts
    for side_systems in (LONG_SYSTEMS, SHORT_SYSTEMS):
        vals = [counts[n] for n in side_systems]
        assert max(vals) - min(vals) <= 1, counts
    caps = (summary.system_diagnostics or {}).get("portfolio_caps", {})
    fair = caps["fair_trim"]
    assert fair["enabled"] is True
    assert fair["epoch"] == pd.Timestamp("2026-08-28").toordinal()


def test_finalize_allocation_rotates_with_the_signal_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """G5: 日付だけが offset の出どころ。7 日で犠牲が一巡し、churn は 1 日 <= 1 枠。

    held long7 / short4 -> allow_long=33 (33=8*4+1 -> 端数 1)、
    allow_short=26 (26=8*3+2 -> 端数 2)。**両 side に端数が出る**構成でないと
    片側だけ回って「全 system が一巡した」が言えない。
    """
    import core.final_allocation as allocation

    monkeypatch.setattr(allocation, "_load_fair_pool_trim_enabled", lambda: True)
    held = _held(7, 4)
    losers: set[str] = set()
    prev = None
    moves = []
    for day in pd.date_range("2026-08-24", periods=14, freq="D"):
        final_df, _summary = _finalize(day, held)
        counts = _counts(final_df)
        # 犠牲は side ごとに見る (プールが別なので side 跨ぎの比較は無意味)。
        for side_systems in (LONG_SYSTEMS, SHORT_SYSTEMS):
            low = min(counts[n] for n in side_systems)
            losers.update(n for n in side_systems if counts[n] == low)
        if prev is not None:
            moves.append(sum(max(0, prev[n] - counts[n]) for n in ALL_SYSTEMS))
        prev = counts
    assert losers == set(ALL_SYSTEMS), losers
    assert max(moves) <= 1, moves


def test_capital_slot_derivation_accepts_the_rotation() -> None:
    """slots_from_capital 経路にも rotation が届き、既定 0 は従来どおりであること。"""
    from core.final_allocation import derive_capital_weighted_slots

    kwargs = dict(
        long_allocations={name: 0.25 for name in LONG_SYSTEMS},
        short_allocations={"system2": 0.40, "system6": 0.40, "system7": 0.20},
        # max_pct=0.01 で raw_slots が side_cap を超える (0.10 だと要求 9 枠で
        # 上限に届かず rotation の出番が無い)。
        max_pct_by_system={name: 0.01 for name in ALL_SYSTEMS},
        equity=100_000.0,
        long_ratio=0.5,
        gross_exposure_pct=1.0,
        gross_budget_factor=1.0,
        min_slots=1,
        max_long_positions=39,
        max_short_positions=29,
        max_total_positions=70,
        max_net_exposure_pct=1.0,
    )
    legacy = derive_capital_weighted_slots(**kwargs)
    explicit_zero = derive_capital_weighted_slots(slot_rotation=0, **kwargs)
    assert legacy.slots == explicit_zero.slots
    rotated = {
        rot: derive_capital_weighted_slots(slot_rotation=rot, **kwargs).slots
        for rot in range(7)
    }
    assert sum(legacy.slots.values()) == sum(rotated[3].values())
    assert len({tuple(sorted(v.items())) for v in rotated.values()}) > 1
