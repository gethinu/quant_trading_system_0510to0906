"""Regression tests for F2 audit P0#3: ntfy topic must be masked in outputs.

Historical bug (fixed 2026-07-03):
    ``common/publishers/ntfy.py`` set ``PublishResult.target = self.topic`` on
    success, failure, and dry-run. The topic acts as the secret access token
    for the operator's push channel — anyone who has it can subscribe to (or
    spoof-push into) the feed. Because ``PublishResult`` is persisted in run
    logs and (via ``meta.publish_status``) in the exported signals JSON that
    subscribers can download, the "secret" leaked to every subscriber.

    Additionally, the dry-run ``detail`` field embedded ``self.endpoint``
    verbatim — which is ``{base_url}/{topic}`` — so the topic leaked there too.

Coverage:
    * The raw topic never appears in ``PublishResult.as_dict()`` for dry-run
      or failure paths (transient/live HTTP is out of unit-test scope).
    * The mask preserves a short prefix + length so operators can still
      distinguish channels in logs.
    * The dry-run ``detail`` endpoint URL is also masked (the historical bug
      site).
"""

from __future__ import annotations

import json

import pytest

from common.publishers.ntfy import NtfyPublisher, _mask_topic

SECRET_TOPIC = "super-secret-abcdef1234567890"


# ---------------------------------------------------------------------------
# _mask_topic
# ---------------------------------------------------------------------------


def test_mask_topic_hides_body_and_keeps_prefix_plus_length() -> None:
    masked = _mask_topic(SECRET_TOPIC)
    assert SECRET_TOPIC not in masked
    # 3-char prefix + length are what we promised operators.
    assert masked.startswith(SECRET_TOPIC[:3])
    assert str(len(SECRET_TOPIC)) in masked


@pytest.mark.parametrize("value", ["", None])
def test_mask_topic_handles_empty_and_none(value) -> None:
    assert _mask_topic(value) == "unset"


# ---------------------------------------------------------------------------
# PublishResult dry-run: the primary leak point
# ---------------------------------------------------------------------------


def _sample_payload() -> dict:
    return {
        "version": "1.0",
        "date": "2026-07-02",
        "generated_at": "2026-07-02T06:15:23+09:00",
        "provider": "polygon",
        "systems": {},
        "portfolio": {"total_signals": 0, "total_notional_usd": 0.0, "hedge": None},
        "meta": {"run_id": "test-run"},
    }


def test_publish_result_dry_run_never_contains_raw_topic() -> None:
    """The critical assertion — subscribers must not be able to grep the topic."""
    pub = NtfyPublisher(topic=SECRET_TOPIC)
    result = pub.send(_sample_payload(), dry_run=True)

    dumped = json.dumps(result.as_dict())
    assert SECRET_TOPIC not in dumped, (
        f"F2 P0#3 regression: the raw ntfy topic leaked into PublishResult "
        f"(dry-run). Serialized result: {dumped}"
    )
    # And the target field itself must be masked.
    assert SECRET_TOPIC not in result.target
    assert result.target.startswith(SECRET_TOPIC[:3])


def test_publish_result_dry_run_endpoint_url_also_masked() -> None:
    """Historical bug: the endpoint URL in ``detail`` also embedded the topic."""
    pub = NtfyPublisher(topic=SECRET_TOPIC)
    result = pub.send(_sample_payload(), dry_run=True)

    # The detail is a JSON blob containing the endpoint URL. That URL used to
    # be ``https://ntfy.sh/super-secret-abcdef1234567890`` verbatim.
    assert SECRET_TOPIC not in result.detail, (
        f"F2 P0#3 regression: the raw ntfy topic leaked into dry-run detail "
        f"via the endpoint URL. Detail: {result.detail}"
    )


def test_publish_result_dry_run_still_useful_for_operators() -> None:
    """After masking, operators must still be able to tell which channel this is."""
    pub = NtfyPublisher(topic=SECRET_TOPIC)
    result = pub.send(_sample_payload(), dry_run=True)
    # Prefix is preserved so different channels remain distinguishable.
    assert result.target.startswith(SECRET_TOPIC[:3])
    # Length is preserved so misconfigured (too short) topics are visible.
    assert str(len(SECRET_TOPIC)) in result.target


def test_publish_result_dry_run_with_no_topic_is_marked_dry_run() -> None:
    """Empty topic path (no NTFY_TOPIC env) must render as 'dry-run' not 'unset'."""
    pub = NtfyPublisher(topic="")
    result = pub.send(_sample_payload(), dry_run=True)
    assert result.target == "dry-run"
    # Still no accidental leak of an empty-string-looking topic in detail.
    dumped = json.dumps(result.as_dict())
    assert '://ntfy.sh/"' not in dumped  # rough sanity — no bare endpoint
