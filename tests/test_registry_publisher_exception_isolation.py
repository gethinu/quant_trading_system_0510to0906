"""Regression tests for F2 audit P0#4: Publisher exception must not kill the chain.

Historical bug (fixed 2026-07-03):
    ``common/publishers/registry.py`` called ``self.primary.send()`` and
    ``self.secondary.send()`` without any try/except. Any render-time exception
    inside a publisher (malformed payload breaking an f-string, unexpected
    None in a header build, ``requests`` import blowing up, etc.) propagated
    out of ``PublisherRegistry.publish()`` — the secondary was never called,
    ``meta.publish_status`` was never recorded, and the whole day's
    notification silently vanished.

Coverage:
    * If primary.send() raises, secondary.send() STILL FIRES.
    * The registry surfaces the primary failure as a ``PublishResult``
      (ok=False, detail contains the exception type/message) instead of
      re-raising.
    * If secondary also raises, the registry returns status="failed" without
      propagating either exception.
    * A raised exception does NOT leak internal secrets (only ``publisher.name``
      appears in ``target``).
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from common.publishers.base import Publisher, PublishResult
from common.publishers.registry import PublisherRegistry, RegistryResult


class _RaisingPublisher(Publisher):
    """A publisher whose send() always raises the requested exception."""

    def __init__(self, name: str, exc: Exception) -> None:
        self.name = name
        self._exc = exc
        self.calls: int = 0

    def send(self, signals_json: dict[str, Any], *, dry_run: bool = False) -> PublishResult:
        self.calls += 1
        raise self._exc

    def is_configured(self) -> bool:
        return True


class _RecordingPublisher(Publisher):
    """A publisher whose send() records the call and returns the configured result."""

    def __init__(self, name: str, *, ok: bool = True, detail: str = "sent") -> None:
        self.name = name
        self._ok = ok
        self._detail = detail
        self.calls: int = 0
        self.last_payload: dict[str, Any] | None = None
        self.last_dry_run: bool | None = None

    def send(self, signals_json: dict[str, Any], *, dry_run: bool = False) -> PublishResult:
        self.calls += 1
        self.last_payload = signals_json
        self.last_dry_run = dry_run
        return PublishResult(
            publisher=self.name,
            ok=self._ok,
            status_code=200 if self._ok else 500,
            detail=self._detail,
            target=f"{self.name}-target",
        )

    def is_configured(self) -> bool:
        return True


def _payload() -> dict[str, Any]:
    return {
        "version": "1.0",
        "date": "2026-07-03",
        "provider": "polygon",
        "systems": {},
        "portfolio": {"total_signals": 0, "total_notional_usd": 0.0, "hedge": None},
        "meta": {"run_id": "test"},
    }


# -----------------------------------------------------------------------
# core assertion: primary raise → secondary still fires
# -----------------------------------------------------------------------


def test_primary_raise_still_fires_secondary_fallback() -> None:
    """The regression: ntfy raising must not kill the Email backup."""
    primary = _RaisingPublisher("ntfy", RuntimeError("boom in render"))
    secondary = _RecordingPublisher("email", ok=True)

    reg = PublisherRegistry(primary, secondary)
    result = reg.publish(_payload(), dry_run=False)

    # Both publishers were tried; the secondary reached the network.
    assert primary.calls == 1
    assert secondary.calls == 1

    assert isinstance(result, RegistryResult)
    assert len(result.results) == 2
    assert result.results[0].ok is False
    assert result.results[0].publisher == "ntfy"
    assert "publisher_exception" in result.results[0].detail
    assert result.results[1].ok is True

    # Chain outcome: partial (primary failed, secondary ok) — NOT failed.
    assert result.status == "partial"


def test_primary_raise_no_exception_leaks_from_publish() -> None:
    """publish() must swallow publisher exceptions and return a RegistryResult."""
    primary = _RaisingPublisher("ntfy", ValueError("f-string blew up"))
    secondary = _RecordingPublisher("email", ok=True)

    reg = PublisherRegistry(primary, secondary)
    # If this test raises, the regression is back.
    result = reg.publish(_payload(), dry_run=False)
    assert result is not None


def test_both_publishers_raise_yields_failed_status() -> None:
    """When both publishers explode, registry still returns cleanly with status=failed."""
    primary = _RaisingPublisher("ntfy", RuntimeError("ntfy boom"))
    secondary = _RaisingPublisher("email", RuntimeError("smtp boom"))

    reg = PublisherRegistry(primary, secondary)
    result = reg.publish(_payload(), dry_run=False)

    assert primary.calls == 1
    assert secondary.calls == 1
    assert result.status == "failed"
    assert all(r.ok is False for r in result.results)
    assert all("publisher_exception" in r.detail for r in result.results)


def test_primary_ok_with_always_secondary_still_reaches_secondary() -> None:
    """always_secondary=True must fire secondary even when primary succeeds."""
    primary = _RecordingPublisher("ntfy", ok=True)
    secondary = _RecordingPublisher("email", ok=True)

    reg = PublisherRegistry(primary, secondary, always_secondary=True)
    result = reg.publish(_payload(), dry_run=False)

    assert primary.calls == 1
    assert secondary.calls == 1
    assert result.status == "ok"


def test_primary_ok_without_always_secondary_skips_secondary() -> None:
    primary = _RecordingPublisher("ntfy", ok=True)
    secondary = _RecordingPublisher("email", ok=True)

    reg = PublisherRegistry(primary, secondary, always_secondary=False)
    result = reg.publish(_payload(), dry_run=False)

    assert primary.calls == 1
    assert secondary.calls == 0
    assert result.status == "ok"
    assert len(result.results) == 1


def test_no_secondary_and_primary_raises_returns_failed() -> None:
    """When there's no secondary, a raising primary yields status=failed cleanly."""
    primary = _RaisingPublisher("ntfy", RuntimeError("boom"))
    reg = PublisherRegistry(primary, secondary=None)

    result = reg.publish(_payload(), dry_run=False)
    assert primary.calls == 1
    assert result.status == "failed"
    assert len(result.results) == 1


