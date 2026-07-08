"""Sync Alpaca positions to position tracker.

This script fetches current positions from Alpaca and creates/updates
the position_tracker.json file with entry information.

For existing positions where we don't know the exact entry date,
we use the current date as a placeholder.

Usage:
    python scripts/sync_positions_to_tracker.py [--paper]
"""

from __future__ import annotations

import argparse
from datetime import datetime
import logging
from pathlib import Path
from typing import Any

from common import broker_alpaca as ba
from common.position_tracker import load_tracker, save_tracker

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Default system assignment based on side
# In reality, this should come from order history or signal logs
DEFAULT_SYSTEM_BY_SIDE = {
    "long": "unknown_long",
    "short": "unknown_short",
}


def infer_system_from_position(symbol: str, side: str, avg_price: float) -> str:
    """Attempt to infer system from position characteristics.

    This is a heuristic - in production, you'd query order history
    or signal logs for accurate system info.
    """
    # If we have a signal log, check there
    try:
        signals_path = (
            Path(__file__).resolve().parents[1] / "results_csv" / "today_signals.csv"
        )
        if signals_path.exists():
            import pandas as pd

            df = pd.read_csv(signals_path)
            match = df[df["symbol"].str.upper() == symbol.upper()]
            if not match.empty:
                system = match.iloc[-1].get("system")
                if system:
                    return str(system)
    except Exception:
        pass

    # Fallback to side-based default
    side_lower = str(side).lower()
    if "long" in side_lower:
        return "unknown_long"
    elif "short" in side_lower:
        return "unknown_short"
    return "unknown"


def sync_positions(paper: bool = True) -> dict[str, Any]:
    """Sync current Alpaca positions to position tracker.

    Returns:
        Updated position tracker dict
    """
    client = ba.get_client(paper=paper)

    try:
        positions = client.get_all_positions()
    except Exception as e:
        logger.error(f"Failed to fetch positions: {e}")
        return {}

    logger.info(f"Found {len(positions)} positions to sync")

    # Load existing tracker
    tracker = load_tracker()

    synced = 0
    skipped = 0

    for p in positions:
        try:
            symbol = str(getattr(p, "symbol", "")).upper()
            if not symbol:
                continue

            # If already in tracker with valid system, skip
            if symbol in tracker:
                existing_system = tracker[symbol].get("system", "")
                if existing_system and not existing_system.startswith("unknown"):
                    logger.debug(
                        f"Skipping {symbol}: already tracked with system={existing_system}"
                    )
                    skipped += 1
                    continue

            # Extract position data
            qty = int(getattr(p, "qty", 0) or 0)
            avg_entry = float(getattr(p, "avg_entry_price", 0.0) or 0.0)
            current_price = float(getattr(p, "current_price", 0.0) or 0.0)
            side = str(getattr(p, "side", ""))

            # Infer system
            system = infer_system_from_position(symbol, side, avg_entry)

            # Create tracker entry
            tracker[symbol] = {
                "system": system,
                "entry_date": datetime.now().isoformat(),  # Placeholder
                "entry_price": avg_entry,
                "current_price": current_price,
                "quantity": abs(qty),
                "side": "long" if "long" in side.lower() else "short",
                "profit_target_price": None,
                "synced_from_alpaca": True,
                "last_update": datetime.now().isoformat(),
            }

            synced += 1
            logger.info(f"Synced: {symbol} ({system})")

        except Exception as e:
            logger.error(f"Failed to sync position {symbol}: {e}")

    # Save tracker
    save_tracker(tracker)

    logger.info(
        f"Sync complete: {synced} synced, {skipped} skipped, {len(tracker)} total"
    )

    return tracker


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--paper", action="store_true", default=True)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()

    paper = not args.live
    tracker = sync_positions(paper=paper)

    # Print summary
    print("\n=== Position Tracker Summary ===")
    print(f"Total positions: {len(tracker)}")

    # Group by system
    by_system: dict[str, list[str]] = {}
    for symbol, info in tracker.items():
        system = info.get("system", "unknown")
        if system not in by_system:
            by_system[system] = []
        by_system[system].append(symbol)

    print("\nBy System:")
    for system, symbols in sorted(by_system.items()):
        print(f"  {system}: {len(symbols)} positions")


if __name__ == "__main__":
    main()
