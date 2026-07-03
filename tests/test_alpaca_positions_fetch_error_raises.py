"""Regression tests for F2 audit P0#7 + P0#8: Alpaca positions & retry idempotency.

Historical bugs (fixed 2026-07-03):

    P0#7 — ``common/alpaca_trading.py::_fetch_open_positions`` returned an
    empty dict on ANY exception (network failure, SDK error). The caller in
    ``signals_to_orders`` could not distinguish "the account is flat" from
    "we failed to check". If we then batch-submitted buys, a symbol we
    already held would receive a fresh buy on top of it — silent duplicate
    exposure.

    P0#8 — ``common/broker_alpaca.py::submit_order_with_retry`` retried any
    exception without requiring a ``client_order_id``. The classic
    catastrophic case: the client-side request times out AFTER Alpaca
    accepted the order. The retry then sends a fresh order without an
    idempotency key, producing two fills for one signal. It also retried
    non-transient errors (422 duplicate, insufficient funds) which cannot
    improve on retry.

Coverage:
    * ``_fetch_open_positions`` raises ``PositionsFetchError`` on SDK error
      (not silent ``{}``).
    * ``submit_order_with_retry`` auto-generates a ``client_order_id`` when
      ``retries>0`` and none is provided, and the SAME id is reused across
      retries.
    * ``submit_order_with_retry`` does NOT retry non-transient errors
      (insufficient funds, 422 duplicate).
    * ``submit_order_with_retry`` DOES retry transient errors (timeout,
      429, 5xx).
"""

from __future__ import annotations

from pathlib import Path
import sys
from unittest import mock

import pytest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common import alpaca_trading as at  # noqa: E402
from common import broker_alpaca as ba  # noqa: E402
from common.alpaca_trading import PositionsFetchError  # noqa: E402


# ===========================================================================
# P0#7: _fetch_open_positions must raise, not silent {}
# ===========================================================================


def test_fetch_open_positions_raises_on_sdk_error() -> None:
    """The regression: fetch failure must NOT be masked as empty positions."""
    class BadClient:
        def get_all_positions(self):
            raise RuntimeError("alpaca sdk connection refused")

    with pytest.raises(PositionsFetchError) as excinfo:
        at._fetch_open_positions(BadClient())

    assert "connection refused" in str(excinfo.value).lower() or "fetch failed" in str(excinfo.value).lower()


def test_fetch_open_positions_happy_path_returns_signed_qty() -> None:
    """Sanity: successful fetch still returns a symbol→qty map."""
    class OkPosition:
        def __init__(self, symbol: str, qty: float) -> None:
            self.symbol = symbol
            self.qty = qty

    class OkClient:
        def get_all_positions(self):
            return [OkPosition("AAPL", 10.0), OkPosition("MSFT", -3.0)]

    result = at._fetch_open_positions(OkClient())
    assert result == {"AAPL": 10.0, "MSFT": -3.0}


def test_positions_fetch_error_is_runtimeerror_subclass() -> None:
    """Callers may catch it as RuntimeError for a broader net; must be a subclass."""
    assert issubclass(PositionsFetchError, RuntimeError)


def test_signals_to_orders_propagates_positions_fetch_error() -> None:
    """The non-dry-run path must NOT proceed if positions cannot be fetched.

    Silently proceeding with `{}` is what caused the duplicate-exposure risk.
    Propagating aborts the batch before we can double-buy anything.
    """
    import pandas as pd

    class BadClient:
        def get_all_positions(self):
            raise RuntimeError("network down")

    signals = pd.DataFrame([
        {"symbol": "AAPL", "side": "buy", "shares": 10, "system": "system1"},
    ])

    with mock.patch.object(at, "assert_paper_env", return_value=None):
        with pytest.raises(PositionsFetchError):
            at.signals_to_orders(
                signals,
                account_equity=10_000.0,
                dry_run=False,  # non-dry-run triggers the fetch
                client=BadClient(),
            )


# ===========================================================================
# P0#8: submit_order_with_retry idempotency + transient-only retry
# ===========================================================================


def test_retry_generates_client_order_id_when_missing() -> None:
    """The regression: retry without an idempotency key can double-submit.

    When retries>0 and no client_order_id, the wrapper must auto-generate one
    so Alpaca dedups any accidental double-submit as 422 duplicate.
    """
    captured_ids: list[str | None] = []

    def fake_submit(client, symbol, qty, **kwargs):
        captured_ids.append(kwargs.get("client_order_id"))
        raise TimeoutError("connection timeout")

    fake_client = mock.Mock()
    with mock.patch.object(ba, "submit_order", side_effect=fake_submit):
        with pytest.raises(TimeoutError):
            ba.submit_order_with_retry(
                fake_client,
                "AAPL",
                10,
                retries=2,
                backoff_seconds=0.0,  # keep test fast
                # deliberately no client_order_id
            )

    # Must have retried, AND all attempts share the SAME auto-generated coid.
    assert len(captured_ids) >= 2, "must retry on transient timeout"
    assert all(cid is not None for cid in captured_ids), (
        "F2 P0#8 regression: retry with no client_order_id could double-submit"
    )
    assert len(set(captured_ids)) == 1, "all retries must use the same coid"


