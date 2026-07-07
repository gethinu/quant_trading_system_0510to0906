"""paper_orders JSON (dry-run 出力) の schema regression test.

daily_pipeline.ps1 の paper_orders step が吐く ``results_csv/paper_orders_YYYYMMDD.json``
の schema を固定化する。dashboard / Vercel 連携先が future に依存する可能性のため、
symbol/side/qty/notional_usd/tier/dry_run の 6 core fields は絶対削除禁止。
"""

from __future__ import annotations

from common.alpaca_trading import (
    TIER_NOTIONAL_USD,
    PreparedOrder,
    signals_json_to_orders,
)


def _sample_signals_json() -> dict:
    """today_signals JSON の縮小 fixture (sys1 のみ 3 銘柄)。"""
    return {
        "version": "1.0",
        "date": "2026-07-01",
        "generated_at": "2026-07-02T21:30:19+09:00",
        "provider": "polygon",
        "systems": {
            "sys1": {
                "signals": [
                    {
                        "symbol": "AAPL",
                        "side": "BUY",
                        "entry_price": 195.5,
                        "weight": 0.5,
                        "rank": 1,
                    },
                    {
                        "symbol": "MSFT",
                        "side": "BUY",
                        "entry_price": 420.0,
                        "weight": 0.3,
                        "rank": 2,
                    },
                    {
                        "symbol": "TSLA",
                        "side": "BUY",
                        "entry_price": 250.0,
                        "weight": 0.2,
                        "rank": 3,
                    },
                ]
            }
        },
    }


REQUIRED_FIELDS = {
    "symbol",
    "side",
    "qty",
    "notional_usd",
    "tier",
    "dry_run",
    "client_order_id",
    "system",
    "order_type",
    "time_in_force",
    "entry_date",
}


def test_dryrun_returns_prepared_orders():
    orders = signals_json_to_orders(_sample_signals_json(), tier="small", dry_run=True)
    assert isinstance(orders, list)
    assert len(orders) == 3
    for o in orders:
        assert isinstance(o, PreparedOrder)


def test_dryrun_output_schema_has_all_required_fields():
    orders = signals_json_to_orders(_sample_signals_json(), tier="small", dry_run=True)
    for o in orders:
        row = o.to_row()
        missing = REQUIRED_FIELDS - set(row.keys())
        assert not missing, f"missing schema fields: {missing}"


def test_dryrun_marks_dry_run_true():
    orders = signals_json_to_orders(_sample_signals_json(), tier="small", dry_run=True)
    for o in orders:
        assert o.dry_run is True
        assert o.order_id is None  # 未発注
        assert o.error is None


def test_tier_notional_allocation():
    """tier=small で total notional が $1000 (± 丸め誤差) に収まる。"""
    orders = signals_json_to_orders(_sample_signals_json(), tier="small", dry_run=True)
    total = sum(o.notional_usd or 0.0 for o in orders)
    assert total == 1000.0
    # weight=0.5/0.3/0.2 → notional=500/300/200
    per = {o.symbol: o.notional_usd for o in orders}
    assert per["AAPL"] == 500.0
    assert per["MSFT"] == 300.0
    assert per["TSLA"] == 200.0


def test_tier_medium_scales_up():
    orders = signals_json_to_orders(_sample_signals_json(), tier="medium", dry_run=True)
    total = sum(o.notional_usd or 0.0 for o in orders)
    assert total == TIER_NOTIONAL_USD["medium"] == 10_000.0


def test_tier_large_scales_up():
    orders = signals_json_to_orders(_sample_signals_json(), tier="large", dry_run=True)
    total = sum(o.notional_usd or 0.0 for o in orders)
    assert total == TIER_NOTIONAL_USD["large"] == 100_000.0


def test_unknown_tier_falls_back_to_small():
    orders = signals_json_to_orders(_sample_signals_json(), tier="giga", dry_run=True)
    total = sum(o.notional_usd or 0.0 for o in orders)
    assert total == TIER_NOTIONAL_USD["small"] == 1_000.0


def test_min_notional_skips_tiny_allocations():
    """weight が極小の signal は min_notional 未達で skip_reason が付く。

    observability fix (2026-07-07): 以前は silent ``continue`` で list から
    消えていたが、silent drop を潰すため skip_reason を付けて残すようになった。
    TINY は返り値に *残る* が skip_reason が付き、BIG は付かないことを検証する。
    """
    data = {
        "date": "2026-07-01",
        "systems": {
            "sys1": {
                "signals": [
                    {"symbol": "BIG", "side": "BUY", "entry_price": 100.0, "weight": 0.999, "rank": 1},
                    {"symbol": "TINY", "side": "BUY", "entry_price": 10.0, "weight": 0.001, "rank": 2},
                ]
            }
        },
    }
    # tier=small ($1000), min_notional=$5 → TINY は $1 で skip_reason 付き、BIG は素通り
    orders = signals_json_to_orders(data, tier="small", dry_run=True, min_notional_usd=5.0)
    by_sym = {o.symbol: o for o in orders}
    assert "BIG" in by_sym
    assert "TINY" in by_sym  # silent drop せず残す
    assert by_sym["BIG"].skip_reason is None
    assert by_sym["TINY"].skip_reason is not None
    assert "below_min_notional" in by_sym["TINY"].skip_reason


def test_client_order_id_is_deterministic():
    """同一 (system, symbol, date) は同じ client_order_id (Alpaca 冪等鍵)。"""
    data = _sample_signals_json()
    orders_a = signals_json_to_orders(data, tier="small", dry_run=True)
    orders_b = signals_json_to_orders(data, tier="small", dry_run=True)
    ids_a = {o.symbol: o.client_order_id for o in orders_a}
    ids_b = {o.symbol: o.client_order_id for o in orders_b}
    assert ids_a == ids_b
    # 形式: system1-AAPL-20260701
    assert ids_a["AAPL"] == "system1-AAPL-20260701"


def test_multi_system_flatten():
    """sys1 + sys2 の signal を両方 flatten し、system 名を正しく付与する。"""
    data = {
        "date": "2026-07-01",
        "systems": {
            "sys1": {"signals": [{"symbol": "A", "side": "BUY", "entry_price": 10.0, "weight": 1.0}]},
            "sys2": {"signals": [{"symbol": "B", "side": "SELL", "entry_price": 20.0, "weight": 1.0}]},
        },
    }
    orders = signals_json_to_orders(data, tier="small", dry_run=True)
    systems = {o.symbol: o.system for o in orders}
    assert systems["A"] == "system1"
    assert systems["B"] == "system2"
    sides = {o.symbol: o.side for o in orders}
    assert sides["A"] == "buy"
    assert sides["B"] == "sell"


def test_empty_signals_returns_empty_list():
    orders = signals_json_to_orders({"date": "2026-07-01", "systems": {}}, tier="small", dry_run=True)
    assert orders == []


def test_no_fractional_uses_integer_qty():
    """prefer_fractional=False は整数株数を計算し 0 になれば skip する。"""
    data = _sample_signals_json()
    # tier=small, weight 0.5/0.3/0.2 → notional 500/300/200 に対して価格 195.5/420/250
    # qty = 500/195.5 ≈ 2 株, 300/420 ≈ 0 株 (skip), 200/250 ≈ 0 株 (skip)
    orders = signals_json_to_orders(
        data, tier="small", dry_run=True, prefer_fractional=False
    )
    symbols = {o.symbol for o in orders}
    assert "AAPL" in symbols
    aapl = next(o for o in orders if o.symbol == "AAPL")
    assert aapl.qty == 2  # int(500/195.5)
