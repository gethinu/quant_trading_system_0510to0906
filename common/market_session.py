"""US 立会セッション基準の日付ユーティリティ (カレンダー当日と混同しないため)。

**なぜ必要か** — この system の実行と観測は JST のカレンダー日ではなく
**US 立会セッション**に紐づいている:

    - signals 生成      : 06:00 JST (前日引け後のデータ)
    - open_auto_run     : 22:35 JST = 09:35 ET (当日セッションの寄り直後)
    - exit 台帳 / snapshot: 上の open_auto_run が書く

したがって「当日ぶんの成果物があるか」を **カレンダー当日 (``date.today()``)**
と比べると、JST の朝 (= まだ当日セッションが寄っていない時刻) は毎日必ず
「未更新」になる。これは監視ではなく毎朝のオオカミ少年で、本物の停止を
埋もれさせる。比較対象は **すでに寄り付きを迎えた直近の立会日** でなければ
ならない。

``last_opened_session()`` がその基準日を返す。カレンダー当日との違い:

    JST 2026-08-20 (木) 08:00  -> 直近立会 = 2026-08-19 (水)  ※当日は 22:30 に寄る
    JST 2026-08-20 (木) 23:00  -> 直近立会 = 2026-08-20 (木)  ※寄り済み
    JST 2026-08-17 (月) 08:00  -> 直近立会 = 2026-08-14 (金)  ※土日は立会無し
    Thanksgiving 翌朝          -> 直近立会 = 祝日の前営業日   ※NYSE 休場を飛ばす

依存は任意 (``pandas_market_calendars`` があれば NYSE の祝日込みで判定し、
無ければ Mon-Fri へフォールバック)。``zoneinfo`` が使えない環境では UTC 基準の
保守的な境界へ落ちる — いずれのフォールバックも **より古いセッションを返す**
方向に倒れるので、偽の赤 (false red) は作らない。
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

__all__ = [
    "NY_TZ",
    "SESSION_OPEN_ET",
    "last_opened_session",
    "last_opened_session_yyyymmdd",
    "latest_trading_day_on_or_before",
]

NY_TZ = "America/New_York"

# US 立会の寄り付き (ET)。JST では夏時間 22:30 / 冬時間 23:30 に相当するため、
# JST の固定時刻ではなく ET で判定する (DST を跨いでもズレない)。
SESSION_OPEN_ET = time(9, 30)

# zoneinfo が無い環境用のフォールバック境界。09:30 EST = 14:30 UTC (冬時間)。
# 夏時間の実際の寄り (13:30 UTC) より遅い側を採るので、寄り直後の 1 時間だけ
# 「まだ寄っていない」と保守的に判断する = 古い方のセッションを返す。
_SESSION_OPEN_UTC_FALLBACK = time(14, 30)


def _ny_now(now: datetime | None = None) -> tuple[date, time, bool]:
    """(ET の日付, ET の時刻, tz 解決できたか) を返す。"""
    base = now if now is not None else datetime.now(timezone.utc)
    if base.tzinfo is None:
        base = base.astimezone()  # naive はホストのローカル時刻とみなす
    try:
        from zoneinfo import ZoneInfo

        ny = base.astimezone(ZoneInfo(NY_TZ))
        return ny.date(), ny.time(), True
    except Exception:
        utc = base.astimezone(timezone.utc)
        return utc.date(), utc.time(), False


def latest_trading_day_on_or_before(day: date) -> date:
    """``day`` 以前で最も新しい NYSE 立会日を返す (``day`` 自身が立会日ならそれ)。

    ``pandas_market_calendars`` があれば祝日を正しく飛ばす。無い/失敗した時は
    Mon-Fri へフォールバックする (祝日は立会日と誤認するが、その場合に返るのは
    「より新しい日」なので、呼び側の ``artifact < expected`` 判定では偽の赤に
    なり得る点に注意 — 実運用ホストには mcal が入っている)。
    """
    try:
        import pandas as pd
        import pandas_market_calendars as mcal

        end = pd.Timestamp(day)
        sched = mcal.get_calendar("NYSE").schedule(
            start_date=end - pd.Timedelta(days=14), end_date=end
        )
        days = pd.to_datetime(sched.index).normalize()
        if len(days):
            return days.max().date()
    except Exception:
        pass

    d = day
    while d.weekday() >= 5:  # 5=Sat, 6=Sun
        d -= timedelta(days=1)
    return d


def last_opened_session(now: datetime | None = None) -> date:
    """**すでに寄り付きを迎えた** 直近の立会日 (ET 日付) を返す。

    「当日セッションが寄る前」に当日の成果物を期待しないための基準日。
    ``now`` は tz-aware を推奨 (naive はホストのローカル時刻として扱う)。
    """
    ny_date, ny_time, tz_ok = _ny_now(now)
    boundary = SESSION_OPEN_ET if tz_ok else _SESSION_OPEN_UTC_FALLBACK
    day = ny_date if ny_time >= boundary else ny_date - timedelta(days=1)
    return latest_trading_day_on_or_before(day)


def last_opened_session_yyyymmdd(now: datetime | None = None) -> int:
    """:func:`last_opened_session` を ``YYYYMMDD`` の int で返す (成果物名と同じ形)。"""
    return int(last_opened_session(now).strftime("%Y%m%d"))
