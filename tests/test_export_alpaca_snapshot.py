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


# --- freeze-aware today baseline (pure) -----------------------------------
# 実データ (2026-07-14 snapshot / live probe) の値で回帰を固定する。
_FREEZE = dict(
    equity=106024.72,  # 07-14 snapshot equity (実勢)
    last_equity=101812.81,  # 07-13 daily-close (凍結ラグで低く据え置き)
    prev_intraday=105825.56,  # 07-13 intraday 最終 (実勢, 1H/ext 実測)
)


def test_baseline_freeze_lag_switches_to_intraday():
    """凍結ラグ日: daily-close が intraday より ~$4,013 低い → intraday 基準に補正。

    phantom (+$4,211 / +4.14%) が消え、実勢の小さな当日差になること。
    """
    baseline, basis, gap = ex.resolve_today_baseline(
        _FREEZE["equity"], _FREEZE["last_equity"], _FREEZE["prev_intraday"]
    )
    assert basis == "freeze_adjusted"
    assert baseline == pytest.approx(105825.56, abs=0.01)
    assert gap == pytest.approx(105825.56 - 101812.81, abs=0.01)  # +4012.75
    # 補正後の当日 P&L は phantom($4,211)でなく実勢(~$199, <0.3%)
    adj_abs = round(_FREEZE["equity"] - baseline, 2)
    adj_pct = round((_FREEZE["equity"] - baseline) / baseline * 100.0, 3)
    assert adj_abs == pytest.approx(199.16, abs=0.01)
    assert abs(adj_pct) < 0.3
    # raw(未補正)は依然 phantom を示す (透明性)
    raw_pct = (
        (_FREEZE["equity"] - _FREEZE["last_equity"]) / _FREEZE["last_equity"] * 100
    )
    assert raw_pct == pytest.approx(4.137, abs=0.01)


def test_baseline_normal_day_unchanged():
    """平常日 (intraday ≈ daily-close): last_equity 基準のまま挙動不変。"""
    # 前日 intraday が daily-close とほぼ一致 (乖離 $12 = 閾値未満)
    baseline, basis, gap = ex.resolve_today_baseline(
        equity=101200.0, last_equity=101000.0, prev_intraday_equity=101012.0
    )
    assert basis == "last_equity"
    assert baseline == 101000.0
    assert gap is None


def test_baseline_no_intraday_falls_back():
    """intraday 取得不可 (None) → last_equity 基準に安全 fallback。"""
    baseline, basis, gap = ex.resolve_today_baseline(
        equity=106000.0, last_equity=101800.0, prev_intraday_equity=None
    )
    assert basis == "last_equity"
    assert baseline == 101800.0
    assert gap is None


def test_baseline_threshold_pct_gate():
    """乖離が equity 比 1% 未満なら補正しない (境界)。"""
    eq = 100000.0
    # gap = $900 (<$1000 かつ <1%) → 補正なし
    _, basis_lo, _ = ex.resolve_today_baseline(eq, 100000.0, 100900.0)
    assert basis_lo == "last_equity"
    # gap = $1500 (>$1000 かつ >1%) → 補正
    _, basis_hi, _ = ex.resolve_today_baseline(eq, 100000.0, 101500.0)
    assert basis_hi == "freeze_adjusted"


def test_baseline_missing_equity_is_safe():
    """equity / last_equity が欠損でも例外を出さず last_equity 基準を返す。"""
    assert ex.resolve_today_baseline(None, 101000.0, 105000.0)[1] == "last_equity"
    assert ex.resolve_today_baseline(106000.0, None, 105000.0) == (
        None,
        "last_equity",
        None,
    )
    assert ex.resolve_today_baseline(106000.0, 0.0, 105000.0)[1] == "last_equity"


# --- baseline session pick (off-by-one regression, pure) -------------------
# 実データ (2026-07-19 live probe, Sunday premarket run) で回帰を固定する。
# 1D(daily-close) は 1H(intraday) より恒常的に ~$4,285 低い (Alpaca paper の
# short 計上差と観測)。real-time equity は 1H と整合するため 1D が異常値。
_D1 = {  # ET date -> daily-close (1D)
    "2026-07-15": 102020.82,
    "2026-07-16": 101130.14,
    "2026-07-17": 100665.12,  # == account.last_equity (直近完了セッション)
}
_H1 = {  # ET date -> last intraday equity (1H/ext)
    "2026-07-15": 106192.77,
    "2026-07-16": 105703.75,
    "2026-07-17": 104922.86,  # 07-17 の intraday 整合 close (正しい基準)
}


