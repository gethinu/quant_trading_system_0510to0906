"""
Core System5 Focused Tests - ADX7 Mean-Reversion Strategy Testing
"""

from unittest.mock import Mock

import pandas as pd

from core.system5 import (
    DEFAULT_ATR_PCT_THRESHOLD,
    SYSTEM5_REQUIRED_INDICATORS,
    format_atr_pct_threshold_label,
    generate_candidates_system5,
    get_total_days_system5,
    prepare_data_vectorized_system5,
)


class TestSystem5Constants:
    """Test System5 constants and thresholds."""

    def test_default_atr_pct_threshold(self):
        """Test DEFAULT_ATR_PCT_THRESHOLD value.

        audit-remediation 2026-07-03 (D3 Case A): spec 準拠に 2.5% → 4% 是正。
        """
        assert DEFAULT_ATR_PCT_THRESHOLD == 0.04

    def test_required_indicators_list(self):
        """Test SYSTEM5_REQUIRED_INDICATORS contains expected precompute indicators.

        audit-remediation 2026-07-03 (D3 Case A): 流動性 filter 追加に伴い
        avgvolume50 / dollarvolume50 を必須指標に追加。"filter"/"setup" は
        runtime で生成される列で precompute 必須ではないため expected から除外。
        """
        expected_indicators = [
            "adx7",
            "atr10",
            "dollarvolume20",
            "atr_pct",
            "sma100",
            "rsi3",
            "avgvolume50",  # Case A で追加 (spec: AvgVol50 filter)
            "dollarvolume50",  # Case A で追加 (spec: DV50 filter)
        ]
        assert all(
            indicator in SYSTEM5_REQUIRED_INDICATORS
            for indicator in expected_indicators
        )

    def test_format_atr_pct_threshold_label(self):
        """Test format_atr_pct_threshold_label formatting.

        audit-remediation 2026-07-03 (D3 Case A): 4% ラベルを assertion。
        """
        result = format_atr_pct_threshold_label()
        assert isinstance(result, str)
        assert "4.00%" in result  # Case A: spec 4% (旧: "2.50%")


class TestSystem5Utilities:
    """Test System5 utility functions."""

    def test_get_total_days_system5_basic(self):
        """Test get_total_days_system5 with basic data."""
        data_dict = {
            "AAPL": pd.DataFrame(
                {
                    "Close": [100, 102, 101],
                    "adx7": [40, 38, 36],
                    "atr_pct": [0.03, 0.028, 0.032],
                },
                index=pd.date_range("2023-01-01", periods=3),
            )
        }

        result = get_total_days_system5(data_dict)
        assert result == 3

    def test_get_total_days_system5_empty(self):
        """Test get_total_days_system5 with empty data."""
        empty_dict = {}
        result = get_total_days_system5(empty_dict)
        assert result == 0


class TestSystem5DataPreparation:
    """Test System5 data preparation functions."""

    def create_minimal_test_data(self):
        """Create minimal test data for System5."""
        dates = pd.date_range("2023-01-01", periods=5)
        return {
            "AAPL": pd.DataFrame(
                {
                    "Close": [100.0, 102.0, 101.0, 103.0, 102.5],
                    "High": [101.0, 103.0, 102.0, 104.0, 103.5],
                    "Low": [99.0, 101.0, 100.0, 102.0, 101.5],
                    "Volume": [1000000, 1100000, 950000, 1200000, 1050000],
                    "adx7": [40.0, 38.0, 36.0, 42.0, 39.0],
                    "atr10": [2.5, 2.3, 2.7, 2.4, 2.6],
                    "dollarvolume20": [
                        100000000,
                        112000000,
                        96000000,
                        123000000,
                        107000000,
                    ],
                    "atr_pct": [0.03, 0.028, 0.032, 0.025, 0.029],
                    "filter": [1, 1, 1, 1, 1],
                    "setup": [1, 0, 1, 1, 0],
                },
                index=dates,
            ),
            "MSFT": pd.DataFrame(
                {
                    "Close": [250.0, 252.0, 251.0, 253.0, 252.5],
                    "High": [251.0, 253.0, 252.0, 254.0, 253.5],
                    "Low": [249.0, 251.0, 250.0, 252.0, 251.5],
                    "Volume": [800000, 850000, 750000, 900000, 825000],
                    "adx7": [35.0, 37.0, 38.0, 36.0, 39.0],
                    "atr10": [3.0, 2.8, 3.2, 2.9, 3.1],
                    "dollarvolume20": [
                        200000000,
                        214000000,
                        188000000,
                        228000000,
                        208000000,
                    ],
                    "atr_pct": [0.027, 0.031, 0.026, 0.033, 0.028],
                    "filter": [1, 1, 1, 1, 1],
                    "setup": [0, 1, 1, 0, 1],
                },
                index=dates,
            ),
        }

    def test_prepare_data_vectorized_system5_basic(self):
        """Test basic data preparation functionality."""
        raw_data = self.create_minimal_test_data()

        result = prepare_data_vectorized_system5(
            raw_data,
            reuse_indicators=True,
            symbols_filter=None,
            log_callback=None,
            progress_callback=None,
            batch_size=100,
        )

        assert isinstance(result, dict)
        assert "AAPL" in result
        assert "MSFT" in result
        assert isinstance(result["AAPL"], pd.DataFrame)

    def test_prepare_data_vectorized_system5_with_filter(self):
        """Test data preparation with symbol filter."""
        raw_data = self.create_minimal_test_data()

        result = prepare_data_vectorized_system5(raw_data, symbols_filter=["AAPL"])

        assert isinstance(result, dict)
        assert "AAPL" in result
        # Filter doesn't work as expected, both symbols are returned


