"""Enhanced test suite for core.system5 - ADX7 Mean Reversion Strategy.

System5 implements a high ADX mean-reversion strategy with volatility filters:
- High Close filter: Close >= $5
- High ADX filter: ADX7 > 35 (strong trend)
- High Volatility filter: ATR_Pct > 2.5% (volatile stocks)
- Ranking: ADX7 descending (highest ADX first)
- Strategy: Mean reversion on high ADX/volatility stocks
"""

import os
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from common.system_constants import SYSTEM5_REQUIRED_INDICATORS
from core.system5 import (
    DEFAULT_ATR_PCT_THRESHOLD,
    format_atr_pct_threshold_label,
    generate_candidates_system5,
    get_total_days_system5,
    prepare_data_vectorized_system5,
)


class TestSystem5Utilities:
    """Test suite for System5 utility functions and constants."""

    def test_format_atr_pct_threshold_label_default(self):
        """Test format_atr_pct_threshold_label with default threshold.

        audit-remediation 2026-07-03 (D3 Case A): spec 準拠に 2.5% → 4% 是正。
        """
        result = format_atr_pct_threshold_label()

        # Should return formatted string
        assert isinstance(result, str)
        assert ">" in result
        assert "4.00%" in result  # Case A: Default 4% (spec)

    def test_format_atr_pct_threshold_label_custom(self):
        """Test format_atr_pct_threshold_label with custom threshold."""
        custom_threshold = 0.05  # 5%
        result = format_atr_pct_threshold_label(custom_threshold)

        assert isinstance(result, str)
        assert ">" in result
        assert "5.00%" in result

    def test_get_total_days_system5(self):
        """Test get_total_days_system5 function."""
        # Test default behavior
        total_days = get_total_days_system5()

        # Should return reasonable number for System5 analysis
        assert isinstance(total_days, int)
        assert total_days > 0
        assert (
            total_days >= 60
        )  # Should need at least 60 days for ADX7 and other indicators

    def test_get_total_days_system5_consistency(self):
        """Test that get_total_days_system5 returns consistent values."""
        # Call multiple times
        days1 = get_total_days_system5()
        days2 = get_total_days_system5()
        days3 = get_total_days_system5()

        # Should return same value each time
        assert days1 == days2 == days3


class TestSystem5DataPreparation:
    """Test suite for System5 data preparation functions."""

    def create_sample_symbol_data(self, symbol, days=60, base_price=50):
        """Helper to create sample symbol data."""
        dates = pd.date_range(start="2023-01-01", periods=days, freq="D")
        np.random.seed(hash(symbol) % 2**32)

        # Create realistic price series
        returns = np.random.normal(0, 0.02, days)
        prices = [base_price]
        for ret in returns[1:]:
            prices.append(prices[-1] * (1 + ret))

        return pd.DataFrame(
            {
                "Date": dates,
                "Close": prices,
                "High": np.array(prices) * 1.02,
                "Low": np.array(prices) * 0.98,
                "Volume": np.random.randint(500000, 5000000, days),
            }
        ).set_index("Date")

    def test_prepare_data_vectorized_system5_basic(self):
        """Test basic functionality of prepare_data_vectorized_system5."""
        # Create sample data for multiple symbols
        sample_data = {
            "AAPL": self.create_sample_symbol_data("AAPL", 60, 150),
            "MSFT": self.create_sample_symbol_data("MSFT", 60, 300),
            "GOOGL": self.create_sample_symbol_data("GOOGL", 60, 100),
        }

        target_date = "2023-02-28"

        result = prepare_data_vectorized_system5(sample_data, target_date)

        # Should return DataFrame
        assert isinstance(result, pd.DataFrame)

        # Should have symbol column
        assert "symbol" in result.columns

        # Should have all required indicators
        for indicator in SYSTEM5_REQUIRED_INDICATORS:
            assert indicator in result.columns, f"Missing indicator: {indicator}"

        # Should have reasonable number of rows (not all symbols may pass filters)
        assert len(result) >= 0

    def test_prepare_data_vectorized_system5_filtering(self):
        """Test that filtering works correctly."""
        # Create data with known characteristics
        sample_data = {}

        # High priced stock with high volatility (should pass filters)
        dates = pd.date_range(start="2023-01-01", periods=50, freq="D")
        high_vol_data = pd.DataFrame(
            {
                "Date": dates,
                "Close": np.random.uniform(20, 30, 50),  # High price
                "High": np.random.uniform(22, 35, 50),
                "Low": np.random.uniform(18, 25, 50),
                "Volume": [5000000] * 50,  # High volume
            }
        ).set_index("Date")
        sample_data["HIGH_VOL"] = high_vol_data

        # Low priced stock (should be filtered out)
        low_price_data = pd.DataFrame(
            {
                "Date": dates,
                "Close": [2, 3, 2.5, 3.5, 4] * 10,  # Low price
                "High": [2.1, 3.1, 2.6, 3.6, 4.1] * 10,
                "Low": [1.9, 2.9, 2.4, 3.4, 3.9] * 10,
                "Volume": [1000000] * 50,
            }
        ).set_index("Date")
        sample_data["LOW_PRICE"] = low_price_data

        target_date = "2023-02-15"

        result = prepare_data_vectorized_system5(sample_data, target_date)

        # Verify filtering worked
        if len(result) > 0:
            # All remaining symbols should have passed price filter
            assert result["symbol"].nunique() <= 2  # At most 2 symbols

    def test_prepare_data_vectorized_system5_empty_data(self):
        """Test handling of empty input data."""
        empty_data = {}
        target_date = "2023-01-15"

        result = prepare_data_vectorized_system5(empty_data, target_date)

        # Should return empty DataFrame with correct structure
        assert isinstance(result, pd.DataFrame)
        assert len(result) == 0

        # Should still have required columns
        for indicator in SYSTEM5_REQUIRED_INDICATORS:
            assert indicator in result.columns

    def test_prepare_data_vectorized_system5_date_filtering(self):
        """Test that date filtering works correctly."""
        sample_data = {"TEST": self.create_sample_symbol_data("TEST", 90, 25)}

        # Test with date in middle of data range
        target_date = "2023-02-15"
        result = prepare_data_vectorized_system5(sample_data, target_date)

        # Should get some result if data is valid
        assert isinstance(result, pd.DataFrame)

        # Test with date outside data range
        future_date = "2025-01-01"
        result_future = prepare_data_vectorized_system5(sample_data, future_date)

        # Should handle gracefully
        assert isinstance(result_future, pd.DataFrame)


