"""signals_json_to_orders の account_equity scale 別 decision matrix テスト (offline)。

signals JSON (Phase 1 pack) → OrderPlan 変換ロジックを scale (1k/10k/100k) で検証する。
実発注は行わない (すべて dry_run=True、live API 呼び出しなし)。
"""

from __future__ import annotations

import pytest

from common.alpaca_trading import (
    resolve_tier,
    signals_json_to_orders,
)


@pytest.fixture
def signals_json() -> dict:
    return {
        "version": "1.0",
        "date": "2026-07-01",
        "systems": {
            "sys1": {
                "signals": [
                    {"symbol": "AAPL", "side": "BUY", "entry_price": 200.0, "weight": 0.18, "rank": 1},
                    {"symbol": "MSFT", "side": "BUY", "entry_price": 500.0, "weight": 0.12, "rank": 2},
                ]
            },
            "sys2": {
                "signals": [
                    {"symbol": "TSLA", "side": "SELL", "entry_price": 250.0, "weight": 0.10, "rank": 1},
                ]
            },
            "sys7": {
                "signals": [
                    {"symbol": "SPY", "side": "SELL", "entry_price": 640.0, "weight": 0.06, "rank": 1},
                ]
            },
        },
    }


# --- tier 判定 ---------------------------------------------------------------
@pytest.mark.parametrize(
    "equity,expected",
    [(999, "small"), (9_999, "small"), (10_000, "medium"), (99_999, "medium"), (100_000, "large"), (1_000_000, "large")],
)
def test_resolve_tier_boundaries(equity, expected):
    assert resolve_tier(equity, "auto") == expected


def test_resolve_tier_explicit_override():
    assert resolve_tier(1_000_000, "small") == "small"
    assert resolve_tier(100, "large") == "large"


# --- small tier: top pick 集中 ----------------------------------------------
def test_small_tier_keeps_only_rank1_per_system(signals_json):
    plan = signals_json_to_orders(signals_json, account_equity=1_000, prefer_fractional=True)
    assert plan.tier == "small"
    syms = {o.symbol for o in plan.orders}
    # sys1 は rank1 の AAPL のみ (MSFT rank2 は落ちる)
    assert "AAPL" in syms
    assert "MSFT" not in syms
    assert syms == {"AAPL", "TSLA", "SPY"}


def test_small_tier_uses_fractional_notional(signals_json):
    plan = signals_json_to_orders(signals_json, account_equity=1_000, prefer_fractional=True)
    aapl = next(o for o in plan.orders if o.symbol == "AAPL")
    assert aapl.fractional is True
    assert aapl.notional == pytest.approx(0.18 * 1_000)  # weight * equity
    assert aapl.order_type == "market"  # fractional は market


# --- medium tier: 全 signals 標準 weight ------------------------------------
def test_medium_tier_keeps_all_signals(signals_json):
    plan = signals_json_to_orders(signals_json, account_equity=50_000, prefer_fractional=True)
    assert plan.tier == "medium"
    syms = {o.symbol for o in plan.orders}
    assert syms == {"AAPL", "MSFT", "TSLA", "SPY"}
    aapl = next(o for o in plan.orders if o.symbol == "AAPL")
    assert aapl.notional == pytest.approx(0.18 * 50_000)


# --- large tier: hedge 強化 --------------------------------------------------
def test_large_tier_boosts_hedge_weight(signals_json):
    plan = signals_json_to_orders(signals_json, account_equity=100_000, prefer_fractional=True)
    assert plan.tier == "large"
    spy = next(o for o in plan.orders if o.symbol == "SPY")
    # sys7 SPY hedge は weight ×1.5 で強化される
    assert spy.notional == pytest.approx(0.06 * 100_000 * 1.5)


# --- whole share 経路 --------------------------------------------------------
def test_whole_share_when_not_fractional(signals_json):
    plan = signals_json_to_orders(
        signals_json, account_equity=100_000, prefer_fractional=False
    )
    aapl = next(o for o in plan.orders if o.symbol == "AAPL")
    assert aapl.fractional is False
    assert aapl.notional is None
    # 0.18 * 100000 / 200 = 90 株
    assert aapl.qty == 90
    # sys2/sys7 は limit system → whole share では limit price 付与
    tsla = next(o for o in plan.orders if o.symbol == "TSLA")
    assert tsla.order_type == "limit"
    assert tsla.limit_price == 250.0


def test_fractionable_map_forces_whole_share(signals_json):
    """fractionable_map で False 指定した銘柄は whole share になる。"""
    plan = signals_json_to_orders(
        signals_json,
        account_equity=100_000,
        prefer_fractional=True,
        fractionable_map={"AAPL": False},
    )
    aapl = next(o for o in plan.orders if o.symbol == "AAPL")
    assert aapl.fractional is False
    assert aapl.qty == 90


# --- min_notional skip -------------------------------------------------------
def test_min_notional_skips_tiny_positions():
    tiny = {
        "date": "2026-07-01",
        "systems": {"sys1": {"signals": [
            {"symbol": "AAPL", "side": "BUY", "entry_price": 200.0, "weight": 0.5, "rank": 1},
            {"symbol": "PENNY", "side": "BUY", "entry_price": 1.0, "weight": 0.001, "rank": 2},
        ]}},
    }
    # equity $1000: PENNY = 0.001*1000 = $1 < min $5 → skip
    plan = signals_json_to_orders(tiny, account_equity=1_000, tier="medium", min_notional_usd=5.0)
    syms = {o.symbol for o in plan.orders}
    assert "PENNY" not in syms
    assert any(s.symbol == "PENNY" for s in plan.skipped)


def test_whole_share_zero_shares_skipped():
    """whole share で notional < 1株価格なら skip。"""
    j = {
        "date": "2026-07-01",
        "systems": {"sys1": {"signals": [
            {"symbol": "BRKA", "side": "BUY", "entry_price": 600_000.0, "weight": 0.1, "rank": 1},
        ]}},
    }
    # 0.1 * 10000 = $1000 < $600k/株 → 0 株 → skip
    plan = signals_json_to_orders(j, account_equity=10_000, prefer_fractional=False)
    assert plan.orders == []
    assert any("0" in s.reason for s in plan.skipped)


# --- preview dict schema -----------------------------------------------------
def test_preview_dict_schema(signals_json):
    plan = signals_json_to_orders(signals_json, account_equity=10_000)
    d = plan.to_preview_dict()
    assert d["date"] == "2026-07-01"
    assert d["account_equity"] == 10_000
    assert d["tier"] == "medium"
    assert isinstance(d["orders"], list) and d["orders"]
    o0 = d["orders"][0]
    assert set(o0) >= {"symbol", "side", "notional_usd", "qty", "fractional", "client_order_id"}
    assert set(d["summary"]) == {"total_notional", "n_orders", "n_skipped", "hedge_notional"}
    # hedge_notional は SPY (sys7) の notional
    assert d["summary"]["hedge_notional"] == pytest.approx(0.06 * 10_000)


def test_client_order_id_deterministic(signals_json):
    plan = signals_json_to_orders(signals_json, account_equity=10_000)
    coids = {o.client_order_id for o in plan.orders}
    assert "sys1_AAPL_20260701" in coids
    assert all(o.client_order_id for o in plan.orders)


def test_empty_systems_returns_empty_plan():
    plan = signals_json_to_orders({"date": "2026-07-01", "systems": {}}, account_equity=10_000)
    assert plan.orders == []
    assert plan.skipped == []
