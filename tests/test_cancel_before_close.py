"""Unit tests for cancel-before-close (held_for_orders exit blocker fix).

`cancel_open_orders_for_symbols` must cancel ONLY the requested symbols' resting
orders (freeing qty for a time-based market close) and leave every other symbol's
protective orders untouched. No network — a fake client records cancel calls.
"""

from __future__ import annotations

import pytest

from common import broker_alpaca as ba


class _FakeOrder:
    def __init__(self, symbol: str, oid: str):
        self.symbol = symbol
        self.id = oid


class _FakeClient:
    def __init__(self, orders, *, fail_ids=frozenset()):
        self._orders = orders
        self.canceled: list[str] = []
        self._fail_ids = set(fail_ids)

    # get_open_orders() -> client.get_orders(GetOrdersRequest(status=OPEN))
    def get_orders(self, _request):
        return self._orders

    def cancel_order_by_id(self, oid):
        if oid in self._fail_ids:
            raise RuntimeError("boom")
        self.canceled.append(oid)


def _orders():
    return [
        _FakeOrder("RGNX", "o1"),
        _FakeOrder("BABA", "o2"),
        _FakeOrder("AAPL", "o3"),  # must NOT be canceled
        _FakeOrder("rgnx", "o4"),  # case-insensitive match
    ]


def test_cancels_only_requested_symbols():
    client = _FakeClient(_orders())
    res = ba.cancel_open_orders_for_symbols(client, {"RGNX", "BABA"})
    assert res["canceled"] == 3  # o1, o2, o4
    assert set(client.canceled) == {"o1", "o2", "o4"}
    assert "o3" not in client.canceled  # AAPL protective order preserved
    assert set(res["symbols"]) == {"RGNX", "BABA"}


def test_empty_symbols_is_noop():
    client = _FakeClient(_orders())
    res = ba.cancel_open_orders_for_symbols(client, set())
    assert res["canceled"] == 0
    assert client.canceled == []


def test_case_insensitive_input():
    client = _FakeClient(_orders())
    res = ba.cancel_open_orders_for_symbols(client, {"rgnx"})
    assert set(client.canceled) == {"o1", "o4"}
    assert res["canceled"] == 2


def test_individual_cancel_failure_is_swallowed():
    client = _FakeClient(_orders(), fail_ids={"o1"})
    res = ba.cancel_open_orders_for_symbols(client, {"RGNX", "BABA"})
    # o1 fails, o2 and o4 still cancel
    assert res["canceled"] == 2
    assert set(client.canceled) == {"o2", "o4"}


def test_get_orders_failure_returns_zero():
    class _Boom:
        def get_orders(self, _r):
            raise RuntimeError("network")

    res = ba.cancel_open_orders_for_symbols(_Boom(), {"RGNX"})
    assert res["canceled"] == 0
    assert res["symbols"] == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
