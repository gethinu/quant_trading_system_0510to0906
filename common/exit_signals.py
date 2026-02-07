"""Exit signal evaluation for pseudo-trade position tracking.

This module inspects positions stored in ``data/position_tracker.json`` and
produces exit signals using cached market data (CacheManager via load_price).
It is intended for daily Discord/Slack notifications in MVP workflows.
"""

from __future__ import annotations

from typing import Any, Callable

import pandas as pd

from common.data_loader import load_price
from common.exit_planner import decide_exit_schedule
from common.position_tracker import load_tracker
from common.trade_management import SYSTEM_TRADE_RULES

ExitRows = list[dict[str, Any]]


def _coerce_float(val: Any) -> float | None:
    if val is None:
        return None
    try:
        if isinstance(val, str) and not val.strip():
            return None
        num = float(val)
    except Exception:
        return None
    return num


def _coerce_int(val: Any) -> int | None:
    if val is None:
        return None
    try:
        if isinstance(val, str) and not val.strip():
            return None
        return int(float(val))
    except Exception:
        return None


def _coerce_timestamp(val: Any) -> pd.Timestamp | None:
    if val is None or val == "":
        return None
    try:
        ts = pd.to_datetime(val, errors="coerce")
    except Exception:
        return None
    if ts is None or pd.isna(ts):
        return None
    try:
        if ts.tzinfo is not None:
            ts = ts.tz_convert(None)
    except Exception:
        try:
            ts = ts.tz_localize(None)
        except Exception:
            pass
    return pd.Timestamp(ts).normalize()


def _normalize_price_df(df: pd.DataFrame) -> pd.DataFrame | None:
    if df is None or df.empty:
        return None
    work = df.copy()
    try:
        work.columns = [str(c).lower() for c in work.columns]
    except Exception:
        pass
    if "date" in work.columns:
        work["date"] = pd.to_datetime(work["date"], errors="coerce")
        work = work.dropna(subset=["date"]).sort_values("date")
        work = work.set_index("date")
    else:
        try:
            work.index = pd.to_datetime(work.index, errors="coerce")
            work = work.loc[~work.index.isna()].sort_index()
        except Exception:
            return None
    return work


def _get_row_for_date(df: pd.DataFrame, ts: pd.Timestamp) -> pd.Series | None:
    if df is None or df.empty:
        return None
    idx = df.index
    if ts in idx:
        row = df.loc[ts]
        return row.iloc[0] if isinstance(row, pd.DataFrame) else row
    for method in ("ffill", "pad", "bfill", "backfill", "nearest"):
        try:
            pos = idx.get_indexer([ts], method=method)
            if len(pos) and pos[0] >= 0:
                row = df.iloc[int(pos[0])]
                return row.iloc[0] if isinstance(row, pd.DataFrame) else row
        except Exception:
            continue
    return None


def _get_col(row: pd.Series, keys: list[str]) -> float | None:
    for key in keys:
        try:
            val = row.get(key)
        except Exception:
            val = None
        num = _coerce_float(val)
        if num is not None:
            return num
    return None


def _atr_value(row: pd.Series, period: int) -> float | None:
    if period <= 0:
        return None
    key = f"atr{int(period)}"
    return _get_col(row, [key, key.upper()])


def _latest_day(df: pd.DataFrame) -> pd.Timestamp | None:
    if df is None or df.empty:
        return None
    try:
        return pd.Timestamp(df.index[-1]).normalize()
    except Exception:
        return None


def _is_new_70day_high(df: pd.DataFrame) -> bool | None:
    if df is None or df.empty:
        return None
    col = "high" if "high" in df.columns else "close"
    if col not in df.columns:
        return None
    try:
        series = df[col].astype(float)
        if len(series) < 70:
            return None
        rolling_max = series.rolling(window=70).max()
        latest_val = float(series.iloc[-1])
        latest_max = float(rolling_max.iloc[-1])
        return latest_val >= latest_max
    except Exception:
        return None


def _segment_from_entry(df: pd.DataFrame, entry: pd.Timestamp) -> pd.DataFrame:
    try:
        seg = df.loc[df.index >= entry]
        if seg is not None and not seg.empty:
            return seg
    except Exception:
        pass
    return df


