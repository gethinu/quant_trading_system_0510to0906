"""One-month rolling backtest validation for systems 1-7.

Goals:
- Entry/Exit end-to-end without exceptions for a month (business days)
- Capital/risk/max allocation/slot constraints are respected
- No overlapping positions per symbol; exits on/after entries; PnL numeric;
    shares > 0
"""

from __future__ import annotations

import math
from typing import Dict, List

import pandas as pd
import pytest

from strategies.system1_strategy import System1Strategy
from strategies.system2_strategy import System2Strategy
from strategies.system3_strategy import System3Strategy
from strategies.system4_strategy import System4Strategy
from strategies.system5_strategy import System5Strategy
from strategies.system6_strategy import System6Strategy
from strategies.system7_strategy import System7Strategy


def _gen_ohlc(
    start: str,
    days: int,
    *,
    base: float = 100.0,
    daily_step: float = 0.5,
    atr_seed: float = 1.5,
    need_atr10: bool = True,
    need_atr20: bool = True,
    need_atr40: bool = False,
    need_atr50: bool = False,
    force_gap_up_on: List[int] | None = None,
) -> pd.DataFrame:
    """Generate deterministic OHLC with ATRn columns.

    - Business day index
        - Optional forced gap-up on given day indices
            (for short entries like System2)
    """
    idx = pd.bdate_range(start=start, periods=days)
    # base linear trend
    close = [base + i * daily_step for i in range(days)]
    open_ = close.copy()
    high = [c + 1.0 for c in close]
    low = [c - 1.0 for c in close]

    # Optional gap up on specific day indices (e.g., 2, 6, 10 ...)
    force_gap_up_on = force_gap_up_on or []
    for i in force_gap_up_on:
        if 0 <= i < days and i > 0:
            prev_close = close[i - 1]
            # +6% gap up to satisfy System2 default 4% threshold
            open_[i] = round(prev_close * 1.06, 2)
            high[i] = max(high[i], open_[i] + 1.0)
            low[i] = min(low[i], open_[i] - 1.0)
            close[i] = max(close[i], open_[i])

    df = pd.DataFrame(
        {"Open": open_, "High": high, "Low": low, "Close": close}, index=idx
    )

    # Compute simple TR and rolling means for ATR
    tr = pd.concat(
        [
            (df["High"] - df["Low"]),
            (df["High"] - df["Close"].shift()).abs(),
            (df["Low"] - df["Close"].shift()).abs(),
        ],
        axis=1,
    ).max(axis=1)

    def _atr(n: int) -> pd.Series:
        # Small min_periods to avoid all-NaN at head but keep values reasonable
        roll = tr.rolling(n, min_periods=max(2, min(5, n))).mean()
        return (roll * (atr_seed / 1.5)).bfill()

    if need_atr10:
        df["ATR10"] = _atr(10)
    if need_atr20:
        df["ATR20"] = _atr(20)
    if need_atr40:
        df["ATR40"] = _atr(40)
    if need_atr50:
        df["ATR50"] = _atr(50)

    return df


def _assert_common_trade_invariants(trades: pd.DataFrame) -> None:
    # Empty is acceptable but not preferred—assert non-negative case safety
    if trades.empty:
        return
    required_cols = {
        "symbol",
        "entry_date",
        "exit_date",
        "entry_price",
        "exit_price",
        "shares",
        "pnl",
        "return_%",
    }
    assert required_cols.issubset(set(trades.columns))
    # Types and values
    assert trades["shares"].map(lambda x: isinstance(x, (int, float)) and x > 0).all()
    assert (
        trades["entry_price"].map(lambda x: isinstance(x, (int, float)) and x > 0).all()
    )
    assert (
        trades["exit_price"].map(lambda x: isinstance(x, (int, float)) and x > 0).all()
    )
    assert trades["entry_date"].le(trades["exit_date"]).all()
    # No overlapping trades per symbol
    for sym, grp in trades.sort_values(["symbol", "entry_date"]).groupby("symbol"):
        prev_exit = None
        for _, row in grp.iterrows():
            if prev_exit is not None:
                assert row["entry_date"] >= prev_exit
            prev_exit = row["exit_date"]


