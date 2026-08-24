"""保有日数を **立会日 (trading day)** で数えることの回帰テスト (2026-08-22)。

背景
----
``SystemTradeRules.max_holding_days`` (S2=2 / S3=3 / S5=6 / S6=3) は
docs/systems/システム{N}.txt と ``strategies/system{N}_strategy.py`` の
``compute_exit`` が示すとおり **立会日 (bar) ベースの spec** である。backtest は
``idx = entry_idx + offset`` と bar を進めるので自動的に立会日だが、live 側は
日付の引き算なので、暦日で数えると週末・祝日ぶん **spec より早く** time exit が
発火する (金曜エントリーの System2 が月曜に手仕舞う = 立会 1 日で 2 日扱い)。

このファイルが固定するもの
--------------------------
1. 金曜またぎ (週末) が立会日として数えられないこと。
2. 祝日またぎ (NYSE 休場) が立会日として数えられないこと。
3. live (``common/alpaca_trading.compute_holding_days``) と
   ``common/trade_management`` の ``max_exit_date`` が **同じ単位** を使うこと
   (換算の実体は ``common/trading_days`` 1 箇所だけ)。
4. calendar が使えない環境でも Mon-Fri へ退避し、暦日には戻らないこと。

暦日 (旧挙動) との差が出るケースだけを選んである。日付はすべて 2026 年で、
NYSE の 2026-07-03 は独立記念日 (07-04 が土曜) の振替休場、2026-11-26 は感謝祭。
"""

from __future__ import annotations

from datetime import date, datetime

import pandas as pd
import pytest

from common import trading_days as td
from common.alpaca_trading import (
    ExitReasonCode,
    PositionSnapshot,
    build_exit_orders_from_positions,
    compute_holding_days,
)

# =====================================================================
# (1) 換算そのもの — 週末 / 祝日
# =====================================================================


class TestCountTradingDays:
    def test_friday_to_monday_is_one_trading_day(self):
        """金 -> 月 は立会 1 日。暦日なら 3 で、ここが旧バグの本体。"""
        d0, d1 = date(2026, 8, 21), date(2026, 8, 24)  # Fri -> Mon
        assert (d1 - d0).days == 3  # 旧挙動 (暦日) の値を明示
        assert td.count_trading_days(d0, d1) == 1

    def test_friday_to_tuesday_is_two_trading_days(self):
        assert td.count_trading_days(date(2026, 8, 21), date(2026, 8, 25)) == 2

    def test_holiday_is_not_a_trading_day(self):
        """2026-07-03 は独立記念日の振替休場 (07-04 が土曜)。

        水 07-01 -> 土 07-04 の立会日は木 07-02 の 1 日だけ。暦日なら 3。
        """
        d0, d1 = date(2026, 7, 1), date(2026, 7, 4)
        assert (d1 - d0).days == 3
        assert td.count_trading_days(d0, d1) == 1

    def test_thanksgiving_is_not_a_trading_day(self):
        """感謝祭 (2026-11-26 木) を挟むと水 -> 金 が立会 1 日になる。"""
        d0, d1 = date(2026, 11, 25), date(2026, 11, 27)  # Wed -> Fri
        assert (d1 - d0).days == 2
        assert td.count_trading_days(d0, d1) == 1

    def test_plain_weekday_span_matches_calendar(self):
        """週末も祝日も跨がなければ暦日と一致する (据え置き確認)。"""
        assert td.count_trading_days(date(2026, 8, 17), date(2026, 8, 19)) == 2

    def test_same_day_and_backwards_are_zero(self):
        assert td.count_trading_days(date(2026, 8, 17), date(2026, 8, 17)) == 0
        assert td.count_trading_days(date(2026, 8, 19), date(2026, 8, 17)) == 0


class TestAddTradingDays:
    def test_add_skips_the_weekend(self):
        assert td.add_trading_days(date(2026, 8, 21), 1) == date(2026, 8, 24)

    def test_add_skips_the_holiday(self):
        """金 06-26 + 6 立会日 = 火 07-07 (07-03 は休場)。暦日なら 07-02。"""
        assert td.add_trading_days(date(2026, 6, 26), 6) == date(2026, 7, 7)

    def test_add_and_count_are_inverses(self):
        for start in (date(2026, 6, 26), date(2026, 8, 21), date(2026, 11, 24)):
            for n in (1, 2, 3, 6):
                assert td.count_trading_days(start, td.add_trading_days(start, n)) == n

    def test_add_zero_is_identity(self):
        assert td.add_trading_days(date(2026, 8, 21), 0) == date(2026, 8, 21)


class TestFallbackLadder:
    def test_falls_back_to_business_days_without_the_calendar(self, monkeypatch):
        """calendar が引けない環境でも Mon-Fri へ退避し、暦日には戻らない。"""
        monkeypatch.setattr(td, "_nyse_sessions", lambda *a, **k: None)
        # 金 -> 月: busday_count でも 1 (暦日なら 3)
        assert td.count_trading_days(date(2026, 8, 21), date(2026, 8, 24)) == 1
        # 祝日は busday では平日扱いなので 2 (calendar 版の 1 とは差が出る)。
        # 「祝日まで拾えるのは calendar 経路だけ」という退避の性質を明示する。
        assert td.count_trading_days(date(2026, 7, 1), date(2026, 7, 4)) == 2

    def test_add_falls_back_without_the_calendar(self, monkeypatch):
        monkeypatch.setattr(td, "_nyse_sessions", lambda *a, **k: None)
        assert td.add_trading_days(date(2026, 8, 21), 1) == date(2026, 8, 24)


