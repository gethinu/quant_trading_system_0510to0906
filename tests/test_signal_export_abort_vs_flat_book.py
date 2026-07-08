"""Regression tests for F2 audit P0#6: signal_export abort vs flat book.

Historical bug (fixed 2026-07-03):
    When ``compute_today_signals`` raised ``SystemExit`` (typically because
    the rolling cache was stale and every symbol got excluded),
    ``common/signal_export.py::run_headless`` set
    ``final_df, per_system = None, {}`` and wrote a schema-valid but empty
    payload with exit code 0. There was no marker on the payload to tell
    subscribers whether:
        (a) the pipeline aborted (stale cache) — no signals to trust, OR
        (b) it ran cleanly and today just has zero signals (real flat book).
    Both cases produced identical output. Subscribers silently accepted the
    abort as "no signals today".

Coverage:
    * ``build_signals_json`` writes ``meta.status`` and ``meta.abort_reason``.
    * Default ``meta.status`` is ``"ok"`` (backward-compat for legit flat books).
    * ``run_headless``: SystemExit path sets ``meta.status="aborted"`` and
      returns exit code 3 (distinguishable from the daily_pipeline "1/2 = FAIL"
      class and from "0 = OK").
    * A successful compute writes ``meta.status="ok"`` and exit code 0.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common import signal_export  # noqa: E402

# ---------------------------------------------------------------------------
# build_signals_json: schema additive fields
# ---------------------------------------------------------------------------


def test_build_signals_json_default_status_is_ok() -> None:
    """Backward-compat: no status arg -> meta.status == "ok"."""
    payload = signal_export.build_signals_json(
        final_df=None,
        per_system={},
        date_str="2026-07-03",
        run_id="test-run",
        elapsed_seconds=0.5,
    )
    assert payload["meta"]["status"] == "ok"
    assert payload["meta"]["abort_reason"] is None


def test_build_signals_json_abort_status_persisted() -> None:
    """Explicit aborted marker survives the round trip and is JSON-safe."""
    payload = signal_export.build_signals_json(
        final_df=None,
        per_system={},
        date_str="2026-07-03",
        run_id="test-run",
        elapsed_seconds=0.0,
        status="aborted",
        abort_reason="stale_rolling_cache",
    )
    assert payload["meta"]["status"] == "aborted"
    assert payload["meta"]["abort_reason"] == "stale_rolling_cache"

    # Must serialize cleanly for subscribers to parse.
    dumped = json.dumps(payload, ensure_ascii=False)
    assert '"status": "aborted"' in dumped
    assert '"abort_reason": "stale_rolling_cache"' in dumped


def test_abort_and_flat_book_payloads_are_distinguishable() -> None:
    """The core assertion: abort payload != flat-book payload."""
    flat = signal_export.build_signals_json(
        final_df=None,
        per_system={},
        date_str="2026-07-03",
        run_id="run-flat",
        elapsed_seconds=1.0,
    )
    abort = signal_export.build_signals_json(
        final_df=None,
        per_system={},
        date_str="2026-07-03",
        run_id="run-abort",
        elapsed_seconds=0.0,
        status="aborted",
        abort_reason="compute_today_signals_systemexit:1",
    )
    # Portfolio / systems are the same shape, but subscribers can distinguish.
    assert flat["meta"]["status"] != abort["meta"]["status"]
    assert flat["meta"]["abort_reason"] is None
    assert abort["meta"]["abort_reason"] is not None
    assert abort["meta"]["status"] == "aborted"


# ---------------------------------------------------------------------------
# run_headless: SystemExit -> status="aborted" + exit code 3
# ---------------------------------------------------------------------------


def test_run_headless_systemexit_writes_aborted_marker_and_exits_3(
    tmp_path: Path,
) -> None:
    """The critical regression: subscribers must be able to detect aborts."""
    out_path = tmp_path / "today_signals.json"

    fake_compute = mock.Mock(side_effect=SystemExit(1))

    with mock.patch.dict(
        sys.modules,
        {
            "scripts.run_all_systems_today": mock.Mock(
                compute_today_signals=fake_compute
            )
        },
    ):
        rc = signal_export.run_headless(
            [
                "--headless",
                "--date",
                "2026-07-03",
                "--output-json",
                str(out_path),
            ]
        )

    assert rc == 3, "SystemExit must surface as exit code 3 (not 0)"
    assert out_path.exists()

    payload = json.loads(out_path.read_text(encoding="utf-8"))
    assert payload["meta"]["status"] == "aborted"
    assert payload["meta"]["abort_reason"] is not None
    assert "systemexit" in payload["meta"]["abort_reason"].lower()
    assert payload["portfolio"]["total_signals"] == 0


def test_run_headless_success_path_writes_ok_status(tmp_path: Path) -> None:
    """Happy path: real flat book (returns empty from compute) -> status='ok'."""
    out_path = tmp_path / "today_signals.json"

    fake_compute = mock.Mock(return_value=(None, {}))
    with mock.patch.dict(
        sys.modules,
        {
            "scripts.run_all_systems_today": mock.Mock(
                compute_today_signals=fake_compute
            )
        },
    ):
        rc = signal_export.run_headless(
            [
                "--headless",
                "--date",
                "2026-07-03",
                "--output-json",
                str(out_path),
            ]
        )

    assert rc == 0
    payload = json.loads(out_path.read_text(encoding="utf-8"))
    assert payload["meta"]["status"] == "ok"
    assert payload["meta"]["abort_reason"] is None
    assert payload["portfolio"]["total_signals"] == 0


def test_run_headless_generic_exception_still_returns_1(tmp_path: Path) -> None:
    """Non-SystemExit failures stay as exit 1 (existing contract)."""
    out_path = tmp_path / "today_signals.json"

    fake_compute = mock.Mock(side_effect=RuntimeError("boom"))
    with mock.patch.dict(
        sys.modules,
        {
            "scripts.run_all_systems_today": mock.Mock(
                compute_today_signals=fake_compute
            )
        },
    ):
        rc = signal_export.run_headless(
            [
                "--headless",
                "--date",
                "2026-07-03",
                "--output-json",
                str(out_path),
            ]
        )

    assert rc == 1  # unchanged from existing behavior
