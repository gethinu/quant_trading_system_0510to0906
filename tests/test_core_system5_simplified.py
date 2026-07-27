"""Simplified test suite for core.system5 - ADX7 Mean Reversion Strategy.

System5 implements high ADX mean-reversion with filters:
- Close >= $5, ADX7 > 35, ATR_Pct > 2.5%
- Ranking: ADX7 descending (highest first)
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
    """Test System5 utility functions and constants."""

    def test_format_atr_pct_threshold_label_default(self):
        """Test format_atr_pct_threshold_label with default threshold."""
        result = format_atr_pct_threshold_label()

        assert isinstance(result, str)
        assert ">" in result
        assert "2.50%" in result

    def test_format_atr_pct_threshold_label_custom(self):
        """Test format_atr_pct_threshold_label with custom threshold."""
        result = format_atr_pct_threshold_label(0.05)  # 5%

        assert isinstance(result, str)
        assert ">" in result
        assert "5.00%" in result

    def test_get_total_days_system5(self):
        """Test get_total_days_system5 function."""
        sample_data = {
            "TEST": pd.DataFrame(
                {"Close": [10, 11, 12], "Date": pd.date_range("2023-01-01", periods=3)}
            )
        }

        total_days = get_total_days_system5(sample_data)

        assert isinstance(total_days, int)
        assert total_days > 0

    def test_get_total_days_system5_empty(self):
        """Test get_total_days_system5 with empty data."""
        total_days = get_total_days_system5({})
        assert isinstance(total_days, int)
        assert total_days > 0


class TestSystem5DataPreparation:
    """Test System5 data preparation."""

    def create_test_data_with_indicators(self, symbol="TEST", days=60):
        """Helper to create test data with all required indicators."""
        dates = pd.date_range(start="2023-01-01", periods=days, freq="D")
        np.random.seed(42)

        df = pd.DataFrame(
            {
                "Date": dates,
                "Close": np.random.uniform(10, 50, days),
                "High": np.random.uniform(11, 55, days),
                "Low": np.random.uniform(9, 45, days),
                "Volume": np.random.randint(1000000, 5000000, days),
            }
        ).set_index("Date")

        # Add required indicators
        df["adx7"] = np.random.uniform(20, 60, days)
        df["atr10"] = np.random.uniform(0.5, 3.0, days)
        df["atr_pct"] = np.random.uniform(0.01, 0.08, days)
        df["dollarvolume20"] = df["Close"] * df["Volume"]

        return df

    def test_prepare_data_vectorized_system5_basic(self):
        """Test basic prepare_data_vectorized_system5 functionality."""
        sample_data = {
            "AAPL": self.create_test_data_with_indicators("AAPL"),
            "MSFT": self.create_test_data_with_indicators("MSFT"),
        }

        result = prepare_data_vectorized_system5(sample_data)

        assert isinstance(result, dict)

    def test_prepare_data_vectorized_system5_with_reuse_indicators_false(self):
        """Test prepare_data_vectorized_system5 with reuse_indicators=False."""
        sample_data = {"TEST": self.create_test_data_with_indicators("TEST")}

        # Test with reuse_indicators=False
        result = prepare_data_vectorized_system5(sample_data, reuse_indicators=False)

        assert isinstance(result, dict)

    def test_prepare_data_vectorized_system5_empty_data(self):
        """Test handling of empty input data."""
        result = prepare_data_vectorized_system5({})

        assert isinstance(result, dict)
        assert len(result) == 0

    def test_prepare_data_vectorized_system5_with_symbols_filter(self):
        """Test with symbols parameter to filter specific symbols."""
        sample_data = {
            "AAPL": self.create_test_data_with_indicators("AAPL"),
            "MSFT": self.create_test_data_with_indicators("MSFT"),
            "GOOGL": self.create_test_data_with_indicators("GOOGL"),
        }

        # Filter to only process AAPL
        result = prepare_data_vectorized_system5(sample_data, symbols=["AAPL"])

        assert isinstance(result, dict)

    def test_prepare_data_vectorized_system5_with_batch_processing(self):
        """Test with batch processing parameters."""
        sample_data = {"TEST": self.create_test_data_with_indicators("TEST")}

        result = prepare_data_vectorized_system5(
            sample_data, batch_size=50, use_process_pool=False
        )

        assert isinstance(result, dict)


class TestSystem5CandidateGeneration:
    """Test System5 candidate generation."""

    def create_prepared_data_dict(self):
        """Helper to create prepared data dict for candidate tests."""
        np.random.seed(42)

        prepared_dict = {}
        symbols = ["AAPL", "MSFT", "GOOGL"]

        for i, symbol in enumerate(symbols):
            dates = pd.date_range("2023-01-01", periods=20, freq="D")
            df = pd.DataFrame(
                {
                    "Date": dates,
                    "Close": [50 + i * 10] * 20,
                    "adx7": [40 + i * 5] * 20,  # Different ADX values for ranking
                    "atr10": [2.0] * 20,
                    "dollarvolume20": [50000000] * 20,
                    "atr_pct": [0.03] * 20,
                    "filter": [True] * 20,
                    "setup": [True] * 20,
                }
            ).set_index("Date")

            prepared_dict[symbol] = df

        return prepared_dict

    def test_generate_candidates_system5_basic(self):
        """Test basic generate_candidates_system5 functionality."""
        prepared_data = self.create_prepared_data_dict()

        candidates_by_date, candidates_df = generate_candidates_system5(
            prepared_data, top_n=3
        )

        assert isinstance(candidates_by_date, dict)
        assert isinstance(candidates_df, pd.DataFrame) or candidates_df is None

    def test_generate_candidates_system5_with_progress_callback(self):
        """Test generate_candidates_system5 with progress callback."""
        prepared_data = self.create_prepared_data_dict()

        # Mock callback function
        callback_calls = []

        def mock_callback(message):
            callback_calls.append(message)

        candidates_by_date, candidates_df = generate_candidates_system5(
            prepared_data, top_n=2, progress_callback=mock_callback
        )

        assert isinstance(candidates_by_date, dict)

    def test_generate_candidates_system5_empty_input(self):
        """Test handling of empty input data."""
        candidates_by_date, candidates_df = generate_candidates_system5({}, top_n=5)

        assert isinstance(candidates_by_date, dict)
        assert len(candidates_by_date) == 0

    def test_generate_candidates_system5_top_n_none(self):
        """Test with top_n=None."""
        prepared_data = self.create_prepared_data_dict()

        candidates_by_date, candidates_df = generate_candidates_system5(
            prepared_data, top_n=None
        )

        assert isinstance(candidates_by_date, dict)

    def test_generate_candidates_system5_batch_processing(self):
        """Test with batch processing parameters."""
        prepared_data = self.create_prepared_data_dict()

        candidates_by_date, candidates_df = generate_candidates_system5(
            prepared_data, top_n=2, batch_size=10
        )

        assert isinstance(candidates_by_date, dict)


class TestSystem5Integration:
    """Integration tests for System5 workflow."""

    def create_integration_test_data(self):
        """Create realistic test data for integration testing."""
        symbols = ["AAPL", "MSFT", "GOOGL"]
        data = {}

        for i, symbol in enumerate(symbols):
            dates = pd.date_range("2023-01-01", periods=100, freq="D")
            np.random.seed(hash(symbol) % 2**32)

            base_price = 50 + i * 20
            df = pd.DataFrame(
                {
                    "Date": dates,
                    "Close": [base_price + np.random.normal(0, 2)] * 100,
                    "High": [base_price + np.random.normal(1, 2)] * 100,
                    "Low": [base_price + np.random.normal(-1, 2)] * 100,
                    "Volume": np.random.randint(1000000, 5000000, 100),
                }
            ).set_index("Date")

            # Add required indicators
            df["adx7"] = np.random.uniform(20, 60, 100)
            df["atr10"] = np.random.uniform(0.5, 3.0, 100)
            df["atr_pct"] = np.random.uniform(0.01, 0.08, 100)
            df["dollarvolume20"] = df["Close"] * df["Volume"]

            data[symbol] = df

        return data

    def test_full_system5_pipeline(self):
        """Test complete System5 pipeline from data to candidates."""
        symbol_data = self.create_integration_test_data()

        # Step 1: Prepare data
        prepared_data = prepare_data_vectorized_system5(symbol_data)
        assert isinstance(prepared_data, dict)

        # Step 2: Generate candidates
        candidates_by_date, candidates_df = generate_candidates_system5(
            prepared_data, top_n=3
        )

        assert isinstance(candidates_by_date, dict)
        assert isinstance(candidates_df, pd.DataFrame) or candidates_df is None

    def test_system5_pipeline_with_callbacks(self):
        """Test System5 pipeline with progress and log callbacks."""
        symbol_data = self.create_integration_test_data()

        progress_calls = []
        log_calls = []

        def progress_callback(msg):
            progress_calls.append(msg)

        def log_callback(msg):
            log_calls.append(msg)

        # Step 1: Prepare with callbacks
        prepared_data = prepare_data_vectorized_system5(
            symbol_data, progress_callback=progress_callback, log_callback=log_callback
        )

        assert isinstance(prepared_data, dict)

        # Step 2: Generate with callbacks
        candidates_by_date, candidates_df = generate_candidates_system5(
            prepared_data,
            top_n=2,
            progress_callback=progress_callback,
            log_callback=log_callback,
        )

        assert isinstance(candidates_by_date, dict)

    def test_system5_error_handling(self):
        """Test System5 error handling with problematic data."""
        # Test with malformed data
        bad_data = {
            "BAD": pd.DataFrame(
                {
                    "Date": pd.date_range("2023-01-01", periods=3),
                    "Close": [None, None, None],  # Bad data
                    "High": [1, 2, 3],
                    "Low": [1, 2, 3],
                    "Volume": [1000, 2000, 3000],
                }
            ).set_index("Date")
        }

        # Should handle gracefully
        try:
            result = prepare_data_vectorized_system5(bad_data)
            assert isinstance(result, dict)
        except Exception as e:
            # Acceptable exceptions
            assert isinstance(e, (ValueError, KeyError, TypeError, AttributeError))


class TestSystem5Constants:
    """Test System5 constants and configuration."""

    def test_system5_required_indicators(self):
        """Test SYSTEM5_REQUIRED_INDICATORS constant.

        audit-remediation 2026-07-03 (D3 Case A): avgvolume50 / dollarvolume50 を追加。
        "filter" / "setup" は runtime 生成列で precompute の必須指標ではないため除外。
        """
        assert isinstance(SYSTEM5_REQUIRED_INDICATORS, (list, tuple))
        assert len(SYSTEM5_REQUIRED_INDICATORS) > 0

        # Key indicators should be present (Case A: 流動性 filter 追加後)
        expected = {
            "adx7",
            "atr10",
            "dollarvolume20",
            "atr_pct",
            "sma100",
            "rsi3",
            "avgvolume50",  # Case A: spec の AvgVol50 filter
            "dollarvolume50",  # Case A: spec の DV50 filter
        }
        actual = set(SYSTEM5_REQUIRED_INDICATORS)
        assert expected.issubset(actual)

    def test_default_atr_pct_threshold(self):
        """Test DEFAULT_ATR_PCT_THRESHOLD constant.

        audit-remediation 2026-07-03 (D3 Case A): spec 準拠に 0.025 → 0.04 是正。
        """
        assert isinstance(DEFAULT_ATR_PCT_THRESHOLD, (int, float))
        assert 0 < DEFAULT_ATR_PCT_THRESHOLD < 1
        assert DEFAULT_ATR_PCT_THRESHOLD == 0.04  # 4% (spec, Case A)

    def test_system5_constants_immutability(self):
        """Test that System5 constants are properly defined."""
        # These should not raise exceptions
        assert DEFAULT_ATR_PCT_THRESHOLD is not None
        assert SYSTEM5_REQUIRED_INDICATORS is not None

        # Should be meaningful values (Case A で avgvolume50 / dollarvolume50 追加後は 8+)
        assert len(SYSTEM5_REQUIRED_INDICATORS) >= 8


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