# =====================================================================
# (2) live 経路 — compute_holding_days
# =====================================================================


class TestComputeHoldingDaysUsesTradingDays:
    def test_friday_entry_is_not_two_days_old_on_monday(self):
        """金曜エントリーの System2 が月曜に time exit しないこと。

        暦日だと金 -> 月 = 3 で ``max_holding_days=2`` を超え、**立会 1 日**しか
        経っていないのに手仕舞ってしまっていた。
        """
        assert compute_holding_days("2026-08-21", "2026-08-24") == 1

    def test_holiday_span(self):
        assert compute_holding_days("2026-07-01", "2026-07-04") == 1

    def test_delegates_to_the_shared_module(self):
        """live の実装は ``common/trading_days`` の再実装を持たない。"""
        for entry, today in (
            ("2026-08-21", "2026-08-24"),
            ("2026-07-01", "2026-07-04"),
            ("2026-06-26", "2026-07-07"),
            ("2026-11-25", "2026-11-27"),
        ):
            assert compute_holding_days(entry, today) == td.count_trading_days(
                date.fromisoformat(entry), date.fromisoformat(today)
            )

    def test_bad_input_is_none(self):
        assert compute_holding_days(None, "2026-07-01") is None
        assert compute_holding_days("not-a-date", "2026-07-01") is None


def _snap(system: str, entry_date: str) -> PositionSnapshot:
    return PositionSnapshot(
        symbol="TEST",
        qty=10.0,
        side="long",
        avg_entry_price=100.0,
        market_value=1000.0,
        unrealized_pl=0.0,
        system=system,
        entry_date=entry_date,
    )


class TestTimeExitFiresOnTheSpecTradingDay:
    """S2 (max_holding_days=2) を金曜エントリーで前後 1 立会日ずつ確認する。"""

    def test_no_time_exit_one_trading_day_after_a_friday_entry(self):
        exits = build_exit_orders_from_positions(
            [_snap("system2", "2026-08-21")],  # Fri
            today="2026-08-24",  # Mon = 立会 1 日 (暦日なら 3)
            atr_by_symbol={"TEST": {10: 2.0}},
        )
        assert [e for e in exits if e.reason == ExitReasonCode.TIME] == []

    def test_time_exit_on_the_second_trading_day(self):
        exits = build_exit_orders_from_positions(
            [_snap("system2", "2026-08-21")],  # Fri
            today="2026-08-25",  # Tue = 立会 2 日
            atr_by_symbol={"TEST": {10: 2.0}},
        )
        te = [e for e in exits if e.reason == ExitReasonCode.TIME]
        assert len(te) == 1
        assert te[0].holding_days == 2
        assert te[0].max_holding_days == 2


# =====================================================================
# (3) live <-> backtest の単位が同じであること
# =====================================================================


class TestMaxExitDateUsesTheSameUnit:
    """``TradeManager.create_trade_entry`` の ``max_exit_date`` も立会日で置く。"""

    @staticmethod
    def _market_data(signal_date: pd.Timestamp) -> pd.DataFrame:
        idx = pd.date_range(end=signal_date, periods=30, freq="B")
        return pd.DataFrame(
            {
                "Open": [100.0] * len(idx),
                "High": [102.0] * len(idx),
                "Low": [98.0] * len(idx),
                "Close": [100.0] * len(idx),
                "ATR10": [2.0] * len(idx),
                "ATR20": [2.0] * len(idx),
                "ATR40": [2.0] * len(idx),
                "ATR50": [2.0] * len(idx),
            },
            index=idx,
        )

    @pytest.mark.parametrize(
        ("system", "side", "signal_date", "expected"),
        [
            # S5 は 6 立会日。金 06-26 起点だと 07-03 が休場なので 07-07 (暦日なら 07-02)。
            ("system5", "long", datetime(2026, 6, 26), datetime(2026, 7, 7)),
            # S2 は 2 立会日。金 08-21 起点だと火 08-25 (暦日なら日曜 08-23)。
            ("system2", "short", datetime(2026, 8, 21), datetime(2026, 8, 25)),
        ],
    )
    def test_max_exit_date_is_a_trading_day(self, system, side, signal_date, expected):
        from common.trade_management import TradeManager

        tm = TradeManager()
        entry = tm.create_trade_entry(
            symbol="TEST",
            system=system,
            side=side,
            signal_date=signal_date,
            entry_data={"shares": 10, "entry_price": 100.0},
            market_data=self._market_data(pd.Timestamp(signal_date)),
        )
        assert entry is not None
        assert entry.max_exit_date is not None
        assert entry.max_exit_date.date() == expected.date()
        # 単位が live と一致していること (= 換算の実体が 1 つ)
        assert (
            td.count_trading_days(signal_date.date(), entry.max_exit_date.date())
            == entry.rules.max_holding_days
        )
