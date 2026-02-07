"""Position Tracker - Save and manage position entry information.

This module provides utilities to track position entries for use in
enhanced auto-exit rules (profit taking, trailing stops, time-based exits).

The position tracker stores:
- System name (system1-7)
- Entry date
- Entry price
- Profit target price (if applicable)
- ATR values for dynamic calculations
"""

from __future__ import annotations

from datetime import datetime
import json
import logging
from pathlib import Path
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)

# Default path for position tracker
DEFAULT_TRACKER_PATH = (
    Path(__file__).resolve().parents[1] / "data" / "position_tracker.json"
)


def load_tracker(path: Path | None = None) -> dict[str, Any]:
    """Load position tracker from JSON file."""
    path = path or DEFAULT_TRACKER_PATH
    try:
        if not path.exists():
            return {}
        with path.open("r", encoding="utf8") as fh:
            return json.load(fh)
    except Exception as e:
        logger.warning(f"Failed to load position tracker: {e}")
        return {}


def save_tracker(tracker: dict[str, Any], path: Path | None = None) -> None:
    """Save position tracker to JSON file."""
    path = path or DEFAULT_TRACKER_PATH
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf8") as fh:
            json.dump(tracker, fh, ensure_ascii=False, indent=2)
        logger.info(f"Position tracker saved: {len(tracker)} positions")
    except Exception as e:
        logger.error(f"Failed to save position tracker: {e}")


def update_position(
    symbol: str,
    system: str,
    entry_date: datetime | pd.Timestamp | str,
    entry_price: float,
    profit_target_price: float | None = None,
    atr10: float | None = None,
    atr20: float | None = None,
    atr40: float | None = None,
    path: Path | None = None,
) -> None:
    """Update single position in tracker.

    Args:
        symbol: Trading symbol
        system: System name (system1-7)
        entry_date: Entry date/time
        entry_price: Entry price
        profit_target_price: Profit target price (optional)
        atr10/20/40: ATR values for different periods
        path: Custom tracker path (optional)
    """
    tracker = load_tracker(path)

    # Convert entry_date to ISO format string
    if isinstance(entry_date, (datetime, pd.Timestamp)):
        entry_date_str = entry_date.isoformat()
    else:
        entry_date_str = str(entry_date)

    tracker[symbol] = {
        "system": system,
        "entry_date": entry_date_str,
        "entry_price": float(entry_price),
        "profit_target_price": (
            float(profit_target_price) if profit_target_price else None
        ),
        "atr10": float(atr10) if atr10 else None,
        "atr20": float(atr20) if atr20 else None,
        "atr40": float(atr40) if atr40 else None,
        "last_update": datetime.now().isoformat(),
    }

    save_tracker(tracker, path)
    logger.info(f"Updated position tracker: {symbol} ({system})")


