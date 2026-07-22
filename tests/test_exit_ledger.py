"""exit (手仕舞い) 計測の regression test.

守りたい不変条件は 3 つだけ:

1. **exit が記録される** — broker の約定 (FILL) から round-trip と実現損益が
   復元できること。long / short / 端株 / 分割約定 / 建玉反転すべて。
2. **取りこぼしが検知される** — 再構成建玉と broker position の食い違い、
   および「exit するつもりだったのに約定していない」が *フラグとして立つ* こと。
   黙って 0 や空にならないこと。
3. **不明を数字で埋めない** — 計測できない時は ``measured=False`` + 理由を返し、
   損益は ``None``。特に当日損益は **基準の違う 2 つの equity を引かない**。

3 は 2026-07 に実際に起きた事故 (「今日 +$2,850.35」が全部幻) の再発防止。
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.exit_ledger import (  # noqa: E402
    SESSION_BEFORE_OPEN,
    SESSION_CLOSED,
    SESSION_OPEN,
    SESSION_UNKNOWN,
    ExitLedgerError,
    parse_fill,
    parse_fills,
    pick_prev_session_close,
    realized_by_day,
    realized_cumulative,
    reconcile_intents_with_fills,
    reconcile_with_broker,
    reconstruct_round_trips,
    resolve_session_pnl,
    session_date_of,
    summarize_by_system,
    summarize_realized,
)


def fill(symbol, side, qty, price, ts, order_id="o1"):
    return {
        "symbol": symbol,
        "side": side,
        "qty": str(qty),
        "price": str(price),
        "transaction_time": ts,
        "order_id": order_id,
        "id": f"{symbol}-{ts}-{side}",
    }


# ---------------------------------------------------------------------------
# 1. exit が記録される
# ---------------------------------------------------------------------------


def test_long_round_trip_records_realized_pl():
    fills = parse_fills(
        [
            fill("AAA", "buy", 10, 100, "2026-07-01T14:00:00Z"),
            fill("AAA", "sell", 10, 110, "2026-07-06T14:00:00Z"),
        ]
    )
    res = reconstruct_round_trips(fills)

    assert res.measured is True
    assert len(res.closed_trades) == 1
    t = res.closed_trades[0]
    assert t.side == "long"
    assert t.realized_pl == Decimal("100")
    assert t.qty == Decimal("10")
    assert t.holding_days == 5
    assert t.realized_pl_pct == Decimal(10)
    assert res.open_lots == {}


def test_short_round_trip_realized_pl_sign_is_inverted():
    """short は「高く売って安く買い戻す」と利益。符号を間違えない。"""
    fills = parse_fills(
        [
            fill("BBB", "sell_short", 5, 50, "2026-07-01T14:00:00Z"),
            fill("BBB", "buy", 5, 45, "2026-07-02T14:00:00Z"),
        ]
    )
    res = reconstruct_round_trips(fills)
    t = res.closed_trades[0]
    assert t.side == "short"
    assert t.realized_pl == Decimal("25")

    losing = reconstruct_round_trips(
        parse_fills(
            [
                fill("BBB", "sell_short", 5, 50, "2026-07-01T14:00:00Z"),
                fill("BBB", "buy", 5, 55, "2026-07-02T14:00:00Z"),
            ]
        )
    )
    assert losing.closed_trades[0].realized_pl == Decimal("-25")


def test_fractional_and_partial_fills_are_not_dropped():
    """端株 (qty<1) と分割約定を切り捨てない (2026-07 の exit silent-drop 再発防止)。"""
    fills = parse_fills(
        [
            fill("CCC", "buy", "0.5", 200, "2026-07-01T14:00:00Z"),
            fill("CCC", "buy", "0.25", 204, "2026-07-01T14:05:00Z"),
            fill("CCC", "sell", "0.75", 220, "2026-07-03T14:00:00Z"),
        ]
    )
    res = reconstruct_round_trips(fills)

    assert len(res.closed_trades) == 2  # FIFO で 2 lot に分かれる
    total_qty = sum(t.qty for t in res.closed_trades)
    assert total_qty == Decimal("0.75")
    # 0.5*(220-200) + 0.25*(220-204) = 10 + 4
    assert sum(t.realized_pl for t in res.closed_trades) == Decimal("14.00")
    assert res.open_lots == {}


def test_position_flip_opens_opposite_lot():
    """long 10 を 15 売ったら 10 決済 + short 5 の新規建玉。"""
    fills = parse_fills(
        [
            fill("DDD", "buy", 10, 10, "2026-07-01T14:00:00Z"),
            fill("DDD", "sell", 15, 12, "2026-07-02T14:00:00Z"),
        ]
    )
    res = reconstruct_round_trips(fills)
    assert len(res.closed_trades) == 1
    assert res.closed_trades[0].qty == Decimal("10")
    assert res.closed_trades[0].realized_pl == Decimal("20")
    assert res.open_lots["DDD"][0].qty == Decimal("-5")


def test_partial_exit_leaves_the_rest_open():
    fills = parse_fills(
        [
            fill("EEE", "buy", 10, 10, "2026-07-01T14:00:00Z"),
            fill("EEE", "sell", 4, 12, "2026-07-02T14:00:00Z"),
        ]
    )
    res = reconstruct_round_trips(fills)
    assert res.closed_trades[0].qty == Decimal("4")
    assert res.open_lots["EEE"][0].qty == Decimal("6")


def test_malformed_fill_raises_instead_of_being_skipped():
    """壊れた activity を黙って捨てると「exit が無かったこと」になる。必ず上げる。"""
    with pytest.raises(ExitLedgerError):
        parse_fill({"symbol": "X", "side": "buy", "qty": "1"})  # price/time 欠落
    with pytest.raises(ExitLedgerError):
        parse_fill(fill("X", "teleport", 1, 1, "2026-07-01T14:00:00Z"))
    with pytest.raises(ExitLedgerError):
        parse_fill(fill("X", "buy", 0, 1, "2026-07-01T14:00:00Z"))


def test_exit_reason_and_system_round_trip_to_row():
    fills = parse_fills(
        [
            fill("FFF", "buy", 2, 10, "2026-07-01T14:00:00Z"),
            fill("FFF", "sell", 2, 9, "2026-07-08T14:00:00Z", order_id="exit-1"),
        ]
    )
    res = reconstruct_round_trips(fills)
    t = res.closed_trades[0]
    t.system = "system3"
    t.exit_reason = "time_based"
    row = t.to_row()
    assert row["system"] == "system3"
    assert row["exit_reason"] == "time_based"
    assert row["exit_order_id"] == "exit-1"
    assert row["realized_pl"] == -2.0
    assert row["holding_days"] == 7
    assert row["exit_session"] == "2026-07-08"


# ---------------------------------------------------------------------------
# 立会日 (ET) の切り分け
# ---------------------------------------------------------------------------


def test_session_date_uses_eastern_time_not_utc():
    """冬時間の時間外約定が UTC 日付だと翌日に飛ぶ。ET で切る。"""
    # 2026-01-09 19:30 EST == 2026-01-10 00:30 UTC -> 立会日は 01-09
    assert session_date_of("2026-01-10T00:30:00Z") == "2026-01-09"
    # 通常の寄り (09:35 EDT == 13:35 UTC) は同日
    assert session_date_of("2026-07-06T13:35:00Z") == "2026-07-06"


def test_realized_by_day_buckets_on_session_not_utc_date():
    fills = parse_fills(
        [
            fill("GGG", "buy", 1, 10, "2026-01-05T14:30:00Z"),
            fill("GGG", "sell", 1, 12, "2026-01-10T00:30:00Z"),  # ET では 01-09
        ]
    )
    res = reconstruct_round_trips(fills)
    by_day = realized_by_day(res.closed_trades)
    assert "2026-01-09" in by_day
    assert "2026-01-10" not in by_day


def test_realized_cumulative_is_monotonic_in_date_order():
    fills = parse_fills(
        [
            fill("H1", "buy", 1, 10, "2026-07-01T14:00:00Z"),
            fill("H1", "sell", 1, 15, "2026-07-02T14:00:00Z"),
            fill("H2", "buy", 1, 10, "2026-07-01T14:00:00Z"),
            fill("H2", "sell", 1, 8, "2026-07-03T14:00:00Z"),
        ]
    )
    res = reconstruct_round_trips(fills)
    rows = realized_cumulative(realized_by_day(res.closed_trades))
    assert [r["t"] for r in rows] == ["2026-07-02", "2026-07-03"]
    assert rows[0]["realized_pl_cum"] == 5.0
    assert rows[1]["realized_pl_cum"] == 3.0


# ---------------------------------------------------------------------------
# 2. 取りこぼしが検知される
# ---------------------------------------------------------------------------


def test_no_fills_means_unmeasured_not_zero():
    res = reconstruct_round_trips([])
    assert res.measured is False
    assert res.complete is False
    assert any("no_fill_activities" in r for r in res.measurement_reasons())
    # 「trade 0 本」を「損益 0」に読み替えない
    assert summarize_realized(res.closed_trades)["total_realized_pl"] is None


def test_broker_reconcile_flags_ticker_rename_and_missing_fills():
    fills = parse_fills(
        [
            fill("FISV", "buy", 100, 10, "2026-07-01T14:00:00Z"),  # rename 前
        ]
    )
    res = reconstruct_round_trips(fills)
    # broker には rename 後 (FI) だけが居る
    discrepancies = reconcile_with_broker(res, {"FI": 100})

    symbols = {d.symbol for d in discrepancies}
    assert symbols == {"FISV", "FI"}
    assert res.measured is True  # 約定は掴めている
    assert res.complete is False  # が、取りこぼしがある
    assert res.unmeasured_symbols == ["FI", "FISV"]
    assert any("reconstructed_only" in d.reason for d in discrepancies)
    assert any("broker_only" in d.reason for d in discrepancies)


def test_broker_reconcile_tolerates_fractional_epsilon():
    fills = parse_fills([fill("III", "buy", "10.00001", 10, "2026-07-01T14:00:00Z")])
    res = reconstruct_round_trips(fills)
    assert reconcile_with_broker(res, {"III": 10}) == []
    assert res.complete is True


def test_broker_reconcile_flags_qty_mismatch():
    fills = parse_fills([fill("JJJ", "buy", 100, 10, "2026-07-01T14:00:00Z")])
    res = reconstruct_round_trips(fills)
    d = reconcile_with_broker(res, {"JJJ": 50})
    assert len(d) == 1
    assert "qty_mismatch" in d[0].reason


def test_intent_not_filled_is_surfaced_after_the_session_closed():
    """「exit するつもりだった」のに約定していない = 取りこぼし。必ず立つ。"""
    fills = parse_fills(
        [
            fill("KKK", "buy", 1, 10, "2026-07-01T14:00:00Z"),
            fill("KKK", "sell", 1, 11, "2026-07-06T14:00:00Z"),
        ]
    )
    res = reconstruct_round_trips(fills)
    recon = reconcile_intents_with_fills(
        [
            {"symbol": "KKK", "reason": "time_based"},
            {"symbol": "LLL", "reason": "protect_stop"},
        ],
        res.closed_trades,
        session_date="2026-07-06",
        session_state=SESSION_CLOSED,
    )
    assert recon["n_intended"] == 2
    assert recon["n_filled"] == 1
    assert recon["intended_not_filled"] == [{"symbol": "LLL", "reason": "protect_stop"}]
    assert recon["fully_reconciled"] is False
    assert recon["evaluated"] is True


@pytest.mark.parametrize("state", [SESSION_BEFORE_OPEN, SESSION_OPEN])
def test_intent_pending_before_the_session_is_not_a_failure(state):
    """寄り前に「20 件が未約定」と毎朝叫ばない。ただし pending として残す。"""
    recon = reconcile_intents_with_fills(
        [{"symbol": "MMM", "reason": "time_based"}],
        [],
        session_date="2026-07-22",
        session_state=state,
    )
    assert recon["intended_not_filled"] == []
    assert recon["intended_pending"] == [{"symbol": "MMM", "reason": "time_based"}]
    assert recon["fully_reconciled"] is True
    assert recon["evaluated"] is False


def test_intent_state_unknown_falls_back_to_surfacing_the_miss():
    """判定不能 (clock 不通) を「問題なし」に倒さない = silent success を作らない。"""
    recon = reconcile_intents_with_fills(
        [{"symbol": "NNN", "reason": "time_based"}],
        [],
        session_date="2026-07-22",
        session_state=SESSION_UNKNOWN,
    )
    assert recon["intended_not_filled"] == [{"symbol": "NNN", "reason": "time_based"}]
    assert recon["fully_reconciled"] is False


def test_unexpected_exit_is_surfaced_too():
    """意図していない決済 (broker 側 close / 手動) も落とさず出す。"""
    fills = parse_fills(
        [
            fill("OOO", "buy", 1, 10, "2026-07-01T14:00:00Z"),
            fill("OOO", "sell", 1, 11, "2026-07-06T14:00:00Z"),
        ]
    )
    res = reconstruct_round_trips(fills)
    recon = reconcile_intents_with_fills(
        [], res.closed_trades, session_date="2026-07-06", session_state=SESSION_CLOSED
    )
    assert recon["filled_not_intended"] == ["OOO"]


# ---------------------------------------------------------------------------
# 集計
# ---------------------------------------------------------------------------


def test_summaries_never_fabricate_zero_for_empty_buckets():
    empty = summarize_realized([])
    assert empty["n_trades"] == 0
    for key in (
        "total_realized_pl",
        "win_rate_pct",
        "avg_win",
        "avg_loss",
        "best",
        "worst",
    ):
        assert empty[key] is None


def test_summarize_by_system_keeps_untagged_trades_under_unknown():
    fills = parse_fills(
        [
            fill("P1", "buy", 1, 10, "2026-07-01T14:00:00Z"),
            fill("P1", "sell", 1, 12, "2026-07-02T14:00:00Z"),
            fill("P2", "buy", 1, 10, "2026-07-01T14:00:00Z"),
            fill("P2", "sell", 1, 9, "2026-07-02T14:00:00Z"),
        ]
    )
    res = reconstruct_round_trips(fills)
    res.closed_trades[0].system = "system1"
    by_system = summarize_by_system(res.closed_trades)
    assert set(by_system) == {"system1", "unknown"}
    assert by_system["system1"]["n_trades"] == 1
    assert by_system["unknown"]["n_trades"] == 1  # 捨てない


# ---------------------------------------------------------------------------
# 3. 当日損益は同一基準でしか出さない (「今日 +$2,850.35」再発防止)
# ---------------------------------------------------------------------------


def test_prev_session_close_never_picks_the_current_session():
    series = {"2026-07-17": 104931.91, "2026-07-20": 103709.63}
    assert pick_prev_session_close(series, "2026-07-20") == ("2026-07-17", 104931.91)
    # 現セッションを基準にすると当日損益が常に 0 になる
    assert pick_prev_session_close(series, "2026-07-17") == (None, None)


def test_session_pnl_uses_intraday_baseline_not_last_equity():
    """2026-07-20 の実データ再現。

    published snapshot は ``equity - last_equity`` で **+$2,850.35** を出していた。
    last_equity (broker 日次終値系列) は上場廃止 (INACTIVE) 建玉の時価
    $4,285.87 を計上しないため、live equity と基準が違う。同一基準
    (intraday 系列) で引き直すと当日は **損** だったのが正しい。
    """
    equity_now = 103515.47
    last_equity = 100665.12  # 使ってはいけない値 (基準違い)
    intraday = {"2026-07-16": 105430.78, "2026-07-17": 104931.91}

    pnl = resolve_session_pnl(
        equity_now=equity_now,
        session_date="2026-07-20",
        intraday_by_session=intraday,
        realized_pl=-2333.06,
    )

    assert pnl.measured is True
    assert pnl.basis == "prev_session_intraday"
    assert pnl.baseline_session == "2026-07-17"
    assert pnl.baseline_equity == 104931.91
    assert pnl.total_pl == pytest.approx(-1416.44, abs=0.01)
    # 幻の数字が二度と出ないこと
    assert pnl.total_pl != pytest.approx(equity_now - last_equity, abs=0.01)
    assert pnl.total_pl < 0


def test_session_pnl_splits_realized_and_unrealized():
    pnl = resolve_session_pnl(
        equity_now=1100.0,
        session_date="2026-07-20",
        intraday_by_session={"2026-07-17": 1000.0},
        realized_pl=40.0,
    )
    assert pnl.total_pl == 100.0
    assert pnl.realized_pl == 40.0
    assert pnl.unrealized_delta == 60.0  # 実現と含みを混ぜない


def test_session_pnl_leaves_unrealized_none_when_realized_unmeasured():
    """実現損益が未計測なら含み分も出さない (差し引きの片側が不明なので)。"""
    pnl = resolve_session_pnl(
        equity_now=1100.0,
        session_date="2026-07-20",
        intraday_by_session={"2026-07-17": 1000.0},
        realized_pl=None,
    )
    assert pnl.total_pl == 100.0
    assert pnl.realized_pl is None
    assert pnl.unrealized_delta is None


@pytest.mark.parametrize(
    ("kwargs", "needle"),
    [
        ({"equity_now": None}, "equity_now"),
        ({"session_date": None}, "セッション"),
        ({"intraday_by_session": {}}, "intraday"),
        ({"intraday_by_session": {"2026-07-20": 1.0}}, "前セッション"),
    ],
)
def test_session_pnl_refuses_to_guess(kwargs, needle):
    """基準が取れない時は数字を出さない。0 でも近似でも埋めない。"""
    base = {
        "equity_now": 1000.0,
        "session_date": "2026-07-20",
        "intraday_by_session": {"2026-07-17": 900.0},
    }
    base.update(kwargs)
    pnl = resolve_session_pnl(**base)

    assert pnl.measured is False
    assert pnl.total_pl is None
    assert pnl.total_pl_pct is None
    assert pnl.basis == "unavailable"
    assert pnl.reason and needle in pnl.reason


def test_session_pnl_row_never_reports_a_number_while_unmeasured():
    """to_row() の契約: measured=False なら total_pl は必ず None。"""
    row = resolve_session_pnl(
        equity_now=1000.0, session_date="2026-07-20", intraday_by_session={}
    ).to_row()
    assert row["measured"] is False
    assert row["total_pl"] is None
    assert row["total_pl_pct"] is None
    assert row["baseline_equity"] is None
