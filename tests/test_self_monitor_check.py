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
    check_daily,
    check_data_advance,
    check_open_run,
    check_publish,
    check_signals,
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
    logs = tmp_path / "logs"
    d = logs / "open_run_20260713"
    _write(
        d / "completion_recon.json",
        {"date": "2026-07-13", "abort": "thin_signals:2<10"},
    )
    r = check_open_run(logs, tmp_path / "results_csv", max_age_hours=96)
    assert r.status == "warn"


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
    logs = tmp_path / "logs"
    d = logs / "open_run_20260713"
    _write(
        d / "completion_recon.json",
        {"date": "2026-07-13", "mode": "paper_submit", "entry_submitted": 0},
    )
    r = check_open_run(logs, tmp_path / "results_csv", max_age_hours=96)
    assert r.status == "warn"


def test_open_run_none_when_no_dirs(tmp_path: Path):
    logs = tmp_path / "logs"
    logs.mkdir()
    r = check_open_run(logs, tmp_path / "results_csv", max_age_hours=96)
    assert r.status == "info"


# --- publish (git 無しの tmp dir では warn へフォールバック) ----------------
def test_publish_warn_when_not_a_git_repo(tmp_path: Path):
    r = check_publish(
        tmp_path, "claude/monitor-webapp", max_age_hours=26, data_dir=tmp_path
    )
    assert r.status == "warn"