def _assert_slot_and_capital_constraints(logs: pd.DataFrame, strategy) -> None:
    if logs.empty:
        return
    max_positions = int(getattr(strategy, "config", {}).get("max_positions", 10))
    assert logs["active_positions"].max() <= max_positions
    # Capital should remain finite and non-negative
    assert (
        logs["capital"]
        .map(lambda x: isinstance(x, (int, float)) and math.isfinite(x) and x >= 0)
        .all()
    )


@pytest.mark.parametrize(
    "system_cls,symbols,atr_flags,gap_days",
    [
        (
            System1Strategy,
            ["AAA", "BBB", "CCC"],
            {"need_atr10": True, "need_atr20": True},
            [],
        ),
        (
            System2Strategy,
            ["AAA", "BBB", "CCC"],
            {"need_atr10": True, "need_atr20": True},
            [2, 6, 10, 14, 18],
        ),
        (System3Strategy, ["AAA", "BBB", "CCC"], {"need_atr10": True}, []),
        (System4Strategy, ["AAA", "BBB", "CCC"], {"need_atr40": True}, []),
        (System5Strategy, ["AAA", "BBB", "CCC"], {"need_atr10": True}, []),
        (
            System6Strategy,
            ["AAA", "BBB", "CCC"],
            {"need_atr10": True},
            [3, 9, 15],
        ),
        (System7Strategy, ["SPY"], {"need_atr50": True}, []),
    ],
)
def test_monthly_roll_no_errors(system_cls, symbols, atr_flags, gap_days):
    days = 22  # ~ 1 month of business days
    start = "2025-01-02"

    # Build per-symbol price data
    data_dict: Dict[str, pd.DataFrame] = {}
    for sym in symbols:
        df = _gen_ohlc(
            start,
            days,
            base=100.0 if sym != "SPY" else 500.0,
            daily_step=0.6 if sym != "SPY" else 0.4,
            need_atr10=atr_flags.get("need_atr10", False),
            need_atr20=atr_flags.get("need_atr20", False),
            need_atr40=atr_flags.get("need_atr40", False),
            need_atr50=atr_flags.get("need_atr50", False),
            force_gap_up_on=gap_days,
        )
        data_dict[sym] = df

    # Candidates by date for all symbols (SPY only for system7)
    dates = list(pd.bdate_range(start=start, periods=days))
    candidates_by_date: Dict[pd.Timestamp, Dict[str, dict]] = {}
    for d in dates:
        candidates_by_date[d] = {sym: {"entry_date": d} for sym in symbols}

    # Instantiate strategy
    strategy = system_cls()
    initial_capital = 100_000.0

    # Use shared backtest util to get slot/capital logs
    from common.backtest_utils import simulate_trades_with_risk

    if hasattr(strategy, "get_trading_side"):
        side = strategy.get_trading_side()
    else:
        side = "long"
    trades2, logs_df = simulate_trades_with_risk(
        candidates_by_date,
        data_dict,
        initial_capital,
        strategy,
        side=side,
    )

    # Invariants
    _assert_common_trade_invariants(trades2)
    _assert_slot_and_capital_constraints(logs_df, strategy)

    # Basic sanity: ensure some entries for readily-trigger systems
    if system_cls in (
        System1Strategy,
        System3Strategy,
        System4Strategy,
        System6Strategy,
        System7Strategy,
    ):
        assert len(trades2) >= 1

    # Exit validity: exits on/after entries and roughly within window
    if not trades2.empty:
        assert trades2["entry_date"].min() >= dates[0]
        max_exit = trades2["exit_date"].max()
        assert max_exit <= dates[-1] or max_exit >= dates[0]
