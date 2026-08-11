"""Orchestrator: run CPCV + bootstrap + DSR over an existing backtest engine.

This module is *engine-agnostic*. The caller supplies ``run_on_dates`` -
``Callable[[set[pd.Timestamp]], pd.DataFrame]`` - that runs a backtest restricted
to a set of signal dates and returns a trades DataFrame (with ``entry_date``,
``exit_date``, ``pnl``). Adapters for both existing engines are provided:
:func:`make_single_system_runner` and :func:`make_integrated_runner`.

Nothing here runs unless explicitly called (all feature-flag gates live at the
call sites), so importing this module changes no behavior.
"""

from __future__ import annotations

import datetime as _dt
from typing import Callable

import numpy as np
import pandas as pd

from common.validation.bootstrap import moving_block_bootstrap
from common.validation.cpcv import (
    cpcv_date_folds,
    filter_candidates_by_dates,
    label_end_map_from_trades,
    n_backtest_paths,
    n_combinations,
)
from common.validation.deflated_sharpe import deannualize_sharpe, deflated_sharpe_ratio
from common.validation.metrics import annualized_sharpe, daily_returns_from_trades
from common.validation.report import ValidationReport
from common.validation.survivorship import audit_survivorship

RunOnDates = Callable[[set], pd.DataFrame]


# --------------------------------------------------------------------------- #
# Engine adapters
# --------------------------------------------------------------------------- #
def make_single_system_runner(
    candidates_by_date: dict,
    data_dict: dict,
    capital: float,
    strategy,
    side: str | None = None,
) -> tuple[RunOnDates, list]:
    """Adapter for ``common.backtest_utils.simulate_trades_with_risk``."""
    from common.backtest_utils import simulate_trades_with_risk

    signal_dates = [pd.Timestamp(d) for d in candidates_by_date.keys()]

    def run_on_dates(allowed) -> pd.DataFrame:
        cbd = filter_candidates_by_dates(candidates_by_date, allowed)
        if not cbd:
            return pd.DataFrame()
        trades, _ = simulate_trades_with_risk(
            cbd, data_dict, capital, strategy, side=side
        )
        return trades

    return run_on_dates, signal_dates


def make_integrated_runner(
    system_states: list,
    capital: float,
    **backtest_kwargs,
) -> tuple[RunOnDates, list]:
    """Adapter for ``common.integrated_backtest.run_integrated_backtest``.

    Filters every system's ``candidates_by_date`` to the allowed dates before
    running, without mutating the shared states.
    """
    from dataclasses import replace

    from common.integrated_backtest import run_integrated_backtest

    all_dates: set = set()
    for st in system_states:
        all_dates.update(pd.Timestamp(d) for d in st.candidates_by_date.keys())
    signal_dates = sorted(all_dates)

    def run_on_dates(allowed) -> pd.DataFrame:
        allowed_set = {pd.Timestamp(d) for d in allowed}
        folded = []
        for st in system_states:
            cbd = {
                k: v
                for k, v in st.candidates_by_date.items()
                if pd.Timestamp(k) in allowed_set
            }
            folded.append(replace(st, candidates_by_date=cbd))
        trades, _counts = run_integrated_backtest(folded, capital, **backtest_kwargs)
        return trades

    return run_on_dates, signal_dates


# --------------------------------------------------------------------------- #
# Evaluators
# --------------------------------------------------------------------------- #
def evaluate_trades(
    trades_df: pd.DataFrame,
    initial_capital: float,
    *,
    n_trials: int = 1,
    label: str = "strategy",
    n_boot: int = 2000,
    seed: int = 12345,
    trial_sharpes=None,
) -> ValidationReport:
    """Distribution-based evaluation of a *single* trades DataFrame (no CPCV).

    Replaces the single point Sharpe estimate with a bootstrap distribution and
    a Deflated Sharpe Ratio.
    """
    daily = daily_returns_from_trades(trades_df, initial_capital)
    boot = moving_block_bootstrap(daily, n_boot=n_boot, seed=seed)
    dsr = deflated_sharpe_ratio(daily, n_trials, trial_sharpes=trial_sharpes)
    return ValidationReport(
        label=label,
        created_at=_dt.datetime.now().isoformat(timespec="seconds"),
        n_trials=int(n_trials),
        bootstrap=boot.to_dict(),
        deflated_sharpe=dsr.to_dict(),
    )


