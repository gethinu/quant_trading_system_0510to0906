"""薄シグナル時に exit が止まらないことの回帰テスト (2026-07-27 A1)。

背景
----
``open_auto_run.py`` の薄シグナルゲートは ``signals()`` の中で run 全体を ABORT
していた。orchestration が::

    if not self.signals():
        self.finalize(aborted=True)
        return 3
    ...
    market_ids = self.exit_stage()      # <- 到達しない

という並びだったため、シグナルが閾値に 1 本届かないだけで **exit_stage() に到達
せず**、時間 exit が 2026-07-21..24 の 4 営業日停止し 20 建玉が期限超過した。
exit は保有ポジションのみに依存し today_signals JSON を一切参照しないので、薄
シグナルでも安全に実行できる。よって薄シグナルは entry 専用ゲートへ降格した。

ここで固定する契約:
  1. 薄シグナルでも exit_stage() は必ず走る (回帰の本丸)。
  2. 薄シグナルでは entry_stage() は走らない (絞り込みは従来どおり)。
  3. 薄シグナルでも run は ABORT せず exit code 0 で完走する。
  4. 潤沢シグナルでは exit も entry も走る (既存挙動の非退行)。
  5. 切り戻しスイッチで旧挙動 (run 全体 ABORT) に戻せる。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_module():
    """scripts/open_auto_run.py を単体 import する (パッケージ化されていないため)。"""
    path = ROOT / "scripts" / "open_auto_run.py"
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    spec = importlib.util.spec_from_file_location("open_auto_run_under_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


oar = _load_module()


def _write_signals(root: Path, date_compact: str, n: int) -> None:
    """systems ブロックに合計 n 本のシグナルを持つ today_signals JSON を書く。"""
    results = root / "results_csv"
    results.mkdir(parents=True, exist_ok=True)
    payload = {
        "date": f"{date_compact[:4]}-{date_compact[4:6]}-{date_compact[6:8]}",
        "systems": {"system1": {"signals": [{"symbol": f"S{i}"} for i in range(n)]}},
    }
    (results / f"today_signals_{date_compact}.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )


class _Recorder:
    """段の実行順を記録するだけの stub runner。"""

    def __init__(self) -> None:
        self.calls: list[str] = []


@pytest.fixture()
def runner(tmp_path, monkeypatch):
    """ROOT を tmp に差し替え、broker/通知に触れない Runner を組み立てる factory。"""
    monkeypatch.setattr(oar, "ROOT", tmp_path)

    def _make(signal_count: int, **overrides):
        date = "2026-07-27"
        compact = date.replace("-", "")
        _write_signals(tmp_path, compact, signal_count)

        args = _args(date=date, **overrides)
        r = oar.Runner(args)
        rec = _Recorder()

        # --- 外部 I/O を全て封じる (paper でも発注させない) ---
        monkeypatch.setattr(r, "_assert_paper", lambda: None)
        monkeypatch.setattr(r, "_ntfy_warn", lambda *a, **k: None)
        monkeypatch.setattr(r, "equity", lambda: 100_000.0)
        monkeypatch.setattr(r, "wait_exit_fills", lambda ids: None)
        monkeypatch.setattr(r, "record_stage", lambda: None)
        monkeypatch.setattr(r, "publish", lambda: 0)
        monkeypatch.setattr(r, "notify", lambda eq: 0)

        def _exit_stage():
            rec.calls.append("exit")
            r.record["exit_count"] = 23
            return []

        def _entry_stage(eq):
            rec.calls.append("entry")

        monkeypatch.setattr(r, "exit_stage", _exit_stage)
        monkeypatch.setattr(r, "entry_stage", _entry_stage)

        # market open を成立させる (clock を叩かない)
        monkeypatch.setattr(r, "gate", lambda: True)
        return r, rec

    return _make


def _args(**kw):
    import argparse

    base = dict(
        date=None,
        min_signals=10,
        poll_timeout=1.0,
        dry_run=False,
        skip_signals=True,  # signal 再生成の subprocess を回避
        allow_closed=True,
        force=True,
        flatten_all=False,
        no_publish=True,
        primary_root=".",
        thin_aborts_run=False,
    )
    base.update(kw)
    return argparse.Namespace(**base)


# ---------------------------------------------------------------------------
# 1) 本丸: 薄シグナルでも exit は走る
# ---------------------------------------------------------------------------


def test_thin_signals_still_runs_exit(runner):
    r, rec = runner(signal_count=9)  # 実測の 9 本 (閾値 10 に 1 本届かない)

    code = r.main()

    assert "exit" in rec.calls, (
        "薄シグナルで exit_stage() に到達しなかった: これが 07-21..24 に "
        "時間 exit を 4 営業日止めた回帰そのもの"
    )
    assert code == 0, "薄シグナルは run を ABORT させてはならない"
    assert r.record.get("abort") is None


# ---------------------------------------------------------------------------
# 2) entry は従来どおり絞られる
# ---------------------------------------------------------------------------


def test_thin_signals_skips_entry(runner):
    r, rec = runner(signal_count=9)

    r.main()

    assert "entry" not in rec.calls, "薄シグナルで新規 entry を出してはならない"
    assert r.entry_allowed is False
    assert r.record["entry_skip_reason"] == "thin_signals:9<10"
    assert r.record["entry_status"] == "skipped_thin_signals"


def test_thin_signals_exit_runs_before_entry_skip(runner):
    """exit->entry の順序契約は維持されたままであること。"""
    r, rec = runner(signal_count=0)  # signals JSON が空でも exit は通す

    code = r.main()

    assert rec.calls == ["exit"]
    assert code == 0


# ---------------------------------------------------------------------------
# 3) 潤沢シグナルの非退行
# ---------------------------------------------------------------------------


def test_rich_signals_runs_exit_then_entry(runner):
    r, rec = runner(signal_count=44)

    code = r.main()

    assert rec.calls == ["exit", "entry"], "exit->entry の順序が壊れている"
    assert code == 0
    assert r.entry_allowed is True
    assert "entry_skip_reason" not in r.record


def test_threshold_boundary_exactly_at_min_allows_entry(runner):
    """n == min_signals は薄くない (境界の off-by-one を固定)。"""
    r, rec = runner(signal_count=10)

    r.main()

    assert rec.calls == ["exit", "entry"]
    assert r.entry_allowed is True


# ---------------------------------------------------------------------------
# 4) 切り戻しスイッチ
# ---------------------------------------------------------------------------


def test_rollback_switch_restores_full_abort(runner):
    """--thin-aborts-run で旧挙動 (exit も止まる run 全体 ABORT) に戻せる。"""
    r, rec = runner(signal_count=9, thin_aborts_run=True)

    code = r.main()

    assert code == 3
    assert rec.calls == [], "切り戻し時は旧挙動どおり exit にも到達しない"
    assert r.record["abort"] == "thin_signals:9<10"


def test_env_flag_parses_truthy_values(monkeypatch):
    for raw, expected in [
        ("1", True),
        ("true", True),
        ("YES", True),
        ("on", True),
        ("0", False),
        ("false", False),
        ("", False),
    ]:
        monkeypatch.setenv("OPEN_RUN_THIN_ABORTS_RUN", raw)
        assert oar._env_flag("OPEN_RUN_THIN_ABORTS_RUN", False) is expected, raw
    monkeypatch.delenv("OPEN_RUN_THIN_ABORTS_RUN", raising=False)
    assert oar._env_flag("OPEN_RUN_THIN_ABORTS_RUN", False) is False


# ---------------------------------------------------------------------------
# 5) 構造的保証: exit 経路は signals JSON に依存しない
# ---------------------------------------------------------------------------


def test_exit_check_script_reads_signals_only_for_provenance():
    """exit **判断** が today_signals に依存しないことを固定する (A1 の前提)。

    2026-08-17 の 8976bba (`feat(obs): stamp signals run_id ...`) で
    ``paper_exit_check`` は recon 突合用の provenance として run_id **だけ** を
    読むようになった。したがって「文字列 today_signals が出てこないこと」では
    もう固定できない (A1 の契約はそこではない)。代わりに構造で固定する:

      1. today_signals を読む関数は provenance 用の ``_signals_run_id`` 1 つだけ。
      2. その戻り値は artifact の meta にしか流れず、exit の生成 / 発注を
         gate しない (= 呼び出しは 1 箇所、結果は分岐条件に使われない)。

    ここが崩れたら「薄シグナルでも exit を通してよい」根拠が崩れるので落とす。
    """
    import ast

    src = (ROOT / "scripts" / "paper_exit_check.py").read_text(encoding="utf-8")
    tree = ast.parse(src)

    # (1) today_signals を読む関数は _signals_run_id だけ
    readers = sorted(
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and "today_signals" in (ast.get_source_segment(src, node) or "")
    )
    assert readers == ["_signals_run_id"], readers

    # (2) 呼び出しは 1 箇所で、結果が if / while の条件に入っていない
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_signals_run_id"
    ]
    assert len(calls) == 1, [ast.dump(c) for c in calls]
    for node in ast.walk(tree):
        if isinstance(node, (ast.If, ast.While)):
            test_src = ast.get_source_segment(src, node.test) or ""
            assert "_signals_run_id" not in test_src, test_src
            assert "signals_run_id" not in test_src, test_src
