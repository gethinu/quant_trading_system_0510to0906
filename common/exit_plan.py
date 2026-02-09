"""Exit plan generation for entry signals.

Builds a simple, deterministic exit plan from entry signals using
SYSTEM_TRADE_RULES. This is intended for daily Discord/Slack MVP flows.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from common.exit_planner import decide_exit_schedule
from common.utils_spy import get_nyse_valid_days
from common.trade_management import SYSTEM_TRADE_RULES
from config.settings import get_settings


def _safe_float(val: Any) -> float | None:
    if val is None:
        return None
    try:
        if isinstance(val, str) and not val.strip():
            return None
        num = float(val)
    except Exception:
        return None
    return num


def _safe_int(val: Any) -> int | None:
    if val is None:
        return None
    try:
        if isinstance(val, str) and not val.strip():
            return None
        return int(float(val))
    except Exception:
        return None


def _safe_date(val: Any) -> pd.Timestamp | None:
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


def _resolve_hold_days(system: str, raw_max: Any = None) -> int | None:
    """Resolve max holding days from row/settings/constants."""
    val = _safe_int(raw_max)
    if val is not None and val > 0:
        return val

    # settings.yaml overrides (strategies.*)
    try:
        settings = get_settings(create_dirs=False)
        strat = settings.strategies.get(system, {}) if settings else {}
        for key in ("max_hold_days", "fallback_exit_after_days", "profit_take_max_days"):
            v = _safe_int(strat.get(key))
            if v is not None and v > 0:
                return v
    except Exception:
        pass

    # constants fallback
    try:
        from strategies.constants import (
            FALLBACK_EXIT_DAYS_DEFAULT,
            MAX_HOLD_DAYS_DEFAULT,
            SYSTEM_SPECIFIC_CONFIG,
        )

        cfg = SYSTEM_SPECIFIC_CONFIG.get(system, {}) if system else {}
        v2 = _safe_int(cfg.get("max_hold_days"))
        if v2 is not None and v2 > 0:
            return v2
        if str(system).lower() == "system5":
            return int(FALLBACK_EXIT_DAYS_DEFAULT)
        if str(system).lower() in {
            "system1",
            "system2",
            "system3",
            "system4",
            "system6",
            "system7",
        }:
            return int(MAX_HOLD_DAYS_DEFAULT)
    except Exception:
        pass
    return None


def _add_trading_days(entry_date: pd.Timestamp, hold_days: int) -> pd.Timestamp | None:
    if hold_days <= 0:
        return None
    base = pd.Timestamp(entry_date).normalize()
    start = base - pd.Timedelta(days=7)
    end = base + pd.Timedelta(days=max(10, hold_days + 5))
    for _ in range(3):
        valid = get_nyse_valid_days(start, end)
        if valid is not None and len(valid) > 0:
            future = valid[valid >= base]
            if len(future) > 0:
                entry_day = future[0]
                try:
                    entry_idx = int(valid.get_indexer([entry_day])[0])
                except Exception:
                    entry_idx = 0
                target_idx = entry_idx + int(hold_days)
                if target_idx < len(valid):
                    try:
                        return pd.Timestamp(valid[target_idx]).normalize()
                    except Exception:
                        return pd.Timestamp(valid[target_idx]).normalize()
        end = end + pd.Timedelta(days=30)
    return base + pd.Timedelta(days=int(hold_days))


def compute_time_exit_date(
    system: str,
    entry_date: Any,
    max_holding_days: Any = None,
) -> tuple[str | None, str | None]:
    """Compute time-based exit date and recommended timing label."""
    entry_ts = _safe_date(entry_date)
    hold_days = _resolve_hold_days(system, max_holding_days)
    if entry_ts is None or hold_days is None or hold_days <= 0:
        return None, None
    exit_ts = _add_trading_days(entry_ts, hold_days)
    if exit_ts is None or pd.isna(exit_ts):
        return None, None
    due, when = decide_exit_schedule(system, exit_ts, exit_ts)
    when_text = when if when else ("today_close" if due else "")
    return exit_ts.strftime("%Y-%m-%d"), when_text


def _pick_atr(row: pd.Series, period: int) -> float | None:
    if period <= 0:
        return None
    key = f"atr{int(period)}"
    for col in (key, key.upper()):
        if col in row:
            val = _safe_float(row.get(col))
            if val is not None:
                return val
    return None


def _compute_target_price(
    *,
    rules,
    row: pd.Series,
    entry_price: float | None,
    side: str,
) -> float | None:
    if entry_price is None or rules.profit_target_type == "none":
        return None
    if rules.profit_target_type == "percentage":
        pct = float(rules.profit_target_value or 0.0) / 100.0
        if pct <= 0:
            return None
        return (
            entry_price * (1.0 + pct)
            if side == "long"
            else entry_price * (1.0 - pct)
        )
    if rules.profit_target_type == "atr":
        atr_period = int(
            rules.profit_target_atr_period or rules.stop_atr_period or 0
        )
        atr_val = _pick_atr(row, atr_period)
        if atr_val is None or atr_val <= 0:
            return None
        return (
            entry_price + atr_val * float(rules.profit_target_value or 0.0)
            if side == "long"
            else entry_price - atr_val * float(rules.profit_target_value or 0.0)
        )
    return None


def build_exit_plan_from_signal_row(row: pd.Series) -> dict[str, Any] | None:
    system = str(row.get("system", "")).lower()
    if not system:
        return None
    rules = SYSTEM_TRADE_RULES.get(system)
    if rules is None:
        return None

    symbol = str(row.get("symbol", "")).upper()
    if not symbol:
        return None

    side = str(row.get("side") or rules.side or "long").lower()
    entry_price = _safe_float(row.get("entry_price"))
    entry_date = _safe_date(row.get("entry_date"))
    if entry_date is None:
        return None

    stop_price = _safe_float(row.get("stop_price"))
    target_price = _compute_target_price(
        rules=rules, row=row, entry_price=entry_price, side=side
    )

    trailing_stop_pct = (
        float(rules.trailing_stop_pct)
        if rules.use_trailing_stop and rules.trailing_stop_pct
        else None
    )

    time_exit_date = None
    time_exit_when = None
    raw_hold_days = row.get("max_holding_days") if row is not None else None
    if raw_hold_days is None and rules.max_holding_days:
        raw_hold_days = rules.max_holding_days
    time_exit_date, time_exit_when = compute_time_exit_date(
        system, entry_date, raw_hold_days
    )

    return {
        "symbol": symbol,
        "system": system,
        "side": side,
        "entry_date": entry_date.strftime("%Y-%m-%d"),
        "entry_price": entry_price,
        "stop_price": stop_price,
        "target_price": target_price,
        "trailing_stop_pct": trailing_stop_pct,
        "time_exit_date": (
            str(time_exit_date) if time_exit_date is not None else ""
        ),
        "time_exit_when": time_exit_when or "",
    }


def build_exit_plan_from_signals(df: pd.DataFrame | None) -> pd.DataFrame:
    if df is None or getattr(df, "empty", True):
        return pd.DataFrame()
    rows: list[dict[str, Any]] = []
    for _idx, row in df.iterrows():
        plan = build_exit_plan_from_signal_row(row)
        if plan:
            rows.append(plan)
    return pd.DataFrame(rows)


def format_exit_plan_from_signal_row(row: pd.Series) -> str:
    plan = build_exit_plan_from_signal_row(row)
    if not plan:
        return ""
    parts: list[str] = []
    stop_price = _safe_float(plan.get("stop_price"))
    if stop_price is not None:
        parts.append(f"stop ${stop_price:.2f}")
    target_price = _safe_float(plan.get("target_price"))
    if target_price is not None:
        parts.append(f"target ${target_price:.2f}")
    trailing = _safe_float(plan.get("trailing_stop_pct"))
    if trailing is not None and trailing > 0:
        parts.append(f"trail {trailing:.0%}")
    time_exit_date = str(plan.get("time_exit_date") or "")
    time_exit_when = str(plan.get("time_exit_when") or "")
    if time_exit_date:
        parts.append(f"time {time_exit_date} {time_exit_when}".strip())
    return " / ".join(parts)


__all__ = [
    "build_exit_plan_from_signals",
    "build_exit_plan_from_signal_row",
    "format_exit_plan_from_signal_row",
    "compute_time_exit_date",
]
