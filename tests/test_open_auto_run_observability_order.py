"""open_auto_run の notify -> publish と失敗伝播を固定する回帰テスト。

注文段が完了した後の observability 契約:
  1. execution summary が recon/pipeline を更新してから dashboard publish する。
  2. notify/publish の片方が失敗しても、もう片方を必ず実行する。
  3. 注文の重複再実行を防ぐ DONE.lock を作成後、観測失敗を rc=4 で返す。
  4. dry-run / --no-publish の外部 publish skip は成功 (rc=0) とする。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_module():
    path = ROOT / "scripts" / "open_auto_run.py"
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    spec = importlib.util.spec_from_file_location(
        "open_auto_run_observability_order_test", path
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


oar = _load_module()


def _args(**overrides) -> argparse.Namespace:
    base = dict(
        date="2026-08-13",
        min_signals=10,
        poll_timeout=0.0,
        dry_run=False,
        skip_signals=True,
        allow_closed=True,
        force=True,
        flatten_all=False,
        no_publish=False,
        primary_root=".",
        thin_aborts_run=False,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


@pytest.fixture()
def make_runner(tmp_path, monkeypatch):
    """注文・broker I/O を全てstub化した Runner factory。"""

    monkeypatch.setattr(oar, "ROOT", tmp_path)

    def _make(**overrides):
        runner = oar.Runner(_args(**overrides))
        monkeypatch.setattr(runner, "gate", lambda: True)
        monkeypatch.setattr(runner, "signals", lambda: True)
        monkeypatch.setattr(runner, "equity", lambda: 100_000.0)
        monkeypatch.setattr(runner, "exit_stage", lambda: [])
        monkeypatch.setattr(runner, "wait_exit_fills", lambda _ids: None)
        monkeypatch.setattr(runner, "entry_stage", lambda _eq: None)
        monkeypatch.setattr(runner, "record_stage", lambda: None)
        return runner

    return _make


def _completion(runner) -> dict:
    return json.loads(
        (runner.out / "completion_recon.json").read_text(encoding="utf-8")
    )


def test_main_runs_notify_before_publish_and_records_codes(make_runner, monkeypatch):
    runner = make_runner()
    calls: list[str] = []

    monkeypatch.setattr(runner, "notify", lambda _eq: calls.append("notify") or 0)
    monkeypatch.setattr(runner, "publish", lambda: calls.append("publish") or 0)

    code = runner.main()

    assert calls == ["notify", "publish"]
    assert code == 0
    assert runner.record["notify_exit_code"] == 0
    assert runner.record["publish_exit_code"] == 0
    assert (runner.out / "DONE.lock").exists()


@pytest.mark.parametrize(
    ("notify_code", "publish_code"),
    [(7, 0), (0, 9), (7, 9)],
)
def test_observability_failure_runs_both_then_done_and_returns_four(
    make_runner, monkeypatch, notify_code, publish_code
):
    runner = make_runner()
    calls: list[str] = []

    monkeypatch.setattr(
        runner, "notify", lambda _eq: calls.append("notify") or notify_code
    )
    monkeypatch.setattr(
        runner, "publish", lambda: calls.append("publish") or publish_code
    )

    code = runner.main()

    assert calls == ["notify", "publish"], "先行段失敗でも後続段を必ず実行する"
    assert code == 4
    assert (runner.out / "DONE.lock").exists(), "rc=4 を返す前に注文完了を固定する"
    saved = _completion(runner)
    assert saved["notify_exit_code"] == notify_code
    assert saved["publish_exit_code"] == publish_code


def test_summary_write_failure_keeps_done_and_observability_exit_code(
    make_runner, monkeypatch
):
    runner = make_runner()
    monkeypatch.setattr(runner, "notify", lambda _eq: 7)
    monkeypatch.setattr(runner, "publish", lambda: 0)
    original_write_text = Path.write_text

    def fail_summary_write(path, *args, **kwargs):
        if path == runner.out / "SUMMARY.md":
            raise OSError("summary disk error")
        return original_write_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail_summary_write)

    code = runner.main()

    assert code == 4
    assert (runner.out / "DONE.lock").exists()
    saved = _completion(runner)
    assert "OSError" in saved["summary_write_error"]
    assert saved["notify_exit_code"] == 7


def test_completion_dump_exception_keeps_done_and_observability_exit_code(
    make_runner, monkeypatch
):
    runner = make_runner()
    monkeypatch.setattr(runner, "notify", lambda _eq: 0)
    monkeypatch.setattr(runner, "publish", lambda: 9)
    original_dump = runner._dump

    def fail_completion_dump(name, obj):
        if name == "completion_recon.json":
            raise OSError("completion disk error")
        return original_dump(name, obj)

    monkeypatch.setattr(runner, "_dump", fail_completion_dump)

    code = runner.main()

    assert code == 4
    assert (runner.out / "DONE.lock").exists()
    assert (runner.out / "SUMMARY.md").exists()
    assert "OSError" in runner.record["completion_recon_write_error"]


def test_unexpected_notify_exception_still_runs_publish(make_runner, monkeypatch):
    runner = make_runner()
    calls: list[str] = []

    def broken_notify(_eq):
        calls.append("notify")
        raise RuntimeError("ntfy boom")

    monkeypatch.setattr(runner, "notify", broken_notify)
    monkeypatch.setattr(runner, "publish", lambda: calls.append("publish") or 0)

    code = runner.main()

    assert calls == ["notify", "publish"]
    assert code == 4
    assert runner.record["notify_exit_code"] == 1
    assert (runner.out / "DONE.lock").exists()


def test_notify_returns_and_records_child_exit_code(make_runner, monkeypatch):
    runner = make_runner()
    monkeypatch.setattr(runner, "run_step", lambda _name, _argv: (6, "", ""))

    code = runner.notify(100_000.0)

    assert code == 6
    assert runner.record["notify_exit_code"] == 6


def test_stale_recon_unlink_failure_is_not_reported_as_notify_success(
    make_runner, monkeypatch
):
    runner = make_runner()
    stale = runner.results / f"recon_{runner.compact}.json"
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text('{"source_signals_run_id":"old-run"}', encoding="utf-8")
    original_unlink = Path.unlink

    def fail_stale_unlink(path, *args, **kwargs):
        if path == stale:
            raise PermissionError("locked stale recon")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_stale_unlink)
    child_calls: list[str] = []
    publish_calls: list[str] = []
    monkeypatch.setattr(
        runner,
        "run_step",
        lambda name, _argv: child_calls.append(name) or (0, "", ""),
    )
    monkeypatch.setattr(runner, "publish", lambda: publish_calls.append("publish") or 0)

    code = runner.main()

    assert child_calls == [], "残存する古い recon を summary 子プロセスへ渡さない"
    assert publish_calls == ["publish"], "notify 失敗後も publish は必ず試す"
    assert stale.exists()
    assert code == 4
    assert runner.record["notify_exit_code"] == 1
    assert runner.record["notify_status"] == "stale_recon_unlink_failed"
    assert "PermissionError" in runner.record["notify_stale_recon_error"]
    assert (runner.out / "DONE.lock").exists()


def test_no_publish_is_successful_skip(make_runner, monkeypatch):
    runner = make_runner(no_publish=True)

    def should_not_run(*_args, **_kwargs):
        raise AssertionError("--no-publish で subprocess を実行してはならない")

    monkeypatch.setattr(runner, "run_step", should_not_run)

    assert runner.publish() == 0
    assert runner.record["publish"] == "skipped_no_publish"
    assert runner.record["publish_exit_code"] == 0


def test_main_no_publish_mode_returns_zero(make_runner, monkeypatch):
    runner = make_runner(no_publish=True)
    monkeypatch.setattr(runner, "notify", lambda _eq: 0)

    assert runner.main() == 0
    assert runner.record["notify_exit_code"] == 0
    assert runner.record["publish_exit_code"] == 0
    assert (runner.out / "DONE.lock").exists()


def test_dry_run_publish_skip_returns_zero_and_keeps_snapshot_code(
    make_runner, monkeypatch
):
    runner = make_runner(dry_run=True)
    monkeypatch.setattr(runner, "run_step", lambda _name, _argv: (5, "", ""))

    assert runner.publish() == 0
    assert runner.record["snapshot_exit_code"] == 5
    assert runner.record["publish"] == "skipped_dry_run"
    assert runner.record["publish_exit_code"] == 0


def test_main_dry_run_returns_zero_without_done(make_runner, monkeypatch):
    runner = make_runner(dry_run=True)
    monkeypatch.setattr(runner, "notify", lambda _eq: 0)
    monkeypatch.setattr(runner, "run_step", lambda _name, _argv: (0, "", ""))

    assert runner.main() == 0
    assert runner.record["notify_exit_code"] == 0
    assert runner.record["publish_exit_code"] == 0
    assert not (runner.out / "DONE.lock").exists()


def test_publish_returns_and_records_vercel_exit_code(
    make_runner, monkeypatch, tmp_path
):
    primary = tmp_path / "primary"
    script = primary / "scripts" / "publish_data_to_vercel.ps1"
    script.parent.mkdir(parents=True)
    script.write_text("# test stub\n", encoding="utf-8")
    runner = make_runner(primary_root=str(primary))

    monkeypatch.setattr(runner, "run_step", lambda _name, _argv: (0, "", ""))
    monkeypatch.setattr(
        oar.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=8, stdout="publish failed", stderr=""
        ),
    )

    assert runner.publish() == 8
    assert runner.record["snapshot_exit_code"] == 0
    assert runner.record["vercel_publish_exit_code"] == 8
    assert runner.record["publish_exit_code"] == 8


def test_wrapper_consumes_reset_marker_only_after_completed_trade_codes():
    """0/4 は注文完了、pre-trade abort 3 等は未完了として marker を保持する。"""

    wrapper = (ROOT / "scripts" / "open_auto_run.ps1").read_text(encoding="utf-8")
    assert "if (($code -eq 0) -or ($code -eq 4))" in wrapper
    assert "($code -eq 3)" not in wrapper
    assert "($code -eq 2)" not in wrapper
