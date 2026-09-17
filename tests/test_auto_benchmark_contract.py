import json
from pathlib import Path
from types import SimpleNamespace

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
