"""NYSE 立会日 (trading day) 換算の single source of truth。

**なぜ独立モジュールなのか**

``SystemTradeRules.max_holding_days`` (S2=2 / S3=3 / S5=6 / S6=3) は
docs/systems/システム{N}.txt と各 ``strategies/system{N}_strategy.py`` の
``compute_exit`` が示すとおり **立会日 (bar) ベースの spec** である。
backtest 側は ``idx = entry_idx + offset`` と bar を進めるので自動的に立会日だが、
live 側は日付の引き算になるため、暦日で数えると週末・祝日ぶん **spec より早く**
time exit が発火する (金曜エントリーの System2 が月曜に手仕舞う等)。

同じ換算を live (``common/alpaca_trading.compute_holding_days``) と
``common/trade_management`` の ``max_exit_date`` の両方が必要とするが、
``common.alpaca_trading`` は ``common.trade_management`` を import しているので
どちらかに置くと循環参照になる。そのため中立な本 module に集約し、
**定義が 2 つに割れないようにする**。

``pandas_market_calendars`` は ``common/utils_spy`` が既に使っている依存なので
新規追加ではない。import コストを増やさないため lazy import する。
calendar が使えない環境では ``np.busday_count`` 相当 (Mon-Fri、祝日は考慮しない)
→ 暦日、の順に退避する。
"""

from __future__ import annotations

from datetime import date, timedelta
import logging

logger = logging.getLogger(__name__)

_CALENDAR_NAME = "NYSE"


def _nyse_sessions(start: date, end: date) -> list[date] | None:
    """``[start, end]`` の NYSE 立会日 (両端含む)。calendar が引けなければ None。"""
    if end < start:
        return []
    try:
        import pandas as pd
        import pandas_market_calendars as mcal

        sched = mcal.get_calendar(_CALENDAR_NAME).schedule(
            start_date=pd.Timestamp(start), end_date=pd.Timestamp(end)
        )
        return [ts.date() for ts in pd.to_datetime(sched.index).normalize()]
    except Exception:  # noqa: BLE001 - calendar 不在は fallback で扱う
        return None


def trading_days_between(d0: date, d1: date) -> int | None:
    """``(d0, d1]`` の立会日数。calendar が使えなければ None。

    entry 当日は 0 日保有、``d1`` 当日は「経過した」ものとして数える
    (bar 単位の ``entry_idx + offset`` と同じ規約)。
    """
    if d1 <= d0:
        return 0
    sessions = _nyse_sessions(d0, d1)
    if sessions is None:
        return None
    return sum(1 for d in sessions if d0 < d <= d1)


def count_trading_days(d0: date, d1: date) -> int:
    """``(d0, d1]`` の立会日数。calendar → busday → 暦日 の順に退避する。"""
    if d1 <= d0:
        return 0
    exact = trading_days_between(d0, d1)
    if exact is not None:
        return exact
    try:
        import numpy as np

        # calendar 経路と同じ **(d0, d1]** 半開区間に揃える。素の
        # ``busday_count(d0, d1)`` は ``[d0, d1)`` なので、entry 当日が営業日で
        # today が休日 (またはその逆) のときに 1 日ずれる。
        return int(np.busday_count(d0 + timedelta(days=1), d1 + timedelta(days=1)))
    except Exception:  # noqa: BLE001
        # 最終手段: 暦日 (従来挙動)。ここに落ちるのは numpy も calendar も
        # 使えない環境だけ。
        logger.warning(
            "NYSE calendar も numpy も使えないため holding days を暦日で数えます "
            "(%s -> %s)。spec は立会日なので time exit が早まる可能性があります。",
            d0,
            d1,
        )
        return int((d1 - d0).days)


def add_trading_days(d0: date, n: int) -> date:
    """``d0`` から ``n`` 立会日後の日付。calendar → busday → 暦日 の順に退避する。

    ``count_trading_days(d0, add_trading_days(d0, n)) == n`` を満たす
    (= time exit が発火する最初の日) ように、``d0`` より後の立会日を n 個数える。
    """
    if n <= 0:
        return d0
    # 立会日は最悪でも週 5 日。祝日を厚めに見て余裕を持った窓を切る。
    horizon = d0 + timedelta(days=int(n * 7 / 5) + 21)
    sessions = _nyse_sessions(d0 + timedelta(days=1), horizon)
    if sessions is not None and len(sessions) >= n:
        return sessions[n - 1]
    try:
        import numpy as np

        # ``roll="backward"`` で「d0 より **後** の n 番目の営業日」になる
        # (d0 が土日なら直前の金曜を起点に数えるので月曜が 1 日目)。
        return np.busday_offset(d0, n, roll="backward").astype(date)
    except Exception:  # noqa: BLE001
        logger.warning(
            "NYSE calendar も numpy も使えないため max_exit_date を暦日で計算します "
            "(%s + %d)。",
            d0,
            n,
        )
        return d0 + timedelta(days=n)


__all__ = ["add_trading_days", "count_trading_days", "trading_days_between"]
