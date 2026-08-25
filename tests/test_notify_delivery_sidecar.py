"""execution summary の ntfy 配信状態を残す sidecar の検証。

signals JSON の ``meta.publish_delivery`` は **publish_signals.py (朝の予告便)** 専用。
夜の open run は publish_signals を呼ばず execution summary だけを送るため、
実績通知が届いたかどうかがどこにも残らず「ntfy が不調でも観測できない」状態だった。

sidecar (`notify_delivery_YYYYMMDD.json`) は run_id を持つ独立ファイルとして書く。
signals JSON を書き換えないので、run をまたいで状態が混ざらない。
"""

from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from scripts.prepare_dashboard_bundle import (  # noqa: E402
    BundleContractError,
    materialize_dashboard_bundle,
)
from scripts.publish_execution_summary import (  # noqa: E402
    build_notify_delivery,
    notify_delivery_path,
    write_notify_delivery,
)
from test_prepare_dashboard_bundle import COMPACT, DATE, _fixtures, _write  # noqa: E402

RUN = "20260813_223505_first"  # _signals() の既定 run_id


def _sidecar(run_id: str | None, state: str = "accepted") -> dict:
    return build_notify_delivery(
        date_str=DATE,
        run_id=run_id,
        channel="ntfy",
        state=state,
        status_code=200 if state == "accepted" else None,
        attempted_at="2026-08-13T13:55:00+00:00",
    )


# --- payload の形 ---------------------------------------------------------
@pytest.mark.parametrize(
    "state", ["accepted", "failed", "not_configured", "not_attempted"]
)
def test_states_use_the_same_vocabulary_as_signals_delivery(state: str) -> None:
    d = _sidecar(RUN, state)
    assert d["schema"] == "notify_delivery/v1"
    assert d["kind"] == "execution_summary"
    assert d["state"] == state
    assert d["channels"]["ntfy"]["state"] == state


def test_payload_carries_run_id_so_it_cannot_be_reused_across_runs() -> None:
    assert _sidecar(RUN)["source_signals_run_id"] == RUN


def test_payload_contains_no_topic_or_endpoint() -> None:
    """secret (topic/URL) を観測用ファイルへ漏らさない。"""
    blob = json.dumps(_sidecar(RUN))
    for leak in ("ntfy.sh", "http://", "https://", "topic"):
        assert leak not in blob


def test_write_is_atomic_and_named_by_date(tmp_path: Path) -> None:
    p = write_notify_delivery(tmp_path, _sidecar(RUN))
    assert p == notify_delivery_path(tmp_path, DATE)
    assert json.loads(p.read_text(encoding="utf-8"))["state"] == "accepted"
    assert not list(tmp_path.glob("*.tmp"))


def test_write_failure_is_swallowed(tmp_path: Path) -> None:
    """観測用の副産物なので、書けなくても通知処理は落とさない。"""
    assert write_notify_delivery(tmp_path / "missing_dir_is_created", _sidecar(RUN))


# --- bundle への取り込み ---------------------------------------------------
def test_bundle_includes_sidecar_when_run_matches(tmp_path: Path) -> None:
    _fixtures(tmp_path)
    _write(tmp_path / f"notify_delivery_{COMPACT}.json", _sidecar(RUN))
    manifest = materialize_dashboard_bundle(
        results_dir=tmp_path, date_str=DATE, require_exit=False
    )
    assert "notify_delivery" in manifest["files"]
    assert not any("notify_delivery" in w for w in manifest.get("warnings", []))


def test_bundle_excludes_sidecar_from_another_run(tmp_path: Path) -> None:
    """別 run の配信状態を今日の bundle に載せない。"""
    _fixtures(tmp_path)
    _write(tmp_path / f"notify_delivery_{COMPACT}.json", _sidecar("some_other_run"))
    manifest = materialize_dashboard_bundle(
        results_dir=tmp_path, date_str=DATE, require_exit=False
    )
    assert "notify_delivery" not in manifest["files"]
    assert any("run_id mismatch" in w for w in manifest["warnings"])


def test_bundle_excludes_sidecar_with_unknown_state(tmp_path: Path) -> None:
    """語彙外の state を黙って表示に通さない。"""
    _fixtures(tmp_path)
    bad = _sidecar(RUN)
    bad["state"] = "delivered"  # 端末到達を含意する語は使わない
    _write(tmp_path / f"notify_delivery_{COMPACT}.json", bad)
    manifest = materialize_dashboard_bundle(
        results_dir=tmp_path, date_str=DATE, require_exit=False
    )
    assert "notify_delivery" not in manifest["files"]
    assert any("unknown delivery state" in w for w in manifest["warnings"])


def test_bundle_without_sidecar_still_publishes(tmp_path: Path) -> None:
    """sidecar が無い日 (朝の pipeline 等) は optional なので publish を止めない。"""
    _fixtures(tmp_path)
    manifest = materialize_dashboard_bundle(
        results_dir=tmp_path, date_str=DATE, require_exit=False
    )
    assert "notify_delivery" not in manifest["files"]
    assert manifest["date"] == DATE


def test_bundle_excludes_sidecar_with_wrong_date(tmp_path: Path) -> None:
    _fixtures(tmp_path)
    wrong = _sidecar(RUN)
    wrong["date"] = "2026-08-12"
    _write(tmp_path / f"notify_delivery_{COMPACT}.json", wrong)
    manifest = materialize_dashboard_bundle(
        results_dir=tmp_path, date_str=DATE, require_exit=False
    )
    assert "notify_delivery" not in manifest["files"]


def test_sidecar_problem_never_fails_the_whole_publish(tmp_path: Path) -> None:
    """観測用 optional の不備で bundle 全体を落とさない (Exit/funnel は無関係)。"""
    _fixtures(tmp_path)
    (tmp_path / f"notify_delivery_{COMPACT}.json").write_text(
        "{ not json", encoding="utf-8"
    )
    try:
        manifest = materialize_dashboard_bundle(
            results_dir=tmp_path, date_str=DATE, require_exit=False
        )
    except BundleContractError as exc:  # pragma: no cover - 失敗時の説明用
        raise AssertionError(f"optional sidecar が publish を止めた: {exc}") from exc
    assert "notify_delivery" not in manifest["files"]
