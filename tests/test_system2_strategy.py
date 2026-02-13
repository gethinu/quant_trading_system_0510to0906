"""
Test suite for strategies/system2_strategy.py
Tests System2Strategy functionality for short strategy implementation
"""

from __future__ import annotations

from unittest.mock import patch

import pandas as pd
import pytest

from strategies.system2_strategy import System2Strategy


class TestSystem2Strategy:
    """Test System2Strategy class functionality"""

    def setup_method(self):
        """Setup test instance"""
        self.strategy = System2Strategy()

    def test_init(self):
        """Test System2Strategy initialization"""
        assert self.strategy.SYSTEM_NAME == "system2"
        assert hasattr(self.strategy, "prepare_data")
        assert hasattr(self.strategy, "generate_candidates")
        assert hasattr(self.strategy, "run_backtest")

    def test_prepare_data_with_dict_input(self):
        """Test prepare_data with dictionary input"""
        mock_raw_data = {
            "AAPL": pd.DataFrame({"Close": [100, 101, 102]}),
            "SPY": pd.DataFrame({"Close": [400, 401, 402]}),
        }

        with patch(
            "strategies.system2_strategy.prepare_data_vectorized_system2"
        ) as mock_prepare:
            mock_prepare.return_value = {"AAPL": mock_raw_data["AAPL"]}

            result = self.strategy.prepare_data(mock_raw_data, use_process_pool=False)

            mock_prepare.assert_called_once()
            assert result is not None

    def test_prepare_data_with_exception_fallback(self):
        """System2 prepare_data does not have strategy-level fallback retry."""
        mock_raw_data = {"AAPL": pd.DataFrame({"Close": [100, 101]})}

        with patch(
            "strategies.system2_strategy.prepare_data_vectorized_system2"
        ) as mock_prepare:
            mock_prepare.side_effect = Exception("Test error")
            with pytest.raises(Exception, match="Test error"):
                self.strategy.prepare_data(mock_raw_data, use_process_pool=True)
            assert mock_prepare.call_count == 1

    def test_generate_candidates(self):
        """Test generate_candidates method"""
        mock_data_dict = {
            "AAPL": pd.DataFrame({"Close": [100, 101]}),
            "SPY": pd.DataFrame({"Close": [400, 401]}),
        }

        with patch(
            "strategies.system2_strategy.generate_candidates_system2"
        ) as mock_generate:
            mock_generate.return_value = {"2023-01-01": ["AAPL"]}

            result = self.strategy.generate_candidates(mock_data_dict)

            mock_generate.assert_called_once()
            assert result is not None

    def test_run_backtest(self):
        """Test run_backtest execution"""
        mock_data_dict = {"AAPL": pd.DataFrame({"Close": [100, 101]})}
        mock_candidates = {"2023-01-01": ["AAPL"]}
        capital = 10000.0

        mock_trades_df = pd.DataFrame(
            {
                "symbol": ["AAPL"],
                "entry_date": ["2023-01-01"],
                "exit_date": ["2023-01-02"],
                "pnl": [-50.0],  # Short strategy loss
            }
        )

        with patch("common.backtest_utils.simulate_trades_with_risk") as mock_simulate:
            mock_simulate.return_value = (mock_trades_df, {})

            result = self.strategy.run_backtest(
                mock_data_dict, mock_candidates, capital
            )

            mock_simulate.assert_called_once()
            assert len(result) == 1
            assert result["symbol"].iloc[0] == "AAPL"

    def test_compute_entry_valid_case(self):
        """Test compute_entry for short strategy"""
        # Create test DataFrame with proper index
        dates = pd.date_range("2023-01-01", periods=5, freq="D")
        df = pd.DataFrame(
            {
                "Open": [100.0, 106.0, 102.0, 103.0, 104.0],
                "High": [101.0, 108.0, 103.0, 104.0, 105.0],
                "Close": [100.0, 105.0, 102.0, 103.0, 104.0],
                "atr10": [2.0, 2.1, 2.2, 2.3, 2.4],
            },
            index=dates,
        )

        candidate = {"entry_date": "2023-01-02"}

        # Mock config to return default values
        self.strategy.config = {}

        result = self.strategy.compute_entry(df, candidate, 10000.0)
        assert result is not None
        entry_price, stop_price = result
        assert entry_price >= 104.0  # prev close 100 の +4%以上で約定
        assert stop_price > entry_price  # short stop

    def test_get_total_days(self):
        """Test get_total_days method"""
        mock_data_dict = {"AAPL": pd.DataFrame({"Close": [100, 101, 102]})}

        with patch(
            "strategies.system2_strategy.get_total_days_system2"
        ) as mock_get_days:
            mock_get_days.return_value = 250

            result = self.strategy.get_total_days(mock_data_dict)

            assert result == 250
            mock_get_days.assert_called_once_with(mock_data_dict)

    def test_compute_exit_placeholder(self):
        """Test compute_exit method exists"""
        # This is a placeholder test for compute_exit
        assert hasattr(self.strategy, "compute_exit")
