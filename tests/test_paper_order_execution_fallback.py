"""Regression: paper 発注の整数株フォールバック + pre-submit バリデータ。

2026-07-04 の実 paper 発注で判明した 3 失敗クラスを固定する:
  1. fractional-short  : 空売りは notional 不可 → 整数株 qty へ自動フォールバック
  2. 非 fractionable   : notional 不可 → 整数株 qty、サイズ不能なら skip (理由付き)
  3. wash-trade        : 反対側の既存 order がある銘柄は skip (既存注文は保持)

さらに「silent drop しない」= skip/fail は必ず skip_reason/error を持つことを固定。
"""

from __future__ import annotations

import pytest

import common.broker_alpaca as ba
from common.alpaca_trading import (
    EXEC_NOTIONAL,
    EXEC_QTY,
    EXEC_SKIP,
    PreparedOrder,
    plan_order_execution,
    signals_json_to_orders,
)


# --------------------------------------------------------------------------
# 1. pure classifier — 全分岐
# --------------------------------------------------------------------------
def test_long_fractionable_uses_notional():
    mode, qty, notional, _ = plan_order_execution(
        side="buy", notional_usd=30.0, price=20.0, fractionable=True
    )
    assert mode == EXEC_NOTIONAL and notional == 30.0


def test_short_falls_back_to_whole_shares_even_if_fractionable():
    # 空売りは fractionable でも notional 不可 → 整数株
    mode, qty, _, reason = plan_order_execution(
        side="sell", notional_usd=30.0, price=10.0, fractionable=True
    )
    assert mode == EXEC_QTY and qty == 3
    assert "short" in reason


def test_non_fractionable_long_falls_back_to_whole_shares():
    mode, qty, _, reason = plan_order_execution(
        side="buy", notional_usd=30.0, price=10.0, fractionable=False
    )
    assert mode == EXEC_QTY and qty == 3
    assert "non_fractionable" in reason


def test_non_fractionable_below_one_share_is_skipped_with_reason():
    mode, qty, _, reason = plan_order_execution(
        side="buy", notional_usd=5.8, price=44.7, fractionable=False
    )
    assert mode == EXEC_SKIP and qty == 0
    assert "below_1_share" in reason  # silent drop ではなく理由が付く


def test_short_below_one_share_is_skipped():
    mode, _, _, reason = plan_order_execution(
        side="sell", notional_usd=4.0, price=10.0, fractionable=True
    )
    assert mode == EXEC_SKIP and "short" in reason


def test_no_price_cannot_size_whole_shares():
    mode, _, _, reason = plan_order_execution(
        side="buy", notional_usd=30.0, price=0.0, fractionable=False
    )
    assert mode == EXEC_SKIP and "no_positive_price" in reason


def test_unknown_fractionable_is_conservative_whole_shares():
    # fractionable 不明 (asset 照会失敗) は保守的に整数株へ
    mode, qty, _, reason = plan_order_execution(
        side="buy", notional_usd=30.0, price=20.0, fractionable=None
    )
    assert mode == EXEC_QTY and qty == 1
    assert "unknown" in reason


def test_prefer_fractional_false_forces_qty():
    mode, qty, _, _ = plan_order_execution(
        side="buy", notional_usd=30.0, price=10.0, fractionable=True,
        prefer_fractional=False,
    )
    assert mode == EXEC_QTY and qty == 3


# --------------------------------------------------------------------------
# 2. integration — signals_json_to_orders が 3 クラスを正しく分岐/skip する
# --------------------------------------------------------------------------
class _FakeAsset:
    def __init__(self, fractionable: bool):
        self.fractionable = fractionable


class _FakeOrder:
    def __init__(self, oid, status="accepted", symbol=None, side=None, coid=None):
        self.id = oid
        self.status = status
        self.symbol = symbol
        self.side = side
        self.client_order_id = coid


class _FakeClient:
    def __init__(self, fractionable_map, open_orders):
        self._frac = fractionable_map
        self._open = open_orders
        self.notional_calls: list = []

    def get_asset(self, sym):
        if sym not in self._frac:
            raise RuntimeError(f"asset {sym} not found")
        return _FakeAsset(self._frac[sym])

    def get_orders(self, filter=None):  # noqa: A002 (mirror alpaca API kw)
        return self._open

    def submit_order(self, order_data=None):
        self.notional_calls.append(order_data)
        return _FakeOrder(f"nid-{order_data.symbol}", "accepted")


@pytest.fixture
def paper_env(monkeypatch):
    monkeypatch.setenv("ALPACA_PAPER", "true")
    monkeypatch.setenv("ALPACA_API_BASE_URL", "https://paper-api.alpaca.markets")


