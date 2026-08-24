"""Regression / OFF-byte-parity guardrails for the validation feature.

Covers the three mandated data-pipeline regression invariants plus the
OFF-default byte-parity proof:

1. file-unit monotonic (non-decreasing) invariant,
2. rolling -> filter -> setup funnel chain (non-increasing) invariant,
3. silent-WARN watchdog (a path that must WARN must not silently succeed),
4. OFF-default parity: with no validation env vars set nothing is enabled and
   the production metric path is unchanged.
"""

from __future__ import annotations

import importlib
import logging
import os
from unittest.mock import patch

import numpy as np
import pandas as pd

from common.invariants.phase1_gates import (
    GateConfig,
    check_file_monotonic,
    check_funnel_monotonic,
)


# --------------------------------------------------------------------------- #
# (1) file-unit monotonic non-decreasing invariant
# --------------------------------------------------------------------------- #
def test_file_monotonic_non_decreasing_sequence():
    cfg = GateConfig()
    counts = [0, 3, 3, 7, 10, 10, 14]
    prev = None
    for c in counts:
        res = check_file_monotonic(prev, c, cfg)
        assert not res.violated, f"regression at {prev}->{c}"
        prev = c


def test_file_monotonic_flags_regression():
    cfg = GateConfig()
    res = check_file_monotonic(10, 8, cfg)
    assert res.violated


# --------------------------------------------------------------------------- #
# (2) rolling -> filter -> setup chain IT
# --------------------------------------------------------------------------- #
def test_funnel_chain_non_increasing_ok():
    cfg = GateConfig()
    # rolling >= filter >= setup must hold at every stage
    assert not check_funnel_monotonic(1000, 400, 120, cfg).violated
    assert not check_funnel_monotonic(50, 50, 50, cfg).violated


def test_funnel_chain_violation_detected():
    cfg = GateConfig()
    assert check_funnel_monotonic(100, 120, 10, cfg).violated  # filter > rolling
    assert check_funnel_monotonic(100, 40, 60, cfg).violated  # setup > filter


# --------------------------------------------------------------------------- #
# (3) silent-WARN watchdog: a WARN path must actually emit, never silently pass
# --------------------------------------------------------------------------- #
def test_silent_warn_watchdog_survivorship(tmp_path, caplog):
    from common.validation.survivorship import survivorship_guard

    with caplog.at_level(logging.WARNING):
        audit = survivorship_guard(["AAA", "BBB"], mode="warn", root=tmp_path)
    # The biased condition is real...
    assert audit.biased is True
    # ...therefore a WARNING MUST have been emitted (no silent success).
    warns = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warns) == 1, "biased universe produced no WARN (silent success!)"


def test_cpcv_evaluation_is_not_silently_empty(tmp_path):
    """A successful run must produce non-empty output (guards exit0+empty)."""
    from common.validation.evaluate import run_cpcv_evaluation

    dates = pd.bdate_range("2020-01-01", periods=120)
    rng = np.random.default_rng(0)
    pnl = {d: float(rng.normal(80, 200)) for d in dates}

    def run_on_dates(allowed):
        rows = [
            {
                "symbol": "AAA",
                "entry_date": d,
                "exit_date": d + pd.Timedelta(days=4),
                "pnl": pnl[pd.Timestamp(d)],
            }
            for d in sorted(pd.Timestamp(x) for x in allowed)
        ]
        return pd.DataFrame(rows)

    report = run_cpcv_evaluation(run_on_dates, list(dates), 100000.0, n_boot=200)
    assert report.fold_sharpes, "empty fold output"
    assert report.deflated_sharpe and report.bootstrap
    assert report.verdict() != "no-metrics"


# --------------------------------------------------------------------------- #
# (4) OFF-default byte-parity
# --------------------------------------------------------------------------- #
def test_all_flags_off_by_default():
    keys = [
        "VALIDATION_ENABLED",
        "VALIDATION_CPCV",
        "VALIDATION_BOOTSTRAP",
        "VALIDATION_DSR",
        "SURVIVORSHIP_GUARD",
    ]
    with patch.dict(os.environ, {k: "" for k in keys}, clear=False):
        for k in keys:
            os.environ.pop(k, None)
        import common.validation.flags as flags

        importlib.reload(flags)
        assert flags.validation_enabled() is False
        assert flags.cpcv_enabled() is False
        assert flags.bootstrap_enabled() is False
        assert flags.dsr_enabled() is False
        assert flags.survivorship_guard_mode() == "off"


def test_metrics_match_production_summarize():
    """validation daily-return Sharpe must equal performance_summary exactly."""
    from common.performance_summary import summarize
    from common.validation.metrics import annualized_sharpe, daily_returns_from_trades

    rng = np.random.default_rng(1)
    n = 140
    dates = pd.bdate_range("2021-01-04", periods=n)
    tr = pd.DataFrame(
        {
            "entry_date": dates - pd.Timedelta(days=3),
            "exit_date": dates,
            "pnl": rng.normal(50, 300, n).round(2),
        }
    )
    summ, _ = summarize(tr, 100000)
    dr = daily_returns_from_trades(tr, 100000)
    assert abs(summ.sharpe - annualized_sharpe(dr)) < 1e-12


def test_evaluate_trades_does_not_mutate_input():
    from common.validation.evaluate import evaluate_trades

    dates = pd.bdate_range("2021-01-01", periods=60)
    tr = pd.DataFrame(
        {
            "entry_date": dates,
            "exit_date": dates + pd.Timedelta(days=2),
            "pnl": np.linspace(-10, 10, 60),
        }
    )
    before = tr.copy(deep=True)
    evaluate_trades(tr, 100000.0, n_trials=5, n_boot=100)
    pd.testing.assert_frame_equal(tr, before)