def update_positions_from_signals(
    signals_df: pd.DataFrame,
    path: Path | None = None,
) -> None:
    """Update multiple positions from signals DataFrame.

    Args:
        signals_df: DataFrame with columns: symbol, system, entry_date, entry_price, etc.
        path: Custom tracker path (optional)
    """
    if signals_df.empty:
        logger.info("No signals to update in position tracker")
        return

    tracker = load_tracker(path)
    updated_count = 0

    def _coerce_float(val: Any) -> float | None:
        if val is None:
            return None
        try:
            if isinstance(val, str) and not val.strip():
                return None
            return float(val)
        except Exception:
            return None

    def _coerce_bool(val: Any) -> bool | None:
        if val is None:
            return None
        if isinstance(val, bool):
            return val
        try:
            s = str(val).strip().lower()
        except Exception:
            return None
        if s in {"1", "true", "yes", "on"}:
            return True
        if s in {"0", "false", "no", "off"}:
            return False
        return None

    def _pick(row: pd.Series, keys: list[str]) -> Any:
        for key in keys:
            try:
                val = row.get(key)
            except Exception:
                val = None
            if val is None or (isinstance(val, float) and pd.isna(val)):
                continue
            if isinstance(val, str) and not val.strip():
                continue
            return val
        return None

    def _normalize_side(raw: Any) -> str | None:
        if raw is None:
            return None
        try:
            val = str(raw).strip().lower()
        except Exception:
            return None
        if not val:
            return None
        if val in {"long", "buy"}:
            return "long"
        if val in {"short", "sell"}:
            return "short"
        return None

    for _, row in signals_df.iterrows():
        try:
            symbol = str(row.get("symbol", "")).upper()
            if not symbol:
                continue

            system = str(row.get("system", "")).lower()
            entry_date = _pick(row, ["entry_date", "signal_date"])
            entry_price = _pick(
                row,
                [
                    "entry_price",
                    "entry_price_final",
                    "entry_order_price",
                    "price",
                ],
            )
            entry_price_val = _coerce_float(entry_price)

            if not all([system, entry_date is not None, entry_price_val]):
                logger.warning(f"Skipping {symbol}: missing required fields")
                continue

            # Convert entry_date
            if isinstance(entry_date, (datetime, pd.Timestamp)):
                entry_date_str = entry_date.isoformat()
            else:
                entry_date_str = str(entry_date)

            # Extract ATR values
            atr10 = row.get("atr10")
            atr20 = row.get("atr20")
            atr40 = row.get("atr40")

            # Optional trade metadata (if present in signals)
            side = _normalize_side(_pick(row, ["side"]))
            if side is None:
                side = _normalize_side(_pick(row, ["signal_type", "action"]))
            stop_price = _coerce_float(_pick(row, ["stop_price", "initial_stop_price"]))
            profit_target_price = _coerce_float(
                _pick(row, ["profit_target_price", "target_price"])
            )
            trailing_stop_pct = _coerce_float(_pick(row, ["trailing_stop_pct"]))
            use_trailing_stop = _coerce_bool(_pick(row, ["use_trailing_stop"]))
            max_holding_days = _pick(row, ["max_holding_days"])
            try:
                max_holding_days = (
                    int(float(max_holding_days))
                    if max_holding_days is not None
                    else None
                )
            except Exception:
                max_holding_days = None
            max_exit_date = _pick(row, ["max_exit_date"])
            if isinstance(max_exit_date, (datetime, pd.Timestamp)):
                max_exit_date_str = max_exit_date.isoformat()
            elif max_exit_date is not None:
                max_exit_date_str = str(max_exit_date)
            else:
                max_exit_date_str = None

            existing = tracker.get(symbol, {})

            tracker[symbol] = {
                **existing,
                "system": system,
                "side": side or existing.get("side"),
                "entry_date": entry_date_str,
                "entry_price": float(entry_price_val),
                "stop_price": stop_price if stop_price is not None else existing.get("stop_price"),
                "profit_target_price": (
                    profit_target_price
                    if profit_target_price is not None
                    else existing.get("profit_target_price")
                ),
                "trailing_stop_pct": (
                    trailing_stop_pct
                    if trailing_stop_pct is not None
                    else existing.get("trailing_stop_pct")
                ),
                "use_trailing_stop": (
                    use_trailing_stop
                    if use_trailing_stop is not None
                    else existing.get("use_trailing_stop")
                ),
                "max_holding_days": (
                    max_holding_days
                    if max_holding_days is not None
                    else existing.get("max_holding_days")
                ),
                "max_exit_date": max_exit_date_str or existing.get("max_exit_date"),
                "atr10": float(atr10) if atr10 and pd.notna(atr10) else existing.get("atr10"),
                "atr20": float(atr20) if atr20 and pd.notna(atr20) else existing.get("atr20"),
                "atr40": float(atr40) if atr40 and pd.notna(atr40) else existing.get("atr40"),
                "last_update": datetime.now().isoformat(),
            }
            updated_count += 1

        except Exception as e:
            logger.error(f"Failed to update position {symbol}: {e}")

    save_tracker(tracker, path)
    logger.info(f"Updated {updated_count} positions in tracker")


def remove_position(symbol: str, path: Path | None = None) -> None:
    """Remove position from tracker (called after exit)."""
    tracker = load_tracker(path)
    if symbol in tracker:
        del tracker[symbol]
        save_tracker(tracker, path)
        logger.info(f"Removed position from tracker: {symbol}")


def remove_positions(symbols: list[str], path: Path | None = None) -> None:
    """Remove multiple positions from tracker."""
    tracker = load_tracker(path)
    removed = []

    for symbol in symbols:
        if symbol in tracker:
            del tracker[symbol]
            removed.append(symbol)

    if removed:
        save_tracker(tracker, path)
        logger.info(f"Removed {len(removed)} positions from tracker")


def get_position_info(symbol: str, path: Path | None = None) -> dict[str, Any] | None:
    """Get position information for a symbol."""
    tracker = load_tracker(path)
    return tracker.get(symbol)


if __name__ == "__main__":
    # Example usage
    logging.basicConfig(level=logging.INFO)

    # Update a single position
    update_position(
        symbol="AAPL",
        system="system1",
        entry_date=datetime.now(),
        entry_price=150.00,
        atr20=5.0,
    )

    # Get position info
    info = get_position_info("AAPL")
    print(f"Position info: {info}")

    # Remove position
    remove_position("AAPL")