def test_pick_baseline_anchors_to_last_equity_session():
    """off-by-one 回帰: 寄り前/週末実行でも last_equity の指すセッションを選ぶ。

    旧 sorted[-2] は 07-16 (105703.75) を誤選択し phantom -$752 を出していた。
    修正後は last_equity(=07-17 daily-close)にアンカーして 07-17 intraday
    (104922.86) を返す → 当日 P&L はほぼ flat になる。
    """
    picked = ex._pick_baseline_intraday_equity(100665.12, _D1, _H1)
    assert picked == pytest.approx(104922.86, abs=0.01)  # 07-17, NOT 07-16
    # このセッション基準なら当日 P&L は実勢 (~$28, flat)。phantom(-$752)ではない。
    equity_now = 104950.99
    assert round(equity_now - picked, 2) == pytest.approx(28.13, abs=0.5)


def test_pick_baseline_ignores_partial_today_daily_point():
    """intraday 実行で 1D に当日 partial 点が混ざっても last_equity で正しく同定。"""
    d1 = dict(_D1)
    d1["2026-07-20"] = 105200.0  # 当日 partial (高い) が混ざるケース
    h1 = dict(_H1)
    h1["2026-07-20"] = 105100.0
    # last_equity は依然 07-17 (前営業日 close)。当日 partial に釣られない。
    picked = ex._pick_baseline_intraday_equity(100665.12, d1, h1)
    assert picked == pytest.approx(104922.86, abs=0.01)  # 07-17


def test_pick_baseline_fallback_latest_when_no_daily():
    """1D 取得不可 (空) → intraday 最新日 (直近完了セッション) に fallback。

    旧 [-2] ではなく [-1]: 寄り前/週末は最新 intraday 日が直近完了セッション。
    """
    picked = ex._pick_baseline_intraday_equity(100665.12, {}, _H1)
    assert picked == pytest.approx(104922.86, abs=0.01)  # 最新 = 07-17


def test_pick_baseline_empty_or_missing_is_safe():
    """intraday 空 / last_equity 欠損なら None (=補正しない)。"""
    assert ex._pick_baseline_intraday_equity(100665.12, _D1, {}) is None
    assert ex._pick_baseline_intraday_equity(None, _D1, _H1) is None


# --- ledger reconciliation (pure) -----------------------------------------
def test_ledger_recon_all_consistent():
    """position が台帳ネットと一致すれば desync_free、mismatch 0。"""
    ledger = {"AAPL": {"net": 10.0, "n_fills": 1}, "MSFT": {"net": 0.5, "n_fills": 2}}
    pos = {"AAPL": 10.0, "MSFT": 0.5}
    rec = ex._build_ledger_reconciliation(ledger, pos)
    assert rec["available"] is True
    assert rec["n_consistent"] == 2
    assert rec["n_mismatch"] == 0
    assert rec["n_desync"] == 0
    assert rec["desync_free"] is True


def test_ledger_recon_flags_real_desync():
    """position≠0 で qty が台帳と乖離 → class=position_vs_ledger, desync として検出。

    forensic が捉えた「fill ネット0なのに逆建玉が残る」broker 過渡不整合の再現。
    """
    ledger = {"AAL": {"net": 0.0, "n_fills": 2}}  # entry+exit で net 0
    pos = {"AAL": 1.0}  # なのに long +1 が残っている (反転ダスト)
    rec = ex._build_ledger_reconciliation(ledger, pos)
    assert rec["n_desync"] == 1
    assert rec["desync_free"] is False
    m = rec["mismatches"][0]
    assert m["symbol"] == "AAL"
    assert m["class"] == "position_vs_ledger"
    assert m["likely_symbol_migration"] is False
    assert m["position_qty"] == 1.0
    assert m["ledger_net"] == 0.0


def test_ledger_recon_symbol_migration_is_benign():
    """position=0 で ledger_net≠0 → ledger_only_flat (ticker 改称等)。desync ではない。"""
    # EXPI で buy 150 / AGNT で sell 150 → symbol 別ネットは非ゼロだが現ポジ0
    ledger = {
        "EXPI": {"net": 150.0, "n_fills": 3},
        "AGNT": {"net": -150.0, "n_fills": 2},
    }
    pos: dict[str, float] = {}  # どちらも保有無し
    rec = ex._build_ledger_reconciliation(ledger, pos)
    assert rec["n_desync"] == 0
    assert rec["desync_free"] is True
    assert rec["n_mismatch"] == 2
    classes = {m["symbol"]: m["class"] for m in rec["mismatches"]}
    assert classes == {"EXPI": "ledger_only_flat", "AGNT": "ledger_only_flat"}
    assert all(m["likely_symbol_migration"] for m in rec["mismatches"])


def test_ledger_recon_unavailable_when_empty():
    """fill 取得不可 (空 ledger) → available False、突合スキップで degrade。"""
    rec = ex._build_ledger_reconciliation({}, {"AAPL": 10.0})
    assert rec["available"] is False
    assert rec["desync_free"] is None
    assert rec["n_desync"] is None