def _signals_json():
    # weight 均等 → tier small ($1000) / 5 signals = $200 ずつ。
    # 実 JSON のキーは "sys1".."sys7" 形式 (_flatten_json_signals が "system1" に正規化)
    return {
        "date": "2026-07-02",
        "systems": {
            "sys1": {
                "signals": [
                    {"symbol": "AAPL", "side": "buy", "entry_price": 200.0, "weight": 1.0},   # frac long -> notional
                    {"symbol": "PENNY", "side": "buy", "entry_price": 10.0, "weight": 1.0},    # non-frac long -> qty
                    {"symbol": "BIGNF", "side": "buy", "entry_price": 500.0, "weight": 1.0},   # non-frac, $200<$500 -> skip
                    {"symbol": "WASHY", "side": "buy", "entry_price": 20.0, "weight": 1.0},    # wash (既存 sell) -> skip
                ]
            },
            "sys2": {
                "signals": [
                    {"symbol": "SHORTX", "side": "sell", "entry_price": 10.0, "weight": 1.0},  # short -> qty
                ]
            },
        },
    }


def test_integration_routes_all_three_classes(monkeypatch, paper_env):
    submitted_qty: list = []

    def fake_retry(client, symbol, qty, **kw):
        submitted_qty.append((symbol, qty, kw.get("side")))
        return _FakeOrder(f"qid-{symbol}", "accepted")

    monkeypatch.setattr(ba, "submit_order_with_retry", fake_retry)

    client = _FakeClient(
        fractionable_map={
            "AAPL": True, "PENNY": False, "BIGNF": False, "WASHY": True, "SHORTX": True,
        },
        open_orders=[_FakeOrder("x", symbol="WASHY", side="sell", coid="user-exit-1")],
    )

    orders = signals_json_to_orders(
        _signals_json(), tier="small", dry_run=False, client=client, min_notional_usd=5.0,
    )
    by_sym = {o.symbol: o for o in orders}

    # frac long → notional 発注
    assert by_sym["AAPL"].exec_mode == EXEC_NOTIONAL
    assert by_sym["AAPL"].order_id and by_sym["AAPL"].order_id.startswith("nid-")

    # 非frac long → 整数株 qty
    assert by_sym["PENNY"].exec_mode == EXEC_QTY
    assert ("PENNY", 20, "buy") in submitted_qty  # $200/$10 = 20 株

    # short → 整数株 qty (空売りは notional 不可)
    assert by_sym["SHORTX"].exec_mode == EXEC_QTY
    assert ("SHORTX", 20, "sell") in submitted_qty

    # 非frac で 1 株に満たない → skip (理由付き, silent drop 禁止)
    assert by_sym["BIGNF"].order_id is None
    assert by_sym["BIGNF"].skip_reason and "below_1_share" in by_sym["BIGNF"].skip_reason

    # wash-trade: 反対側 (sell) の既存注文がある WASHY は skip。既存注文は触らない。
    assert by_sym["WASHY"].order_id is None
    assert by_sym["WASHY"].skip_reason and "wash_trade_conflict" in by_sym["WASHY"].skip_reason


def test_integration_idempotency_skips_duplicate_coid(monkeypatch, paper_env):
    monkeypatch.setattr(
        ba, "submit_order_with_retry",
        lambda *a, **k: _FakeOrder("should-not-be-called"),
    )
    # 既に system1-AAPL-20260702 が open → 二重 submit しない
    client = _FakeClient(
        fractionable_map={"AAPL": True},
        open_orders=[_FakeOrder("x", symbol="AAPL", side="buy", coid="system1-AAPL-20260702")],
    )
    json_data = {
        "date": "2026-07-02",
        "systems": {"sys1": {"signals": [
            {"symbol": "AAPL", "side": "buy", "entry_price": 200.0, "weight": 1.0},
        ]}},
    }
    orders = signals_json_to_orders(json_data, tier="small", dry_run=False, client=client)
    assert len(orders) == 1
    assert orders[0].order_id is None
    assert "duplicate_client_order_id" in orders[0].skip_reason
    assert client.notional_calls == []  # 実 submit 経路には入っていない


def test_no_silent_drop_every_order_has_terminal_state(monkeypatch, paper_env):
    """全 order は submitted(order_id) / failed(error) / skipped(skip_reason) の
    いずれか終端状態を必ず持つ (silent success / silent drop を潰す)。"""

    def fake_retry(client, symbol, qty, **kw):
        return _FakeOrder(f"qid-{symbol}", "accepted")

    monkeypatch.setattr(ba, "submit_order_with_retry", fake_retry)
    client = _FakeClient(
        fractionable_map={
            "AAPL": True, "PENNY": False, "BIGNF": False, "WASHY": True, "SHORTX": True,
        },
        open_orders=[_FakeOrder("x", symbol="WASHY", side="sell", coid="user-exit-1")],
    )
    orders = signals_json_to_orders(_signals_json(), tier="small", dry_run=False, client=client)
    for o in orders:
        terminal = bool(o.order_id) or bool(o.error) or bool(o.skip_reason)
        assert terminal, f"{o.symbol} has no terminal state (silent drop!)"
