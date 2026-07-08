"""Monthly Detailed Report - Full performance analysis with Excel export.

Scheduled to run on the 1st of each month at 09:00 JST.
Generates detailed Excel/CSV reports with:
- Account history
- Trade history
- System performance breakdown
- PnL analysis

Usage:
    python scripts/monthly_detailed_report.py [--paper]
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta
import json
import logging
from pathlib import Path

import pandas as pd

from common import broker_alpaca as ba
from common.notifier import create_notifier

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
RESULTS_DIR = ROOT / "results_csv"
REPORTS_DIR = ROOT / "reports"


def ensure_reports_dir():
    """Ensure reports directory exists."""
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)


def get_order_history(paper: bool = True) -> pd.DataFrame:
    """Get order history from Alpaca."""
    try:
        client = ba.get_client(paper=paper)

        # Get orders from the past month
        end_date = datetime.now()
        start_date = end_date - timedelta(days=31)

        orders = client.get_orders(
            status="all",
            after=start_date.isoformat(),
            until=end_date.isoformat(),
            limit=500,
        )

        records = []
        for order in orders:
            records.append(
                {
                    "order_id": getattr(order, "id", ""),
                    "symbol": getattr(order, "symbol", ""),
                    "side": str(getattr(order, "side", "")),
                    "qty": int(getattr(order, "qty", 0) or 0),
                    "filled_qty": int(getattr(order, "filled_qty", 0) or 0),
                    "order_type": str(getattr(order, "order_type", "")),
                    "status": str(getattr(order, "status", "")),
                    "submitted_at": str(getattr(order, "submitted_at", "")),
                    "filled_at": str(getattr(order, "filled_at", "")),
                    "filled_avg_price": float(
                        getattr(order, "filled_avg_price", 0) or 0
                    ),
                }
            )

        return pd.DataFrame(records)
    except Exception as e:
        logger.error(f"Failed to get order history: {e}")
        return pd.DataFrame()


def get_portfolio_history(paper: bool = True) -> pd.DataFrame:
    """Get portfolio history from Alpaca."""
    try:
        client = ba.get_client(paper=paper)

        # Get portfolio history for the past month
        history = client.get_portfolio_history(
            period="1M",
            timeframe="1D",
        )

        if history is None:
            return pd.DataFrame()

        timestamps = getattr(history, "timestamp", [])
        equity = getattr(history, "equity", [])
        profit_loss = getattr(history, "profit_loss", [])
        profit_loss_pct = getattr(history, "profit_loss_pct", [])

        records = []
        for i, ts in enumerate(timestamps):
            records.append(
                {
                    "date": datetime.fromtimestamp(ts).strftime("%Y-%m-%d"),
                    "equity": equity[i] if i < len(equity) else 0,
                    "profit_loss": profit_loss[i] if i < len(profit_loss) else 0,
                    "profit_loss_pct": (
                        profit_loss_pct[i] * 100 if i < len(profit_loss_pct) else 0
                    ),
                }
            )

        return pd.DataFrame(records)
    except Exception as e:
        logger.error(f"Failed to get portfolio history: {e}")
        return pd.DataFrame()


def get_trade_log() -> pd.DataFrame:
    """Get trade log from sent markers."""
    try:
        sent_path = DATA_DIR / "alpaca_sent_markers.json"
        if not sent_path.exists():
            return pd.DataFrame()

        with sent_path.open("r", encoding="utf8") as f:
            markers = json.load(f)

        records = []
        for key, value in markers.items():
            symbol = key.split("_")[0]
            when = value.get("when", "")
            records.append(
                {
                    "symbol": symbol,
                    "exit_date": when,
                    "marker_key": key,
                }
            )

        return pd.DataFrame(records)
    except Exception as e:
        logger.error(f"Failed to get trade log: {e}")
        return pd.DataFrame()


def generate_monthly_report(paper: bool = True) -> dict[str, Path]:
    """Generate monthly detailed report files."""
    ensure_reports_dir()

    month_str = datetime.now().strftime("%Y%m")
    report_files = {}

    # 1. Portfolio History
    portfolio_df = get_portfolio_history(paper)
    if not portfolio_df.empty:
        filepath = REPORTS_DIR / f"portfolio_history_{month_str}.csv"
        portfolio_df.to_csv(filepath, index=False, encoding="utf-8-sig")
        report_files["portfolio_history"] = filepath
        logger.info(f"Saved: {filepath}")

    # 2. Order History
    orders_df = get_order_history(paper)
    if not orders_df.empty:
        filepath = REPORTS_DIR / f"order_history_{month_str}.csv"
        orders_df.to_csv(filepath, index=False, encoding="utf-8-sig")
        report_files["order_history"] = filepath
        logger.info(f"Saved: {filepath}")

    # 3. Trade Log
    trade_log_df = get_trade_log()
    if not trade_log_df.empty:
        filepath = REPORTS_DIR / f"trade_log_{month_str}.csv"
        trade_log_df.to_csv(filepath, index=False, encoding="utf-8-sig")
        report_files["trade_log"] = filepath
        logger.info(f"Saved: {filepath}")

    # 4. Summary Statistics
    summary_data = []

    if not portfolio_df.empty:
        summary_data.append(
            {
                "metric": "期間開始残高",
                "value": portfolio_df.iloc[0]["equity"] if len(portfolio_df) > 0 else 0,
            }
        )
        summary_data.append(
            {
                "metric": "期間終了残高",
                "value": (
                    portfolio_df.iloc[-1]["equity"] if len(portfolio_df) > 0 else 0
                ),
            }
        )
        total_pnl = portfolio_df["profit_loss"].sum()
        summary_data.append(
            {
                "metric": "期間合計PnL",
                "value": total_pnl,
            }
        )

    if not orders_df.empty:
        filled = orders_df[orders_df["status"].str.contains("filled", case=False)]
        summary_data.append(
            {
                "metric": "約定注文数",
                "value": len(filled),
            }
        )
        buy_orders = filled[filled["side"].str.contains("buy", case=False)]
        sell_orders = filled[filled["side"].str.contains("sell", case=False)]
        summary_data.append(
            {
                "metric": "買い注文数",
                "value": len(buy_orders),
            }
        )
        summary_data.append(
            {
                "metric": "売り注文数",
                "value": len(sell_orders),
            }
        )

    if summary_data:
        summary_df = pd.DataFrame(summary_data)
        filepath = REPORTS_DIR / f"monthly_summary_{month_str}.csv"
        summary_df.to_csv(filepath, index=False, encoding="utf-8-sig")
        report_files["summary"] = filepath
        logger.info(f"Saved: {filepath}")

    # 5. Combined Excel (if openpyxl is available)
    try:
        excel_path = REPORTS_DIR / f"monthly_report_{month_str}.xlsx"
        with pd.ExcelWriter(excel_path, engine="openpyxl") as writer:
            if not portfolio_df.empty:
                portfolio_df.to_excel(
                    writer, sheet_name="Portfolio History", index=False
                )
            if not orders_df.empty:
                orders_df.to_excel(writer, sheet_name="Order History", index=False)
            if not trade_log_df.empty:
                trade_log_df.to_excel(writer, sheet_name="Trade Log", index=False)
            if summary_data:
                pd.DataFrame(summary_data).to_excel(
                    writer, sheet_name="Summary", index=False
                )
        report_files["excel"] = excel_path
        logger.info(f"Saved Excel: {excel_path}")
    except ImportError:
        logger.warning("openpyxl not installed, skipping Excel export")
    except Exception as e:
        logger.warning(f"Failed to create Excel: {e}")

    return report_files


def send_notification(report_files: dict[str, Path], paper: bool = True) -> None:
    """Send Slack notification with report summary and Google Drive links."""
    try:
        client = ba.get_client(paper=paper)
        account = client.get_account()
        equity = float(getattr(account, "equity", 0) or 0)
    except Exception:
        equity = 0

    month_str = datetime.now().strftime("%Y年%m月")

    # Try to upload to Google Drive
    drive_links = {}

    try:
        import os

        from common.google_drive_uploader import GDRIVE_AVAILABLE, upload_to_drive

        folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")

        if GDRIVE_AVAILABLE and folder_id:
            logger.info("Uploading reports to Google Drive...")

            for name, path in report_files.items():
                try:
                    url = upload_to_drive(path, folder_id=folder_id)
                    drive_links[name] = url
                    logger.info(f"Uploaded {name}: {url}")
                except Exception as e:
                    logger.error(f"Failed to upload {name}: {e}")
        else:
            if not GDRIVE_AVAILABLE:
                logger.warning("Google Drive libraries not available")
            if not folder_id:
                logger.warning("GOOGLE_DRIVE_FOLDER_ID not set")

    except ImportError:
        logger.warning("Google Drive uploader not available")

    # Build notification message
    if drive_links:
        # Google Drive links available
        links_list = "\n".join(
            [
                f"• [{name}]({url})" if url.startswith("http") else f"• {name}: Error"
                for name, url in drive_links.items()
            ]
        )

        message = f"""📊 【月次詳細レポート】{month_str}