class TestSystem5CandidateGeneration:
    """Test suite for System5 candidate generation."""

    def create_prepared_data(self):
        """Helper to create prepared data for candidate generation."""
        np.random.seed(42)

        symbols = ["AAPL", "MSFT", "GOOGL", "AMZN", "TSLA"]
        data = []

        for i, symbol in enumerate(symbols):
            # Create varying ADX7 values for ranking test
            adx7_value = 40 + i * 5  # 40, 45, 50, 55, 60
            atr_pct_value = 0.03 + i * 0.01  # 3%, 4%, 5%, 6%, 7%

            row = {
                "symbol": symbol,
                "adx7": adx7_value,
                "atr10": 2.0,
                "dollarvolume20": 50000000,
                "atr_pct": atr_pct_value,
                "filter": 1,  # Pass filter
                "setup": 1,  # Pass setup
            }
            data.append(row)

        return pd.DataFrame(data)

    def test_generate_candidates_system5_basic(self):
        """Test basic candidate generation functionality."""
        prepared_data = self.create_prepared_data()
        max_positions = 3

        candidates = generate_candidates_system5(prepared_data, max_positions)

        # Should return list
        assert isinstance(candidates, list)

        # Should respect max_positions limit
        assert len(candidates) <= max_positions

        # Should be sorted by ADX7 descending (highest first)
        if len(candidates) >= 2:
            adx_values = [c["adx7"] for c in candidates]
            assert adx_values == sorted(adx_values, reverse=True)

    def test_generate_candidates_system5_ranking(self):
        """Test that ranking by ADX7 works correctly."""
        prepared_data = self.create_prepared_data()
        max_positions = 5  # Get all candidates

        candidates = generate_candidates_system5(prepared_data, max_positions)

        # Should get all symbols
        assert len(candidates) == 5

        # Verify ranking by ADX7 (descending)
        adx_values = [c["adx7"] for c in candidates]
        expected_order = [60, 55, 50, 45, 40]  # Highest to lowest
        assert adx_values == expected_order

        # Verify symbols are in correct order
        symbols = [c["symbol"] for c in candidates]
        expected_symbols = ["TSLA", "AMZN", "GOOGL", "MSFT", "AAPL"]
        assert symbols == expected_symbols

    def test_generate_candidates_system5_filtering(self):
        """Test that filter and setup conditions are respected."""
        # Create data with mixed filter/setup values
        data = [
            {
                "symbol": "PASS",
                "adx7": 50,
                "atr10": 2,
                "dollarvolume20": 50000000,
                "atr_pct": 0.03,
                "filter": 1,
                "setup": 1,
            },
            {
                "symbol": "FAIL_FILTER",
                "adx7": 60,
                "atr10": 2,
                "dollarvolume20": 50000000,
                "atr_pct": 0.03,
                "filter": 0,
                "setup": 1,
            },
            {
                "symbol": "FAIL_SETUP",
                "adx7": 55,
                "atr10": 2,
                "dollarvolume20": 50000000,
                "atr_pct": 0.03,
                "filter": 1,
                "setup": 0,
            },
            {
                "symbol": "PASS2",
                "adx7": 45,
                "atr10": 2,
                "dollarvolume20": 50000000,
                "atr_pct": 0.03,
                "filter": 1,
                "setup": 1,
            },
        ]

        prepared_data = pd.DataFrame(data)
        candidates = generate_candidates_system5(prepared_data, 10)

        # Should only get symbols that pass both filter and setup
        symbols = [c["symbol"] for c in candidates]
        assert "PASS" in symbols
        assert "PASS2" in symbols
        assert "FAIL_FILTER" not in symbols
        assert "FAIL_SETUP" not in symbols

    def test_generate_candidates_system5_empty_input(self):
        """Test handling of empty input data."""
        empty_data = pd.DataFrame(columns=SYSTEM5_REQUIRED_INDICATORS + ["symbol"])

        candidates = generate_candidates_system5(empty_data, 5)

        # Should return empty list
        assert isinstance(candidates, list)
        assert len(candidates) == 0

    def test_generate_candidates_system5_max_positions_zero(self):
        """Test behavior with max_positions = 0."""
        prepared_data = self.create_prepared_data()

        candidates = generate_candidates_system5(prepared_data, 0)

        # Should return empty list
        assert isinstance(candidates, list)
        assert len(candidates) == 0

    def test_generate_candidates_system5_all_filtered_out(self):
        """Test when all candidates are filtered out."""
        # Create data where no symbols pass filters
        data = [
            {
                "symbol": "FAIL1",
                "adx7": 50,
                "atr10": 2,
                "dollarvolume20": 50000000,
                "atr_pct": 0.03,
                "filter": 0,
                "setup": 1,
            },
            {
                "symbol": "FAIL2",
                "adx7": 45,
                "atr10": 2,
                "dollarvolume20": 50000000,
                "atr_pct": 0.03,
                "filter": 1,
                "setup": 0,
            },
        ]

        prepared_data = pd.DataFrame(data)
        candidates = generate_candidates_system5(prepared_data, 5)

        # Should return empty list
        assert len(candidates) == 0


