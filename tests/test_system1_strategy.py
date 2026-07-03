"""
Test suite for strategies/system1_strategy.py
Tests System1Strategy functionality including data preparation,
candidate generation, and backtest execution
"""

from __future__ import annotations

from unittest.mock import Mock, patch

import pandas as pd
import pytest

from strategies.system1_strategy import System1Strategy


class TestSystem1Strategy:
    """Test System1Strategy class functionality"""

    def setup_method(self):
        """Setup test instance"""
        self.strategy = System1Strategy()

    def test_init(self):
        """Test System1Strategy initialization"""
        assert self.strategy.SYSTEM_NAME == "system1"
        assert hasattr(self.strategy, "prepare_data")
        assert hasattr(self.strategy, "generate_candidates")
        assert hasattr(self.strategy, "run_backtest")

    def test_prepare_data_with_dict_input(self):
        """Test prepare_data with dictionary input"""
        mock_raw_data = {
            "AAPL": pd.DataFrame(
                {"Close": [100, 101, 102], "Volume": [1000, 1100, 1200]}
            ),
            "SPY": pd.DataFrame(
                {"Close": [400, 401, 402], "Volume": [5000, 5100, 5200]}
            ),
        }

        mock_log_callback = Mock()

        with patch(
            "strategies.system1_strategy.prepare_data_vectorized_system1"
        ) as mock_prepare:
            mock_prepare.return_value = {"AAPL": mock_raw_data["AAPL"]}

            result = self.strategy.prepare_data(
                mock_raw_data, log_callback=mock_log_callback, use_process_pool=False
            )

            mock_prepare.assert_called_once()
            assert result is not None

    def test_prepare_data_with_exception_fallback(self):
        """Test prepare_data fallback on exception"""
        mock_raw_data = {"AAPL": pd.DataFrame({"Close": [100, 101]})}
        mock_log_callback = Mock()

        with patch(
            "strategies.system1_strategy.prepare_data_vectorized_system1"
        ) as mock_prepare:
            # First call raises exception, second succeeds
            mock_prepare.side_effect = [
                Exception("Test error"),
                {"AAPL": mock_raw_data["AAPL"]},
            ]

            result = self.strategy.prepare_data(
                mock_raw_data, log_callback=mock_log_callback, use_process_pool=True
            )

            # Should be called twice (initial + fallback)
            assert mock_prepare.call_count == 2
            mock_log_callback.assert_called()
            assert result is not None

    def test_generate_candidates_with_top_n_override(self):
        """Test generate_candidates with top_n override"""
        mock_data_dict = {
            "AAPL": pd.DataFrame({"Close": [100, 101], "ROC200": [0.1, 0.2]}),
            "SPY": pd.DataFrame({"Close": [400, 401], "ROC200": [0.05, 0.06]}),
        }

        with patch(
            "strategies.system1_strategy.generate_roc200_ranking_system1"
        ) as mock_generate:
            mock_generate.return_value = {"2023-01-01": ["AAPL"]}

            self.strategy.generate_candidates(mock_data_dict, top_n=5)

            mock_generate.assert_called_once()
            # Verify top_n=5 was passed
            call_kwargs = mock_generate.call_args[1]
            assert call_kwargs["top_n"] == 5

    def test_generate_candidates_with_default_settings(self):
        """Test generate_candidates with default settings"""
        mock_data_dict = {
            "AAPL": pd.DataFrame({"Close": [100, 101]}),
            "SPY": pd.DataFrame({"Close": [400, 401]}),
        }

        with patch(
            "strategies.system1_strategy.generate_roc200_ranking_system1"
        ) as mock_generate:
            with patch("config.settings.get_settings") as mock_get_settings:
                mock_settings = Mock()
                mock_settings.backtest.top_n_rank = 15
                mock_get_settings.return_value = mock_settings
                mock_generate.return_value = {"2023-01-01": ["AAPL"]}

                self.strategy.generate_candidates(mock_data_dict)

                mock_generate.assert_called_once()
                call_kwargs = mock_generate.call_args[1]
                assert call_kwargs["top_n"] == 15

    def test_generate_candidates_missing_spy_data(self):
        """Test generate_candidates raises error when SPY data missing"""
        mock_data_dict = {"AAPL": pd.DataFrame({"Close": [100, 101]})}

        with pytest.raises(ValueError, match="SPY data not found"):
            self.strategy.generate_candidates(mock_data_dict)

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
                "pnl": [100.0],
            }
        )

        with patch(
            "strategies.system1_strategy.simulate_trades_with_risk"
        ) as mock_simulate:
            mock_simulate.return_value = (mock_trades_df, {})

            result = self.strategy.run_backtest(
                mock_data_dict, mock_candidates, capital
            )

            mock_simulate.assert_called_once()
            assert len(result) == 1
            assert result["symbol"].iloc[0] == "AAPL"

    def test_compute_entry_valid_case(self):
        """Test compute_entry with valid data"""
        # Create test DataFrame with proper index
        dates = pd.date_range("2023-01-01", periods=5, freq="D")
        df = pd.DataFrame(
            {
                "Open": [100.0, 101.0, 102.0, 103.0, 104.0],
                "ATR20": [2.0, 2.1, 2.2, 2.3, 2.4],
            },
            index=dates,
        )

        candidate = {"entry_date": "2023-01-02"}

        # Mock config to return stop_atr_multiple
        self.strategy.config = {"stop_atr_multiple": 5.0}

        result = self.strategy.compute_entry(df, candidate, 10000.0)

        assert result is not None
        entry_price, stop_price = result
        assert entry_price == 101.0
        # stop_price = 101.0 - 5.0 * 2.0 = 91.0
        assert stop_price == 91.0

    def test_compute_entry_invalid_date(self):
        """Test compute_entry with invalid entry date"""
        dates = pd.date_range("2023-01-01", periods=3, freq="D")
        df = pd.DataFrame(
            {"Open": [100.0, 101.0, 102.0], "ATR20": [2.0, 2.1, 2.2]}, index=dates
        )

        candidate = {"entry_date": "2023-01-10"}  # Date not in DataFrame

        self.strategy.config = {"stop_atr_multiple": 5.0}

        result = self.strategy.compute_entry(df, candidate, 10000.0)
        assert result is None

    def test_compute_exit_stop_hit(self):
        """Test compute_exit when Close falls below initial stop (Close-based).

        D5 (2026-07-02) 更新: 旧実装は Low <= stop で判定していたが、
        S4 と同型の trailing-only 化により Close-based stop check に変更。
        """
        dates = pd.date_range("2023-01-01", periods=5, freq="D")
        df = pd.DataFrame(
            {
                "Open": [100.0, 101.0, 90.0, 88.0, 87.0],
                "High": [101.0, 102.0, 91.0, 89.0, 88.0],
                "Low": [99.0, 100.0, 85.0, 82.0, 80.0],
                "Close": [100.5, 101.5, 89.0, 88.0, 87.0],  # Day 2 Close < stop
            },
            index=dates,
        )

        self.strategy.config = {}
        entry_idx = 1
        entry_price = 101.0
        stop_price = 90.0

        exit_price, exit_date = self.strategy.compute_exit(
            df, entry_idx, entry_price, stop_price
        )

        # Day 2 Close=89.0 は stop=90 を下回るので同日 Close で決済
        assert exit_price == pytest.approx(89.0)
        assert exit_date == dates[2]

    def test_compute_exit_hold_until_end_no_time_exit(self):
        """Test compute_exit does NOT force-exit by time (S1 spec: 時間制限無し).

        D5 (2026-07-02) 更新: 旧実装は max_hold_days=3 で強制決済して
        いたが、S1 spec (25% trailing、時間制限無し) に整合させた。
        """
        dates = pd.date_range("2023-01-01", periods=5, freq="D")
        df = pd.DataFrame(
            {
                "Open": [100.0, 101.0, 102.0, 103.0, 104.0],
                "High": [101.0, 102.0, 103.0, 104.0, 105.0],
                "Low": [99.0, 100.0, 101.0, 102.0, 103.0],  # No stop hit
                "Close": [100.5, 101.5, 102.5, 103.5, 104.5],
            },
            index=dates,
        )

        # config に max_hold_days を差し込んでも新実装は無視する
        self.strategy.config = {"max_hold_days": 2}
        entry_idx = 1
        entry_price = 101.0
        stop_price = 90.0

        exit_price, exit_date = self.strategy.compute_exit(
            df, entry_idx, entry_price, stop_price
        )

        # trailing/stop 両方 trigger しないので末尾 Close まで hold
        assert exit_price == pytest.approx(104.5)
        assert exit_date == dates[-1]

    def test_get_total_days(self):
        """Test get_total_days method"""
        mock_data_dict = {"AAPL": pd.DataFrame({"Close": [100, 101, 102]})}

        with patch(
            "strategies.system1_strategy.get_total_days_system1"
        ) as mock_get_days:
            mock_get_days.return_value = 250

            result = self.strategy.get_total_days(mock_data_dict)

            assert result == 250
            mock_get_days.assert_called_once_with(mock_data_dict)
