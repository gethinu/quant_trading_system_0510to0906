"""自己監視ガード (scripts/self_monitor_check) の各チェック検証。

tmp fixture の results_csv / logs を組み立て、鮮度・シグナル数・open_run 状態の
判定 (ok/warn/crit/info) が期待通りに出ることを固定化する。Alpaca / git / ntfy は不要。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.self_monitor_check import (  # noqa: E402
    _abort_reason,
    _latest_open_run_dir,
    check_daily,
    check_data_advance,
    check_open_run,
    check_publish,
    check_signals,
    classify_zero_entry,
    notification_evidence,
)


def _write(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _latest_nyse_or_skip():
    """Resolve the real latest NYSE trading day; skip if the dep is unavailable."""
    pd = pytest.importorskip("pandas")
    try:
        from common.utils_spy import get_latest_nyse_trading_day
    except Exception:  # noqa: BLE001
        pytest.skip("common.utils_spy unavailable")
    now = pd.Timestamp.now(tz="America/New_York").tz_localize(None).normalize()
    return pd, pd.Timestamp(get_latest_nyse_trading_day(now)).normalize()


# --- daily freshness -------------------------------------------------------
def test_daily_ok_when_fresh(tmp_path: Path):
    rd = tmp_path / "results_csv"
    _write(rd / "today_signals_20260712.json", {"date": "2026-07-12", "portfolio": {}})
    r = check_daily(rd, max_age_hours=26)
    assert r.status == "ok"


def test_daily_crit_when_missing(tmp_path: Path):
    rd = tmp_path / "results_csv"
    rd.mkdir(parents=True)
    r = check_daily(rd, max_age_hours=26)
    assert r.status == "crit"


def test_daily_crit_when_stale(tmp_path: Path):
    rd = tmp_path / "results_csv"
    f = rd / "today_signals_20260701.json"
    _write(f, {"date": "2026-07-01"})
    # mtime を 48h 前へ
    old = time.time() - 48 * 3600
    os.utime(f, (old, old))
    r = check_daily(rd, max_age_hours=26)
    assert r.status == "crit"


# --- data_fresh (full_backup absolute staleness) ---------------------------
def test_data_fresh_ok_ignores_stale_rolling_spy(tmp_path: Path):
    """full_backup が新鮮なら、rolling/SPY.csv が古くても OK。

    SPY は non-universe ETF で rolling へは同期されない (毎日 stale drift)。
    verdict は full_backup 基準なので rolling SPY の陳腐化に釣られてはいけない
    (2026-07-19 の恒久 fix: 火曜以降も再発しないことの固定化)。
    """
    _pd, latest = _latest_nyse_or_skip()
    dc = tmp_path / "data_cache"
    _write_text(dc / "full_backup" / "SPY.csv", f"Date,Close\n{latest.date()},700\n")
    # rolling SPY は意図的に大幅 stale (半年前) にしておく
    _write_text(dc / "rolling" / "SPY.csv", "index,Date,Close\n0,2026-01-02,600\n")
    r = check_data_advance(dc)
    assert r.status == "ok", r.detail
    # honest display: 誤解を招く rolling 値を出さない
    assert "rolling" not in r.detail
    assert "rolling_last" not in r.data


def test_data_fresh_crit_when_fullbackup_stale(tmp_path: Path):
    """full_backup 自体が市場より大きく遅れていれば CRIT (cache 凍結を正しく検出)。"""
    _latest_nyse_or_skip()
    dc = tmp_path / "data_cache"
    _write_text(dc / "full_backup" / "SPY.csv", "Date,Close\n2026-01-02,600\n")
    r = check_data_advance(dc)
    assert r.status == "crit", r.detail


def test_data_fresh_skip_when_fullbackup_missing(tmp_path: Path):
    dc = tmp_path / "data_cache"
    (dc / "full_backup").mkdir(parents=True)
    r = check_data_advance(dc)
    assert r.status == "skip"


# --- signals abundance -----------------------------------------------------
def test_signals_ok(tmp_path: Path):
    rd = tmp_path / "results_csv"
    _write(
        rd / "today_signals_20260712.json",
        {"date": "2026-07-12", "portfolio": {"total_signals": 44}},
    )
    r = check_signals(rd, min_signals=10)
    assert r.status == "ok"
    assert r.data["total_signals"] == 44


def test_signals_crit_when_zero(tmp_path: Path):
    rd = tmp_path / "results_csv"
    _write(
        rd / "today_signals_20260712.json",
        {"date": "2026-07-12", "portfolio": {"total_signals": 0}},
    )
    r = check_signals(rd, min_signals=10)
    assert r.status == "crit"


def test_signals_warn_when_thin(tmp_path: Path):
    rd = tmp_path / "results_csv"
    _write(
        rd / "today_signals_20260712.json",
        {"date": "2026-07-12", "portfolio": {"total_signals": 3}},
    )
    r = check_signals(rd, min_signals=10)
    assert r.status == "warn"


def test_signals_counts_from_systems_when_no_portfolio(tmp_path: Path):
    rd = tmp_path / "results_csv"
    _write(
        rd / "today_signals_20260712.json",
        {"date": "2026-07-12", "systems": {"system1": {"signals": [1, 2, 3]}}},
    )
    r = check_signals(rd, min_signals=2)
    assert r.data["total_signals"] == 3
    assert r.status == "ok"


# --- open_run status -------------------------------------------------------
def test_open_run_market_closed_is_ok(tmp_path: Path):
    logs = tmp_path / "logs"
    d = logs / "open_run_20260711"
    _write(
        d / "completion_recon.json", {"date": "2026-07-11", "abort": "market_closed"}
    )
    r = check_open_run(logs, tmp_path / "results_csv", max_age_hours=96)
    assert r.status == "ok"


def test_open_run_thin_signal_abort_is_warn(tmp_path: Path):
    """通知が **届いた** abort は従来どおり WARN (人がもう知っている)。"""
    logs = tmp_path / "logs"
    d = logs / "open_run_20260713"
    _write(
        d / "completion_recon.json",
        {"date": "2026-07-13", "abort": "thin_signals:2<10"},
    )
    _write_text(d / "run.log", "[2026-07-13 22:35:10] [ntfy] warn 送信 ok=True\n")
    r = check_open_run(logs, tmp_path / "results_csv", max_age_hours=96)
    assert r.status == "warn"
    assert r.data["notification"] == "delivered"


# --- 通知が出ないまま abort した run は CRIT (2026-08-24 の host DNS 断) ---------
# 実データ: logs/open_run_20260824/ は abort=clock_unavailable + run.log に
# `[ntfy] warn 送信 ok=False` が 2 本 (22:35 / 23:35)。gate の clock も停止通知の ntfy も
# 同じ死んだ DNS を通るので、run が止まったこと自体が誰にも届かなかった。
# 「abort した」だけなら WARN、「abort して **かつ通知も出なかった**」は沈黙なので CRIT。


def _abort_run(
    tmp_path: Path,
    *,
    abort: str | None = "clock_unavailable",
    ntfy: str | None = "ok=False",
    summary: bool = True,
) -> Path:
    """abort で終わった run の成果物一式を組み立てる (DONE.lock は作らない)。"""
    logs = tmp_path / "logs"
    d = logs / "open_run_20260824"
    recon: dict = {"date": "2026-08-24", "mode": "paper_submit"}
    if abort is not None:
        recon["abort"] = abort
    _write(d / "completion_recon.json", recon)
    if summary:
        _write_text(
            d / "SUMMARY.md",
            "# OPEN AUTO RUN 2026-08-24 (paper_submit)\n\n" f"- **ABORTED**: {abort}\n",
        )
    if ntfy is not None:
        _write_text(
            d / "run.log",
            "[2026-08-24 22:35:10] [gate] clock_unavailable -> ABORT\n"
            f"[2026-08-24 22:35:17] [ntfy] warn 送信 {ntfy}\n",
        )
    return logs


def test_open_run_abort_without_notification_is_crit(tmp_path: Path):
    """2026-08-24 の再現: abort したのに ntfy が落ちた = 誰も知らない -> CRIT。"""
    logs = _abort_run(tmp_path)
    r = check_open_run(logs, tmp_path / "results_csv", max_age_hours=96)
    assert r.status == "crit", r.detail
    assert r.data["notification"] == "failed"
    assert "clock_unavailable" in r.detail
    assert "2026-08-24" in r.detail


def test_open_run_abort_with_delivered_notification_stays_warn(tmp_path: Path):
    """同じ abort でも通知が出ていれば WARN のまま (昇格は沈黙にだけ効く)。"""
    logs = _abort_run(tmp_path, ntfy="ok=True")
    r = check_open_run(logs, tmp_path / "results_csv", max_age_hours=96)
    assert r.status == "warn", r.detail
    assert r.data["notification"] == "delivered"


def test_open_run_abort_without_any_ntfy_line_is_crit(tmp_path: Path):
    """送信を試みた形跡すら無い (not_paper 等) も fail-closed で CRIT。"""
    logs = _abort_run(tmp_path, abort="not_paper:live env", ntfy=None)
    r = check_open_run(logs, tmp_path / "results_csv", max_age_hours=96)
    assert r.status == "crit", r.detail
    assert r.data["notification"] == "absent"


def test_open_run_abort_recorded_only_in_summary_is_crit(tmp_path: Path):
    """recon に理由が残らないまま死んでも SUMMARY の **ABORTED** で拾う。"""
    logs = _abort_run(tmp_path, abort=None, ntfy=None)
    r = check_open_run(logs, tmp_path / "results_csv", max_age_hours=96)
    assert r.status == "crit", r.detail
    assert "unrecorded" in r.detail


def test_open_run_market_closed_stays_ok_even_without_notification(tmp_path: Path):
    """休場 abort は良性。clock が読めた夜なので通知の有無は問わない。"""
    logs = _abort_run(tmp_path, abort="market_closed", ntfy=None)
    r = check_open_run(logs, tmp_path / "results_csv", max_age_hours=96)
    assert r.status == "ok", r.detail


def test_open_run_drawdown_flatten_without_notification_is_crit(tmp_path: Path):
    logs = _abort_run(tmp_path, abort="drawdown_flatten", ntfy="ok=False")
    r = check_open_run(logs, tmp_path / "results_csv", max_age_hours=96)
    assert r.status == "crit", r.detail


def test_open_run_normal_filled_run_is_not_escalated(tmp_path: Path):
    """abort していない通常 run は ntfy 痕跡が無くても CRIT にしない。"""
    logs = tmp_path / "logs"
    d = logs / "open_run_20260824"
    _write(
        d / "completion_recon.json",
        {
            "date": "2026-08-24",
            "mode": "paper_submit",
            "entry_submitted": 47,
            "entry_status": "ok",
        },
    )
    _write_text(d / "SUMMARY.md", "# OPEN AUTO RUN 2026-08-24\n\n- entry: 47\n")
    (d / "DONE.lock").write_text("x", encoding="utf-8")
    r = check_open_run(logs, tmp_path / "results_csv", max_age_hours=96)
    assert r.status == "ok", r.detail


def test_open_run_cap_saturated_zero_entry_is_not_escalated(tmp_path: Path):
    """cap 満杯の entry_submitted=0 (2026-08-21 実データ) も CRIT にしない。"""
    orders = _skips(
        *(["already_held:buy_qty=41"] * 4),
        *(["standing_cap:system2_held=10+batch=0>=cap=10"] * 7),
    )
    logs = _zero_entry_run(tmp_path, orders)
    d = logs / "open_run_20260821"
    _write_text(d / "SUMMARY.md", "# OPEN AUTO RUN 2026-08-21\n\n- entry: 0\n")
    (d / "DONE.lock").write_text("x", encoding="utf-8")
    r = check_open_run(logs, tmp_path / "results_csv", max_age_hours=96)
    assert r.status == "ok", r.detail


def test_notification_evidence_states(tmp_path: Path):
    d = tmp_path / "run"
    d.mkdir()
    assert notification_evidence(d)[0] == "absent", "run.log 自体が無い"
    _write_text(d / "run.log", "[..] start\n[..] done\n")
    assert notification_evidence(d)[0] == "absent", "[ntfy] 行が 1 本も無い"
    _write_text(d / "run.log", "[..] [ntfy] warn 送信失敗 (無視): boom\n")
    assert notification_evidence(d)[0] == "failed"
    _write_text(
        d / "run.log", "[..] [ntfy] NTFY_TOPIC 未設定のため warn 通知スキップ\n"
    )
    assert notification_evidence(d)[0] == "failed"
    _write_text(
        d / "run.log",
        "[..] [ntfy] warn 送信 ok=False\n[..] [ntfy] warn 送信 ok=True\n",
    )
    assert notification_evidence(d)[0] == "delivered", "1 本でも成功すれば届いている"


def test_abort_reason_prefers_recon_over_summary(tmp_path: Path):
    d = tmp_path / "run"
    d.mkdir()
    assert _abort_reason(d, {}) is None
    _write_text(d / "SUMMARY.md", "- entry: 47\n")
    assert _abort_reason(d, {}) is None
    _write_text(d / "SUMMARY.md", "- **ABORTED**: None\n")
    assert _abort_reason(d, {}) == "unrecorded"
    assert _abort_reason(d, {"abort": "market_closed"}) == "market_closed"


def test_open_run_filled_is_ok(tmp_path: Path):
    logs = tmp_path / "logs"
    d = logs / "open_run_20260713"
    _write(
        d / "completion_recon.json",
        {
            "date": "2026-07-13",
            "mode": "paper_submit",
            "entry_submitted": 30,
            "entry_status": "ok",
        },
    )
    (d / "DONE.lock").write_text("x", encoding="utf-8")
    r = check_open_run(logs, tmp_path / "results_csv", max_age_hours=96)
    assert r.status == "ok"


def test_open_run_zero_entries_is_warn(tmp_path: Path):
    """paper_orders 成果物が無い entry 0 は fail-closed で WARN のまま。"""
    logs = tmp_path / "logs"
    d = logs / "open_run_20260713"
    _write(
        d / "completion_recon.json",
        {"date": "2026-07-13", "mode": "paper_submit", "entry_submitted": 0},
    )
    r = check_open_run(logs, tmp_path / "results_csv", max_age_hours=96)
    assert r.status == "warn"
    assert r.data["zero_entry_verdict"] == "anomaly"


def test_open_run_none_when_no_dirs(tmp_path: Path):
    logs = tmp_path / "logs"
    logs.mkdir()
    r = check_open_run(logs, tmp_path / "results_csv", max_age_hours=96)
    assert r.status == "info"


# --- open_run: one-shot sidecar dir を nightly と取り違えない (2026-08-20 の偽 WARN) ---
def _nightly_and_sidecar(tmp_path: Path) -> Path:
    """08-20 の実データと同じ構図: canonical nightly + `_oneshot_flatten` sidecar。"""
    logs = tmp_path / "logs"
    _write(
        logs / "open_run_20260820" / "completion_recon.json",
        {
            "date": "2026-08-20",
            "mode": "paper_submit",
            "entry_submitted": 47,
            "entry_status": "ok",
        },
    )
    (logs / "open_run_20260820" / "DONE.lock").write_text("x", encoding="utf-8")
    # sidecar は 22:30 の flatten が残す退避 dir。entry を出さないのが正常なので
    # entry_submitted=0 / skipped_thin_signals は「異常」ではない。
    _write(
        logs / "open_run_20260820_oneshot_flatten" / "completion_recon.json",
        {
            "date": "2026-08-20",
            "mode": "paper_submit",
            "entry_submitted": 0,
            "entry_status": "skipped_thin_signals",
        },
    )
    return logs


def test_latest_open_run_dir_skips_oneshot_sidecar(tmp_path: Path):
    logs = _nightly_and_sidecar(tmp_path)
    # 名前順 sorted(reverse=True) だと sidecar が先頭に来る = 旧実装が踏んだ罠。
    assert sorted(p.name for p in logs.iterdir())[-1] == (
        "open_run_20260820_oneshot_flatten"
    )
    assert _latest_open_run_dir(logs).name == "open_run_20260820"


def test_open_run_ignores_oneshot_sidecar_and_stays_ok(tmp_path: Path):
    logs = _nightly_and_sidecar(tmp_path)
    r = check_open_run(logs, tmp_path / "results_csv", max_age_hours=96)
    assert r.data["dir"] == "open_run_20260820"
    assert r.data["entry_submitted"] == 47
    assert r.status == "ok"


def test_open_run_sidecar_from_a_later_date_does_not_win(tmp_path: Path):
    """sidecar だけが最新日でも nightly の canonical 最新日を選ぶ (mtime 順の穴も塞ぐ)。"""
    logs = _nightly_and_sidecar(tmp_path)
    _write(
        logs / "open_run_20260821_oneshot_flatten" / "completion_recon.json",
        {"date": "2026-08-21", "mode": "paper_submit", "entry_submitted": 0},
    )
    assert _latest_open_run_dir(logs).name == "open_run_20260820"


def test_open_run_info_when_only_sidecars_exist(tmp_path: Path):
    logs = tmp_path / "logs"
    _write(
        logs / "open_run_20260820_oneshot_flatten" / "completion_recon.json",
        {"date": "2026-08-20", "mode": "paper_submit", "entry_submitted": 0},
    )
    assert _latest_open_run_dir(logs) is None
    r = check_open_run(logs, tmp_path / "results_csv", max_age_hours=96)
    assert r.status == "info"


def test_open_run_ignores_non_dir_and_malformed_names(tmp_path: Path):
    logs = tmp_path / "logs"
    _write(
        logs / "open_run_20260819" / "completion_recon.json",
        {"date": "2026-08-19", "mode": "paper_submit", "entry_submitted": 12},
    )
    (logs / "open_run_20260820").write_text("not a dir", encoding="utf-8")
    (logs / "open_run_2026082").mkdir()  # 7 桁 = 日付として不正
    assert _latest_open_run_dir(logs).name == "open_run_20260819"


# --- open_run: entry_submitted=0 の cry-wolf 修正 (2026-08-22) ----------------
# book が満杯の夜は生成 order が全件 standing_cap / already_held で pre-submit skip
# され送信 0 になる = 設計どおりの正常終了。旧実装は entry_submitted<=0 を無条件
# WARN にしていたので、cap 飽和のたびに「実 run だが entry_submitted=0」と誤報した。
# 本物の異常 (送信失敗 / 生成ゼロ / capacity 以外の skip / 成果物欠落) は必ず WARN。
def _zero_entry_run(
    tmp_path: Path, orders: list[dict] | None, *, recon_extra: dict | None = None
) -> Path:
    logs = tmp_path / "logs"
    d = logs / "open_run_20260821"
    _write(
        d / "completion_recon.json",
        {
            "date": "2026-08-21",
            "mode": "paper_submit",
            "entry_submitted": 0,
            "entry_skipped": len(orders or []),
            "entry_failed": 0,
            "entry_status": "no_orders_submitted",
            **(recon_extra or {}),
        },
    )
    if orders is not None:
        _write(
            d / "paper_orders.json",
            {
                "date": "2026-08-21",
                "count": len(orders),
                "submitted": 0,
                "failed": 0,
                "skipped": len(orders),
                "input_signals": len(orders),
                "status": "no_orders_submitted",
                "orders": orders,
            },
        )
    return logs


def _skips(*reasons: str) -> list[dict]:
    return [
        {"symbol": f"S{i}", "side": "buy", "system": "system2", "skip_reason": r}
        for i, r in enumerate(reasons)
    ]


def test_open_run_zero_entries_ok_when_cap_saturated(tmp_path: Path):
    """2026-08-21 の実データ再現: 11 件が already_held 4 + standing_cap 7 で全 skip。"""
    orders = _skips(
        *(["already_held:buy_qty=41"] * 4),
        *(["standing_cap:system2_held=10+batch=0>=cap=10"] * 7),
    )
    logs = _zero_entry_run(tmp_path, orders)
    r = check_open_run(logs, tmp_path / "results_csv", max_age_hours=96)
    assert r.status == "ok", r.detail
    assert r.data["zero_entry_verdict"] == "expected"
    assert r.data["skip_kinds"] == {"already_held": 4, "standing_cap": 7}


def test_open_run_zero_entries_ok_for_every_capacity_kind(tmp_path: Path):
    logs = _zero_entry_run(
        tmp_path,
        _skips(
            "standing_cap:portfolio_total_held=40",
            "already_held:sell_qty=-261",
            "already_open:duplicate_client_order_id",
            "qty_reserved:protective_order_already_open",
        ),
    )
    r = check_open_run(logs, tmp_path / "results_csv", max_age_hours=96)
    assert r.status == "ok", r.detail


def test_open_run_zero_entries_warns_on_non_capacity_skip(tmp_path: Path):
    """untradable のような capacity 以外の skip は握り潰さない。"""
    logs = _zero_entry_run(
        tmp_path,
        _skips(
            "standing_cap:system2_held=10+batch=0>=cap=10",
            "untradable:not_tradable_at_broker",
        ),
    )
    r = check_open_run(logs, tmp_path / "results_csv", max_age_hours=96)
    assert r.status == "warn"
    assert "untradable" in r.detail


def test_open_run_zero_entries_warns_on_order_without_skip_reason(tmp_path: Path):
    """送信も skip もされず消えた order = silent drop。必ず WARN。"""
    orders = _skips("standing_cap:system2_held=10+batch=0>=cap=10")
    orders.append({"symbol": "GHOST", "side": "buy", "system": "system1"})
    logs = _zero_entry_run(tmp_path, orders)
    r = check_open_run(logs, tmp_path / "results_csv", max_age_hours=96)
    assert r.status == "warn"
    assert r.data["orders_without_skip_reason"] == 1


def test_open_run_zero_entries_warns_when_submits_failed(tmp_path: Path):
    """cap で説明できても entry_failed>0 なら本物の失敗。"""
    logs = _zero_entry_run(
        tmp_path,
        _skips("standing_cap:system2_held=10+batch=0>=cap=10"),
        recon_extra={"entry_failed": 3, "entry_status": "all_submit_failed"},
    )
    r = check_open_run(logs, tmp_path / "results_csv", max_age_hours=96)
    assert r.status == "warn"


def test_open_run_zero_entries_warns_when_no_orders_generated(tmp_path: Path):
    """signals はあるのに order 生成ゼロ = schema drift。cap とは別物。"""
    logs = _zero_entry_run(
        tmp_path,
        [],
        recon_extra={"entry_status": "no_orders_generated"},
    )
    r = check_open_run(logs, tmp_path / "results_csv", max_age_hours=96)
    assert r.status == "warn"


def test_open_run_zero_entries_falls_back_to_results_csv_same_date(tmp_path: Path):
    """open_run dir に dump が無くても、同じ日付の results_csv 版なら採用する。"""
    logs = _zero_entry_run(tmp_path, None)
    results = tmp_path / "results_csv"
    _write(
        results / "paper_orders_20260821.json",
        {
            "input_signals": 2,
            "orders": _skips(
                "standing_cap:system2_held=10+batch=0>=cap=10",
                "already_held:buy_qty=5",
            ),
        },
    )
    r = check_open_run(logs, results, max_age_hours=96)
    assert r.status == "ok", r.detail


def test_open_run_zero_entries_ignores_other_date_paper_orders(tmp_path: Path):
    """別日の paper_orders で『正常』にしない (実際 Sat には翌日ぶんが残っている)。"""
    logs = _zero_entry_run(tmp_path, None)
    results = tmp_path / "results_csv"
    _write(
        results / "paper_orders_20260822.json",
        {"input_signals": 1, "orders": _skips("standing_cap:x")},
    )
    r = check_open_run(logs, results, max_age_hours=96)
    assert r.status == "warn"


def test_classify_zero_entry_is_fail_closed_on_unreadable_artifact():
    assert classify_zero_entry({}, None)[0] is True
    assert classify_zero_entry({}, {"orders": "not-a-list"})[0] is True


def test_classify_zero_entry_flat_book_is_not_an_anomaly():
    """input signals 自体が 0 なら order 0 は正常 (薄シグナルは signals check の担当)。"""
    is_anomaly, _why, _extra = classify_zero_entry(
        {"entry_status": "no_input_signals"}, {"input_signals": 0, "orders": []}
    )
    assert is_anomaly is False


# --- publish (git 無しの tmp dir では warn へフォールバック) ----------------
def test_publish_warn_when_not_a_git_repo(tmp_path: Path):
    r = check_publish(
        tmp_path, "claude/monitor-webapp", max_age_hours=26, data_dir=tmp_path
    )
    assert r.status == "warn"


# --- publish の判定基準は origin ref (2026-08-19 の毎日 CRIT 誤警報の回帰) ------
# publish_data_to_vercel.ps1 は commit-tree で origin tip にだけ commit を載せ、
# local の branch ref を進めない。local を見ていると publish が正常でも age が伸び
# 続けて毎日 CRIT になっていた。ここでは「local は 8 日前 / origin は今」という
# 実際の構図を本物の git remote で再現する。
def _git_ok(root, *args):
    import subprocess

    subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=True
    )


def _make_publish_repo(tmp_path: Path):
    """(work, branch): local branch は 8 日前、origin/<branch> は今。"""
    import subprocess

    branch = "claude/monitor-webapp"
    bare = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "--quiet", str(bare)], capture_output=True, check=True
    )
    work = tmp_path / "work"
    subprocess.run(
        ["git", "clone", "--quiet", str(bare), str(work)],
        capture_output=True,
        check=True,
    )
    _git_ok(work, "config", "user.email", "t@example.com")
    _git_ok(work, "config", "user.name", "t")
    _git_ok(work, "checkout", "--quiet", "-b", branch)

    data = work / "apps" / "dashboards" / "alpaca-next" / "data"
    data.mkdir(parents=True)
    (data / "today_signals_20260811.json").write_text("{}", encoding="utf-8")
    old = "2026-08-11T00:00:00+09:00"
    env_old = ["-c", "user.name=t", "-c", "user.email=t@example.com"]
    import os

    e = dict(os.environ, GIT_AUTHOR_DATE=old, GIT_COMMITTER_DATE=old)
    _git_ok(work, "add", "-A")
    subprocess.run(
        ["git", "-C", str(work), *env_old, "commit", "--quiet", "-m", "old"],
        capture_output=True,
        text=True,
        check=True,
        env=e,
    )
    stale = subprocess.run(
        ["git", "-C", str(work), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    # 新しい publish 相当 (今) を作って origin へ push
    (data / "today_signals_20260819.json").write_text("{}", encoding="utf-8")
    _git_ok(work, "add", "-A")
    _git_ok(work, "commit", "--quiet", "-m", "publish 08-19")
    _git_ok(work, "push", "--quiet", "origin", branch)

    # local branch だけ古い commit に戻す = publish が local を進めない状況
    _git_ok(work, "reset", "--hard", "--quiet", stale)
    return work, branch


def test_publish_judges_origin_ref_not_stale_local_branch(tmp_path: Path):
    work, branch = _make_publish_repo(tmp_path)
    r = check_publish(work, branch, max_age_hours=26, data_dir=work / "nope")
    assert r.status == "ok", r.detail
    assert r.data["basis"] == "origin"
    assert r.data["ref"] == "origin/" + branch
    # 副シグナルも origin の tree から読む
    assert r.data["dashboard_data_date"] == 20260819


def test_publish_local_basis_would_have_been_crit(tmp_path: Path):
    """同じリポを local branch で測ると CRIT。= 直前まで出ていた誤警報そのもの。"""
    import subprocess

    work, branch = _make_publish_repo(tmp_path)
    out = subprocess.run(
        ["git", "-C", str(work), "log", "-1", "--format=%cI", branch],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    from datetime import datetime

    ct = datetime.fromisoformat(out)
    age_h = (datetime.now(tz=ct.tzinfo) - ct).total_seconds() / 3600.0
    assert age_h > 26  # local を見ていれば必ず閾値超え = 毎日 CRIT


def test_publish_falls_back_to_local_without_origin(tmp_path: Path):
    """origin が無いリポでは従来どおり local branch で判定する。"""
    import subprocess

    work = tmp_path / "solo"
    work.mkdir()
    subprocess.run(
        ["git", "init", "--quiet", str(work)], capture_output=True, check=True
    )
    _git_ok(work, "config", "user.email", "t@example.com")
    _git_ok(work, "config", "user.name", "t")
    (work / "f.txt").write_text("x", encoding="utf-8")
    _git_ok(work, "add", "-A")
    _git_ok(work, "commit", "--quiet", "-m", "only")
    head = subprocess.run(
        ["git", "-C", str(work), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    r = check_publish(work, head, max_age_hours=26, data_dir=work)
    assert r.data["basis"] == "local-fallback"
    assert r.status == "ok"
