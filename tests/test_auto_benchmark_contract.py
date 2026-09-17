import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.pipeline.benchmark import LightweightBenchmark
import tools.auto_benchmark as auto_benchmark


def _sample_report(total: float = 1.25) -> dict:
    return {
        "enabled": True,
        "phase_times": {"phase0_initialization": total},
        "total_time": total,
    }


def test_lightweight_report_keeps_auto_benchmark_compat_fields():
    bench = LightweightBenchmark(enabled=True)
    bench.phases["phase0_initialization"] = {
        "start": 0.0,
        "end": 1.25,
        "duration_sec": 1.25,
    }

    report = bench.get_report()

    assert report["phase_times"] == {"phase0_initialization": 1.25}
    assert report["total_time"] == 1.25
    assert report["total_duration_sec"] == 1.25


def test_run_benchmark_reads_only_explicit_current_report(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    stale = tmp_path / "results_csv_test" / "benchmark_stale.json"
    stale.parent.mkdir(parents=True)
    stale.write_text(json.dumps(_sample_report(999.0)), encoding="utf-8")

    def fake_run(argv, **_kwargs):
        output = Path(argv[argv.index("--benchmark-output") + 1])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(_sample_report(1.25)), encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(auto_benchmark.subprocess, "run", fake_run)

    report = auto_benchmark.run_benchmark()

    assert report is not None
    assert report["total_time"] == 1.25


def test_run_benchmark_fails_closed_when_current_report_is_missing(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    stale = tmp_path / "results_csv_test" / "benchmark_stale.json"
    stale.parent.mkdir(parents=True)
    stale.write_text(json.dumps(_sample_report(999.0)), encoding="utf-8")
    monkeypatch.setattr(
        auto_benchmark.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )

    assert auto_benchmark.run_benchmark() is None


def test_cli_parser_accepts_benchmark_output(tmp_path):
    import scripts.run_all_systems_today as runner

    output = tmp_path / "benchmark.json"
    args = runner.build_cli_parser().parse_args(
        ["--benchmark", "--benchmark-output", str(output)]
    )

    assert args.benchmark is True
    assert args.benchmark_output == str(output)


def test_run_signal_pipeline_persists_requested_benchmark_report(tmp_path, monkeypatch):
    import pandas as pd

    import scripts.run_all_systems_today as runner

    output = tmp_path / "current-benchmark.json"
    args = SimpleNamespace(
        full_scan_today=False,
        filter_debug=False,
        detailed_perf=False,
        benchmark=True,
        benchmark_output=str(output),
        perf_snapshot=False,
        symbols=[],
        slots_long=None,
        slots_short=None,
        capital_long=None,
        capital_short=None,
        save_csv=False,
        csv_name_mode=None,
        parallel=False,
        test_mode="mini",
        skip_external=True,
        skip_latest_check=True,
    )
    monkeypatch.setattr(
        runner,
        "compute_today_signals",
        lambda *_args, **_kwargs: (pd.DataFrame(), {}),
    )
    monkeypatch.setattr(runner, "_log", lambda *_args, **_kwargs: None)

    runner.run_signal_pipeline(args)

    assert output.exists()
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["enabled"] is True
    assert "phase_times" in report
    assert "total_time" in report


def test_history_scope_repo_preserves_legacy_path():
    assert auto_benchmark._resolve_history_path("repo") == Path(
        "benchmarks/history.jsonl"
    )


def test_history_scope_git_common_uses_git_metadata(tmp_path, monkeypatch):
    common_dir = tmp_path / "git-common"
    monkeypatch.setattr(
        auto_benchmark.subprocess,
        "check_output",
        lambda *_args, **_kwargs: str(common_dir),
    )

    assert auto_benchmark._resolve_history_path("git-common") == (
        common_dir / "auto_benchmark_history.jsonl"
    )


def test_compare_ignores_relative_noise_below_absolute_phase_floor():
    baseline = {"phase_times": {"tiny": 0.02}, "total_time": 10.0}
    current = {"phase_times": {"tiny": 0.04}, "total_time": 10.0}

    assert (
        auto_benchmark.compare_with_baseline(
            current,
            baseline,
            threshold=0.10,
            min_phase_regression_sec=0.5,
        )
        == []
    )


def test_compare_flags_phase_when_relative_and_absolute_limits_are_exceeded():
    baseline = {"phase_times": {"signal": 3.0}, "total_time": 10.0}
    current = {"phase_times": {"signal": 3.7}, "total_time": 10.0}

    regressions = auto_benchmark.compare_with_baseline(
        current,
        baseline,
        threshold=0.10,
        min_phase_regression_sec=0.5,
    )

    assert [row["phase"] for row in regressions] == ["signal"]
    assert regressions[0]["delta_sec"] == pytest.approx(0.7)


def test_total_regression_is_not_suppressed_by_phase_floor():
    baseline = {"phase_times": {"tiny": 0.02}, "total_time": 10.0}
    current = {"phase_times": {"tiny": 0.04}, "total_time": 11.2}

    regressions = auto_benchmark.compare_with_baseline(
        current,
        baseline,
        threshold=0.10,
        min_phase_regression_sec=0.5,
    )

    assert [row["phase"] for row in regressions] == ["TOTAL"]
    assert regressions[0]["delta_sec"] == pytest.approx(1.2)


def test_observed_same_main_noise_shape_does_not_block_when_total_improves():
    baseline = {
        "phase_times": {
            "phase0_initialization": 1.034822,
            "phase1_symbol_universe": 0.023667,
            "phase2_data_loading": 46.562672,
            "phase3_filtering": 0.006432,
            "phase4_signal_generation": 3.022753,
            "phase5_allocation": 1.171627,
        },
        "total_time": 59.762028,
    }
    current = {
        "phase_times": {
            "phase0_initialization": 1.277723,
            "phase1_symbol_universe": 0.040788,
            "phase2_data_loading": 43.753735,
            "phase3_filtering": 0.008461,
            "phase4_signal_generation": 3.453516,
            "phase5_allocation": 1.310667,
        },
        "total_time": 49.84489,
    }

    assert (
        auto_benchmark.compare_with_baseline(
            current,
            baseline,
            threshold=0.10,
            min_phase_regression_sec=0.5,
        )
        == []
    )
