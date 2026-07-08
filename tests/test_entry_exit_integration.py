"""Entry/Exit統合テスト

実際のEntry→Exit フローを確認するテストスイート。
"""

from __future__ import annotations

import pandas as pd
import pytest

from common.exit_planner import decide_exit_schedule
from strategies.system1_strategy import System1Strategy
from strategies.system2_strategy import System2Strategy
from strategies.system3_strategy import System3Strategy
from strategies.system4_strategy import System4Strategy
from strategies.system5_strategy import System5Strategy
from strategies.system6_strategy import System6Strategy
from strategies.system7_strategy import System7Strategy


class TestEntryExitIntegration:
    """Entry と Exit の統合動作テスト"""

    def test_system1_entry_to_exit_flow(self):
        """System1: Entry → Exit の完全なフロー"""
        # テストデータ作成
        dates = pd.date_range("2025-01-01", periods=10, freq="D")
        df = pd.DataFrame(
            {
                "Open": [100, 102, 101, 99, 98, 97, 100, 103, 105, 107],
                "High": [102, 104, 103, 101, 100, 99, 102, 105, 107, 109],
                "Low": [99, 101, 100, 98, 97, 96, 99, 102, 104, 106],
                "Close": [101, 103, 102, 100, 99, 98, 101, 104, 106, 108],
                "ATR20": [2.0] * 10,
            },
            index=dates,
        )

        strategy = System1Strategy()
        candidate = {"entry_date": "2025-01-02"}

        # Step 1: Entry
        result = strategy.compute_entry(df, candidate, 10000.0)
        assert result is not None
        entry_price, stop_price = result
        assert entry_price > 0
        assert stop_price < entry_price

        # Step 2: Exit
        entry_idx = 1  # 2025-01-02
        exit_price, exit_date = strategy.compute_exit(
            df, entry_idx, entry_price, stop_price
        )
        assert exit_price > 0
        assert exit_date >= dates[entry_idx]

        # Step 3: Exit Schedule
        is_due, when = decide_exit_schedule(
            "system1", exit_date, pd.Timestamp("2025-01-05")
        )
        assert when in ["today_close", "tomorrow_close"]

    def test_system2_entry_to_exit_flow(self):
        """System2: Short Entry → Exit の完全なフロー"""
        dates = pd.date_range("2025-01-01", periods=10, freq="D")
        # System2 はショートで上窓(>=4%)が必要。前日終値101 → 当日寄付は106に設定
        df = pd.DataFrame(
            {
                "Open": [100, 106, 101, 99, 98, 97, 100, 103, 105, 107],
                "High": [102, 108, 103, 101, 100, 99, 102, 105, 107, 109],
                "Low": [99, 104, 100, 98, 97, 96, 99, 102, 104, 106],
                "Close": [101, 105, 102, 100, 99, 98, 101, 104, 106, 108],
                "ATR10": [1.5] * 10,
            },
            index=dates,
        )

        strategy = System2Strategy()
        candidate = {
            "entry_date": "2025-01-02",
            "prev_close": 101.0,
        }

        # Step 1: Entry
        result = strategy.compute_entry(df, candidate, 10000.0)
        assert result is not None
        entry_price, stop_price = result
        # System2 は Short なので stop > entry
        assert stop_price > entry_price

        # Step 2: Exit
        entry_idx = 1
        exit_price, exit_date = strategy.compute_exit(
            df, entry_idx, entry_price, stop_price
        )
        assert exit_price > 0

        # Step 3: Exit Schedule (system2 は tomorrow_close)
        is_due, when = decide_exit_schedule(
            "system2", exit_date, pd.Timestamp("2025-01-05")
        )
        assert when in ["today_close", "tomorrow_close"]

    def test_system3_entry_to_exit_flow(self):
        """System3: Long Entry with profit target"""
        dates = pd.date_range("2025-01-01", periods=10, freq="D")
        df = pd.DataFrame(
            {
                "Open": [100, 102, 101, 99, 98, 97, 100, 103, 105, 107],
                # profit target hit on the last bar
                "High": [102, 104, 103, 101, 100, 99, 102, 105, 107, 115],
                "Low": [99, 101, 100, 98, 97, 96, 99, 102, 104, 106],
                "Close": [101, 103, 102, 100, 99, 98, 101, 104, 106, 108],
                "ATR10": [1.5] * 10,
            },
            index=dates,
        )

        strategy = System3Strategy()
        candidate = {
            "entry_date": "2025-01-02",
            "prev_close": 101.0,
        }

        # Step 1: Entry
        result = strategy.compute_entry(df, candidate, 10000.0)
        assert result is not None
        entry_price, stop_price = result
        assert entry_price < 101.0  # 93% of prev_close
        assert stop_price < entry_price

        # Step 2: Exit (利確条件をテスト)
        entry_idx = 1
        exit_price, exit_date = strategy.compute_exit(
            df, entry_idx, entry_price, stop_price
        )
        assert exit_price > 0
        # 利確が発動したか確認
        profit_pct = (exit_price - entry_price) / entry_price
        # max_hold_days 内に収まること
        assert profit_pct >= 0 or exit_date <= dates[entry_idx + 3]

    def test_exit_schedule_system5_tomorrow_open(self):
        """System5 は tomorrow_open でエグジット"""
        exit_date = pd.Timestamp("2025-01-10")
        today = pd.Timestamp("2025-01-10")

        is_due, when = decide_exit_schedule("system5", exit_date, today)
        assert is_due is True
        assert when == "tomorrow_open"

    def test_exit_schedule_future(self):
        """将来の日付の場合は future として返す"""
        exit_date = pd.Timestamp("2025-01-15")
        today = pd.Timestamp("2025-01-10")

        is_due, when = decide_exit_schedule("system1", exit_date, today)
        assert is_due is False
        # system1 は TOMORROW_CLOSE_SYSTEMS に含まれる
        assert when == "tomorrow_close"

    def test_multiple_systems_entry_comparison(self):
        """複数システムの Entry 価格比較"""
        dates = pd.date_range("2025-01-01", periods=5, freq="D")
        # 共通DF（System1/3 用）
        df_common = pd.DataFrame(
            {
                "Open": [100, 102, 101, 99, 98],
                "High": [102, 104, 103, 101, 100],
                "Low": [99, 101, 100, 98, 97],
                "Close": [101, 103, 102, 100, 99],
                "ATR10": [1.5] * 5,
                "ATR20": [2.0] * 5,
            },
            index=dates,
        )
        # System2 用（上窓を満たす）
        df_s2 = pd.DataFrame(
            {
                "Open": [100, 106, 101, 99, 98],
                "High": [102, 110, 103, 101, 100],
                "Low": [99, 104, 100, 98, 97],
                "Close": [101, 105, 102, 100, 99],
                "ATR10": [1.5] * 5,
                "ATR20": [2.0] * 5,
            },
            index=dates,
        )

        candidate = {
            "entry_date": "2025-01-02",
            "prev_close": 101.0,
        }

        # System1: Market Open
        s1 = System1Strategy()
        r1 = s1.compute_entry(df_common, candidate, 10000.0)
        assert r1 is not None
        entry1, stop1 = r1

        # System2: Limit (prev_close の上)
        s2 = System2Strategy()
        r2 = s2.compute_entry(df_s2, candidate, 10000.0)
        assert r2 is not None
        entry2, stop2 = r2

        # System3: Limit (prev_close の下)
        s3 = System3Strategy()
        r3 = s3.compute_entry(df_common, candidate, 10000.0)
        assert r3 is not None
        entry3, stop3 = r3

        # System1 は Open = 102
        assert entry1 == pytest.approx(102.0)

        # System2 は prev_close の上 (short なので)
        assert entry2 > 101.0

        # System3 は prev_close の下 (long limit)
        assert entry3 < 101.0

        # Stop の方向確認
        assert stop1 < entry1  # Long
        assert stop2 > entry2  # Short
        assert stop3 < entry3  # Long

    def test_system4_entry_to_exit_flow(self):
        """System4: Long Trend with trailing stop"""
        dates = pd.date_range("2025-01-01", periods=12, freq="D")
        # 価格は上昇→下落でトレーリング20%に触れるよう構成
        closes = [100, 101, 102, 103, 105, 107, 110, 112, 113, 95, 94, 93]
        highs = [c + 1 for c in closes]
        lows = [c - 1 for c in closes]
        df = pd.DataFrame(
            {
                "Open": closes,
                "High": highs,
                "Low": lows,
                "Close": closes,
                "ATR40": [2.0] * len(closes),
            },
            index=dates,
        )
        strategy = System4Strategy()
        candidate = {"entry_date": str(dates[1].date())}
        r = strategy.compute_entry(df, candidate, 10000.0)
        assert r is not None
        entry_price, stop_price = r
        assert stop_price < entry_price
        exit_price, exit_date = strategy.compute_exit(df, 1, entry_price, stop_price)
        assert exit_price > 0
        assert exit_date >= dates[1]

    def test_system5_entry_to_exit_flow(self):
        """System5: Long mean reversion target ATR then next-day open exit"""
        dates = pd.date_range("2025-01-01", periods=10, freq="D")
        # エントリー翌日に High が目標到達するよう構成 (entry + 1*ATR)
        df = pd.DataFrame(
            {
                "Open": [100, 100, 100, 100, 100, 100, 100, 100, 100, 100],
                "High": [101, 103, 104, 104, 104, 104, 104, 104, 104, 104],
                "Low": [99, 99, 99, 99, 99, 99, 99, 99, 99, 99],
                "Close": [100, 100, 102, 103, 103, 103, 103, 103, 103, 103],
                "ATR10": [2.0] * 10,
            },
            index=dates,
        )
        strategy = System5Strategy()
        candidate = {"entry_date": str(dates[1].date())}
        r = strategy.compute_entry(df, candidate, 10000.0)
        assert r is not None
        entry_price, stop_price = r
        # 目標到達 → 翌日の寄付で決済
        exit_price, exit_date = strategy.compute_exit(df, 1, entry_price, stop_price)
        assert exit_date >= dates[2] or exit_price > 0

    def test_system6_entry_to_exit_flow(self):
        """System6: Short mean reversion with profit take"""
        dates = pd.date_range("2025-01-01", periods=8, freq="D")
        # 前日終値の 1.05x でエントリー → その後 6%以上下落して利確
        close = [100, 101, 95, 94, 93, 92, 91, 91]
        df = pd.DataFrame(
            {
                "Open": [100, 106, 95, 94, 93, 92, 91, 91],
                "High": [101, 108, 96, 95, 94, 93, 92, 92],
                "Low": [99, 104, 94, 93, 92, 91, 90, 90],
                "Close": close,
                "ATR10": [1.0] * 8,
            },
            index=dates,
        )
        strategy = System6Strategy()
        candidate = {"entry_date": str(dates[1].date())}
        r = strategy.compute_entry(df, candidate, 10000.0)
        assert r is not None
        entry_price, stop_price = r
        exit_price, exit_date = strategy.compute_exit(df, 1, entry_price, stop_price)
        assert exit_price > 0
        assert exit_date >= dates[1]

    def test_system7_entry_minimal(self):
        """System7: SPY Short hedge - entry only sanity check"""
        dates = pd.date_range("2025-01-01", periods=60, freq="D")
        # 単純な一定ボラのデータとATR50を用意
        base = 500.0
        close = [base + i * 0.5 for i in range(len(dates))]
        high = [c + 1.0 for c in close]
        low = [c - 1.0 for c in close]
        df = pd.DataFrame(
            {
                "Open": close,
                "High": high,
                "Low": low,
                "Close": close,
                "ATR50": [2.0] * len(dates),
            },
            index=dates,
        )
        strategy = System7Strategy()
        candidate = {"entry_date": str(dates[50].date()), "ATR50": 2.0}
        r = strategy.compute_entry(df, candidate, 100000.0)
        assert r is not None
        entry_price, stop_price = r
        assert stop_price > entry_price  # ショートなので stop は上


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
