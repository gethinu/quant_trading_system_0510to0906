"""Exit All Positions - Close all current Alpaca positions.

Use this script to clear all positions for a fresh start.
All exits will be executed at market price.

Usage:
    python scripts/exit_all_positions.py --paper --dry-run  # Preview only
    python scripts/exit_all_positions.py --paper             # Execute
"""

from __future__ import annotations

import argparse
from datetime import datetime
import logging
from typing import Any

from common import broker_alpaca as ba
from common.notifier import create_notifier

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def exit_all_positions(paper: bool = True, dry_run: bool = False) -> dict[str, Any]:
    """Exit all current positions.

    Args:
        paper: Use paper trading mode
        dry_run: If True, only preview without executing

    Returns:
        Summary dict with results
    """
    client = ba.get_client(paper=paper)

    try:
        positions = client.get_all_positions()
    except Exception as e:
        logger.error(f"Failed to fetch positions: {e}")
        return {"error": str(e)}

    if not positions:
        logger.info("No positions to exit")
        return {"total": 0, "exited": 0}

    logger.info(f"Found {len(positions)} positions to exit")

    if dry_run:
        print("\n=== DRY RUN - Preview Only ===\n")
        for p in positions:
            symbol = getattr(p, "symbol", "")
            qty = int(getattr(p, "qty", 0) or 0)
            side = str(getattr(p, "side", ""))
            unrealized_pnl = float(getattr(p, "unrealized_pl", 0) or 0)
            print(
                f"  {symbol}: {abs(qty)} shares ({side}) "
                f"PnL: ${unrealized_pnl:+,.2f}"
            )

        total_pnl = sum(float(getattr(p, "unrealized_pl", 0) or 0) for p in positions)
        print(f"\nTotal positions: {len(positions)}")
        print(f"Total unrealized PnL: ${total_pnl:+,.2f}")
        print("\nTo execute, run without --dry-run flag")

        return {
            "total": len(positions),
            "exited": 0,
            "dry_run": True,
            "total_pnl": total_pnl,
        }

    # Execute exits
    exited = []
    failed = []
    total_pnl = 0.0

    for p in positions:
        try:
            symbol = getattr(p, "symbol", "")
            qty = int(getattr(p, "qty", 0) or 0)
            side = str(getattr(p, "side", "")).lower()
            unrealized_pnl = float(getattr(p, "unrealized_pl", 0) or 0)

            if qty == 0:
                continue

            # Determine order side (opposite of position)
            if "long" in side:
                order_side = "sell"
            else:
                order_side = "buy"

            # Submit market order
            from alpaca.trading.enums import OrderSide, TimeInForce
            from alpaca.trading.requests import MarketOrderRequest

            order_request = MarketOrderRequest(
                symbol=symbol,
                qty=abs(qty),
                side=OrderSide.SELL if order_side == "sell" else OrderSide.BUY,
                time_in_force=TimeInForce.DAY,
            )

            client.submit_order(order_request)

            exited.append(
                {
                    "symbol": symbol,
                    "qty": abs(qty),
                    "side": side,
                    "pnl": unrealized_pnl,
                }
            )
            total_pnl += unrealized_pnl

            logger.info(
                f"Exited: {symbol} ({abs(qty)} shares) PnL: ${unrealized_pnl:+,.2f}"
            )

        except Exception as e:
            failed.append({"symbol": symbol, "error": str(e)})
            logger.error(f"Failed to exit {symbol}: {e}")

    logger.info(f"Exit complete: {len(exited)} exited, {len(failed)} failed")

    # Clear position tracker
    try:
        from pathlib import Path

        tracker_path = (
            Path(__file__).resolve().parents[1] / "data" / "position_tracker.json"
        )
        if tracker_path.exists():
            tracker_path.unlink()
            logger.info("Position tracker cleared")
    except Exception as e:
        logger.warning(f"Failed to clear position tracker: {e}")

    # Send notification
    try:
        notifier = create_notifier(platform="slack", fallback=True)

        message = f"""🔄 **全ポジションエグジット完了**

📊 **サマリー**
• エグジット数: {len(exited)}銘柄
• 失敗: {len(failed)}銘柄
• 合計PnL: ${total_pnl:+,.2f}

📍 **実行時刻**
{datetime.now().strftime('%Y-%m-%d %H:%M:%S JST')}

✅ ポジショントラッカーをクリアしました。
次回のシグナル生成から新規エントリーを開始できます。
"""

        notifier.send("🔄 全ポジションエグジット", message, channel=None)
        logger.info("Slack notification sent")

    except Exception as e:
        logger.warning(f"Failed to send notification: {e}")

    return {
        "total": len(positions),
        "exited": len(exited),
        "failed": len(failed),
        "total_pnl": total_pnl,
        "exited_positions": exited,
        "failed_positions": failed,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--paper", action="store_true", default=True)
    parser.add_argument("--live", action="store_true")
    parser.add_argument(
        "--dry-run", action="store_true", help="Preview only, do not execute"
    )
    args = parser.parse_args()

    paper = not args.live

    print("=" * 50)
    print("⚠️  EXIT ALL POSITIONS")
    print("=" * 50)
    print(f"Mode: {'PAPER' if paper else '*** LIVE ***'}")
    print(f"Dry run: {args.dry_run}")
    print("=" * 50)

    if not args.dry_run:
        confirm = input("\nType 'YES' to confirm exit all positions: ")
        if confirm != "YES":
            print("Cancelled.")
            return

    result = exit_all_positions(paper=paper, dry_run=args.dry_run)

    print("\n=== Result ===")
    print(f"Total: {result.get('total', 0)}")
    print(f"Exited: {result.get('exited', 0)}")
    if result.get("failed"):
        print(f"Failed: {result.get('failed', 0)}")


if __name__ == "__main__":
    main()