💰 **現在の口座残高**
${equity:,.2f}

📁 **Google Driveレポート**
{links_list}

✅ リンクをクリックしてGoogleスプレッドシートで開けます。
"""
    else:
        # Fallback to local file list
        files_list = "\n".join(
            [f"• {name}: {path.name}" for name, path in report_files.items()]
        )

        message = f"""📊 【月次詳細レポート】{month_str}

💰 **現在の口座残高**
${equity:,.2f}

📁 **生成されたレポート**
{files_list}

📂 **保存先**
`reports/` フォルダ

⚠️ Google Drive未設定。セットアップ: docs/GOOGLE_DRIVE_SETUP.md
"""

    print(message)

    try:
        notifier = create_notifier(platform="slack", fallback=True)
        notifier.send("📊 月次詳細レポート生成完了", message, channel=None)
        logger.info("Notification sent")
    except Exception as e:
        logger.error(f"Failed to send notification: {e}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--paper", action="store_true", default=True)
    args = parser.parse_args()

    print("=" * 50)
    print("📊 Monthly Detailed Report Generator")
    print("=" * 50)

    report_files = generate_monthly_report(paper=args.paper)

    print(f"\nGenerated {len(report_files)} report files:")
    for name, path in report_files.items():
        print(f"  • {name}: {path}")

    send_notification(report_files, paper=args.paper)


if __name__ == "__main__":
    main()
