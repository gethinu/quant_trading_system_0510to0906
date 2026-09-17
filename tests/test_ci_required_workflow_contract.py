from pathlib import Path
import re

WORKFLOW = (
    Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci-unified.yml"
)


def _pull_request_block() -> str:
    text = WORKFLOW.read_text(encoding="utf-8")
    return text.split("  pull_request:\n", 1)[1].split("  workflow_dispatch:\n", 1)[0]


def _job_block(job_id: str) -> str:
    text = WORKFLOW.read_text(encoding="utf-8")
    tail = text.split(f"  {job_id}:\n", 1)[1]
    return re.split(r"\n  [a-z0-9-]+:\n", tail, maxsplit=1)[0]


def test_pull_request_trigger_is_not_path_filtered() -> None:
    assert "paths-ignore:" not in _pull_request_block()


def test_heavy_jobs_are_gated_by_ci_scope() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "  ci-scope:\n" in text
    for job_id in (
        "dashboard-build",
        "powershell-contract",
        "lint-and-format",
    ):
        block = _job_block(job_id)
        assert "needs: ci-scope" in block
        assert "if: needs.ci-scope.outputs.run_full == 'true'" in block


def test_ci_required_aggregates_all_ci_results() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "  ci-required:\n" in text
    block = _job_block("ci-required")
    assert "name: CI Required" in block
    assert "if: always()" in block
    assert "test-and-coverage" in block
    assert "needs.ci-scope.outputs.run_full" in block