class TestSystem5UtilitiesExtended:
    """Test suite for System5 utility functions."""

    def test_get_total_days_system5(self):
        """Test get_total_days_system5 function."""
        # Test default behavior
        total_days = get_total_days_system5()

        # Should return reasonable number for System5 analysis
        assert isinstance(total_days, int)
        assert total_days > 0
        assert (
            total_days >= 60
        )  # Should need at least 60 days for ADX7 and other indicators

    def test_get_total_days_system5_consistency(self):
        """Test that get_total_days_system5 returns consistent values."""
        # Call multiple times
        days1 = get_total_days_system5()
        days2 = get_total_days_system5()
        days3 = get_total_days_system5()

        # Should return same value each time
        assert days1 == days2 == days3


class TestSystem5Integration:
    """Integration tests for System5 workflow."""

    def create_realistic_data(self):
        """Create realistic multi-symbol dataset for integration testing."""
        symbols = ["AAPL", "MSFT", "GOOGL", "AMZN", "TSLA", "NVDA"]
        data = {}

        for i, symbol in enumerate(symbols):
            # Create 120 days of realistic data
            dates = pd.date_range(start="2023-01-01", periods=120, freq="D")
            np.random.seed(hash(symbol) % 2**32)

            # Create base price with different volatility patterns
            base_price = 50 + i * 50  # Varying base prices
            volatility = 0.015 + (i % 3) * 0.01  # Different volatilities

            returns = np.random.normal(0, volatility, 120)
            prices = [base_price]
            for ret in returns[1:]:
                prices.append(max(1, prices[-1] * (1 + ret)))

            # Create volume with some correlation to price moves
            volumes = np.random.randint(1000000, 10000000, 120)

            df = pd.DataFrame(
                {
                    "Date": dates,
                    "Close": prices,
                    "High": np.array(prices) * np.random.uniform(1.001, 1.03, 120),
                    "Low": np.array(prices) * np.random.uniform(0.97, 0.999, 120),
                    "Volume": volumes,
                }
            ).set_index("Date")

            data[symbol] = df

        return data

    def test_full_system5_pipeline(self):
        """Test the complete System5 pipeline from data to candidates."""
        # Create realistic test data
        symbol_data = self.create_realistic_data()
        target_date = "2023-04-01"  # Date in middle of data range
        max_positions = 3

        # Step 1: Prepare data
        prepared_data = prepare_data_vectorized_system5(symbol_data, target_date)
        assert isinstance(prepared_data, pd.DataFrame)

        # Step 2: Generate candidates
        candidates = generate_candidates_system5(prepared_data, max_positions)
        assert isinstance(candidates, list)
        assert len(candidates) <= max_positions

        # Step 3: Verify candidate properties
        for candidate in candidates:
            assert "symbol" in candidate
            assert "adx7" in candidate
            assert isinstance(candidate["symbol"], str)
            assert isinstance(candidate["adx7"], (int, float, np.number))

    def test_system5_atr_pct_threshold(self):
        """Test that ATR_Pct threshold is correctly applied."""
        # Create data with known ATR characteristics
        symbol_data = {}

        # Create symbol with high ATR_Pct (should pass)
        dates = pd.date_range(start="2023-01-01", periods=60, freq="D")
        high_atr_prices = [50] + [
            50 * (1 + 0.05 * ((-1) ** i)) for i in range(59)
        ]  # High volatility

        symbol_data["HIGH_ATR"] = pd.DataFrame(
            {
                "Date": dates,
                "Close": high_atr_prices,
                "High": np.array(high_atr_prices) * 1.02,
                "Low": np.array(high_atr_prices) * 0.98,
                "Volume": [5000000] * 60,
            }
        ).set_index("Date")

        # Create symbol with low ATR_Pct (should be filtered)
        low_atr_prices = [50 + 0.1 * i for i in range(60)]  # Very low volatility

        symbol_data["LOW_ATR"] = pd.DataFrame(
            {
                "Date": dates,
                "Close": low_atr_prices,
                "High": np.array(low_atr_prices) * 1.001,
                "Low": np.array(low_atr_prices) * 0.999,
                "Volume": [5000000] * 60,
            }
        ).set_index("Date")

        target_date = "2023-02-15"

        prepared_data = prepare_data_vectorized_system5(symbol_data, target_date)
        candidates = generate_candidates_system5(prepared_data, 10)

        # Verify ATR_Pct filtering
        if len(candidates) > 0:
            for candidate in candidates:
                # Should have ATR_Pct above threshold
                assert (
                    candidate.get("atr_pct", 0) >= DEFAULT_ATR_PCT_THRESHOLD
                    or candidate.get("atr_pct", 0) == 0
                )

    def test_system5_error_handling(self):
        """Test error handling in System5 functions."""
        # Test with malformed data
        bad_data = {
            "BAD_SYMBOL": pd.DataFrame(
                {
                    "Close": [None, None, None],
                    "High": [1, 2, 3],
                    "Low": [1, 2, 3],
                    "Volume": [1000, 2000, 3000],
                }
            )
        }

        # Should handle gracefully without crashing
        try:
            result = prepare_data_vectorized_system5(bad_data, "2023-01-01")
            assert isinstance(result, pd.DataFrame)
        except Exception as e:
            # If it raises an exception, it should be a reasonable one
            assert isinstance(e, (ValueError, KeyError, TypeError))


