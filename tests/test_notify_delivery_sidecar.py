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

from scripts.publish_execution_summary import (  # noqa: E402
    build_notify_delivery,
    notify_delivery_path,
    write_notify_delivery,
)

DATE = "2026-08-13"

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