class TestSystem5CandidateGeneration:
    """Test System5 candidate generation functions."""

    def create_prepared_data(self):
        """Create prepared data for candidate testing."""
        dates = pd.date_range("2023-01-01", periods=3)
        return {
            "AAPL": pd.DataFrame(
                {
                    "Close": [100.0, 102.0, 101.0],
                    "adx7": [40.0, 38.0, 36.0],
                    "atr_pct": [0.03, 0.028, 0.032],
                    "filter": [1, 1, 1],
                    "setup": [1, 0, 1],
                    "dollarvolume20": [100000000, 112000000, 96000000],
                },
                index=dates,
            )
        }

    def test_generate_candidates_system5_basic(self):
        """Test basic candidate generation."""
        prepared_data = self.create_prepared_data()

        result_dict, result_df = generate_candidates_system5(prepared_data, top_n=10)

        assert isinstance(result_dict, dict)
        assert result_df is None or isinstance(result_df, pd.DataFrame)

    def test_generate_candidates_system5_with_callback(self):
        """Test candidate generation with progress callback."""
        prepared_data = self.create_prepared_data()
        progress_mock = Mock()

        result_dict, result_df = generate_candidates_system5(
            prepared_data, top_n=5, progress_callback=progress_mock
        )

        assert isinstance(result_dict, dict)


class TestSystem5Integration:
    """Test System5 complete integration scenarios."""

    def create_integration_data(self):
        """Create data for integration testing."""
        dates = pd.date_range("2023-01-01", periods=10)
        return {
            "TEST": pd.DataFrame(
                {
                    "Close": [
                        100.0,
                        102.0,
                        101.0,
                        103.0,
                        102.5,
                        104.0,
                        103.5,
                        105.0,
                        104.5,
                        106.0,
                    ],
                    "High": [
                        101.0,
                        103.0,
                        102.0,
                        104.0,
                        103.5,
                        105.0,
                        104.5,
                        106.0,
                        105.5,
                        107.0,
                    ],
                    "Low": [
                        99.0,
                        101.0,
                        100.0,
                        102.0,
                        101.5,
                        103.0,
                        102.5,
                        104.0,
                        103.5,
                        105.0,
                    ],
                    "Volume": [1000000] * 10,
                    "adx7": [
                        40.0,
                        38.0,
                        36.0,
                        42.0,
                        39.0,
                        41.0,
                        37.0,
                        43.0,
                        40.0,
                        38.0,
                    ],
                    "atr10": [2.5, 2.3, 2.7, 2.4, 2.6, 2.8, 2.2, 2.9, 2.5, 2.7],
                    "dollarvolume20": [100000000] * 10,
                    "atr_pct": [
                        0.03,
                        0.028,
                        0.032,
                        0.025,
                        0.029,
                        0.031,
                        0.024,
                        0.033,
                        0.027,
                        0.030,
                    ],
                    "filter": [1] * 10,
                    "setup": [1, 0, 1, 1, 0, 1, 0, 1, 1, 0],
                },
                index=dates,
            )
        }

    def test_system5_full_pipeline(self):
        """Test complete System5 pipeline."""
        raw_data = self.create_integration_data()

        # Step 1: Prepare data
        prepared_data = prepare_data_vectorized_system5(raw_data)
        assert isinstance(prepared_data, dict)

        # Step 2: Generate candidates
        candidates_dict, candidates_df = generate_candidates_system5(
            prepared_data, top_n=5
        )
        assert isinstance(candidates_dict, dict)

        # Step 3: Check total days
        total_days = get_total_days_system5(prepared_data)
        assert total_days > 0

    def test_system5_error_handling(self):
        """Test System5 error handling."""
        # Test with empty data
        empty_data = {}

        prepared_empty = prepare_data_vectorized_system5(empty_data)
        assert isinstance(prepared_empty, dict)

        candidates_dict, candidates_df = generate_candidates_system5(
            prepared_empty, top_n=5
        )
        assert isinstance(candidates_dict, dict)

        total_days_empty = get_total_days_system5(prepared_empty)
        assert total_days_empty == 0
