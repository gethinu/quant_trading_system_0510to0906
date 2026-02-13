from __future__ import annotations

from unittest.mock import patch

import pandas as pd

from strategies.system4_strategy import System4Strategy


class TestSystem4Strategy:
    def setup_method(self):
        self.strategy = System4Strategy()

    def test_generate_candidates_applies_spy_sma200_gate(self):
        spy_df = pd.DataFrame(
            {"Close": [350.0], "sma200": [400.0]},
            index=pd.to_datetime(["2024-01-10"]),
        )
        prepared = {
            "AAPL": pd.DataFrame({"Close": [180.0, 181.0]}),
            "SPY": spy_df,
        }
        core_candidates = {
            "2024-01-11": {
                "AAPL": {
                    "symbol": "AAPL",
                    "date": "2024-01-10",
                    "entry_date": "2024-01-11",
                }
            }
        }
        core_merged = pd.DataFrame(
            [{"symbol": "AAPL", "date": "2024-01-10", "entry_date": "2024-01-11"}]
        )
        core_diag = {
            "ranking_source": "rsi4",
            "setup_predicate_count": 1,
            "ranked_top_n_count": 1,
        }

        with patch("strategies.system4_strategy.generate_candidates_system4") as mock_generate:
            mock_generate.return_value = (core_candidates, core_merged, core_diag)
            by_date, merged_df = self.strategy.generate_candidates(
                prepared,
                market_df=spy_df,
            )

        assert by_date == {}
        assert merged_df is not None
        assert merged_df.empty
        assert self.strategy.last_diagnostics is not None
        assert self.strategy.last_diagnostics["spy_gate_condition"] == "SPY close > SMA200"
        assert self.strategy.last_diagnostics["spy_gate_total_candidates_before"] == 1
        assert self.strategy.last_diagnostics["spy_gate_total_candidates_after"] == 0
        assert self.strategy.last_diagnostics["spy_gate_dropped"] == 1
        assert self.strategy.last_diagnostics["ranked_top_n_count"] == 0