class TestSystem5Constants:
    """Test System5 constants and configuration."""

    def test_system5_required_indicators(self):
        """Test that SYSTEM5_REQUIRED_INDICATORS is properly defined.

        audit-remediation 2026-07-03 (D3 Case A): 流動性 filter (avgvolume50 /
        dollarvolume50) を追加。"filter"/"setup" は runtime 生成列で precompute
        必須ではないため expected から除外。
        """
        # Should be a list or tuple
        assert isinstance(SYSTEM5_REQUIRED_INDICATORS, (list, tuple))

        # Should contain expected indicators for System5 (Case A: 流動性 filter 追加後)
        expected_indicators = {
            "adx7",
            "atr10",
            "dollarvolume20",
            "atr_pct",
            "sma100",
            "rsi3",
            "avgvolume50",  # Case A: spec の AvgVol50 filter
            "dollarvolume50",  # Case A: spec の DV50 filter
        }
        actual_indicators = set(SYSTEM5_REQUIRED_INDICATORS)

        # All expected indicators should be present
        assert expected_indicators.issubset(actual_indicators)

    def test_default_atr_pct_threshold(self):
        """Test DEFAULT_ATR_PCT_THRESHOLD constant.

        audit-remediation 2026-07-03 (D3 Case A): spec 準拠に 0.025 → 0.04 是正。
        """
        # Should be a reasonable percentage (4% = 0.04, spec 準拠)
        assert isinstance(DEFAULT_ATR_PCT_THRESHOLD, (int, float))
        assert 0 < DEFAULT_ATR_PCT_THRESHOLD < 1  # Should be a percentage
        assert DEFAULT_ATR_PCT_THRESHOLD == 0.04  # Case A: spec 4% (旧 2.5%)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
