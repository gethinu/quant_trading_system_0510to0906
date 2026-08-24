"""Return / Sharpe primitives shared by the validation toolkit.

The daily-return construction here is intentionally identical to
``common/performance_summary.py`` (``resample("D").last().ffill().pct_change()``)
and the annualized Sharpe matches ``_sharpe_daily`` there, so validation numbers
are directly comparable to the metrics the rest of the system reports.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

TRADING_DAYS = 252


def equity_from_trades(trades_df: pd.DataFrame, initial_capital: float) -> pd.Series:
    """Cumulative equity indexed by ``exit_date`` (mirrors performance_summary)."""
    if trades_df is None or trades_df.empty:
        return pd.Series([float(initial_capital)])
    df = trades_df.copy()
    df["exit_date"] = pd.to_datetime(df["exit_date"])
    df = df.sort_values("exit_date")
    equity = float(initial_capital) + df["pnl"].astype(float).cumsum()
    equity.index = pd.to_datetime(df["exit_date"].values)
    return equity


def daily_returns_from_trades(
    trades_df: pd.DataFrame, initial_capital: float
) -> pd.Series:
    """Daily return series, matching performance_summary.summarize exactly."""
    equity = equity_from_trades(trades_df, initial_capital)
    if len(equity) <= 1:
        return pd.Series(dtype=float)
    equity = equity.sort_index()
    daily_equity = equity.resample("D").last().ffill()
    return daily_equity.pct_change().dropna()


def annualized_sharpe(
    returns: pd.Series | np.ndarray,
    risk_free: float = 0.0,
    periods: int = TRADING_DAYS,
) -> float:
    """Annualized Sharpe; identical formula to performance_summary._sharpe_daily."""
    r = np.asarray(returns, dtype=float)
    r = r[~np.isnan(r)]
    if r.size == 0:
        return 0.0
    r = r - risk_free / periods
    denom = r.std(ddof=0)
    if denom <= 0:
        return 0.0
    return float(np.sqrt(periods) * (r.mean() / denom))


def per_period_sharpe(returns: pd.Series | np.ndarray, risk_free: float = 0.0) -> float:
    """Non-annualized (per-observation) Sharpe used by PSR / DSR math."""
    r = np.asarray(returns, dtype=float)
    r = r[~np.isnan(r)]
    if r.size == 0:
        return 0.0
    r = r - risk_free
    denom = r.std(ddof=0)
    if denom <= 0:
        return 0.0
    return float(r.mean() / denom)
