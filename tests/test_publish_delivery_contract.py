"""publish delivery の 2 表現が矛盾しないことを固定する契約テスト。

Codex review (PR#152) の 2 指摘に対応する回帰テスト:

1. 生成直後の signals payload が legacy scalar ``meta.publish_status`` を
   truthy な値で持たないこと。既存 dashboard (SignalsSection.tsx) は
   ``publish_status ? ...`` の真値判定で failed/partial 以外を成功色 (bg-ok)
   にするため、"not_attempted" を入れると「未試行」が緑で出る。
2. 実 publish 後に ``publish_status`` と ``publish_delivery`` が同一 projection
   から同時に更新され、"ok" + "not_attempted" のような矛盾を残さないこと。
"""

from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common.publishers import PublishResult, RegistryResult  # noqa: E402
from common.signal_export import build_signals_json  # noqa: E402
from scripts.publish_signals import (  # noqa: E402
    _delivery_state,
    _legacy_publish_status,
    _write_publish_status,
)

# legacy UI が「成功色 (bg-ok)」で描画してしまう値の判定。
# SignalsSection.tsx: publish_status ? (failed -> fail, partial -> warn, else -> ok)
_LEGACY_GREEN = lambda v: bool(v) and v not in {"failed", "partial"}  # noqa: E731


def _fresh_payload() -> dict:
    import pandas as pd

    return build_signals_json(
        pd.DataFrame(),
        None,
        date_str="2026-08-17",
        run_id="20260817_060721_test",
    )


def test_fresh_payload_does_not_render_green_on_legacy_dashboard() -> None:
    meta = _fresh_payload()["meta"]
    status = meta.get("publish_status")
    assert not _LEGACY_GREEN(status), (
        "生成直後の publish_status が legacy dashboard で成功色になる: " f"{status!r}"
    )


def test_fresh_payload_marks_structured_delivery_not_attempted() -> None:
    delivery = _fresh_payload()["meta"]["publish_delivery"]
    assert delivery["state"] == "not_attempted"
    assert delivery["channels"] == {}
    assert delivery["attempted_at"] is None


@pytest.mark.parametrize(
    "state,expected",
    [
        ("primary_accepted", "ok"),
        ("all_accepted", "ok"),
        ("fallback_accepted", "partial"),
        ("partial", "partial"),
        ("all_failed", "failed"),
        ("not_configured", "failed"),
        ("not_attempted", ""),
    ],
)
def test_legacy_scalar_is_derived_from_structured_state(
    state: str, expected: str
) -> None:
    assert _legacy_publish_status(state) == expected


def test_only_accepted_states_map_to_legacy_green() -> None:
    """accepted 系以外が legacy 成功色に落ちないことを全 state で保証する。"""
    for state in (
        "primary_accepted",
        "all_accepted",
        "fallback_accepted",
        "partial",
        "all_failed",
        "not_configured",
        "not_attempted",
    ):
        legacy = _legacy_publish_status(state)
        if state in {"primary_accepted", "all_accepted"}:
            assert _LEGACY_GREEN(legacy)
        else:
            assert not _LEGACY_GREEN(legacy), f"{state} -> {legacy!r} が成功色"


def _write_and_reload(tmp_path: Path, result: RegistryResult) -> dict:
    payload = _fresh_payload()
    path = tmp_path / "today_signals_20260817.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    assert _write_publish_status(path, payload, result) is True
    return json.loads(path.read_text(encoding="utf-8"))["meta"]


def test_successful_publish_updates_both_representations(tmp_path: Path) -> None:
    result = RegistryResult(
        status="ok",
        results=[PublishResult(publisher="ntfy", ok=True, status_code=200)],
    )
    meta = _write_and_reload(tmp_path, result)
    assert meta["publish_delivery"]["state"] == "primary_accepted"
    assert meta["publish_status"] == "ok"
    assert meta["publish_delivery"]["attempted_at"] is not None
    assert meta["publish_delivery"]["channels"]["ntfy"]["state"] == "accepted"


def test_failed_publish_updates_both_representations(tmp_path: Path) -> None:
    result = RegistryResult(
        status="failed",
        results=[PublishResult(publisher="ntfy", ok=False, status_code=500)],
    )
    meta = _write_and_reload(tmp_path, result)
    assert meta["publish_delivery"]["state"] == "all_failed"
    assert meta["publish_status"] == "failed"


def test_publish_never_leaves_contradictory_metadata(tmp_path: Path) -> None:
    """publish 後に「legacy ok なのに structured は not_attempted」を作らない。"""
    for ok in (True, False):
        result = RegistryResult(
            status="ok" if ok else "failed",
            results=[PublishResult(publisher="ntfy", ok=ok, status_code=200)],
        )
        meta = _write_and_reload(tmp_path, result)
        structured = meta["publish_delivery"]["state"]
        assert structured != "not_attempted"
        assert meta["publish_status"] == _legacy_publish_status(structured)


def test_cas_skips_writeback_when_run_id_changed(tmp_path: Path) -> None:
    """publish 中に別 run が同じ file を差し替えたら巻き戻さない。"""
    payload = _fresh_payload()
    path = tmp_path / "today_signals_20260817.json"
    newer = _fresh_payload()
    newer["meta"]["run_id"] = "20260817_223505_other"
    path.write_text(json.dumps(newer, ensure_ascii=False), encoding="utf-8")

    result = RegistryResult(
        status="ok",
        results=[PublishResult(publisher="ntfy", ok=True, status_code=200)],
    )
    assert _write_publish_status(path, payload, result) is False
    meta = json.loads(path.read_text(encoding="utf-8"))["meta"]
    assert meta["run_id"] == "20260817_223505_other"
    assert meta["publish_delivery"]["state"] == "not_attempted"


def test_fallback_accepted_when_primary_fails_and_secondary_succeeds() -> None:
    channels = {
        "ntfy": {"state": "failed"},
        "email": {"state": "accepted"},
    }
    assert _delivery_state(channels) == "fallback_accepted"
    assert _legacy_publish_status(_delivery_state(channels)) == "partial"
