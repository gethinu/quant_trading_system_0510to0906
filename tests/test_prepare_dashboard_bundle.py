from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts.prepare_dashboard_bundle import (
    BundleContractError,
    materialize_dashboard_bundle,
)

DATE = "2026-08-13"
COMPACT = "20260813"
PHASES = ("Tgt", "FILpass", "STUpass", "TRDlist", "Entry", "Exit")


def _phase(name: str) -> dict:
    return {
        "name": name,
        "label": name,
        "condition": name,
        "count": None,
        "measured": False,
        "ratio_of_prev": None,
        "ratio_of_universe": None,
    }


def _pipeline(*, date: str = DATE) -> dict:
    return {
        "date": date,
        "schema": "signal_pipeline/v1",
        "provider": "polygon_grouped_daily",
        "systems": {
            f"sys{i}": {
                "system_id": f"sys{i}",
                "phases": [_phase(name) for name in PHASES],
                "final_signals": None,
            }
            for i in range(1, 8)
        },
        "notes": [],
    }


def _signals(*, run_id: str = "20260813_223505_first", spy_target: int = 100) -> dict:
    systems = {}
    for i in range(1, 8):
        funnel = {
            "target": spy_target if i == 7 else 100,
            "filter_pass": 1 if i == 7 else 50,
            "setup_pass": 1 if i == 7 else 25,
            "candidate_count": 1 if i == 7 else 10,
            "entry_count": 1 if i == 7 else 5,
        }
        systems[f"sys{i}"] = {
            "signals": [],
            "n_candidates_input": funnel["candidate_count"],
            "n_signals_output": funnel["entry_count"],
            "funnel": funnel,
        }
    return {
        "version": "1.0",
        "date": DATE,
        "generated_at": "2026-08-13T22:50:36+09:00",
        "provider": "polygon",
        "systems": systems,
        "portfolio": {"total_signals": 31, "total_notional_usd": 0, "hedge": None},
        "meta": {"run_id": run_id, "publish_status": "not_attempted"},
    }


def _recon() -> dict:
    return {
        "version": "1.0",
        "date": DATE,
        "generated_at": "2026-08-13T22:51:58+09:00",
        "source_signals_run_id": "20260813_223505_first",
        "inputs": {"signals": True, "paper_orders": True, "exit_orders": True},
        "systems": {},
    }


