"""Run auto-rule outside Streamlit.

This script loads settings and auto-rule config from the same files used by
`app_alpaca_dashboard.py`, loads current positions using the broker wrapper,
and submits exit orders via `submit_exit_orders_df`.

Usage:
    python scripts/run_auto_rule.py --paper --dry-run

Notes:
- Ensure environment variables (ALPACA keys, SLACK/Discord tokens) are available to the process.
- Recommended to run under the same venv as the app.
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

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
SENT_PATH = DATA_DIR / "alpaca_sent_markers.json"
CONFIG_PATH = DATA_DIR / "auto_rule_config.json"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("run_auto_rule")


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


def build_auto_rows(
    cfg: dict[str, Any], markers: dict[str, Any]
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    # fetch positions via Alpaca client
    client = ba.get_client()
    try:
        positions = client.get_all_positions()
    except Exception:
        logger.exception("failed to fetch positions")
        return rows
    # normalize to list of dicts similar to UI DataFrame
    records: list[dict[str, Any]] = []
    for p in positions:
        try:
            sym = getattr(p, "symbol", None) or getattr(p, "symbol_raw", None)
            qty = int(getattr(p, "qty", 0) or 0)
            avg = float(getattr(p, "avg_entry_price", 0.0) or 0.0)
            cur = float(getattr(p, "market_value", 0.0) or 0.0)
            # approximate pnl pct if price available
            try:
                price = float(getattr(p, "current_price", 0.0) or 0.0)
                pnl_pct = ((price - avg) / avg * 100.0) if avg else 0.0
            except Exception:
                pnl_pct = 0.0
            records.append(
                {
                    "symbol": sym,
                    "数量": qty,
                    "平均取得単価": avg,
                    "現在値": cur,
                    "損益率(%)": pnl_pct,
                    "side": getattr(p, "side", ""),
                }
            )
        except Exception:
            continue

    pos_df = pd.DataFrame(records)
    if pos_df is None or pos_df.empty:
        return rows
    for _, r in pos_df.iterrows():
        try:
            sym = str(r.get("symbol", "")).upper()
            if not sym:
                continue
            system_name = str(r.get("システム", "")).strip() or "unknown"
            c = cfg.get(system_name, {})
            threshold = float(c.get("pnl_threshold", -20.0))
            partial_pct = int(c.get("partial_pct", 100))
            pnl_pct = float(r.get("損益率(%)", 0.0) or 0.0)
            # Note: This flag is likely always False when run from this script.
            limit_reached = bool(r.get("_limit_reached"))
            if limit_reached or pnl_pct <= threshold:
                key = today_key_for(sym)
                if key in markers:
                    logger.info("skip %s already sent today", sym)
                    continue
                qty = int(r.get("数量") or r.get("qty") or 0)
                if qty <= 0:
                    continue
                apply_qty = max(1, int(qty * partial_pct / 100))
                rows.append(
                    {
                        "symbol": sym,
                        "qty": apply_qty,
                        "position_side": r.get("side") or r.get("position_side") or "",
                        "system": system_name,
                        "when": "today_close",
                    }
                )
        except Exception:
            logger.exception("failed to evaluate row")
    return rows


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
    rows = build_auto_rows(cfg, markers)

    # エグジット候補が0件の場合も通知
    if not rows:
        logger.info("no candidates for auto-rule")
        try:
            from common.notifier import create_notifier

            notifier = create_notifier(platform="slack", fallback=True)

            # ポジション数を取得
            try:
                client = ba.get_client(paper=args.paper)
                positions_count = len(client.get_all_positions())
            except Exception:
                positions_count = 0

            message = f"""
📊 **現在のポジション状況**
• 保有銘柄数: {positions_count}銘柄

✅ エグジット条件に該当する銘柄はありませんでした
"""

            notifier.send(
                "🤖 自動エグジット確認完了",
                message,
                channel=None,
            )
            logger.info("No exit candidates - Slack notification sent")
        except Exception:
            logger.exception("notify failed")
        return

    df = pd.DataFrame(rows)
    logger.info("candidates: %s", ", ".join(r["symbol"] for r in rows))

    if args.dry_run:
        logger.info("dry-run enabled, not submitting orders")
        return

    # 実行前のポジション数を記録
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

        # 実行後のポジション数を取得
        try:
            positions_after = len(client.get_all_positions())
        except Exception:
            positions_after = positions_before

        # Slack通知を送信（ポジション変化の詳細付き）
        try:
            from common.notifier import create_notifier

            notifier = create_notifier(platform="slack", fallback=True)

            # エグジット詳細を整形
            exit_details = []
            for r in rows:
                symbol = r["symbol"]
                qty = r["qty"]
                system = r.get("system", "unknown")
                exit_details.append(f"• {symbol} ({system}): {qty}株")

            details_text = "\n".join(exit_details) if exit_details else "なし"

            # ポジション変化のサマリー
            position_change = positions_before - positions_after

            message = f"""
📊 **ポジション変化**
• エグジット前: {positions_before}銘柄
• エグジット後: {positions_after}銘柄
• 減少数: {position_change}銘柄

🔻 **エグジット銘柄（{len(rows)}件）**
{details_text}

✅ 自動エグジット処理が完了しました
"""

            notifier.send(
                "🤖 自動エグジット実行完了",
                message,
                channel=None,  # SLACK_CHANNEL_SIGNALS を使用
            )
            logger.info("Slack notification sent successfully")
        except Exception:
            logger.exception("notify failed")
    except Exception:
        logger.exception("submission failed")


if __name__ == "__main__":
    main()
