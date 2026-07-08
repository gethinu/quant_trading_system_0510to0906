#!/usr/bin/env python3
"""Alpaca API connection test

Alpaca Paper Trading APIへの接続テストを実行します。

Usage:
    python tools/test_alpaca_connection.py
"""

from __future__ import annotations

from pathlib import Path
import sys

# プロジェクトルートをパスに追加
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# ruff: noqa: E402
from config.environment import get_env_config


def main() -> int:
    """Alpaca API接続テスト"""
    print("🔍 Alpaca API Connection Test\n")

    # 環境変数チェック
    env = get_env_config()

    print("📋 Environment Check:")
    key_status = "✅ Set" if env.apca_api_key_id else "❌ Not set"
    print(f"  APCA_API_KEY_ID: {key_status}")
    secret_status = "✅ Set" if env.apca_api_secret_key else "❌ Not set"
    print(f"  APCA_API_SECRET_KEY: {secret_status}")
    print(f"  ALPACA_PAPER: {env.alpaca_paper}")
    print()

    if not env.apca_api_key_id or not env.apca_api_secret_key:
        print("❌ Alpaca API credentials not configured")
        print("   Please add to .env:")
        print("   APCA_API_KEY_ID=your_key_id")
        print("   APCA_API_SECRET_KEY=your_secret_key")
        return 1

    # Alpacaクライアント初期化
    try:
        from common import broker_alpaca as ba

        print("🔌 Connecting to Alpaca API...")
        client = ba.get_client(paper=env.alpaca_paper)
        print("✅ Client initialized successfully\n")
    except Exception as e:
        print(f"❌ Failed to initialize client: {e}")
        return 1

    # アカウント情報取得
    try:
        print("📊 Account Information:")
        account = client.get_account()

        print(f"  Account Number: {account.account_number}")
        print(f"  Status: {account.status}")
        print(f"  Cash: ${float(account.cash):,.2f}")
        print(f"  Buying Power: ${float(account.buying_power):,.2f}")
        print(f"  Portfolio Value: ${float(account.portfolio_value):,.2f}")
        print()

    except Exception as e:
        print(f"❌ Failed to get account info: {e}")
        return 1

    # ポジション確認
    try:
        print("📦 Current Positions:")
        positions = client.get_all_positions()

        if not positions:
            print("  (No positions)")
        else:
            for pos in positions:
                symbol = pos.symbol
                qty = pos.qty
                avg_price = float(pos.avg_entry_price)
                current_price = float(pos.current_price)
                unrealized_pl = float(pos.unrealized_pl)

                print(f"  {symbol}: {qty} shares @ ${avg_price:.2f}")
                print(f"    Current: ${current_price:.2f}")
                print(f"    P/L: ${unrealized_pl:+.2f}")
        print()

    except Exception as e:
        print(f"⚠️ Could not fetch positions: {e}")
        print()

    # 最近の注文確認
    try:
        print("📝 Recent Orders (last 5):")
        orders = client.get_orders(limit=5)

        if not orders:
            print("  (No orders)")
        else:
            for order in orders:
                oid = str(order.id)[:8]
                symbol = order.symbol
                side = order.side
                qty = order.qty
                status = order.status

                print(f"  [{oid}...] {symbol} {side.upper()} {qty} - {status}")
        print()

    except Exception as e:
        print(f"⚠️ Could not fetch orders: {e}")
        print()

    print("✅ All tests passed! Alpaca connection is working.")
    print()
    print("🚀 Next steps:")
    print("  1. Run dry-run: python scripts/daily_paper_trade.py --dry-run")
    print("  2. Execute paper trade: python scripts/daily_paper_trade.py")

    return 0


if __name__ == "__main__":
    sys.exit(main())
