"""open_auto_run の「起動の身元」と abort 時の不変条件を固定する回帰テスト。

2026-08-24 はホストの DNS が落ち、22:35 / 23:35 の 2 トリガとも
`abort=clock_unavailable` で停止した。発注は 0 件で安全だったが、成果物側に 2 つ穴が
あった:

  1. どちらの起動の記録か書いていないので、22:35 の abort 原因を 23:35 が
     同じ completion_recon.json へ上書きして消した (残ったのは 1 回ぶん)。
  2. off-hours テスト用の --allow-closed が「休場と分かっている」だけでなく
     「開場か分からない」まで一緒に素通しできてしまう。

ここで固定する契約:
  - record は run_id / trigger / observed_at を必ず持つ (F-2)。
  - 記録は run_id 別に 1 ファイル残り上書きされない。completion_recon.json は
     最新起動へのポインタとして据え置く (F-3)。
  - --allow-closed は clock_unavailable を素通ししない (F-4)。
  - gate abort は DONE.lock を作らず、注文段を 1 度も呼ばない。
  - abort した日は同日中の再試行が skip されない (2 トリガ目が走れる)。
  - 成功した run の DONE.lock は SUMMARY / completion より **先に** durable 化され、
    2 トリガ目は注文を 1 件も出さずに skip する。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
from datetime import datetime
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_module():
    path = ROOT / "scripts" / "open_auto_run.py"
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    spec = importlib.util.spec_from_file_location("open_auto_run_run_record_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


oar = _load_module()


def _args(**overrides) -> argparse.Namespace:
    base = dict(
        date="2026-08-24",
        min_signals=10,
        poll_timeout=0.0,
        dry_run=False,
        skip_signals=True,
        allow_closed=False,
        allow_clock_unknown=False,
        trigger=None,
        force=False,
        flatten_all=False,
        no_publish=True,
        primary_root=".",
        thin_aborts_run=False,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def _clock(is_open: bool):
    return SimpleNamespace(is_open=is_open, next_open="2026-08-25 09:30:00-04:00")


@pytest.fixture()
def make_runner(tmp_path, monkeypatch):
    """注文 / broker I/O を全て stub 化した Runner factory。

    `runner.submits` に注文段の呼び出しが積まれる (== 0 が「1 件も出していない」)。
    `runner.observed` に notify / publish の呼び出し順が積まれる。
    """
    monkeypatch.setattr(oar, "ROOT", tmp_path)
    monkeypatch.setattr(oar, "_CLOCK_FETCH_BACKOFF_SECONDS", 0.0)

    def _make(*, clock_open: bool | None = True, **overrides):
        runner = oar.Runner(_args(**overrides))
        submits: list[str] = []
        observed: list[str] = []

        def _client():
            if clock_open is None:  # DNS 断 = clock が読めない
                raise OSError("Failed to resolve 'paper-api.alpaca.markets'")
            return SimpleNamespace(get_clock=lambda: _clock(clock_open))

        monkeypatch.setattr(runner, "_assert_paper", lambda: None)
        monkeypatch.setattr(runner, "_ntfy_warn", lambda *a, **k: None)
        monkeypatch.setattr(runner, "_client", _client)
        monkeypatch.setattr(runner, "signals", lambda: True)
        monkeypatch.setattr(runner, "equity", lambda: 100_000.0)
        monkeypatch.setattr(runner, "exit_stage", lambda: submits.append("exit") or [])
        monkeypatch.setattr(runner, "wait_exit_fills", lambda _ids: None)
        monkeypatch.setattr(runner, "entry_stage", lambda _eq: submits.append("entry"))
        monkeypatch.setattr(runner, "reconcile_entry_fills", lambda: None)
        monkeypatch.setattr(runner, "record_stage", lambda: None)
        monkeypatch.setattr(
            runner, "notify", lambda _eq: observed.append("notify") or 0
        )
        monkeypatch.setattr(runner, "publish", lambda: observed.append("publish") or 0)
        runner.submits = submits
        runner.observed = observed
        return runner

    return _make


def _completion(runner) -> dict:
    return json.loads((runner.out / "completion_recon.json").read_text(encoding="utf-8"))


def _per_run(runner) -> dict:
    path = runner.out / f"completion_recon_{runner.run_id}.json"
    return json.loads(path.read_text(encoding="utf-8"))


# --- F-2: 起動の身元 --------------------------------------------------------
def test_record_carries_run_id_trigger_and_observed_at(make_runner):
    runner = make_runner(trigger="2235")

    assert runner.main() == 0

    for rec in (runner.record, _completion(runner), _per_run(runner)):
        assert rec["run_id"] == runner.run_id
        assert rec["trigger"] == "2235"
        assert rec["observed_at"] == runner.observed_at
    assert runner.run_id.startswith("20260824-2235-")
    # observed_at は UTC の ISO8601 (パースできること)
    assert datetime.fromisoformat(runner.observed_at)


def test_summary_names_the_run_id(make_runner):
    runner = make_runner(trigger="2335")
    runner.main()
    summary = (runner.out / "SUMMARY.md").read_text(encoding="utf-8")
    assert runner.run_id in summary
    assert "trigger=2335" in summary


@pytest.mark.parametrize(
    ("hour", "expected"),
    [(22, "2235"), (23, "2335"), (6, "manual"), (0, "manual"), (21, "manual")],
)
def test_resolve_trigger_maps_scheduler_slots(hour, expected):
    started = datetime(2026, 8, 24, hour, 35, 2)
    assert oar._resolve_trigger(None, started) == expected


def test_resolve_trigger_explicit_wins_over_clock():
    started = datetime(2026, 8, 24, 22, 35, 2)
    assert oar._resolve_trigger("manual", started) == "manual"
    assert oar._resolve_trigger("  2335  ", started) == "2335"


# --- F-3: 記録を上書きしない ------------------------------------------------
def test_first_trigger_abort_record_survives_second_trigger(make_runner):
    """2026-08-24 の再現: 22:35 の abort 原因を 23:35 が消してはいけない。"""
    first = make_runner(clock_open=None, trigger="2235")
    assert first.main() == 3
    assert first.record["abort"] == "clock_unavailable"

    second = make_runner(clock_open=True, trigger="2335")
    assert second.main() == 0

    assert first.run_id != second.run_id
    # 1 回目の記録は残ったまま (上書きされていない)
    assert _per_run(first)["abort"] == "clock_unavailable"
    assert _per_run(first)["trigger"] == "2235"
    # 2 回目は成功して abort を持たない
    assert "abort" not in _per_run(second)
    # canonical は最新起動を指す
    assert _completion(second)["run_id"] == second.run_id


def test_per_run_records_accumulate_one_file_per_trigger(make_runner):
    first = make_runner(clock_open=None, trigger="2235")
    first.main()
    second = make_runner(clock_open=None, trigger="2335")
    second.main()

    files = sorted(p.name for p in first.out.glob("completion_recon_*.json"))
    assert len(files) == 2, files
    assert all("-2235-" in f or "-2335-" in f for f in files)


# --- F-4: --allow-closed と clock 不明を分ける ------------------------------
def test_allow_closed_does_not_bypass_clock_unknown(make_runner):
    """休場の逃げ道で「開場か分からない」まで素通しさせない。"""
    runner = make_runner(clock_open=None, allow_closed=True)

    assert runner.gate() is False
    assert runner.record["abort"] == "clock_unavailable"
    assert runner.record["market_is_open"] is None
    assert "gate_bypass" not in runner.record


def test_allow_closed_still_bypasses_known_market_closed(make_runner):
    runner = make_runner(clock_open=False, allow_closed=True)

    assert runner.gate() is True
    assert runner.record["gate_bypass"] == "market_closed"
    assert "abort" not in runner.record


def test_market_closed_without_allow_closed_aborts(make_runner):
    runner = make_runner(clock_open=False)

    assert runner.gate() is False
    assert runner.record["abort"] == "market_closed"


def test_allow_clock_unknown_is_the_only_clock_escape_hatch(make_runner):
    runner = make_runner(clock_open=None, allow_clock_unknown=True)

    assert runner.gate() is True
    assert runner.record["gate_bypass"] == "clock_unavailable"
    assert "abort" not in runner.record


@pytest.fixture()
def captured_cli(monkeypatch):
    """oar.main() が組んだ argparse.Namespace を掴む (Runner は走らせない)。"""
    box: dict[str, argparse.Namespace] = {}

    class _FakeRunner:
        def __init__(self, args):
            box["args"] = args

        def main(self):
            return 0

    monkeypatch.setattr(oar, "Runner", _FakeRunner)
    return box


def test_cli_defaults_allow_clock_unknown_off(captured_cli, monkeypatch):
    monkeypatch.delenv("OPEN_RUN_ALLOW_CLOCK_UNKNOWN", raising=False)
    assert oar.main(["--date", "2026-08-24"]) == 0
    args = captured_cli["args"]
    assert args.allow_clock_unknown is False, "既定 OFF (緊急脱出ハッチ)"
    assert args.allow_closed is False
    assert args.trigger is None


def test_cli_env_can_arm_allow_clock_unknown(captured_cli, monkeypatch):
    monkeypatch.setenv("OPEN_RUN_ALLOW_CLOCK_UNKNOWN", "1")
    assert oar.main(["--date", "2026-08-24"]) == 0
    assert captured_cli["args"].allow_clock_unknown is True


# --- abort / 冪等ロックの不変条件 -------------------------------------------
def test_gate_abort_leaves_no_done_lock(make_runner):
    """gate で落ちた run は注文を 1 件も出さず、DONE.lock も残さない。"""
    runner = make_runner(clock_open=None)

    assert runner.main() == 3
    assert runner.submits == [], "gate abort で注文段を呼んではいけない"
    assert not (runner.out / "DONE.lock").exists()
    assert _completion(runner)["abort"] == "clock_unavailable"


def test_same_day_retry_after_abort_runs_again(make_runner):
    """abort は DONE.lock を作らないので、同日 2 トリガ目が必ず走る。"""
    first = make_runner(clock_open=None, trigger="2235")
    assert first.main() == 3
    assert first.submits == []

    second = make_runner(clock_open=True, trigger="2335")
    assert second.main() == 0
    assert second.submits == ["exit", "entry"], "2 回目は skip されず注文段まで行く"
    assert (second.out / "DONE.lock").exists()


def test_second_trigger_skips_after_success(make_runner):
    """成功した日の 2 トリガ目は DONE.lock で skip し、注文を重複させない。"""
    first = make_runner(clock_open=True, trigger="2235")
    assert first.main() == 0
    assert first.submits == ["exit", "entry"]

    second = make_runner(clock_open=True, trigger="2335")
    assert second.main() == 0
    assert second.submits == [], "冪等ロックがあるので 2 回目は 1 件も出さない"
    assert second.observed == [], "観測段も走らない"


def test_done_lock_written_before_observability(make_runner, monkeypatch):
    """DONE.lock は SUMMARY / completion_recon より **先に** durable 化される。

    後続の I/O が失敗しても rc!=0 の再試行で同じ注文を二度出さないための順序。
    """
    runner = make_runner()
    seen: list[tuple[str, bool]] = []
    original_write_text = Path.write_text

    def spy_write_text(path, *args, **kwargs):
        if path.parent == runner.out and path.name != "DONE.lock":
            seen.append((path.name, (runner.out / "DONE.lock").exists()))
        return original_write_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", spy_write_text)

    assert runner.main() == 0

    written = dict(seen)
    assert written["SUMMARY.md"] is True
    assert written[f"completion_recon_{runner.run_id}.json"] is True
    assert written["completion_recon.json"] is True


def test_done_lock_survives_completion_write_failure(make_runner, monkeypatch):
    """記録の書き込みが両方失敗しても、DONE.lock と rc=4 は保たれる。"""
    runner = make_runner()

    def fail_dump(name, _obj):
        raise OSError(f"disk error: {name}")

    monkeypatch.setattr(runner, "_dump", fail_dump)
    monkeypatch.setattr(runner, "publish", lambda: 9)

    assert runner.main() == 4
    assert (runner.out / "DONE.lock").exists()
    assert runner.record["completion_recon_run_write_error"]
    assert runner.record["completion_recon_write_error"]
