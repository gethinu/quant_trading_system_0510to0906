"""common/market_session.py — 「カレンダー当日」でなく立会セッションで測る。

回帰の芯: JST の朝は **当日セッションがまだ寄っていない**。当日を期待すると
毎朝オオカミ少年になり、本物の停止が埋もれる (2026-08-20 のモーニングブリーフ
🔴「exit 台帳が当日ぶん未更新」がこれだった)。
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from common.market_session import (
    last_opened_session,
    last_opened_session_yyyymmdd,
    latest_trading_day_on_or_before,
)

JST = ZoneInfo("Asia/Tokyo")


def test_jst_morning_expects_the_previous_session_not_today():
    """08:00 JST (木) は当日 22:30 の寄り前 -> 直近立会は前営業日 (水)。"""
    now = datetime(2026, 8, 20, 8, 0, tzinfo=JST)
    assert last_opened_session(now) == date(2026, 8, 19)
    assert last_opened_session_yyyymmdd(now) == 20260819


def test_after_the_open_expects_todays_session():
    """23:00 JST (木) は寄り済み -> 当日セッションを期待する (本物の stale は赤のまま)。"""
    now = datetime(2026, 8, 20, 23, 0, tzinfo=JST)
    assert last_opened_session(now) == date(2026, 8, 20)


def test_boundary_is_the_et_open_not_a_fixed_jst_hour():
    """22:29 JST は寄り前 / 22:31 JST は寄り後 (夏時間の 09:30 ET)。"""
    assert last_opened_session(datetime(2026, 8, 20, 22, 29, tzinfo=JST)) == date(
        2026, 8, 19
    )
    assert last_opened_session(datetime(2026, 8, 20, 22, 31, tzinfo=JST)) == date(
        2026, 8, 20
    )


def test_winter_time_boundary_shifts_with_dst():
    """冬時間の寄りは 09:30 EST = 23:30 JST。固定 JST 時刻だと 1 時間ズレる。"""
    # 2026-12-10 (木) 22:31 JST = 08:31 EST -> まだ寄っていない
    assert last_opened_session(datetime(2026, 12, 10, 22, 31, tzinfo=JST)) == date(
        2026, 12, 9
    )
    # 23:31 JST = 09:31 EST -> 寄り済み
    assert last_opened_session(datetime(2026, 12, 10, 23, 31, tzinfo=JST)) == date(
        2026, 12, 10
    )


def test_monday_morning_reaches_back_over_the_weekend():
    """月曜朝は金曜の立会が直近 (土日は立会無し)。"""
    now = datetime(2026, 8, 17, 8, 0, tzinfo=JST)  # Mon
    assert last_opened_session(now) == date(2026, 8, 14)  # Fri


def test_saturday_morning_is_friday_not_saturday():
    now = datetime(2026, 8, 15, 8, 0, tzinfo=JST)  # Sat
    assert last_opened_session(now) == date(2026, 8, 14)


def test_nyse_holiday_is_skipped():
    """Thanksgiving (2026-11-26 木) は休場 -> その翌朝の直近立会は水曜。"""
    assert latest_trading_day_on_or_before(date(2026, 11, 26)) == date(2026, 11, 25)
    now = datetime(2026, 11, 27, 8, 0, tzinfo=JST)  # 翌朝 (金) JST
    assert last_opened_session(now) == date(2026, 11, 25)


def test_july4_observed_holiday_is_skipped():
    """2026-07-03 (金) は Independence Day 振替休場。"""
    assert latest_trading_day_on_or_before(date(2026, 7, 3)) == date(2026, 7, 2)


def test_trading_day_passthrough_and_naive_now_is_accepted():
    assert latest_trading_day_on_or_before(date(2026, 8, 19)) == date(2026, 8, 19)
    # naive datetime はホストのローカル時刻として扱う (例外を投げない)。
    assert isinstance(last_opened_session(datetime(2026, 8, 20, 8, 0)), date)
    # 引数無し (実時刻) でも立会日を返す。
    assert isinstance(last_opened_session(), date)


def test_utc_input_is_converted_not_assumed_local():
    # 2026-08-19T23:28Z = 19:28 ET (同日) -> 寄り済みなので 08-19。
    assert last_opened_session(
        datetime(2026, 8, 19, 23, 28, tzinfo=timezone.utc)
    ) == date(2026, 8, 19)
