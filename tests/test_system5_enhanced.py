"""
Enhanced tests for core.system5 module targeting main functions for improved coverage.
Focus on the main pipeline functions and integration tests.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pandas as pd
import pytest

from common.testing import set_test_determinism

# Import functions directly to avoid dependency issues
generate_candidates_system5: Any = None
get_total_days_system5: Any = None
prepare_data_vectorized_system5: Any = None
format_atr_pct_threshold_label: Any = None
DEFAULT_ATR_PCT_THRESHOLD: Any = None
try:
    from core.system5 import DEFAULT_ATR_PCT_THRESHOLD as _def_atr
    from core.system5 import format_atr_pct_threshold_label as _fmt_atr
    from core.system5 import generate_candidates_system5 as _gc5
    from core.system5 import get_total_days_system5 as _gt5
    from core.system5 import prepare_data_vectorized_system5 as _prep5

    generate_candidates_system5 = _gc5
    get_total_days_system5 = _gt5
    prepare_data_vectorized_system5 = _prep5
    format_atr_pct_threshold_label = _fmt_atr
    DEFAULT_ATR_PCT_THRESHOLD = _def_atr
    IMPORTS_AVAILABLE = True
except ImportError:
    IMPORTS_AVAILABLE = False


class TestSystem5MainFunctions:
    """Test main System5 functions for improved coverage"""

    def setup_method(self):
        set_test_determinism()
        if not IMPORTS_AVAILABLE:
            pytest.skip("core.system5 imports not available")

    def test_prepare_data_vectorized_system5_fast_path_success(self):
        """Test fast path with precomputed indicators"""
        # Create mock data with all required System5 indicators
        mock_data = {
            "AAPL": pd.DataFrame(
                {
                    "Close": [100, 110, 120],
                    "adx7": [60, 65, 70],  # High ADX for System5
                    "atr10": [2.5, 2.7, 2.9],
                    "sma100": [95, 100, 105],
                    "rsi3": [40, 45, 48],  # Below 50 for setup
                    "atr_pct": [0.03, 0.035, 0.04],  # Above 2.5% threshold
                    "dollarvolume50": [5_000_000, 6_000_000, 7_000_000],
                    "avgvolume50": [600_000, 700_000, 800_000],
                },
                index=pd.date_range("2023-01-01", periods=3),
            ),
            "MSFT": pd.DataFrame(
                {
                    "Close": [200, 210, 220],
                    "adx7": [58, 62, 68],
                    "atr10": [3.0, 3.2, 3.4],
                    "sma100": [190, 195, 200],
                    "rsi3": [35, 40, 45],
                    "atr_pct": [0.028, 0.032, 0.036],
                    "dollarvolume50": [8_000_000, 9_000_000, 10_000_000],
                    "avgvolume50": [700_000, 800_000, 900_000],
                },
                index=pd.date_range("2023-01-01", periods=3),
            ),
        }

        # Mock the check_precomputed_indicators to return valid data
        with patch("core.system5.check_precomputed_indicators") as mock_check:
            mock_check.return_value = (mock_data, [])

            result = prepare_data_vectorized_system5(
                raw_data_dict=mock_data, reuse_indicators=True
            )

        assert isinstance(result, dict)
        assert "AAPL" in result
        assert "MSFT" in result

        # Check that filter and setup columns were added
        for _symbol, df in result.items():
            assert "filter" in df.columns
            assert "setup" in df.columns
            assert all(df["filter"].notna())
            assert all(df["setup"].notna())

    def test_prepare_data_vectorized_system5_with_symbols_list(self):
        """Test normal processing path with symbols list"""
        symbols = ["AAPL", "MSFT"]

        # Mock _compute_indicators function
        def mock_compute_indicators(symbol):
            df = pd.DataFrame(
                {
                    "Close": [100, 110],
                    "adx7": [60, 65],
                    "atr10": [2.5, 2.7],
                    "sma100": [95, 100],
                    "rsi3": [40, 45],
                    "atr_pct": [0.03, 0.035],
                    "dollarvolume50": [5_000_000, 6_000_000],
                    "avgvolume50": [600_000, 700_000],
                    "filter": [True, True],
                    "setup": [True, True],
                },
                index=pd.date_range("2023-01-01", periods=2),
            )
            return symbol, df

        with patch("core.system5.process_symbols_batch") as mock_batch:
            mock_batch.return_value = (
                {
                    "AAPL": mock_compute_indicators("AAPL")[1],
                    "MSFT": mock_compute_indicators("MSFT")[1],
                },
                [],
            )

            result = prepare_data_vectorized_system5(
                raw_data_dict=None, symbols=symbols
            )

        assert isinstance(result, dict)
        assert len(result) == 2

    def test_prepare_data_vectorized_system5_empty_input(self):
        """Test handling of empty input"""
        result = prepare_data_vectorized_system5(raw_data_dict={})
        assert isinstance(result, dict)
        assert len(result) == 0

    def test_prepare_data_vectorized_system5_none_input(self):
        """Test handling of None input without symbols"""
        result = prepare_data_vectorized_system5(raw_data_dict=None, symbols=[])
        assert isinstance(result, dict)
        assert len(result) == 0

    def test_generate_candidates_system5_basic(self):
        """Test basic candidate generation"""
        prepared_dict = {
            "AAPL": pd.DataFrame(
                {
                    "Close": [100, 110, 120],
                    "adx7": [60, 65, 70],  # Descending ranking key
                    "atr10": [2.5, 2.7, 2.9],
                    "sma100": [95, 100, 105],
                    "rsi3": [40, 45, 48],
                    "setup": [True, True, True],
                },
                index=pd.date_range("2023-01-01", periods=3),
            ),
            "MSFT": pd.DataFrame(
                {
                    "Close": [200, 210, 220],
                    "adx7": [58, 62, 68],
                    "atr10": [3.0, 3.2, 3.4],
                    "sma100": [190, 195, 200],
                    "rsi3": [35, 40, 45],
                    "setup": [True, True, True],
                },
                index=pd.date_range("2023-01-01", periods=3),
            ),
        }

        candidates, merged_df = generate_candidates_system5(
            prepared_dict=prepared_dict, top_n=10, latest_only=False
        )

        assert isinstance(candidates, dict)
        assert merged_df is None or isinstance(merged_df, pd.DataFrame)

    def test_generate_candidates_system5_latest_only(self):
        """Test candidate generation with latest_only=True"""
        prepared_dict = {
            "AAPL": pd.DataFrame(
                {
                    "Close": [100, 110, 120],
                    "adx7": [60, 65, 70],
                    "atr10": [2.5, 2.7, 2.9],
                    "sma100": [95, 100, 105],
                    "rsi3": [40, 45, 48],
                    "setup": [False, False, True],  # Only last row setup
                },
                index=pd.date_range("2023-01-01", periods=3),
            ),
        }

        candidates, merged_df = generate_candidates_system5(
            prepared_dict=prepared_dict, top_n=10, latest_only=True
        )

        assert isinstance(candidates, dict)
        # With latest_only, should only check last row
        if candidates:
            for _date, _cands in candidates.items():
                assert isinstance(_cands, dict)

    def test_generate_candidates_system5_with_diagnostics(self):
        """Test candidate generation with diagnostics enabled"""
        prepared_dict = {
            "AAPL": pd.DataFrame(
                {
                    "Close": [100, 110, 120],
                    "adx7": [60, 65, 70],
                    "atr10": [2.5, 2.7, 2.9],
                    "sma100": [95, 100, 105],
                    "rsi3": [40, 45, 48],
                    "setup": [True, True, True],
                },
                index=pd.date_range("2023-01-01", periods=3),
            ),
        }

        candidates, merged_df, diagnostics = generate_candidates_system5(
            prepared_dict=prepared_dict, top_n=10, include_diagnostics=True
        )

        assert isinstance(candidates, dict)
        assert isinstance(diagnostics, dict)
        # Check for required diagnostic keys
        assert "ranking_source" in diagnostics
        assert "setup_predicate_count" in diagnostics
        assert "ranked_top_n_count" in diagnostics

    def test_generate_candidates_system5_empty_prepared_dict(self):
        """Test handling of empty prepared_dict"""
        candidates, merged_df = generate_candidates_system5(prepared_dict={}, top_n=10)

        assert isinstance(candidates, dict)
        assert len(candidates) == 0
        assert merged_df is None or (
            isinstance(merged_df, pd.DataFrame) and merged_df.empty
        )

    def test_generate_candidates_system5_ranking_order(self):
        """Test that candidates are ranked by ADX7 descending"""
        prepared_dict = {
            "AAPL": pd.DataFrame(
                {
                    "Close": [100],
                    "adx7": [70],  # Higher ADX
                    "atr10": [2.5],
                    "sma100": [95],
                    "rsi3": [40],
                    "setup": [True],
                },
                index=pd.date_range("2023-01-01", periods=1),
            ),
            "MSFT": pd.DataFrame(
                {
                    "Close": [200],
                    "adx7": [60],  # Lower ADX
                    "atr10": [3.0],
                    "sma100": [190],
                    "rsi3": [35],
                    "setup": [True],
                },
                index=pd.date_range("2023-01-01", periods=1),
            ),
        }

        candidates, _merged_df = generate_candidates_system5(
            prepared_dict=prepared_dict, top_n=1, latest_only=True
        )

        # AAPL should rank higher due to higher ADX7
        if candidates:
            for _date, cands in candidates.items():
                if cands:
                    # First candidate should be AAPL (higher ADX)
                    first_symbol = list(cands.keys())[0]
                    assert first_symbol == "AAPL"

    def test_generate_candidates_system5_missing_indicators(self):
        """Test handling of missing required indicators"""
        prepared_dict = {
            "AAPL": pd.DataFrame(
                {
                    "Close": [100, 110],
                    # Missing adx7, atr10, etc.
                    "setup": [True, True],
                },
                index=pd.date_range("2023-01-01", periods=2),
            ),
        }

        # Should handle gracefully without crashing
        try:
            candidates, _merged_df = generate_candidates_system5(
                prepared_dict=prepared_dict, top_n=10
            )
            # Either returns empty or handles missing columns
            assert isinstance(candidates, dict)
        except KeyError:
            # Acceptable to raise KeyError for missing required columns
            pass

    def test_get_total_days_system5(self):
        """Test get_total_days_system5 function"""
        test_data = {
            "AAPL": pd.DataFrame(
                {
                    "Close": [100, 110, 120],
                    "adx7": [60, 65, 70],
                },
                index=pd.date_range("2023-01-01", periods=3),
            ),
        }
        result = get_total_days_system5(test_data)
        assert isinstance(result, int)
        assert result > 0  # Should return a positive number

    def test_format_atr_pct_threshold_label_default(self):
        """Test ATR threshold label formatting with default.

        audit-remediation 2026-07-03 (D3 Case A): default 2.5% → 4% 是正。
        """
        label = format_atr_pct_threshold_label()
        assert isinstance(label, str)
        assert ">" in label
        assert "4.00%" in label or "0.04" in label

    def test_format_atr_pct_threshold_label_custom(self):
        """Test ATR threshold label formatting with custom value"""
        label = format_atr_pct_threshold_label(threshold=0.05)
        assert isinstance(label, str)
        assert ">" in label
        assert "5.00%" in label or "0.05" in label

    def test_default_atr_pct_threshold_constant(self):
        """Test DEFAULT_ATR_PCT_THRESHOLD constant.

        audit-remediation 2026-07-03 (D3 Case A): spec 準拠に 0.025 → 0.04 是正。
        """
        assert DEFAULT_ATR_PCT_THRESHOLD == 0.04
        assert isinstance(DEFAULT_ATR_PCT_THRESHOLD, float)


class TestSystem5EdgeCases:
    """Test edge cases and error handling for System5"""

    def setup_method(self):
        set_test_determinism()
        if not IMPORTS_AVAILABLE:
            pytest.skip("core.system5 imports not available")

    def test_prepare_data_with_nan_values(self):
        """Test handling of NaN values in indicators"""
        mock_data = {
            "AAPL": pd.DataFrame(
                {
                    "Close": [100, None, 120],
                    "adx7": [60, 65, None],
                    "atr10": [2.5, None, 2.9],
                    "sma100": [95, 100, 105],
                    "rsi3": [40, 45, 48],
                    "atr_pct": [0.03, 0.035, 0.04],
                    "dollarvolume50": [5_000_000, 6_000_000, 7_000_000],
                    "avgvolume50": [600_000, 700_000, 800_000],
                },
                index=pd.date_range("2023-01-01", periods=3),
            ),
        }

        with patch("core.system5.check_precomputed_indicators") as mock_check:
            mock_check.return_value = (mock_data, [])

            result = prepare_data_vectorized_system5(
                raw_data_dict=mock_data, reuse_indicators=True
            )

        assert isinstance(result, dict)
        # Should handle NaN values gracefully

    def test_generate_candidates_with_all_false_setup(self):
        """Test candidate generation when all setup values are False"""
        prepared_dict = {
            "AAPL": pd.DataFrame(
                {
                    "Close": [100, 110, 120],
                    "adx7": [60, 65, 70],
                    "atr10": [2.5, 2.7, 2.9],
                    "sma100": [95, 100, 105],
                    "rsi3": [40, 45, 48],
                    "setup": [False, False, False],  # All False
                },
                index=pd.date_range("2023-01-01", periods=3),
            ),
        }

        candidates, merged_df = generate_candidates_system5(
            prepared_dict=prepared_dict, top_n=10
        )

        assert isinstance(candidates, dict)
        # Should return empty candidates or handle gracefully
        if candidates:
            for _date, cands in candidates.items():
                # If any candidates, they should be dict
                assert isinstance(cands, dict)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