def build_exit_signals_from_tracker(
    tracker: dict[str, Any] | None = None,
    *,
    cache_profile: str = "rolling/base/full",
    today: pd.Timestamp | None = None,
    price_loader: Callable[[str, str], pd.DataFrame] | None = None,
    only_due: bool = True,
) -> pd.DataFrame:
    """Return exit signals DataFrame for positions in tracker.

    Each row contains at least: symbol, system, side, exit_reason, exit_price,
    exit_date, and when. When ``only_due`` is True, only exits due as of
    ``today`` are included.
    """

    tracker = tracker or load_tracker()
    if not tracker:
        return pd.DataFrame()

    loader = price_loader or (lambda sym, prof: load_price(sym, prof))
    rows: ExitRows = []

    for symbol, info in tracker.items():
        sym = str(symbol).upper()
        system = str(info.get("system", "")).lower()
        if not system:
            continue
        rules = SYSTEM_TRADE_RULES.get(system)
        if rules is None:
            continue
        side = str(info.get("side") or rules.side or "").lower()
        if side not in {"long", "short"}:
            side = str(rules.side or "long").lower()

        df_raw = loader(sym, cache_profile)
        df = _normalize_price_df(df_raw)
        if df is None or df.empty:
            continue

        latest_day = _latest_day(df)
        if latest_day is None:
            continue
        ref_today = today or latest_day

        # System7 special rule: exit after new 70-day high (short SPY hedge)
        if system == "system7" and side == "short":
            high_check = _is_new_70day_high(df)
            if high_check is True:
                latest_row = _get_row_for_date(df, latest_day)
                exit_price = (
                    _get_col(latest_row, ["close"]) if latest_row is not None else None
                )
                exit_date = ref_today
                entry_date = _coerce_timestamp(info.get("entry_date"))
                entry_date_str = (
                    entry_date.strftime("%Y-%m-%d") if entry_date is not None else ""
                )
                entry_price = _coerce_float(info.get("entry_price"))
                rows.append(
                    {
                        "symbol": sym,
                        "system": system,
                        "side": side,
                        "exit_reason": "new_70day_high",
                        "exit_price": exit_price,
                        "exit_date": exit_date.strftime("%Y-%m-%d"),
                        "when": "tomorrow_open",
                        "entry_date": entry_date_str,
                        "entry_price": entry_price if entry_price is not None else "",
                        "stop_price": "",
                        "profit_target_price": "",
                        "trailing_stop_price": "",
                        "max_exit_date": "",
                    }
                )
            continue

        entry_date = _coerce_timestamp(info.get("entry_date"))
        if entry_date is None:
            continue

        # Skip if entry is after latest available data
        if entry_date > latest_day:
            continue

        entry_row = _get_row_for_date(df, entry_date)
        if entry_row is None:
            continue

        entry_price = _coerce_float(info.get("entry_price"))
        if entry_price is None:
            entry_price = _get_col(entry_row, ["open", "close"])
        if entry_price is None:
            continue

        stop_price = _coerce_float(info.get("stop_price"))
        if stop_price is None:
            atr_val = _atr_value(entry_row, rules.stop_atr_period)
            if atr_val is not None and atr_val > 0:
                if side == "long":
                    stop_price = entry_price - (rules.stop_atr_multiplier * atr_val)
                else:
                    stop_price = entry_price + (rules.stop_atr_multiplier * atr_val)

        profit_target = _coerce_float(info.get("profit_target_price"))
        if profit_target is None and rules.profit_target_type != "none":
            if rules.profit_target_type == "percentage":
                pct = float(rules.profit_target_value or 0.0) / 100.0
                if pct > 0:
                    if side == "long":
                        profit_target = entry_price * (1.0 + pct)
                    else:
                        profit_target = entry_price * (1.0 - pct)
            elif rules.profit_target_type == "atr":
                atr_val = _atr_value(entry_row, rules.profit_target_atr_period)
                if atr_val is not None and atr_val > 0:
                    if side == "long":
                        profit_target = entry_price + (atr_val * rules.profit_target_value)
                    else:
                        profit_target = entry_price - (atr_val * rules.profit_target_value)

        trailing_pct = _coerce_float(info.get("trailing_stop_pct"))
        if trailing_pct is None:
            if rules.use_trailing_stop and rules.trailing_stop_pct:
                trailing_pct = float(rules.trailing_stop_pct)
        trailing_pct = float(trailing_pct or 0.0)

        max_exit_date = _coerce_timestamp(info.get("max_exit_date"))
        max_holding_days = _coerce_int(info.get("max_holding_days"))
        if max_exit_date is None and max_holding_days is None:
            max_holding_days = int(rules.max_holding_days or 0)
        if max_exit_date is None and max_holding_days and max_holding_days > 0:
            max_exit_date = entry_date + pd.Timedelta(days=max_holding_days)

        seg = _segment_from_entry(df, entry_date)
        latest_row = _get_row_for_date(df, latest_day)
        if latest_row is None:
            continue

        latest_close = _get_col(latest_row, ["close"])
        latest_high = _get_col(latest_row, ["high", "close"])
        latest_low = _get_col(latest_row, ["low", "close"])

        triggered: list[str] = []

        # Stop loss check
        if stop_price is not None:
            if side == "long":
                if latest_low is not None and latest_low <= stop_price:
                    triggered.append("stop_loss")
            else:
                if latest_high is not None and latest_high >= stop_price:
                    triggered.append("stop_loss")

        # Profit target check
        if profit_target is not None:
            if side == "long":
                if latest_high is not None and latest_high >= profit_target:
                    triggered.append("profit_target")
            else:
                if latest_low is not None and latest_low <= profit_target:
                    triggered.append("profit_target")

        # Trailing stop check
        trailing_stop_price = None
        if trailing_pct > 0:
            if side == "long":
                high_col = "high" if "high" in seg.columns else "close"
                try:
                    max_high = float(seg[high_col].max())
                except Exception:
                    max_high = None
                if max_high is not None and max_high > 0:
                    trailing_stop_price = max_high * (1.0 - trailing_pct)
                    if latest_low is not None and latest_low <= trailing_stop_price:
                        triggered.append("trailing_stop")
            else:
                low_col = "low" if "low" in seg.columns else "close"
                try:
                    min_low = float(seg[low_col].min())
                except Exception:
                    min_low = None
                if min_low is not None and min_low > 0:
                    trailing_stop_price = min_low * (1.0 + trailing_pct)
                    if latest_high is not None and latest_high >= trailing_stop_price:
                        triggered.append("trailing_stop")

        # Time-based exit check
        if max_exit_date is not None and ref_today >= max_exit_date:
            triggered.append("time_based")

        if not triggered:
            continue

        # Priority: stop > profit > trailing > time_based
        priority = ["stop_loss", "profit_target", "trailing_stop", "time_based"]
        reason = next((r for r in priority if r in triggered), triggered[0])

        if reason == "stop_loss":
            exit_price = stop_price
            exit_date = ref_today
        elif reason == "profit_target":
            exit_price = profit_target
            exit_date = ref_today
        elif reason == "trailing_stop":
            exit_price = trailing_stop_price
            exit_date = ref_today
        else:
            exit_price = latest_close
            exit_date = max_exit_date or ref_today

        exit_date = _coerce_timestamp(exit_date) or ref_today
        due, when = decide_exit_schedule(system, exit_date, ref_today)
        if only_due and not due:
            continue

        rows.append(
            {
                "symbol": sym,
                "system": system,
                "side": side,
                "exit_reason": reason,
                "exit_price": exit_price,
                "exit_date": exit_date.strftime("%Y-%m-%d"),
                "when": when,
                "entry_date": entry_date.strftime("%Y-%m-%d"),
                "entry_price": entry_price,
                "stop_price": stop_price,
                "profit_target_price": profit_target,
                "trailing_stop_price": trailing_stop_price,
                "max_exit_date": max_exit_date.strftime("%Y-%m-%d")
                if max_exit_date is not None
                else "",
            }
        )

    return pd.DataFrame(rows)


__all__ = ["build_exit_signals_from_tracker"]
