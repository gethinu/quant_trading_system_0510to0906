"""End-to-end CPCV evaluation over a synthetic engine (common.validation.evaluate)."""

from __future__ import annotations

import numpy as np
import pandas as pd

from common.validation.evaluate import (
    evaluate_trades,
    make_single_system_runner,
    run_cpcv_evaluation,
)


def _synthetic_run_on_dates(drift: float, noise: float, seed: int = 0):
    """A deterministic fake engine: one trade per signal date, held 5 days."""
    rng_master = np.random.default_rng(seed)
    # pre-draw a pnl per possible date index for determinism across folds
    base_dates = pd.bdate_range("2020-01-01", periods=180)
    pnl_by_date = {d: float(rng_master.normal(drift, noise)) for d in base_dates}

    def run_on_dates(allowed) -> pd.DataFrame:
        rows = []
        for d in sorted(pd.Timestamp(x) for x in allowed):
            rows.append(
                {
                    "symbol": "AAA",
                    "entry_date": d,
                    "exit_date": d + pd.Timedelta(days=5),
                    "entry_price": 10.0,
                    "exit_price": 10.0,
                    "shares": 1,
                    "pnl": pnl_by_date.get(d, 0.0),
                    "return_%": 0.0,
                }
            )
        return pd.DataFrame(rows)

    return run_on_dates, list(base_dates)


def test_cpcv_evaluation_structure_and_counts(tmp_path):
    run_on_dates, dates = _synthetic_run_on_dates(drift=120.0, noise=200.0, seed=1)
    report = run_cpcv_evaluation(
        run_on_dates,
        dates,
        100000.0,
        n_groups=6,
        k_test=2,
        embargo_pct=0.02,
        label="synthetic",
        n_boot=300,
        universe_symbols=["AAA", "BBB"],
        survivorship_root=tmp_path,
        results_dir=str(tmp_path),
    )
    assert report.n_trials == 15
    assert report.cpcv["n_combinations"] == 15
    assert report.cpcv["n_backtest_paths"] == 5
    assert len(report.fold_sharpes) == 15
    assert "deflated_sharpe" in report.deflated_sharpe
    assert "p_value_le_zero" in report.bootstrap
    assert report.survivorship["biased"] is True  # no membership file in tmp
    # durable artifacts were written
    import os

    files = os.listdir(tmp_path)
    assert any(f.endswith(".json") for f in files)
    assert any(f.endswith("_folds.csv") for f in files)


def test_cpcv_positive_signal_has_positive_full_sharpe():
    run_on_dates, dates = _synthetic_run_on_dates(drift=200.0, noise=100.0, seed=2)
    report = run_cpcv_evaluation(
        run_on_dates, dates, 100000.0, n_groups=6, k_test=2, n_boot=200
    )
    assert report.cpcv["full_sample_sharpe"] > 0
    assert report.cpcv["frac_folds_positive"] > 0.5


def test_evaluate_trades_smoke():
    rng = np.random.default_rng(3)
    n = 150
    dates = pd.bdate_range("2021-01-01", periods=n)
    tr = pd.DataFrame(
        {
            "entry_date": dates,
            "exit_date": dates + pd.Timedelta(days=3),
            "pnl": rng.normal(40, 250, n),
        }
    )
    rep = evaluate_trades(tr, 100000.0, n_trials=20, n_boot=300, label="t")
    assert rep.n_trials == 20
    assert rep.deflated_sharpe["n_trials"] == 20
    assert rep.bootstrap["n_obs"] > 0


def test_single_system_runner_adapter():
    # minimal fake strategy compatible with simulate_trades_with_risk
    dates = pd.bdate_range("2020-01-01", periods=30)

    class FakeStrat:
        config = {"max_positions": 5, "risk_pct": 0.02, "max_pct": 0.1}

        def update_capital_with_exits(self, capital, active, date):
            still = [p for p in active if p["exit_date"] > date]
            return capital, still

        def compute_entry(self, df, candidate, capital):
            return 10.0, 9.0

        def calculate_position_size(self, capital, entry, stop, risk_pct, max_pct):
            return 10

        def compute_exit(self, df, entry_idx, entry, stop):
            return 11.0, df.index[min(entry_idx + 2, len(df) - 1)]

        def compute_pnl(self, entry, exit_, shares):
            return (exit_ - entry) * shares

    data = {"AAA": pd.DataFrame({"Open": 10.0, "ATR20": 0.5}, index=dates)}
    cbd = {d: [{"symbol": "AAA", "entry_date": d}] for d in dates[:20]}
    strat = FakeStrat()
    run_on_dates, sig_dates = make_single_system_runner(cbd, data, 100000.0, strat)
    trades = run_on_dates(set(sig_dates))
    assert not trades.empty
    assert {"entry_date", "exit_date", "pnl"}.issubset(trades.columns)