def test_retry_preserves_explicit_client_order_id() -> None:
    """If the caller passes a coid, we must NOT overwrite it."""
    captured_ids: list[str | None] = []

    def fake_submit(client, symbol, qty, **kwargs):
        captured_ids.append(kwargs.get("client_order_id"))
        raise TimeoutError("connection timeout")

    fake_client = mock.Mock()
    with mock.patch.object(ba, "submit_order", side_effect=fake_submit):
        with pytest.raises(TimeoutError):
            ba.submit_order_with_retry(
                fake_client,
                "AAPL",
                10,
                client_order_id="system1-AAPL-20260703",
                retries=2,
                backoff_seconds=0.0,
            )

    assert captured_ids == ["system1-AAPL-20260703"] * 3


def test_retry_skips_non_transient_error_insufficient_funds() -> None:
    """Retrying "insufficient buying power" is pure log spam — abort immediately."""
    call_count = mock.Mock()

    def fake_submit(client, symbol, qty, **kwargs):
        call_count()
        raise RuntimeError("insufficient buying power for order")

    fake_client = mock.Mock()
    with mock.patch.object(ba, "submit_order", side_effect=fake_submit):
        with pytest.raises(RuntimeError, match="insufficient"):
            ba.submit_order_with_retry(
                fake_client,
                "AAPL",
                10,
                client_order_id="system1-AAPL-20260703",
                retries=3,
                backoff_seconds=0.0,
            )

    # Exactly ONE attempt — non-transient, no retry.
    assert call_count.call_count == 1


def test_retry_skips_non_transient_error_422_duplicate() -> None:
    """422 duplicate cannot be resolved by retrying."""
    call_count = mock.Mock()

    def fake_submit(client, symbol, qty, **kwargs):
        call_count()
        raise RuntimeError("422 duplicate client_order_id")

    fake_client = mock.Mock()
    with mock.patch.object(ba, "submit_order", side_effect=fake_submit):
        with pytest.raises(RuntimeError, match="duplicate"):
            ba.submit_order_with_retry(
                fake_client,
                "AAPL",
                10,
                client_order_id="system1-AAPL-20260703",
                retries=3,
                backoff_seconds=0.0,
            )
    assert call_count.call_count == 1


def test_retry_retries_transient_error_429() -> None:
    """429 rate-limit is transient — retry."""
    call_count = mock.Mock()

    def fake_submit(client, symbol, qty, **kwargs):
        call_count()
        raise RuntimeError("429 rate limit exceeded")

    fake_client = mock.Mock()
    with mock.patch.object(ba, "submit_order", side_effect=fake_submit):
        with pytest.raises(RuntimeError):
            ba.submit_order_with_retry(
                fake_client,
                "AAPL",
                10,
                client_order_id="system1-AAPL-20260703",
                retries=2,
                backoff_seconds=0.0,
            )
    assert call_count.call_count == 3  # 1 initial + 2 retries


def test_retry_retries_timeout() -> None:
    """Timeout is transient — retry (with the same coid for idempotency)."""
    call_count = mock.Mock()

    def fake_submit(client, symbol, qty, **kwargs):
        call_count()
        raise TimeoutError("connection timed out")

    fake_client = mock.Mock()
    with mock.patch.object(ba, "submit_order", side_effect=fake_submit):
        with pytest.raises(TimeoutError):
            ba.submit_order_with_retry(
                fake_client,
                "AAPL",
                10,
                client_order_id="system1-AAPL-20260703",
                retries=2,
                backoff_seconds=0.0,
            )
    assert call_count.call_count == 3


def test_retry_success_after_transient_returns_order() -> None:
    """First transient error, then success — the wrapper returns the order."""
    calls: list[str] = []

    def fake_submit(client, symbol, qty, **kwargs):
        calls.append("call")
        if len(calls) == 1:
            raise TimeoutError("timeout")
        return mock.Mock(id="order-123", status="accepted")

    fake_client = mock.Mock()
    with mock.patch.object(ba, "submit_order", side_effect=fake_submit):
        order = ba.submit_order_with_retry(
            fake_client,
            "AAPL",
            10,
            client_order_id="system1-AAPL-20260703",
            retries=2,
            backoff_seconds=0.0,
        )
    assert len(calls) == 2
    assert order.id == "order-123"


def test_is_transient_error_classifier_reasonable_defaults() -> None:
    """Ambiguous errors default to transient (retry) for backward-compat."""
    assert ba._is_transient_error(RuntimeError("timeout")) is True
    assert ba._is_transient_error(RuntimeError("connection reset by peer")) is True
    assert ba._is_transient_error(RuntimeError("insufficient buying power")) is False
    assert ba._is_transient_error(RuntimeError("422 duplicate")) is False
    assert ba._is_transient_error(RuntimeError("some new sdk error we haven't seen")) is True
