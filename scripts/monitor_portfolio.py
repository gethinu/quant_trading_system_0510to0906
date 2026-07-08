"""Portfolio Monitor - Real-time PnL Alert System.

This script monitors portfolio value and sends alerts when significant
changes occur, such as:
- Rapid PnL drops (circuit breaker)
- Large drawdowns
- Unusual volatility
"""

from __future__ import annotations

import argparse
from datetime import datetime
import logging
from pathlib import Path
import sys

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv

from common import broker_alpaca as ba
from common.notifier import Notifier

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# Alert thresholds
DEFAULT_DROP_THRESHOLD_PCT = 3.0  # Alert if drop > 3%
DEFAULT_GAIN_THRESHOLD_PCT = 5.0  # Alert if gain > 5%
DRAWDOWN_THRESHOLD_PCT = 5.0  # Alert if drawdown > 5%

# State file for tracking
STATE_FILE = Path(__file__).parent.parent / "data" / "pnl_monitor_state.json"


def load_state() -> dict:
    """Load previous state from JSON file."""
    import json

    try:
        if STATE_FILE.exists():
            with STATE_FILE.open("r", encoding="utf8") as fh:
                return json.load(fh)
    except Exception as e:
        logger.warning(f"Failed to load state: {e}")
    return {}


def save_state(state: dict) -> None:
    """Save state to JSON file."""
    import json

    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with STATE_FILE.open("w", encoding="utf8") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"Failed to save state: {e}")


def get_portfolio_value(paper: bool = True) -> tuple[float, float, float]:
    """Get current portfolio value, equity, and cash.

    Returns:
        Tuple of (total_equity, cash, unrealized_pnl)
    """
    try:
        client = ba.get_client(paper=paper)
        account = client.get_account()
        equity = float(account.equity)
        cash = float(account.cash)
        unrealized_pnl = float(account.unrealized_pl or 0)
        return equity, cash, unrealized_pnl
    except Exception as e:
        logger.error(f"Failed to get portfolio value: {e}")
        raise


def get_top_movers(paper: bool = True, limit: int = 5) -> tuple[list, list]:
    """Get top gainers and losers.

    Returns:
        Tuple of (gainers, losers) where each is a list of
        (symbol, pnl_percent) tuples
    """
    try:
        client = ba.get_client(paper=paper)
        positions = client.get_all_positions()

        movers = []
        for pos in positions:
            try:
                symbol = pos.symbol
                change_pct = float(pos.unrealized_plpc or 0) * 100
                movers.append((symbol, change_pct))
            except Exception:
                continue

        # Sort by change percentage
        movers.sort(key=lambda x: x[1], reverse=True)

        gainers = movers[:limit]
        losers = movers[-limit:][::-1]

        return gainers, losers
    except Exception as e:
        logger.error(f"Failed to get movers: {e}")
        return [], []


def format_alert_message(
    alert_type: str,
    equity: float,
    change_pct: float,
    prev_equity: float | None,
    gainers: list,
    losers: list,
) -> str:
    """Format alert message for Slack."""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if alert_type == "drop":
        emoji = "🚨"
        title = f"PnLアラート: {change_pct:+.2f}% 急落"
    elif alert_type == "gain":
        emoji = "📈"
        title = f"PnLアラート: {change_pct:+.2f}% 急騰"
    elif alert_type == "drawdown":
        emoji = "⚠️"
        title = f"ドローダウン警告: {abs(change_pct):.2f}%"
    else:
        emoji = "📊"
        title = "ポートフォリオ更新"

    lines = [
        f"{emoji} **{title}**",
        f"発生時刻: {now_str}",
        "",
        f"💰 **現在資産**: ${equity:,.2f}",
    ]

    if prev_equity:
        change_usd = equity - prev_equity
        lines.append(f"📉 **変動額**: ${change_usd:+,.2f}")

    if gainers:
        lines.append("")
        lines.append("🔺 **Top Gainers**")
        for sym, pct in gainers[:3]:
            lines.append(f"• {sym}: {pct:+.2f}%")

    if losers:
        lines.append("")
        lines.append("🔻 **Top Losers**")
        for sym, pct in losers[:3]:
            lines.append(f"• {sym}: {pct:+.2f}%")

    return "\n".join(lines)


