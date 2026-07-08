"""Regression gate: current code must match the committed golden signal snapshot.

Runs ``scripts.golden_signal_harness.build_snapshot`` against the seven
``core.systemN`` candidate generators and asserts a byte-level match with
``tests/golden_signals/<YYYYMMDD>.json``.

This test is the single most important regression tripwire for the planned
big-ticket refactor of ``core/system1-7.py`` (see
``docs/CORE_SYSTEM_REFACTOR_PLAN_20260703.md``). If any structural change to
those files silently alters the ranked candidate list or ranking value, this
test will fail with a per-system diff.

If a signal-changing edit is intentional (which is out-of-scope for the
refactor plan and requires product sign-off), regenerate the golden::

    python scripts/golden_signal_harness.py --regenerate

and commit both the code change and the JSON delta in the same PR.

Note: the golden snapshot stores diagnostics counters under
``diagnostics_info`` for reference, but they are stripped before the hash
comparison — see ``golden_signal_harness._hashable`` for the rationale.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from scripts.golden_signal_harness import (
    DEFAULT_FIXTURE_DATE,
    DEFAULT_TOP_N,
    _canonical_json,
    _diff_snapshot,
    _golden_path,
    _hashable,
    _sha256,
    build_snapshot,
)


def _load_golden(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture(scope="module")
def golden_snapshot() -> dict[str, Any]:
    """Load the committed golden snapshot once per test module."""

    golden = _golden_path(DEFAULT_FIXTURE_DATE)
    if not golden.exists():
        pytest.skip(
            f"golden reference missing: {golden}. "
            "Run `python scripts/golden_signal_harness.py --regenerate` first."
        )
    return _load_golden(golden)


@pytest.fixture(scope="module")
def current_snapshot() -> dict[str, Any]:
    """Run the harness ONCE per test module and reuse for every assertion.

    Per-system parametrized tests reuse this — building the snapshot for all
    seven systems is ~15 s, so a per-test rebuild would make CI painfully
    slow.
    """

    return build_snapshot(fixture_date=DEFAULT_FIXTURE_DATE, top_n=DEFAULT_TOP_N)


def test_golden_signals_default_date_matches(
    golden_snapshot: dict[str, Any], current_snapshot: dict[str, Any]
) -> None:
    """Byte-parity for the default fixture date (2026-07-01)."""

    want_hash = _sha256(
        _canonical_json(_hashable(golden_snapshot.get("per_system", {})))
    )
    got_hash = _sha256(
        _canonical_json(_hashable(current_snapshot.get("per_system", {})))
    )

    if want_hash != got_hash:
        diff = _diff_snapshot(golden_snapshot, current_snapshot)
        pytest.fail(
            "golden signal snapshot mismatch: want sha256="
            f"{want_hash[:12]} got sha256={got_hash[:12]}\n" + "\n".join(diff)
        )


@pytest.mark.parametrize(
    "system_name",
    ["system1", "system2", "system3", "system4", "system5", "system6", "system7"],
)
def test_golden_per_system_stable(
    system_name: str,
    golden_snapshot: dict[str, Any],
    current_snapshot: dict[str, Any],
) -> None:
    """Per-system parametrized guard so a failure names the offending system."""

    want = golden_snapshot.get("per_system", {}).get(system_name)
    if want is None:
        pytest.skip(f"golden has no entry for {system_name}")

    got = current_snapshot.get("per_system", {}).get(system_name)
    assert got is not None, f"harness produced no entry for {system_name}"

    want_h = _hashable({system_name: want})[system_name]
    got_h = _hashable({system_name: got})[system_name]
    assert _canonical_json(want_h) == _canonical_json(
        got_h
    ), f"{system_name} snapshot diverged"
