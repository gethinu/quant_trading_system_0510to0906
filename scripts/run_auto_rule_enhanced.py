"""Enhanced Auto-Rule Script with Profit Taking and Trailing Stop Support.

This script extends run_auto_rule.py to include:
- Stop loss (existing functionality)
- Profit taking (percentage/ATR based)
- Trailing stops
- Time-based exits

Usage:
    python scripts/run_auto_rule_enhanced.py --paper --dry-run
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import logging
from pathlib import Path
from typing import Any

import pandas as pd

from common import broker_alpaca as ba
from common.alpaca_order import submit_exit_orders_df
from common.trade_management import SYSTEM_TRADE_RULES

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
SENT_PATH = DATA_DIR / "alpaca_sent_markers.json"
CONFIG_PATH = DATA_DIR / "auto_rule_config.json"
POSITION_TRACKER_PATH = DATA_DIR / "position_tracker.json"
TRAILING_STOPS_PATH = DATA_DIR / "trailing_stops.json"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("run_auto_rule_enhanced")


def load_json(path: Path) -> dict[str, Any]:
    try:
        if not path.exists():
            return {}
        with path.open("r", encoding="utf8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def save_json(path: Path, d: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf8") as fh:
            json.dump(d, fh, ensure_ascii=False, indent=2)
    except Exception:
        logger.exception("failed to save json")


def today_key_for(sym: str) -> str:
    return f"{sym}_today_close_{datetime.now().date().isoformat()}"


def load_sent_markers() -> dict[str, Any]:
    return load_json(SENT_PATH)


def mark_sent(sym: str, markers: dict[str, Any]) -> None:
    markers[today_key_for(sym)] = {"when": datetime.now().isoformat()}


def load_position_tracker() -> dict[str, Any]:
    """Load position tracker which stores entry information."""
    return load_json(POSITION_TRACKER_PATH)


def save_position_tracker(tracker: dict[str, Any]) -> None:
    """Save position tracker."""
    save_json(POSITION_TRACKER_PATH, tracker)


def load_trailing_stops() -> dict[str, Any]:
    """Load trailing stop states."""
    return load_json(TRAILING_STOPS_PATH)


def save_trailing_stops(stops: dict[str, Any]) -> None:
    """Save trailing stop states."""
    save_json(TRAILING_STOPS_PATH, stops)


def build_auto_rows_enhanced(
    cfg: dict[str, Any], markers: dict[str, Any]
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Build exit rows with enhanced exit strategies.

    Returns:
        Tuple of (exit_rows, exit_reason_counts)
    """
    rows: list[dict[str, Any]] = []
    exit_reason_counts = {
        "stop_loss": 0,
        "profit_target": 0,
        "trailing_stop": 0,
        "time_based": 0,
    }

    # Load position tracker and trailing stops
    position_tracker = load_position_tracker()
    trailing_stops = load_trailing_stops()

    # Fetch positions via Alpaca client
    client = ba.get_client()
    try:
        positions = client.get_all_positions()
    except Exception:
        logger.exception("failed to fetch positions")
        return rows, exit_reason_counts

    # Process each position
    for p in positions:
        try:
            sym = getattr(p, "symbol", None) or getattr(p, "symbol_raw", None)
            if not sym:
                continue

            qty = int(getattr(p, "qty", 0) or 0)
            avg_entry = float(getattr(p, "avg_entry_price", 0.0) or 0.0)
            current_price = float(getattr(p, "current_price", 0.0) or 0.0)
            side = getattr(p, "side", "long")

            if qty <= 0 or avg_entry <= 0 or current_price <= 0:
                continue

            # Calculate PnL percentage
            if side == "long":
                pnl_pct = ((current_price - avg_entry) / avg_entry) * 100.0
            else:
                pnl_pct = ((avg_entry - current_price) / avg_entry) * 100.0

            # Get position info from tracker
            pos_info = position_tracker.get(sym, {})
            system_name = pos_info.get("system", "unknown")
            entry_date_str = pos_info.get("entry_date")

            # Get system rules
            rules = SYSTEM_TRADE_RULES.get(system_name.lower())
            if not rules:
                logger.warning(f"No rules found for system {system_name}")
                continue

            # Get configuration
            sys_cfg = cfg.get(system_name, {})
            stop_threshold = float(sys_cfg.get("pnl_threshold", -10.0))
            partial_pct = int(sys_cfg.get("partial_pct", 100))

            # Check exit conditions in order of priority
            exit_reason = None
            exit_price = current_price

            # 1. Check time-based exit
            if rules.max_holding_days > 0 and entry_date_str:
                try:
                    entry_date = datetime.fromisoformat(entry_date_str)
                    days_held = (datetime.now() - entry_date).days
                    if days_held >= rules.max_holding_days:
                        exit_reason = "time_based"
                        logger.info(
                            f"{sym}: Time limit reached ({days_held} days >= {rules.max_holding_days})"
                        )
                except Exception as e:
                    logger.debug(f"Could not parse entry date for {sym}: {e}")

            # 2. Check profit target
            if not exit_reason and rules.profit_target_type != "none":
                if rules.profit_target_type == "percentage":
                    target_pct = rules.profit_target_value
                    if pnl_pct >= target_pct:
                        exit_reason = "profit_target"
                        logger.info(
                            f"{sym}: Profit target reached ({pnl_pct:.2f}% >= {target_pct}%)"
                        )
                elif rules.profit_target_type == "atr":
                    # For ATR-based, we'd need to calculate from market data
                    # Simplified: check if profit target price is set in tracker
                    profit_target_price = pos_info.get("profit_target_price")
                    if profit_target_price:
                        if (
                            side == "long" and current_price >= profit_target_price
                        ) or (side == "short" and current_price <= profit_target_price):
                            exit_reason = "profit_target"
                            logger.info(f"{sym}: ATR profit target reached")

            # 3. Check trailing stop
            if not exit_reason and rules.use_trailing_stop:
                trailing_pct = rules.trailing_stop_pct
                trailing_state = trailing_stops.get(sym, {})

                # Update highest/lowest price
                if side == "long":
                    highest = max(
                        trailing_state.get("highest_price", avg_entry), current_price
                    )
                    trailing_stop_price = highest * (1 - trailing_pct)

                    trailing_stops[sym] = {
                        "system": system_name,
                        "entry_price": avg_entry,
                        "highest_price": highest,
                        "trailing_stop_price": trailing_stop_price,
                        "last_update": datetime.now().isoformat(),
                    }

                    if current_price <= trailing_stop_price:
                        exit_reason = "trailing_stop"
                        exit_price = trailing_stop_price
                        logger.info(
                            f"{sym}: Trailing stop triggered (${current_price:.2f} <= ${trailing_stop_price:.2f})"
                        )
                else:  # short
                    lowest = min(
                        trailing_state.get("lowest_price", avg_entry), current_price
                    )
                    trailing_stop_price = lowest * (1 + trailing_pct)

                    trailing_stops[sym] = {
                        "system": system_name,
                        "entry_price": avg_entry,
                        "lowest_price": lowest,
                        "trailing_stop_price": trailing_stop_price,
                        "last_update": datetime.now().isoformat(),
                    }

                    if current_price >= trailing_stop_price:
                        exit_reason = "trailing_stop"
                        exit_price = trailing_stop_price
                        logger.info(
                            f"{sym}: Trailing stop triggered (${current_price:.2f} >= ${trailing_stop_price:.2f})"
                        )

            # 4. Check stop loss (traditional)
            if not exit_reason and pnl_pct <= stop_threshold:
                exit_reason = "stop_loss"
                logger.info(
                    f"{sym}: Stop loss triggered ({pnl_pct:.2f}% <= {stop_threshold}%)"
                )

            # If any exit condition met, add to rows
            if exit_reason:
                # Check if already sent today
                key = today_key_for(sym)
                if key in markers:
                    logger.info(f"skip {sym} already sent today")
                    continue

                apply_qty = max(1, int(qty * partial_pct / 100))

                rows.append(
                    {
                        "symbol": sym,
                        "qty": apply_qty,
                        "position_side": side,
                        "system": system_name,
                        "when": "today_close",
                        "exit_reason": exit_reason,
                        "pnl_pct": round(pnl_pct, 2),
                        "exit_price": round(exit_price, 2),
                    }
                )

                exit_reason_counts[exit_reason] += 1

        except Exception:
            logger.exception("failed to evaluate position")

    # Save updated trailing stops
    save_trailing_stops(trailing_stops)

    return rows, exit_reason_counts


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--paper", action="store_true", help="use paper trading mode")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="do not submit orders, only simulate",
    )
    args = parser.parse_args()

    cfg = load_json(CONFIG_PATH)
    markers = load_sent_markers()
    rows, exit_counts = build_auto_rows_enhanced(cfg, markers)

    if not rows:
        logger.info("No exit candidates found")
        # Send notification
        try:
            from common.notifier import create_notifier

            notifier = create_notifier(platform="slack", fallback=True)

            client = ba.get_client(paper=args.paper)
            try:
                positions_count = len(client.get_all_positions())
            except Exception:
                positions_count = 0

            message = f"""
📊 **現在のポジション状況**
• 保有銘柄数: {positions_count}銘柄

✅ エグジット条件に該当する銘柄はありませんでした
"""
            notifier.send("🤖 自動エグジット確認完了", message, channel=None)
            logger.info("No exit candidates - Slack notification sent")
        except Exception:
            logger.exception("notify failed")
        return

    df = pd.DataFrame(rows)

    # Log exit breakdown
    logger.info("=== Exit Breakdown ===")
    for reason, count in exit_counts.items():
        if count > 0:
            logger.info(f"  {reason}: {count} positions")
    logger.info(f"Total candidates: {len(rows)}")

    # Log candidates
    candidates_by_reason = {}
    for r in rows:
        reason = r["exit_reason"]
        if reason not in candidates_by_reason:
            candidates_by_reason[reason] = []
        candidates_by_reason[reason].append(r["symbol"])

    for reason, symbols in candidates_by_reason.items():
        logger.info(f"{reason}: {', '.join(symbols)}")

    if args.dry_run:
        logger.info("dry-run enabled, not submitting orders")
        print("\n" + df.to_string())
        return

    # Execute exits
    client = ba.get_client(paper=args.paper)
    try:
        positions_before = len(client.get_all_positions())
    except Exception:
        positions_before = 0

    try:
        res = submit_exit_orders_df(df, paper=args.paper, tif="CLS", notify=False)
        logger.info("submitted %d orders", len(res))

        for r in rows:
            mark_sent(r["symbol"], markers)
        save_json(SENT_PATH, markers)

        # Get positions after
        try:
            positions_after = len(client.get_all_positions())
        except Exception:
            positions_after = positions_before

        # Send Slack notification with breakdown
        try:
            from common.notifier import create_notifier

            notifier = create_notifier(platform="slack", fallback=True)

            # Build exit details by reason
            exit_details_by_reason = {}
            for r in rows:
                reason = r["exit_reason"]
                if reason not in exit_details_by_reason:
                    exit_details_by_reason[reason] = []

                reason_label = {
                    "stop_loss": "損切り",
                    "profit_target": "利食い",
                    "trailing_stop": "トレーリング",
                    "time_based": "時間切れ",
                }.get(reason, reason)

                exit_details_by_reason[reason].append(
                    f"• {r['symbol']} ({r['system']}・{reason_label}): {r['qty']}株 @ ${r['exit_price']} ({r['pnl_pct']:+.1f}%)"
                )

            # Build breakdown section
            breakdown_lines = []
            for reason in ["stop_loss", "profit_target", "trailing_stop", "time_based"]:
                count = exit_counts.get(reason, 0)
                if count > 0:
                    reason_label = {
                        "stop_loss": "損切り",
                        "profit_target": "利食い",
                        "trailing_stop": "トレーリングストップ",
                        "time_based": "時間ベース",
                    }.get(reason)
                    breakdown_lines.append(f"• {reason_label}: {count}銘柄")

            breakdown_text = "\n".join(breakdown_lines) if breakdown_lines else "なし"

            # Build details section
            details_lines = []
            for reason in ["profit_target", "trailing_stop", "stop_loss", "time_based"]:
                if reason in exit_details_by_reason:
                    details_lines.extend(exit_details_by_reason[reason])

            details_text = "\n".join(details_lines) if details_lines else "なし"

            position_change = positions_before - positions_after

            message = f"""
📊 **エグジット種別内訳**
{breakdown_text}

📉 **ポジション変化**
• エグジット前: {positions_before}銘柄
• エグジット後: {positions_after}銘柄
• 減少数: {position_change}銘柄

🔻 **エグジット銘柄（{len(rows)}件）**
{details_text}

✅ 自動エグジット処理が完了しました
"""

            notifier.send("🤖 自動エグジット実行完了", message, channel=None)
            logger.info("Slack notification sent successfully")
        except Exception:
            logger.exception("notify failed")
    except Exception:
        logger.exception("submission failed")


if __name__ == "__main__":
    main()