def check_and_alert(
    paper: bool = True,
    drop_threshold: float = DEFAULT_DROP_THRESHOLD_PCT,
    gain_threshold: float = DEFAULT_GAIN_THRESHOLD_PCT,
    force_notify: bool = False,
) -> dict:
    """Check portfolio and send alerts if thresholds exceeded.

    Returns:
        Current state dict
    """
    state = load_state()
    prev_equity = state.get("last_equity")
    high_watermark = state.get("high_watermark", 0)

    # Get current values
    equity, cash, unrealized_pnl = get_portfolio_value(paper=paper)
    gainers, losers = get_top_movers(paper=paper)

    # Update high watermark
    if equity > high_watermark:
        high_watermark = equity

    # Calculate changes
    change_pct = 0.0
    drawdown_pct = 0.0

    if prev_equity and prev_equity > 0:
        change_pct = ((equity - prev_equity) / prev_equity) * 100

    if high_watermark > 0:
        drawdown_pct = ((high_watermark - equity) / high_watermark) * 100

    # Determine alert type
    alert_type = None
    should_notify = force_notify

    if change_pct <= -drop_threshold:
        alert_type = "drop"
        should_notify = True
        logger.warning(f"PnL drop detected: {change_pct:.2f}%")

    elif change_pct >= gain_threshold:
        alert_type = "gain"
        should_notify = True
        logger.info(f"PnL gain detected: {change_pct:.2f}%")

    elif drawdown_pct >= DRAWDOWN_THRESHOLD_PCT:
        # Only alert once per hour for drawdown
        last_dd_alert = state.get("last_drawdown_alert", "")
        current_hour = datetime.now().strftime("%Y-%m-%d-%H")
        if last_dd_alert != current_hour:
            alert_type = "drawdown"
            should_notify = True
            state["last_drawdown_alert"] = current_hour
            logger.warning(f"Drawdown alert: {drawdown_pct:.2f}%")

    # Send notification
    if should_notify:
        msg = format_alert_message(
            alert_type or "update",
            equity,
            change_pct if alert_type != "drawdown" else -drawdown_pct,
            prev_equity,
            gainers,
            losers,
        )
        try:
            notifier = Notifier(platform="slack_api")
            notifier.send(msg)
            logger.info("Alert sent to Slack")
        except Exception as e:
            logger.error(f"Failed to send alert: {e}")

    # Update state
    state["last_equity"] = equity
    state["last_check"] = datetime.now().isoformat()
    state["high_watermark"] = high_watermark
    state["unrealized_pnl"] = unrealized_pnl
    save_state(state)

    return state


def main() -> None:
    """Main entry point."""
    parser = argparse.ArgumentParser(description="Portfolio PnL Monitor")
    parser.add_argument(
        "--paper", action="store_true", default=True, help="Use paper trading account"
    )
    parser.add_argument("--live", action="store_true", help="Use live trading account")
    parser.add_argument(
        "--drop-threshold",
        type=float,
        default=DEFAULT_DROP_THRESHOLD_PCT,
        help=f"Drop threshold %% (default: {DEFAULT_DROP_THRESHOLD_PCT})",
    )
    parser.add_argument(
        "--gain-threshold",
        type=float,
        default=DEFAULT_GAIN_THRESHOLD_PCT,
        help=f"Gain threshold %% (default: {DEFAULT_GAIN_THRESHOLD_PCT})",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force send notification regardless of thresholds",
    )
    args = parser.parse_args()

    paper = not args.live

    print("=" * 50)
    print("📊 Portfolio PnL Monitor")
    print("=" * 50)
    print(f"Mode: {'Paper' if paper else 'LIVE'}")
    print(f"Drop threshold: {args.drop_threshold}%")
    print(f"Gain threshold: {args.gain_threshold}%")
    print()

    try:
        state = check_and_alert(
            paper=paper,
            drop_threshold=args.drop_threshold,
            gain_threshold=args.gain_threshold,
            force_notify=args.force,
        )
        print("✅ Check complete")
        print(f"   Equity: ${state.get('last_equity', 0):,.2f}")
        print(f"   High Watermark: ${state.get('high_watermark', 0):,.2f}")
        print(f"   Unrealized PnL: ${state.get('unrealized_pnl', 0):,.2f}")
    except Exception as e:
        logger.error(f"Monitor failed: {e}")
        raise


if __name__ == "__main__":
    main()
