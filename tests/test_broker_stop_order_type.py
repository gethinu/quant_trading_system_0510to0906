"""Regression: broker_alpaca.submit_order が order_type='stop' を扱えること。

2026-07-04 の実 paper exit 発注で、protect_stop 注文 (order_type='stop') が
"未知の order_type: stop" で全滅していた。StopOrderRequest 分岐を追加した修正を固定する。
"""

from __future__ import annotations

import pytest

import common.broker_alpaca as ba


class _FakeOrder:
    id = "oid-stop-1"
    status = "accepted"


class _CapturingClient:
    def __init__(self):
        self.req = None

    def submit_order(self, order_data=None):
        self.req = order_data
        return _FakeOrder()


def test_stop_order_type_builds_stop_request():
    client = _CapturingClient()
    order = ba.submit_order(
        client,
        "HST",
        70,
        side="sell",
        order_type="stop",
        stop_price=17.33,
        time_in_force="gtc",
        client_order_id="system4-HST-exit",
    )
    assert type(client.req).__name__ == "StopOrderRequest"
    assert float(client.req.stop_price) == 17.33
    assert order.id == "oid-stop-1"


def test_stop_order_requires_stop_price():
    client = _CapturingClient()
    with pytest.raises(ValueError, match="stop_price"):
        ba.submit_order(client, "X", 1, side="sell", order_type="stop")


def test_unknown_order_type_still_raises():
    client = _CapturingClient()
    with pytest.raises(ValueError, match="未知の order_type"):
        ba.submit_order(client, "X", 1, side="sell", order_type="bogus")