def _write(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _fixtures(
    tmp_path: Path, *, signals: dict | None = None, pipeline: dict | None = None
) -> None:
    _write(tmp_path / f"today_signals_{COMPACT}.json", signals or _signals())
    _write(tmp_path / f"pipeline_{COMPACT}.json", pipeline or _pipeline())
    _write(tmp_path / f"recon_{COMPACT}.json", _recon())


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_materializes_same_run_funnel_exit_and_manifest(tmp_path: Path) -> None:
    _fixtures(tmp_path)
    manifest = materialize_dashboard_bundle(
        results_dir=tmp_path, date_str=DATE, require_exit=True
    )

    assert manifest["measurement"] == {
        "funnel_measured": 34,
        "funnel_total": 35,
        "exit_measured": 7,
    }
    pipeline_path = tmp_path / f"pipeline_{COMPACT}.json"
    pipeline = json.loads(pipeline_path.read_text(encoding="utf-8"))
    assert pipeline["source_signals_run_id"] == "20260813_223505_first"
    assert pipeline["source_signals_sha256"] == _sha256(
        tmp_path / f"today_signals_{COMPACT}.json"
    )
    sys1 = {p["name"]: p for p in pipeline["systems"]["sys1"]["phases"]}
    assert sys1["Tgt"]["count"] == 100 and sys1["Tgt"]["measured"] is True
    assert sys1["Exit"]["count"] == 0 and sys1["Exit"]["measured"] is True
    sys7_tgt = pipeline["systems"]["sys7"]["phases"][0]
    assert sys7_tgt["count"] is None and sys7_tgt["measured"] is False
    assert sys7_tgt["unmeasured_reason"] == (
        "shared_universe_not_applicable_to_spy_only"
    )
    assert manifest["files"]["pipeline"]["sha256"] == _sha256(pipeline_path)
    assert "recon" not in manifest["files"]
    assert manifest["sources"]["recon"]["sha256"] == _sha256(
        tmp_path / f"recon_{COMPACT}.json"
    )


def test_newer_same_day_run_replaces_signal_projection(tmp_path: Path) -> None:
    _fixtures(tmp_path)
    materialize_dashboard_bundle(results_dir=tmp_path, date_str=DATE, require_exit=True)

    second = _signals(run_id="20260813_225000_second")
    second["generated_at"] = "2026-08-13T22:55:00+09:00"
    second["systems"]["sys1"]["funnel"]["entry_count"] = 3
    second["systems"]["sys1"]["n_signals_output"] = 3
    _write(tmp_path / f"today_signals_{COMPACT}.json", second)
    second_recon = _recon()
    second_recon["source_signals_run_id"] = "20260813_225000_second"
    second_recon["generated_at"] = "2026-08-13T22:55:30+09:00"
    _write(tmp_path / f"recon_{COMPACT}.json", second_recon)
    materialize_dashboard_bundle(results_dir=tmp_path, date_str=DATE, require_exit=True)

    pipeline = json.loads(
        (tmp_path / f"pipeline_{COMPACT}.json").read_text(encoding="utf-8")
    )
    entry = next(
        p for p in pipeline["systems"]["sys1"]["phases"] if p["name"] == "Entry"
    )
    assert entry["count"] == 3
    assert entry["source_run_id"] == "20260813_225000_second"
    assert pipeline["systems"]["sys1"]["final_signals"] == 3


def test_identical_sources_produce_byte_stable_pipeline_and_manifest(
    tmp_path: Path,
) -> None:
    _fixtures(tmp_path)
    materialize_dashboard_bundle(results_dir=tmp_path, date_str=DATE, require_exit=True)
    pipeline_path = tmp_path / f"pipeline_{COMPACT}.json"
    manifest_path = tmp_path / f"dashboard_bundle_{COMPACT}.json"
    first_pipeline = pipeline_path.read_bytes()
    first_manifest = manifest_path.read_bytes()

    materialize_dashboard_bundle(results_dir=tmp_path, date_str=DATE, require_exit=True)

    assert pipeline_path.read_bytes() == first_pipeline
    assert manifest_path.read_bytes() == first_manifest


def test_grouped_measurement_wins_and_ratios_use_merged_universe(
    tmp_path: Path,
) -> None:
    pipeline = _pipeline()
    tgt = pipeline["systems"]["sys1"]["phases"][0]
    tgt.update({"count": 200, "measured": True, "source": "polygon_grouped_daily"})
    _fixtures(tmp_path, pipeline=pipeline)
    materialize_dashboard_bundle(results_dir=tmp_path, date_str=DATE, require_exit=True)
    materialized = json.loads(
        (tmp_path / f"pipeline_{COMPACT}.json").read_text(encoding="utf-8")
    )
    phases = {p["name"]: p for p in materialized["systems"]["sys1"]["phases"]}
    assert phases["Tgt"]["count"] == 200
    assert phases["FILpass"]["ratio_of_universe"] == 0.25


def test_sys7_target_one_is_valid_and_all_35_phases_are_measured(
    tmp_path: Path,
) -> None:
    _fixtures(tmp_path, signals=_signals(spy_target=1))
    manifest = materialize_dashboard_bundle(
        results_dir=tmp_path, date_str=DATE, require_exit=True
    )
    assert manifest["measurement"]["funnel_measured"] == 35


def test_date_mismatch_fails_before_writing(tmp_path: Path) -> None:
    _fixtures(tmp_path, pipeline=_pipeline(date="2026-08-12"))
    pipeline_path = tmp_path / f"pipeline_{COMPACT}.json"
    before = pipeline_path.read_bytes()
    with pytest.raises(BundleContractError, match="same-date contract"):
        materialize_dashboard_bundle(
            results_dir=tmp_path, date_str=DATE, require_exit=True
        )
    assert pipeline_path.read_bytes() == before
    assert not (tmp_path / f"dashboard_bundle_{COMPACT}.json").exists()


def test_fractional_count_and_partial_funnel_fail_closed(tmp_path: Path) -> None:
    signals = _signals()
    signals["systems"]["sys2"]["funnel"]["filter_pass"] = 49.5
    _fixtures(tmp_path, signals=signals)
    with pytest.raises(BundleContractError, match="sys2.FILpass"):
        materialize_dashboard_bundle(
            results_dir=tmp_path, date_str=DATE, require_exit=True
        )


def test_require_exit_rejects_missing_recon_without_partial_write(
    tmp_path: Path,
) -> None:
    _write(tmp_path / f"today_signals_{COMPACT}.json", _signals())
    _write(tmp_path / f"pipeline_{COMPACT}.json", _pipeline())
    before = (tmp_path / f"pipeline_{COMPACT}.json").read_bytes()
    with pytest.raises(BundleContractError, match="Exit materialization"):
        materialize_dashboard_bundle(
            results_dir=tmp_path, date_str=DATE, require_exit=True
        )
    assert (tmp_path / f"pipeline_{COMPACT}.json").read_bytes() == before
    assert not (tmp_path / f"dashboard_bundle_{COMPACT}.json").exists()


def test_same_date_stale_recon_run_is_rejected(tmp_path: Path) -> None:
    _fixtures(tmp_path)
    stale = _recon()
    stale["source_signals_run_id"] = "20260813_063700_old"
    _write(tmp_path / f"recon_{COMPACT}.json", stale)

    with pytest.raises(BundleContractError, match="source_signals_run_id"):
        materialize_dashboard_bundle(
            results_dir=tmp_path, date_str=DATE, require_exit=True
        )


def test_legacy_recon_without_run_id_must_be_newer_than_signals(tmp_path: Path) -> None:
    _fixtures(tmp_path)
    legacy = _recon()
    legacy.pop("source_signals_run_id")
    legacy["generated_at"] = "2026-08-13T13:49:00+00:00"
    _write(tmp_path / f"recon_{COMPACT}.json", legacy)
    with pytest.raises(BundleContractError, match="legacy recon"):
        materialize_dashboard_bundle(
            results_dir=tmp_path, date_str=DATE, require_exit=True
        )

    legacy["generated_at"] = "2026-08-13T13:51:00+00:00"
    _write(tmp_path / f"recon_{COMPACT}.json", legacy)
    manifest = materialize_dashboard_bundle(
        results_dir=tmp_path, date_str=DATE, require_exit=True
    )
    assert any("generated_at only" in warning for warning in manifest["warnings"])


def test_invalid_optional_artifact_is_excluded_without_blocking_core_bundle(
    tmp_path: Path,
) -> None:
    _fixtures(tmp_path)
    (tmp_path / f"narrative_{COMPACT}.json").write_text("{", encoding="utf-8")
    _write(
        tmp_path / f"alpaca_snapshot_{COMPACT}.json",
        {
            "schema": "alpaca_snapshot/v1",
            "date": DATE,
            "account": {"equity": 100_570.41},
        },
    )

    manifest = materialize_dashboard_bundle(
        results_dir=tmp_path, date_str=DATE, require_exit=True
    )

    assert "narrative" not in manifest["files"]
    assert "alpaca_snapshot" in manifest["files"]
    assert any(
        "optional narrative excluded" in warning for warning in manifest["warnings"]
    )