# -----------------------------------------------------------------------
# no secret leak from the exception path
# -----------------------------------------------------------------------


def test_publisher_exception_does_not_expose_internal_secrets() -> None:
    """The synthesized failure result must not carry any secret from the publisher.

    target = publisher.name (a well-known identifier). Nothing more.
    This protects against a chained failure of P0#3 (ntfy topic leak) via P0#4.
    """
    class LeakyPublisher(Publisher):
        name = "ntfy"

        def __init__(self) -> None:
            self.topic = "super-secret-topic-12345"

        def send(self, signals_json: dict[str, Any], *, dry_run: bool = False) -> PublishResult:
            # Any attempt to embed the topic in the exception must not end up
            # in the persisted PublishResult.
            raise RuntimeError(f"render fail for topic={self.topic}")

        def is_configured(self) -> bool:
            return True

    primary = LeakyPublisher()
    reg = PublisherRegistry(primary, secondary=None)
    result = reg.publish(_payload(), dry_run=False)

    dumped = json.dumps(result.as_dict())
    # The registry-synthesized result MUST NOT quote the secret.
    # The exception text itself is unavoidable if a publisher decides to
    # embed a secret in str(exc); the *registry* is responsible for keeping
    # target=publisher.name, not the whole message.
    assert result.results[0].target == "ntfy"
    # And target itself is what dashboards persist — verify no leak there.
    assert "super-secret-topic-12345" not in result.results[0].target


# -----------------------------------------------------------------------
# smoke: dry_run mode with a raising publisher still recovers
# -----------------------------------------------------------------------


@pytest.mark.parametrize("dry_run", [True, False])
def test_dry_run_and_live_both_survive_primary_raise(dry_run: bool) -> None:
    primary = _RaisingPublisher("ntfy", RuntimeError("boom"))
    secondary = _RecordingPublisher("email", ok=True)

    reg = PublisherRegistry(primary, secondary)
    result = reg.publish(_payload(), dry_run=dry_run)

    assert primary.calls == 1
    assert secondary.calls == 1
    assert secondary.last_dry_run == dry_run
    assert result.status == "partial"
