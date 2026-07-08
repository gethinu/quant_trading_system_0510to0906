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
