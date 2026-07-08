"""common/broker_alpaca.py の order_type='bracket' 追加分の unit test.

alpaca-py SDK が無い CI 環境でも走るよう、SDK シンボルは module-level で dummy
に replace して check する。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import common.broker_alpaca as ba


class _FakeReq:
    """SDK Request obj の recorder。__init__ の kwargs をそのまま保持。"""

    def __init__(self, **kwargs):
        self.kwargs = kwargs


class _FakeTakeProfit:
    def __init__(self, limit_price):
        self.limit_price = limit_price


class _FakeStopLoss:
    def __init__(self, stop_price):
        self.stop_price = stop_price


class _FakeOrderClass:
    OCO = "OCO"
    BRACKET = "BRACKET"


class _FakeOrderSide:
    BUY = SimpleNamespace(name="BUY")
    SELL = SimpleNamespace(name="SELL")


class _FakeTimeInForce:
    DAY = "day"
    GTC = "gtc"


@pytest.fixture
def patched_sdk(monkeypatch):
    # _require_sdk() は TradingClient is None を見るので non-None sentinel を差し込む
    monkeypatch.setattr(ba, "TradingClient", object)
    monkeypatch.setattr(ba, "MarketOrderRequest", _FakeReq)
    monkeypatch.setattr(ba, "LimitOrderRequest", _FakeReq)
    monkeypatch.setattr(ba, "TakeProfitRequest", _FakeTakeProfit)
    monkeypatch.setattr(ba, "StopLossRequest", _FakeStopLoss)
    monkeypatch.setattr(ba, "OrderClass", _FakeOrderClass)
    monkeypatch.setattr(ba, "OrderSide", _FakeOrderSide)
    monkeypatch.setattr(ba, "TimeInForce", _FakeTimeInForce)
    monkeypatch.setattr(ba, "TrailingStopOrderRequest", _FakeReq)
    yield


class _FakeClient:
    def __init__(self):
        self.submitted = []

    def submit_order(self, order_data):
        self.submitted.append(order_data)
        return SimpleNamespace(id="ORD-BRACKET-1", status="accepted")


def test_bracket_market_entry_carries_take_profit_and_stop_loss(patched_sdk):
    client = _FakeClient()
    ba.submit_order(
        client,
        "AAPL",
        10,
        side="buy",
        order_type="bracket",
        take_profit=210.0,
        stop_loss=180.0,
        time_in_force="gtc",
        client_order_id="system2-AAPL-20260702",
    )
    assert len(client.submitted) == 1
    req = client.submitted[0]
    assert req.kwargs["symbol"] == "AAPL"
    assert req.kwargs["qty"] == 10
    assert req.kwargs["order_class"] == "BRACKET"
    assert isinstance(req.kwargs["take_profit"], _FakeTakeProfit)
    assert req.kwargs["take_profit"].limit_price == 210.0
    assert isinstance(req.kwargs["stop_loss"], _FakeStopLoss)
    assert req.kwargs["stop_loss"].stop_price == 180.0
    assert req.kwargs["client_order_id"] == "system2-AAPL-20260702"


def test_bracket_limit_entry_carries_limit_price(patched_sdk):
    client = _FakeClient()
    ba.submit_order(
        client,
        "TSLA",
        8,
        side="sell",
        order_type="bracket",
        limit_price=260.0,
        take_profit=240.0,
        stop_loss=275.0,
    )
    req = client.submitted[0]
    assert req.kwargs["limit_price"] == 260.0
    assert req.kwargs["order_class"] == "BRACKET"


def test_bracket_requires_take_profit_and_stop_loss(patched_sdk):
    client = _FakeClient()
    with pytest.raises(ValueError):
        ba.submit_order(client, "AAPL", 10, side="buy", order_type="bracket")


def test_trailing_stop_passes_client_order_id(patched_sdk):
    client = _FakeClient()
    ba.submit_order(
        client,
        "AAPL",
        10,
        side="sell",
        order_type="trailing_stop",
        trail_percent=25.0,
        client_order_id="protect-system1-AAPL-20260702-protect-trail",
    )
    req = client.submitted[0]
    assert req.kwargs["trail_percent"] == 25.0
    assert (
        req.kwargs["client_order_id"] == "protect-system1-AAPL-20260702-protect-trail"
    )
