"""publish した run が **本番に届いたか** を見る watchdog の検証。

2026-08-17 incident: `publish_data_to_vercel.ps1` の `verify OK` は origin の
git blob 同士を比べているだけで、Vercel が実際に build/deploy したかを見ていない。
その結果「publish 成功・本番 stale」を検出できなかった (本番は朝の run を表示し続けた)。

既存の freshness 判定も results_csv と data/ という **どちらもローカル** の比較なので
同じ盲点を持つ。ここでは描画済み HTML に manifest の run_id が現れるかで到達を判定する
(dashboard は静的 export で data/*.json を build 時に取り込むため、JSON は URL で
配信されない = HTML を見るしかない)。

正常な deploy 遅延を誤検知しないこと (2026-08-17 実測で約 20 分) も併せて固定する。
"""

from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import check_dashboard_freshness as cdf  # noqa: E402

RUN = "20260817_223506_185304"


def _write_manifest(data_dir: Path, run_id: str | None, *, name: str = "20260817"):
    payload: dict = {"schema": "dashboard_bundle/v1", "date": "2026-08-17"}
    if run_id is not None:
        payload["source_run_id"] = run_id
    p = data_dir / f"dashboard_bundle_{name}.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


def _patch_fetch(monkeypatch, html: str | None):
    monkeypatch.setattr(cdf, "_fetch_served_html", lambda url, timeout: html)


def _patch_age(monkeypatch, minutes: float | None):
    monkeypatch.setattr(cdf, "_minutes_since", lambda path: minutes)


def test_run_id_present_in_html_is_served(tmp_path: Path, monkeypatch):
    _write_manifest(tmp_path, RUN)
    _patch_fetch(monkeypatch, f"<html>as of 2026-08-17 · run {RUN}</html>")
    _patch_age(monkeypatch, 3.0)
    r = cdf.check_served_run(tmp_path)
    assert r["status"] == "served"
    assert r["run_id"] == RUN


def test_absent_run_id_within_grace_is_lagging(tmp_path: Path, monkeypatch):
    """正常な deploy 遅延を「不達」と誤検知しない (実測 ~20 分の実績あり)。"""
    _write_manifest(tmp_path, RUN)
    _patch_fetch(monkeypatch, "<html>run 20260817_060721_dbdc19</html>")
    _patch_age(monkeypatch, 5.0)
    r = cdf.check_served_run(tmp_path, grace_minutes=30)
    assert r["status"] == "deploy_lagging"


def test_absent_run_id_past_grace_is_missing(tmp_path: Path, monkeypatch):
    """grace を超えても出てこなければ build 不達として警告する。"""
    _write_manifest(tmp_path, RUN)
    _patch_fetch(monkeypatch, "<html>run 20260817_060721_dbdc19</html>")
    _patch_age(monkeypatch, 45.0)
    r = cdf.check_served_run(tmp_path, grace_minutes=30)
    assert r["status"] == "deploy_missing"
    assert r["run_id"] == RUN


def test_20_minute_lag_is_still_tolerated(tmp_path: Path, monkeypatch):
    """2026-08-17 の実測値 (約 20 分) が既定 grace 内に収まること。"""
    _write_manifest(tmp_path, RUN)
    _patch_fetch(monkeypatch, "<html>old</html>")
    _patch_age(monkeypatch, 20.0)
    r = cdf.check_served_run(tmp_path)
    assert r["status"] == "deploy_lagging"


def test_fetch_failure_is_unknown_not_missing(tmp_path: Path, monkeypatch):
    """ネットワーク失敗を「不達」と断定しない (誤警報を出さない)。"""
    _write_manifest(tmp_path, RUN)
    _patch_fetch(monkeypatch, None)
    _patch_age(monkeypatch, 60.0)
    r = cdf.check_served_run(tmp_path)
    assert r["status"] == "unknown"
    assert r["reason"] == "fetch_failed"


def test_missing_manifest_is_unknown(tmp_path: Path, monkeypatch):
    _patch_fetch(monkeypatch, "<html/>")
    r = cdf.check_served_run(tmp_path)
    assert r["status"] == "unknown"
    assert r["reason"] == "no_bundle_manifest"


def test_manifest_without_run_id_is_unknown(tmp_path: Path, monkeypatch):
    _write_manifest(tmp_path, None)
    _patch_fetch(monkeypatch, "<html/>")
    r = cdf.check_served_run(tmp_path)
    assert r["status"] == "unknown"
    assert r["reason"] == "manifest_has_no_run_id"


def test_newest_manifest_wins(tmp_path: Path, monkeypatch):
    """複数日ぶんある時は最新 manifest の run_id を見る。"""
    _write_manifest(tmp_path, "old_run", name="20260816")
    _write_manifest(tmp_path, RUN, name="20260817")
    _patch_fetch(monkeypatch, f"<html>{RUN}</html>")
    _patch_age(monkeypatch, 1.0)
    r = cdf.check_served_run(tmp_path)
    assert r["run_id"] == RUN
    assert r["status"] == "served"


def test_broken_manifest_json_is_unknown(tmp_path: Path, monkeypatch):
    (tmp_path / "dashboard_bundle_20260817.json").write_text(
        "{ not json", encoding="utf-8"
    )
    _patch_fetch(monkeypatch, "<html/>")
    r = cdf.check_served_run(tmp_path)
    assert r["status"] == "unknown"


@pytest.mark.parametrize("status", ["served", "deploy_lagging", "unknown"])
def test_non_missing_statuses_do_not_exit_3(tmp_path: Path, monkeypatch, status):
    """警告 exit=3 は deploy_missing の時だけ (誤警報で運用を鈍らせない)。"""
    _write_manifest(tmp_path, RUN)
    html = f"<html>{RUN}</html>" if status == "served" else "<html>old</html>"
    _patch_fetch(monkeypatch, None if status == "unknown" else html)
    _patch_age(monkeypatch, 1.0 if status != "unknown" else 99.0)
    results = tmp_path / "results_csv"
    results.mkdir()
    rc = cdf.main(
        [
            "--repo-root",
            str(tmp_path),
            "--results-dir",
            str(results),
            "--data-dir",
            str(tmp_path),
            "--check-served",
        ]
    )
    assert rc != 3
