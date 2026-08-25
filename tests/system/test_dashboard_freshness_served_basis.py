"""served の判定基準が origin ref であることを固定する回帰テスト.

2026-08-19 の誤警報: publish_data_to_vercel.ps1 は `git commit-tree` で origin tip に
commit を作って直接 push し、**local の working tree / branch ref を一切触らない**。
そのため freshness チェックがローカル data/ を served とみなしていた間は、origin が
当日分を配信済みでも毎日 STALE(exit=2) を出し続けていた。ここではその構図を実物の
git リポジトリで再現し、ref 基準なら fresh、local 基準なら stale になることを固定する。
"""

import json
from pathlib import Path
import subprocess
import sys

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.check_dashboard_freshness import (  # noqa: E402
    check_freshness,
    newest_bundle_ref,
    newest_signal_date_ref,
)

DATA_REL = "apps/dashboards/alpaca-next/data"


def _git(root: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return proc.stdout


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """data/ に 08-19 をコミットしたあと、作業ツリーだけ 08-11 に巻き戻す。

    = publish が origin にだけ書き、ローカルツリーが取り残された実際の状態。
    """
    root = tmp_path / "repo"
    (root / DATA_REL).mkdir(parents=True)
    _git(root.parent, "init", "--quiet", str(root))
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")

    data = root / DATA_REL
    _write(data / "today_signals_20260819.json", {"generated_at": "2026-08-19T00:00Z"})
    _write(
        data / "dashboard_bundle_20260819.json",
        {"source_run_id": "20260819_080000_aaa"},
    )
    _git(root, "add", "-A")
    _git(root, "commit", "--quiet", "-m", "publish 08-19")
    _git(root, "branch", "-f", "publishref")

    # 作業ツリーだけ古い状態にする (commit はしない = origin は 08-19 のまま)
    for f in data.iterdir():
        f.unlink()
    _write(data / "today_signals_20260811.json", {"generated_at": "2026-08-11T00:00Z"})
    return root


def test_ref_basis_reports_fresh_when_origin_is_current(repo: Path) -> None:
    """origin が当日分を持つなら、ローカル data/ が古くても fresh。"""
    results = repo / "results_csv"
    _write(results / "today_signals_20260819.json", {"generated_at": "x"})

    res = check_freshness(
        results,
        repo / DATA_REL,
        repo_root=repo,
        served_ref="publishref",
        fetch=False,
    )
    assert res["status"] == "fresh"
    assert res["data_date"] == 20260819
    assert res["served_basis"] == "ref:publishref"


def test_local_basis_still_reports_stale(repo: Path) -> None:
    """旧挙動 (local 基準) は従来どおり stale — 誤警報の再現。"""
    results = repo / "results_csv"
    _write(results / "today_signals_20260819.json", {"generated_at": "x"})

    res = check_freshness(results, repo / DATA_REL)
    assert res["status"] == "stale"
    assert res["data_date"] == 20260811
    assert res["served_basis"] == "local"


def test_unresolvable_ref_falls_back_to_local(repo: Path) -> None:
    """ref が読めない (offline / ref 不明) ときは旧挙動に安全に落ちる。"""
    results = repo / "results_csv"
    _write(results / "today_signals_20260819.json", {"generated_at": "x"})

    res = check_freshness(
        results,
        repo / DATA_REL,
        repo_root=repo,
        served_ref="origin/does-not-exist",
        fetch=False,
    )
    assert res["served_basis"] == "local"
    assert res["data_date"] == 20260811


def test_genuine_gap_is_still_detected(repo: Path) -> None:
    """ref 基準にしても本物の publish 落ちは stale のまま (警報を殺さない)。"""
    results = repo / "results_csv"
    _write(results / "today_signals_20260820.json", {"generated_at": "x"})

    res = check_freshness(
        results,
        repo / DATA_REL,
        repo_root=repo,
        served_ref="publishref",
        fetch=False,
    )
    assert res["status"] == "stale"
    assert res["results_date"] == 20260820
    assert res["data_date"] == 20260819


def test_newest_signal_date_ref_reads_committed_tree(repo: Path) -> None:
    assert newest_signal_date_ref(repo, "publishref") == 20260819


def test_newest_bundle_ref_reads_manifest_and_commit_time(repo: Path) -> None:
    manifest = newest_bundle_ref(repo, "publishref")
    assert manifest is not None
    assert manifest["source_run_id"] == "20260819_080000_aaa"
    # ref 基準の manifest は mtime を持たないので commit 時刻で経過を測る
    assert manifest["_committed_iso"]
