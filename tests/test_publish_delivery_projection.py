from __future__ import annotations

import json
from pathlib import Path

from common.publishers.base import PublishResult
from common.publishers.registry import RegistryResult
from scripts.publish_signals import (
    _delivery_projection,
    _write_publish_status,
    _write_unavailable_delivery,
    build_registry,
)


def test_primary_acceptance_is_distinct_from_device_delivery() -> None:
    result = RegistryResult(
        status="ok",
        results=[
            PublishResult(
                publisher="ntfy",
                ok=True,
                status_code=202,
                detail="accepted",
                target="sup…(28)",
            )
        ],
    )
    projection = _delivery_projection(result)
    assert projection["state"] == "primary_accepted"
    assert projection["channels"]["ntfy"] == {
        "state": "accepted",
        "status_code": 202,
    }


def test_email_fallback_has_an_explicit_policy_outcome() -> None:
    result = RegistryResult(
        status="partial",
        results=[
            PublishResult(publisher="ntfy", ok=False, status_code=503),
            PublishResult(publisher="email", ok=True, status_code=202),
        ],
    )
    projection = _delivery_projection(result)
    assert projection["state"] == "fallback_accepted"
    assert projection["channels"]["ntfy"]["state"] == "failed"
    assert projection["channels"]["email"]["state"] == "accepted"


def test_persisted_delivery_projection_excludes_targets_and_provider_details(
    tmp_path: Path,
) -> None:
    path = tmp_path / "today_signals_20260813.json"
    payload = {
        "date": "2026-08-13",
        "meta": {"run_id": "run-2", "publish_status": "not_attempted"},
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    result = RegistryResult(
        status="partial",
        results=[
            PublishResult(
                publisher="ntfy",
                ok=False,
                status_code=500,
                detail="secret provider response",
                target="secret-topic",
            ),
            PublishResult(
                publisher="email",
                ok=True,
                status_code=202,
                target="operator@example.test",
            ),
        ],
    )

    _write_publish_status(path, payload, result)
    persisted = path.read_text(encoding="utf-8")
    stored = json.loads(persisted)
    assert stored["meta"]["publish_status"] == "partial"
    assert stored["meta"]["publish_delivery"]["state"] == "fallback_accepted"
    assert "secret provider response" not in persisted
    assert "secret-topic" not in persisted
    assert "operator@example.test" not in persisted


def test_delivery_write_is_compare_and_swap_by_run_id(tmp_path: Path) -> None:
    path = tmp_path / "today_signals_20260813.json"
    in_flight = {"date": "2026-08-13", "meta": {"run_id": "morning-run"}}
    current = {
        "date": "2026-08-13",
        "meta": {"run_id": "night-run", "publish_status": "not_attempted"},
    }
    path.write_text(json.dumps(current), encoding="utf-8")
    result = RegistryResult(
        status="ok",
        results=[PublishResult(publisher="ntfy", ok=True, status_code=202)],
    )

    assert _write_publish_status(path, in_flight, result) is False
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["meta"] == {
        "run_id": "night-run",
        "publish_status": "not_attempted",
    }


def test_duplicate_same_run_never_regresses_accepted_channel(tmp_path: Path) -> None:
    path = tmp_path / "today_signals_20260813.json"
    payload = {
        "date": "2026-08-13",
        "meta": {
            "run_id": "same-run",
            "publish_delivery": {
                "state": "primary_accepted",
                "attempted_at": "2026-08-13T12:00:00+00:00",
                "channels": {"ntfy": {"state": "accepted", "status_code": 202}},
            },
        },
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    failed = RegistryResult(
        status="failed",
        results=[PublishResult(publisher="ntfy", ok=False, status_code=503)],
    )

    assert _write_publish_status(path, payload, failed) is True
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["meta"]["publish_status"] == "ok"
    assert stored["meta"]["publish_delivery"]["channels"]["ntfy"]["state"] == "accepted"


def test_unconfigured_email_only_is_not_mislabeled_as_ntfy(tmp_path: Path) -> None:
    path = tmp_path / "today_signals_20260813.json"
    payload = {"date": "2026-08-13", "meta": {"run_id": "email-run"}}
    path.write_text(json.dumps(payload), encoding="utf-8")

    _write_unavailable_delivery(path, payload, build_registry("email", fallback=False))

    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["meta"]["publish_delivery"] == {
        "state": "not_configured",
        "attempted_at": stored["meta"]["publish_delivery"]["attempted_at"],
        "channels": {"email": {"state": "not_configured", "status_code": None}},
    }
