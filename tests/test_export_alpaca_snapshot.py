"""export_alpaca_snapshot の read-only / paper 契約と純関数の regression test.

Alpaca に接続しない (offline)。pure helper と --no-alpaca 経路のみ検証する。
live URL 混入は tests/test_alpaca_no_live_url.py が別途 global scan で守る。
"""

from __future__ import annotations

import json

import pytest

from scripts import export_alpaca_snapshot as ex


# --- safety contract ------------------------------------------------------
def test_paper_base_is_paper_only():
    """portfolio-history の base URL は paper-api 固定 (live host を含まない)。"""
    assert ex.PAPER_BASE == "https://paper-api.alpaca.markets"
    # host は必ず paper- 前置 (live host は 'paper-' の後ろに来ない)。
    assert ex.PAPER_BASE.startswith("https://paper-api.")
    host = ex.PAPER_BASE.split("://", 1)[1].split("/", 1)[0]
    assert host.startswith("paper-")


def test_no_submit_symbols_referenced():
    """発注系 API シンボルを import していない (read-only 保証の一助)。"""
    src = ex.__file__
    with open(src, encoding="utf-8") as fh:
        text = fh.read()
    for banned in (
        "submit_order",
        "MarketOrderRequest",
        "cancel_orders",
        "reset_paper_account",
    ):
        assert banned not in text, f"read-only exporter に発注系 {banned} が混入"


def test_no_alpaca_mode_returns_zero(capsys):
    """--no-alpaca は接続せず 0 で終了 (snapshot 未生成)。"""
    rc = ex.main(["--no-alpaca"])
    assert rc == 0


# --- pure helpers ---------------------------------------------------------
class _FakePos:
    def __init__(self, side, qty):
        self.side = side
        self.qty = qty


def test_side_of_handles_enum_and_qty_sign():
    # enum-like str "PositionSide.LONG" は解釈不能 → qty 符号で fallback
    assert ex._side_of(_FakePos("PositionSide.LONG", 5), 5.0) == "long"
    assert ex._side_of(_FakePos("PositionSide.SHORT", -5), -5.0) == "short"
    # 素の value なら直接
    assert ex._side_of(_FakePos("long", 1), 1.0) == "long"
    assert ex._side_of(_FakePos("short", -1), -1.0) == "short"
    # side 不明でも qty 符号で決まる
    assert ex._side_of(_FakePos("", -3), -3.0) == "short"


def test_augment_curve_drawdown_and_live_point():
    curve = {
        "points": [
            {"t": "2026-06-01", "equity": 100.0, "pl": None, "pl_pct": None},
            {"t": "2026-06-02", "equity": 110.0, "pl": None, "pl_pct": None},
            {"t": "2026-06-03", "equity": 99.0, "pl": None, "pl_pct": None},
        ]
    }
    ex._augment_curve(curve, live_equity=104.5, today="2026-06-04")
    pts = curve["points"]
    # live point が末尾に付与
    assert pts[-1]["t"] == "2026-06-04"
    assert pts[-1]["equity"] == 104.5
    assert pts[-1].get("live") is True
    # peak は 110 で確定、最大DD は 99/110-1 = -10%
    assert curve["peak_equity"] == 110.0
    assert curve["max_drawdown_pct"] == pytest.approx(-10.0, abs=0.01)
    # 期間リターン: (104.5-100)/100 = +4.5%
    assert curve["period_return_pct"] == pytest.approx(4.5, abs=0.01)
    # 各点に peak / dd_pct が付く
    assert all("peak" in p and "dd_pct" in p for p in pts)


def test_augment_curve_replaces_same_day_point():
    curve = {
        "points": [{"t": "2026-06-04", "equity": 100.0, "pl": None, "pl_pct": None}]
    }
    ex._augment_curve(curve, live_equity=101.0, today="2026-06-04")
    assert len(curve["points"]) == 1
    assert curve["points"][0]["equity"] == 101.0
    assert curve["points"][0]["live"] is True


def test_estimate_stop_target_long_short():
    from common.trade_management import SYSTEM_TRADE_RULES

    rules = SYSTEM_TRADE_RULES["system2"]  # short, atr stop + pct target
    atr = {int(rules.stop_atr_period): 2.0, int(rules.profit_target_atr_period): 2.0}
    stop, target = ex._estimate_stop_target(
        side="short", avg_entry=100.0, rules=rules, atr=atr
    )
    # short の stop は entry より上
    assert stop is not None and stop > 100.0


def test_exit_type_mapping():
    from common.trade_management import SYSTEM_TRADE_RULES

    assert ex._exit_type("system7", None) == "spy_hedge"
    assert ex._exit_type("system2", SYSTEM_TRADE_RULES["system2"]) == "time"
    assert ex._exit_type("unknownsys", None) == "unknown"


