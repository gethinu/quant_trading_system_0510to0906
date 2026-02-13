from typing import cast

import pandas as pd
import pytest

from strategies.constants import MAX_HOLD_DAYS_DEFAULT
from strategies.system6_strategy import System6Strategy


@pytest.fixture
def dummy_data():
    dates = pd.date_range("2024-01-01", periods=50, freq="B")
    df = pd.DataFrame(
        {
            "Open": [100] * 50,
            "High": [101] * 50,
            "Low": [99] * 50,
            "Close": [100] * 50,
            "Volume": [1_000_000] * 50,
        },
        index=dates,
    )
    return {"DUMMY": df}


def test_minimal_indicators(dummy_data):
    strategy = System6Strategy()
    processed = strategy.prepare_minimal_for_test(dummy_data)
    assert "atr10" in processed["DUMMY"].columns


def test_placeholder_run(dummy_data):
    strategy = System6Strategy()
    dates = pd.date_range("2024-01-01", periods=4, freq="D")
    df = pd.DataFrame(
        {
            "Open": [100, 100, 100, 100],
            # Short limit at 1.05 * prev_close (=105.0) should fill only if High touches it
            "High": [100, 105, 100, 100],
            "Low": [100, 100, 100, 100],
            "Close": [100, 105, 90, 95],
            "Volume": [1_000_000] * 4,
            "ATR10": [1, 1, 1, 1],
        },
        index=dates,
    )
    prepared = {"DUMMY": df}
    entry_date = dates[1]
    candidates = {entry_date: [{"symbol": "DUMMY", "entry_date": entry_date}]}
    trades = strategy.run_backtest(prepared, candidates, capital=10_000)
    assert not trades.empty
    assert "pnl" in trades.columns


def test_entry_rule_limit_short():
    strategy = System6Strategy()
    dates = pd.date_range("2024-01-01", periods=2, freq="B")
    df = pd.DataFrame(
        {
            "Open": [100, 100],
            # Ensure entry-day High touches the limit (105.0)
            "High": [101, 105],
            "Low": [99, 99],
            "Close": [100, 100],
            "ATR10": [1, 1],
        },
        index=dates,
    )
    candidate = {"symbol": "DUMMY", "entry_date": dates[1]}
    entry = strategy.compute_entry(df, candidate, 10_000)
    assert entry is not None
    assert entry == (105.0, 108.0)


def test_system6_profit_target_exits_next_close():
    strategy = System6Strategy()
    dates = pd.date_range("2024-01-01", periods=5, freq="B")
    df = pd.DataFrame(
        {
            "Open": [100] * 5,
            # Ensure entry-day High touches the limit (105.0)
            "High": [100, 105, 107, 100, 100],
            "Low": [99, 99, 97, 95, 95],
            "Close": [100, 100, 99, 95, 95],
            "ATR10": [1] * 5,
        },
        index=dates,
    )
    candidate = {"symbol": "DUMMY", "entry_date": dates[1]}
    entry = strategy.compute_entry(df, candidate, 10_000)
    assert entry is not None
    entry_price, stop_price = entry
    entry_idx = cast(int, df.index.get_loc(dates[1]))

    exit_price, exit_date = strategy.compute_exit(
        df, entry_idx, entry_price, stop_price
    )

    assert exit_date == dates[3]
    assert exit_price == pytest.approx(float(df.iloc[3]["Close"]))


def test_system6_stop_exit_same_day_at_stop_price():
    strategy = System6Strategy()
    dates = pd.date_range("2024-01-01", periods=4, freq="B")
    df = pd.DataFrame(
        {
            "Open": [100] * 4,
            # Ensure entry-day High touches the limit (105.0)
            "High": [100, 105, 110, 100],
            "Low": [99] * 4,
            "Close": [100, 100, 104, 100],
            "ATR10": [1] * 4,
        },
        index=dates,
    )
    candidate = {"symbol": "DUMMY", "entry_date": dates[1]}
    entry = strategy.compute_entry(df, candidate, 10_000)
    assert entry is not None
    entry_price, stop_price = entry
    entry_idx = cast(int, df.index.get_loc(dates[1]))

    exit_price, exit_date = strategy.compute_exit(
        df, entry_idx, entry_price, stop_price
    )

    assert exit_date == dates[2]
    assert exit_price == pytest.approx(stop_price)


def test_system6_time_exit_after_max_days_close():
    strategy = System6Strategy()
    max_days = strategy.config.get("profit_take_max_days", MAX_HOLD_DAYS_DEFAULT)
    periods = max_days + 3
    dates = pd.date_range("2024-01-01", periods=periods, freq="B")
    df = pd.DataFrame(
        {
            "Open": [100] * periods,
            # Ensure entry-day High touches the limit (105.0)
            "High": [100, 105] + [107] * (periods - 2),
            "Low": [99] * periods,
            "Close": [100, 100] + [103] * (periods - 2),
            "ATR10": [1] * periods,
        },
        index=dates,
    )
    candidate = {"symbol": "DUMMY", "entry_date": dates[1]}
    entry = strategy.compute_entry(df, candidate, 10_000)
    assert entry is not None
    entry_price, stop_price = entry
    entry_idx = cast(int, df.index.get_loc(dates[1]))

    exit_price, exit_date = strategy.compute_exit(
        df, entry_idx, entry_price, stop_price
    )

    expected_idx = entry_idx + max_days
    assert exit_date == dates[expected_idx]
    assert exit_price == pytest.approx(float(df.iloc[expected_idx]["Close"]))


def test_compute_indicators_from_frame_aligns_range_index():
    from core.system6 import _compute_indicators_from_frame

    periods = 120
    base_dates = pd.date_range("2024-01-01", periods=periods, freq="B")
    df = pd.DataFrame(
        {
            "date": base_dates.strftime("%Y-%m-%d"),
            "open": 20.0,
            "high": 21.0,
            "low": 19.5,
            "close": 20.5,
            "volume": 2_000_000,
            "atr10": 1.2,
            "dollarvolume50": 50_000_000,
            "return_6d": 0.25,
            "uptwodays": True,
            "hv50": 25.0,
        }
    )

    prepared = _compute_indicators_from_frame(df)

    assert len(prepared) == periods
    assert prepared.index.is_monotonic_increasing
    assert {"atr10", "dollarvolume50", "return_6d", "UpTwoDays", "hv50"}.issubset(
        prepared.columns
    )
    # 既存の指標がそのまま活用され、filter/setupも生成される
    assert prepared["filter"].all()
    assert prepared["setup"].all()
