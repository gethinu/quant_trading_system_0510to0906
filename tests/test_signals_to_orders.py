"""common.alpaca_trading.signals_to_orders の decision matrix テスト (offline)。

final_df 形式のシグナル → PreparedOrder 変換ロジックを検証する。
実発注は行わない (すべて dry_run=True)。
"""

from __future__ import annotations

import pandas as pd
import pytest

from common.alpaca_trading import signals_to_orders


@pytest.fixture
def signals() -> pd.DataFrame:
    return pd.DataFrame(
        [
            # system1 = market, long -> buy
            {"symbol": "AAPL", "system": "system1", "side": "long", "shares": 10, "entry_price": 195.0, "entry_date": "2026-06-30"},
            # system2 = limit, short -> sell (limit_price=entry_price)
            {"symbol": "TSLA", "system": "system2", "side": "short", "shares": 8, "entry_price": 250.0, "entry_date": "2026-06-30"},
            # system3 = limit (前日終値-7% 指値買), long -> buy (docs-alignment 2026-07-03)
            {"symbol": "AMD", "system": "system3", "side": "long", "shares": 5, "entry_price": 140.0, "entry_date": "2026-06-30"},
            # system5 = limit (前日終値-3% 指値買), long -> buy (docs-alignment 2026-07-03)
            {"symbol": "NVDA", "system": "system5", "side": "long", "shares": 4, "entry_price": 120.0, "entry_date": "2026-06-30"},
            # system7 = MARKET (SPY hedge, 翌日寄付成行 per docs/systems/システム7.txt),
            # short -> sell. docs-alignment 2026-07-03 に是正 (旧: limit)。
            {"symbol": "SPY", "system": "system7", "side": "short", "shares": 3, "entry_price": 545.0, "entry_date": "2026-06-30"},
            # shares<=0 -> フィルタされる
            {"symbol": "ZERO", "system": "system1", "side": "long", "shares": 0, "entry_price": 10.0, "entry_date": "2026-06-30"},
        ]
    )


def test_side_and_order_type_mapping(signals):
    orders = signals_to_orders(signals, account_equity=100000.0, dry_run=True)
    by_sym = {o.symbol: o for o in orders}

    assert "ZERO" not in by_sym  # shares<=0 は除外
    assert len(orders) == 5

    assert by_sym["AAPL"].side == "buy"
    assert by_sym["AAPL"].order_type == "market"  # system1 = docs 明記 market open

    assert by_sym["TSLA"].side == "sell"
    assert by_sym["TSLA"].order_type == "limit"  # system2 = docs 明記 limit +4%
    assert by_sym["TSLA"].limit_price == 250.0

    # docs-alignment 2026-07-03: S3/S5 は docs で limit 指定なのに旧 map で market
    # になっていた乖離を是正 (docs/systems/システム3.txt, システム5.txt)。
    assert by_sym["AMD"].side == "buy"
    assert by_sym["AMD"].order_type == "limit"  # system3 = docs 明記 limit -7%
    assert by_sym["AMD"].limit_price == 140.0

    assert by_sym["NVDA"].side == "buy"
    assert by_sym["NVDA"].order_type == "limit"  # system5 = docs 明記 limit -3%
    assert by_sym["NVDA"].limit_price == 120.0

    # docs-alignment 2026-07-03: S7 は docs 明記 market open。旧 map の limit は誤り。
    assert by_sym["SPY"].side == "sell"  # sys7 hedge short
    assert by_sym["SPY"].order_type == "market"  # system7 = docs 明記 翌日寄付成行


def test_qty_matches_shares_column(signals):
    """position sizing は shares 列に委譲され改変されない。"""
    orders = signals_to_orders(signals, account_equity=50000.0, dry_run=True)
    by_sym = {o.symbol: o for o in orders}
    assert by_sym["AAPL"].qty == 10
    assert by_sym["TSLA"].qty == 8
    assert by_sym["AMD"].qty == 5
    assert by_sym["NVDA"].qty == 4
    assert by_sym["SPY"].qty == 3


def test_client_order_id_generated(signals):
    orders = signals_to_orders(signals, account_equity=100000.0, dry_run=True)
    coids = {o.client_order_id for o in orders}
    assert "system1-AAPL-20260630" in coids
    assert all(o.client_order_id for o in orders)


def test_duplicate_signals_deduplicated():
    """(symbol, system, entry_date) 重複は 1 注文に統合。"""
    df = pd.DataFrame(
        [
            {"symbol": "AAPL", "system": "system1", "side": "long", "shares": 10, "entry_date": "2026-06-30"},
            {"symbol": "AAPL", "system": "system1", "side": "long", "shares": 10, "entry_date": "2026-06-30"},
        ]
    )
    orders = signals_to_orders(df, account_equity=100000.0, dry_run=True)
    assert len(orders) == 1


def test_open_position_suppresses_duplicate_buy():
    """既にロング保有中の銘柄は買い増ししない。"""
    df = pd.DataFrame(
        [{"symbol": "AAPL", "system": "system1", "side": "long", "shares": 10, "entry_date": "2026-06-30"}]
    )
    orders = signals_to_orders(
        df, account_equity=100000.0, dry_run=True, open_positions={"AAPL": 10.0}
    )
    assert orders == []


def test_open_position_allows_opposite_side():
    """ショート保有中に long シグナルは (ドテン) 許可される。"""
    df = pd.DataFrame(
        [{"symbol": "AAPL", "system": "system1", "side": "long", "shares": 10, "entry_date": "2026-06-30"}]
    )
    orders = signals_to_orders(
        df, account_equity=100000.0, dry_run=True, open_positions={"AAPL": -5.0}
    )
    assert len(orders) == 1
    assert orders[0].side == "buy"


def test_empty_or_missing_shares_returns_empty():
    assert signals_to_orders(pd.DataFrame(), account_equity=1.0, dry_run=True) == []
    no_shares = pd.DataFrame([{"symbol": "AAPL", "system": "system1", "side": "long"}])
    assert signals_to_orders(no_shares, account_equity=1.0, dry_run=True) == []


def test_limit_without_price_falls_back_to_market():
    """limit システムでも entry_price が無ければ market にフォールバック。"""
    df = pd.DataFrame(
        [{"symbol": "TSLA", "system": "system2", "side": "short", "shares": 5, "entry_date": "2026-06-30"}]
    )
    orders = signals_to_orders(df, account_equity=100000.0, dry_run=True)
    assert orders[0].order_type == "market"
    assert orders[0].limit_price is None