def test_build_reconciliation_reads_latest_files(tmp_path):
    # today_signals ファイルを2件置き、新しい方 (20260707) が採用されること
    (tmp_path / "today_signals_20260706.json").write_text(
        json.dumps(
            {"date": "2026-07-06", "portfolio": {"total_signals": 3}, "systems": {}}
        ),
        encoding="utf-8",
    )
    (tmp_path / "today_signals_20260707.json").write_text(
        json.dumps(
            {
                "date": "2026-07-07",
                "portfolio": {"total_signals": 2},
                "systems": {
                    "system1": {
                        "signals": [
                            {"symbol": "AAPL", "side": "BUY"},
                            {"symbol": "MSFT", "side": "SELL"},
                        ]
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    rec = ex._build_reconciliation(tmp_path, held_symbols={"AAPL", "TSLA"})
    assert rec["signals_date"] == "2026-07-07"
    assert rec["signals_total"] == 2
    assert rec["signals_buy"] == 1
    assert rec["signals_sell"] == 1
    assert rec["held_now"] == 2
    # AAPL は保有中、MSFT は非保有 → 1
    assert rec["held_from_signals"] == 1


def test_latest_json_numeric_ordering(tmp_path):
    # 数値比較 (lexical でなく) で最大日付を採る
    for d in ("20260701", "20260709", "20260630"):
        (tmp_path / f"alpaca_snapshot_{d}.json").write_text("{}", encoding="utf-8")
    latest = ex._latest_json(tmp_path, "alpaca_snapshot_")
    assert latest is not None
    assert latest.name == "alpaca_snapshot_20260709.json"


# --- 当日損益 / 期間切替 / 実現損益 ---------------------------------------
def test_last_equity_is_never_used_for_today_pnl():
    """``equity - last_equity`` は基準違いの引き算。二度と書かない (source guard)。

    2026-07-20 の published snapshot はこの式で「今日 +$2,850.35」を出していた。
    """
    import io
    import tokenize

    with open(ex.__file__, encoding="utf-8") as fh:
        source = fh.read()
    # コメント / docstring は「なぜ禁止か」の説明でこの式を含むので除外し、
    # 実行される code だけを見る。
    code = "".join(
        tok.string if tok.type not in (tokenize.COMMENT, tokenize.STRING) else " "
        for tok in tokenize.generate_tokens(io.StringIO(source).readline)
        if tok.type
        not in (tokenize.NL, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT)
    )
    normalized = code.replace(" ", "")
    for expr in ("equity-last_equity", "last_equity-equity"):
        assert expr not in normalized, f"当日損益に基準違いの式 {expr!r} が復活している"
    # last_equity は「参考値としてそのまま載せる」以外に使わない。
    assert "resolve_session_pnl" in normalized


def test_fold_intraday_by_session_takes_last_point_of_each_session():
    points = [
        {"t": "2026-07-17 15:55", "session": "2026-07-17", "equity": 104900.0},
        {"t": "2026-07-17 16:00", "session": "2026-07-17", "equity": 104931.91},
        {"t": "2026-07-20 09:35", "session": "2026-07-20", "equity": 103515.47},
    ]
    folded = ex.fold_intraday_by_session(points)
    assert folded == {"2026-07-17": 104931.91, "2026-07-20": 103515.47}


def test_fold_intraday_drops_nonpositive_and_missing_equity():
    points = [
        {"t": "x", "session": "2026-07-17", "equity": 0},
        {"t": "y", "session": "2026-07-17", "equity": None},
        {"t": "z", "session": None, "equity": 100.0},
    ]
    assert ex.fold_intraday_by_session(points) == {}


def _daily(*pairs):
    return [{"t": t, "equity": e, "pl": None, "pl_pct": None} for t, e in pairs]


def test_build_equity_ranges_marks_basis_and_never_mixes_them():
    """1D は intraday、長期は broker 日次。会計基準が違うので必ずラベルする。"""
    daily = _daily(
        ("2026-04-01", 100.0),
        ("2026-06-01", 110.0),
        ("2026-07-17", 100665.12),
        ("2026-07-20", 99355.81),
    )
    intraday = [
        {"t": "2026-07-20 09:35", "session": "2026-07-20", "equity": 103515.47},
        {"t": "2026-07-20 09:40", "session": "2026-07-20", "equity": 103600.0},
    ]
    ranges = ex._build_equity_ranges(daily, intraday, "2026-07-20", 103700.0)

    assert set(ranges) == {"1D", "1W", "1M", "3M", "ALL"}
    assert ranges["1D"]["basis"] == "intraday"
    for key in ("1W", "1M", "3M", "ALL"):
        assert ranges[key]["basis"] == "broker_daily"
    # 日次レンジに live equity 点を足さない (末尾だけ数千ドル跳ねる旧事故の再発防止)
    for key in ("1W", "1M", "3M", "ALL"):
        assert all(not p.get("live") for p in ranges[key]["points"])
        assert ranges[key]["points"][-1]["equity"] == 99355.81
    # 1D は当日 intraday + live 点
    assert ranges["1D"]["points"][-1]["equity"] == 103700.0
    assert ranges["1D"]["points"][-1]["live"] is True


def test_build_equity_ranges_slices_by_days_and_recomputes_drawdown_per_range():
    daily = _daily(
        ("2026-01-05", 100.0),  # ALL にだけ入る古い山
        ("2026-07-15", 90.0),
        ("2026-07-20", 95.0),
    )
    ranges = ex._build_equity_ranges(daily, [], "2026-07-20", None)
    assert [p["t"] for p in ranges["1W"]["points"]] == ["2026-07-15", "2026-07-20"]
    assert [p["t"] for p in ranges["ALL"]["points"]] == [
        "2026-01-05",
        "2026-07-15",
        "2026-07-20",
    ]
    # 区間ごとに peak を取り直す (1W が過去の 100 からの DD を引きずらない)
    assert ranges["1W"]["peak_equity"] == 95.0
    assert ranges["ALL"]["peak_equity"] == 100.0


def test_build_equity_ranges_empty_is_empty_not_zero():
    """データが無いレンジは points 空。0 の点をでっち上げない。"""
    ranges = ex._build_equity_ranges([], [], None, None)
    for key in ("1D", "1W", "ALL"):
        assert ranges[key]["points"] == []
        assert ranges[key]["n_points"] == 0
        assert ranges[key]["period_return_pct"] is None


def test_compute_equity_basis_explains_the_daily_series_gap():
    """live equity と broker 日次系列の差を「上場廃止建玉の時価」で説明する。"""
    positions = [
        {"symbol": "CDTX", "system": "delisted", "market_value": 2213.80},
        {"symbol": "FOLD", "system": "delisted", "market_value": 2072.07},
        {"symbol": "AAPL", "system": "system1", "market_value": 5000.0},
    ]
    basis = ex.compute_equity_basis(
        positions,
        equity=103804.68,
        last_daily_equity=99355.81,
        last_daily_session="2026-07-20",
    )
    assert basis["n_frozen"] == 2
    assert basis["frozen_symbols"] == ["CDTX", "FOLD"]
    assert basis["frozen_market_value"] == pytest.approx(4285.87, abs=0.01)
    assert basis["daily_series_gap"] == pytest.approx(4448.87, abs=0.01)
    assert basis["last_daily_session"] == "2026-07-20"
    # 説明しきれない残差 (日次最終点以降の値動きを含む) も隠さない
    assert basis["residual_usd"] == pytest.approx(163.0, abs=0.01)


def test_compute_equity_basis_without_daily_series_returns_none_gap():
    basis = ex.compute_equity_basis([], equity=100.0, last_daily_equity=None)
    assert basis["daily_series_gap"] is None
    assert basis["residual_usd"] is None
    assert basis["frozen_market_value"] == 0.0


def _ledger(date="2026-07-20", measured=True, by_day=None):
    return {
        "date": date,
        "run_id": "r1",
        "generated_at": "2026-07-20T14:00:00Z",
        "measurement": {"measured": measured, "complete": False, "reasons": ["x"]},
        "today": {"realized_pl": -2333.06, "measured": True, "n_closed": 3},
        "realized": {
            "all_time": {"n_trades": 649},
            "by_day": (
                [
                    {
                        "t": "2026-07-20",
                        "realized_pl": -2333.06,
                        "realized_pl_cum": 3334.08,
                    }
                ]
                if by_day is None
                else by_day
            ),
            "by_system": {},
        },
        "closed_trades": [{"symbol": "ZCMD"}],
        "exit_intent_reconciliation": {"fully_reconciled": True},
    }


def test_realized_block_absent_ledger_is_unmeasured_not_zero():
    block = ex._realized_block(None, "2026-07-20")
    assert block["available"] is False
    assert block["measured"] is False
    assert block["all_time"] is None
    assert block["closed_trades"] == []
    assert "build_exit_ledger" in block["reason"]


def test_realized_block_flags_a_stale_ledger():
    block = ex._realized_block(_ledger(date="2026-07-17"), "2026-07-20")
    assert block["available"] is True
    assert block["stale"] is True
    assert "2026-07-17" in block["reason"]


def test_realized_for_session_matches_the_pnl_baseline_session():
    """実現損益は **当日損益と同じ立会日** で引く (台帳の "today" を流用しない)。

    台帳の "today" は pipeline のローカル日 (JST)、当日損益の基準は broker の
    立会日 (ET)。JST 昼に走らせるとこの 2 つは 1 日ずれるので、混ぜると
    「07-21 の値動きに 07-22 の実現損益をぶつける」ことになる。
    """
    ledger = _ledger(
        date="2026-07-22", by_day=[{"t": "2026-07-20", "realized_pl": -2333.06}]
    )
    # 台帳範囲内で該当日に決済が無い = 実現 0 (事実)
    assert ex._realized_for_session(ledger, "2026-07-21") == 0.0
    assert ex._realized_for_session(ledger, "2026-07-20") == -2333.06


def test_realized_for_session_refuses_sessions_beyond_the_ledger():
    """台帳が届いていないセッションを 0 で埋めない。"""
    ledger = _ledger(date="2026-07-20")
    assert ex._realized_for_session(ledger, "2026-07-21") is None


def test_realized_for_session_is_none_when_unmeasured_or_absent():
    assert ex._realized_for_session(_ledger(measured=False), "2026-07-20") is None
    assert ex._realized_for_session(None, "2026-07-20") is None
    assert ex._realized_for_session(_ledger(), None) is None