def run_cpcv_evaluation(
    run_on_dates: RunOnDates,
    signal_dates,
    initial_capital: float,
    *,
    n_groups: int = 6,
    k_test: int = 2,
    embargo_pct: float = 0.01,
    label: str = "strategy",
    use_trade_labels: bool = True,
    n_boot: int = 2000,
    seed: int = 12345,
    universe_symbols=None,
    survivorship_root=None,
    results_dir=None,
    logs_dir=None,
) -> ValidationReport:
    """Full CPCV evaluation: per-fold OOS Sharpe distribution + bootstrap + DSR.

    The number of CPCV combinations is the DSR trial multiplicity ``N``, and the
    cross-fold Sharpe variance estimates the deflation benchmark - so a strategy
    whose OOS Sharpe swings across folds is penalized as an overfit strategy
    should be.
    """
    all_dates = sorted({pd.Timestamp(d) for d in signal_dates})
    if len(all_dates) < n_groups:
        raise ValueError(
            f"need >= {n_groups} distinct signal dates, got {len(all_dates)}"
        )

    # 1) Full-sample run (moments + bootstrap use the full series).
    full_trades = run_on_dates(set(all_dates))
    full_daily = daily_returns_from_trades(full_trades, initial_capital)
    full_sharpe = annualized_sharpe(full_daily)

    # 2) CPCV folds (purge on trade label spans, embargo on the timeline).
    label_end = label_end_map_from_trades(full_trades) if use_trade_labels else None
    folds = cpcv_date_folds(
        all_dates,
        n_groups=n_groups,
        k_test=k_test,
        embargo_pct=embargo_pct,
        label_end_by_date=label_end,
    )

    fold_rows: list[dict] = []
    fold_sharpes: list[float] = []
    for fold in folds:
        tr = run_on_dates(fold.test_dates)
        dr = daily_returns_from_trades(tr, initial_capital)
        sh = annualized_sharpe(dr)
        fold_sharpes.append(sh)
        n_tr = 0 if tr is None or tr.empty else int(len(tr))
        pnl = 0.0 if tr is None or tr.empty else float(tr["pnl"].sum())
        fold_rows.append(
            {
                "combo": "-".join(map(str, fold.combo)),
                "n_test_dates": fold.n_test,
                "n_train_dates": fold.n_train,
                "n_trades": n_tr,
                "sharpe": round(sh, 6),
                "total_pnl": round(pnl, 2),
            }
        )

    n_trials = n_combinations(n_groups, k_test)
    n_paths = n_backtest_paths(n_groups, k_test)

    # 3) Cross-fold Sharpe variance -> deflation benchmark (per-period units).
    per_period_folds = [deannualize_sharpe(s) for s in fold_sharpes]
    sr_var = (
        float(np.var(per_period_folds, ddof=1)) if len(per_period_folds) > 1 else None
    )

    dsr = deflated_sharpe_ratio(full_daily, n_trials, sr_variance=sr_var)
    boot = moving_block_bootstrap(full_daily, n_boot=n_boot, seed=seed)

    fs = np.asarray(fold_sharpes, dtype=float)
    cpcv_summary = {
        "n_groups": n_groups,
        "k_test": k_test,
        "embargo_pct": embargo_pct,
        "n_combinations": n_trials,
        "n_backtest_paths": n_paths,
        "full_sample_sharpe": round(full_sharpe, 6),
        "fold_sharpe_mean": round(float(fs.mean()), 6) if fs.size else 0.0,
        "fold_sharpe_std": round(float(fs.std(ddof=1)), 6) if fs.size > 1 else 0.0,
        "fold_sharpe_min": round(float(fs.min()), 6) if fs.size else 0.0,
        "fold_sharpe_max": round(float(fs.max()), 6) if fs.size else 0.0,
        "frac_folds_positive": round(float(np.mean(fs > 0)), 6) if fs.size else 0.0,
    }

    surv = {}
    if universe_symbols is not None:
        surv = audit_survivorship(universe_symbols, root=survivorship_root).to_dict()

    report = ValidationReport(
        label=label,
        created_at=_dt.datetime.now().isoformat(timespec="seconds"),
        n_trials=int(n_trials),
        cpcv=cpcv_summary,
        bootstrap=boot.to_dict(),
        deflated_sharpe=dsr.to_dict(),
        survivorship=surv,
        fold_sharpes=fold_rows,
    )
    if results_dir:
        report.save(results_dir, logs_dir)
    return report
