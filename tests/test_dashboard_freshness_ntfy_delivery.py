"""check_dashboard_freshness の ntfy 到達性テスト (2026-07-27 A4)。

背景
----
``_notify_stale`` は ``from common.publishers.ntfy import NtfyPublisher`` を
遅延 import するが、モジュールが ``_REPO_ROOT_DEFAULT`` を **sys.path に入れて
いなかった**。``python scripts/check_dashboard_freshness.py`` では sys.path[0] が
*scripts/* になり (cwd は sys.path に入らない)、import が
``ModuleNotFoundError: No module named 'common'`` で落ちる。それを広い
``except Exception`` が print だけして飲み込んでいたため、**stale を検知した日に
限って通知が消える**という最悪の silent-fail になっていた
(2026-07-23..26 の 4 日連続不達)。

ここで固定する契約:
  1. import は cwd 非依存 (別 cwd から起動しても common が解決できる)。
  2. 送信失敗は握り潰さず pending キューへ退避し、次回に持ち越して再送する。
  3. 再送に成功したらキューは消える。
  4. 同じ gap の重複エンキューはしない。
  5. fresh な回でも持ち越し分は再送される (stale が消えても通知は失わない)。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_dashboard_freshness.py"


def _load():
    spec = importlib.util.spec_from_file_location("cdf_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


cdf = _load()


# ---------------------------------------------------------------------------
# 1) import が cwd 非依存であること (本丸)
# ---------------------------------------------------------------------------


def test_common_import_works_from_foreign_cwd(tmp_path):
    """repo 外の cwd から起動しても common を import できる。

    これが 07-23..26 の 4 日不達の直接原因。sys.path 挿入が消えたら落ちる。
    """
    code = (
        "import runpy, sys;"
        f"sys.argv=['x'];"
        f"runpy.run_path(r'{SCRIPT}', run_name='not_main');"
        "from common.publishers.ntfy import NtfyPublisher;"
        "print('IMPORT_OK')"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(tmp_path),  # repo とは無関係な cwd
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert "IMPORT_OK" in (proc.stdout or ""), (
        f"repo 外 cwd から common を import できない\n"
        f"stdout={proc.stdout}\nstderr={proc.stderr}"
    )


def test_repo_root_is_on_sys_path():
    assert str(cdf._REPO_ROOT_DEFAULT) in sys.path


# ---------------------------------------------------------------------------
# 2)-5) pending キューによる持ち越し
# ---------------------------------------------------------------------------


@pytest.fixture()
def result():
    return {
        "status": "stale",
        "results_date_str": "2026-07-26",
        "data_date_str": "2026-07-25",
        "results_generated_at": "2026-07-26T08:00:00",
        "data_generated_at": "2026-07-25T08:00:00",
    }


def _fail_send(monkeypatch, detail="ModuleNotFoundError: No module named 'common'"):
    monkeypatch.setattr(cdf, "_send_one", lambda item: (False, detail))


def _ok_send(monkeypatch, seen: list):
    def _send(item):
        seen.append(item)
        return True, "ok=True"

    monkeypatch.setattr(cdf, "_send_one", _send)


def test_failed_send_is_queued_not_swallowed(tmp_path, monkeypatch, result, capsys):
    _fail_send(monkeypatch)

    cdf._notify_stale(result, tmp_path, "2026-07-26T08:00:00+00:00")

    pending = json.loads(cdf._pending_path(tmp_path).read_text(encoding="utf-8"))
    assert len(pending) == 1
    assert pending[0]["attempts"] == 1
    assert "No module named" in pending[0]["last_error"]
    out = capsys.readouterr().out
    assert "持ち越し" in out
    assert "[ntfy][PENDING]" in out, "未送信が残ったことをログで可視化していない"


def test_pending_is_retried_and_cleared_on_next_run(tmp_path, monkeypatch, result):
    _fail_send(monkeypatch)
    cdf._notify_stale(result, tmp_path, "2026-07-26T08:00:00+00:00")
    assert cdf._pending_path(tmp_path).exists()

    sent: list = []
    _ok_send(monkeypatch, sent)
    cdf._flush_pending(tmp_path, "2026-07-27T08:00:00+00:00")

    assert len(sent) == 1, "持ち越し分が再送されていない"
    assert not cdf._pending_path(tmp_path).exists(), "成功後もキューが残っている"


def test_pending_flushed_even_when_now_fresh(tmp_path, monkeypatch, result):
    """stale が解消した回でも、過去の未送信は届く。"""
    _fail_send(monkeypatch)
    cdf._notify_stale(result, tmp_path, "2026-07-26T08:00:00+00:00")

    sent: list = []
    _ok_send(monkeypatch, sent)
    # main() の fresh 経路が呼ぶのと同じ呼び出し
    cdf._flush_pending(tmp_path, "2026-07-27T08:00:00+00:00")

    assert [s["title"] for s in sent] == [
        "Dashboard STALE: served 2026-07-25 < generated 2026-07-26"
    ]


def test_same_gap_is_not_duplicated(tmp_path, monkeypatch, result):
    _fail_send(monkeypatch)
    cdf._notify_stale(result, tmp_path, "2026-07-26T08:00:00+00:00")
    cdf._notify_stale(result, tmp_path, "2026-07-27T08:00:00+00:00")

    pending = json.loads(cdf._pending_path(tmp_path).read_text(encoding="utf-8"))
    assert len(pending) == 1, "同一 gap が重複エンキューされている"
    assert pending[0]["attempts"] == 2, "再試行回数が積み上がっていない"


def test_distinct_gaps_are_kept_separately(tmp_path, monkeypatch, result):
    _fail_send(monkeypatch)
    cdf._notify_stale(result, tmp_path, "2026-07-26T08:00:00+00:00")
    other = dict(result, results_date_str="2026-07-28", data_date_str="2026-07-27")
    cdf._notify_stale(other, tmp_path, "2026-07-28T08:00:00+00:00")

    pending = json.loads(cdf._pending_path(tmp_path).read_text(encoding="utf-8"))
    assert len(pending) == 2


def test_send_one_never_raises(monkeypatch):
    """_send_one は例外を投げず (ok, detail) を返す契約。"""

    def _boom():
        raise RuntimeError("boom")

    monkeypatch.setitem(
        sys.modules, "common.publishers.ntfy", type(sys)("common.publishers.ntfy")
    )
    sys.modules["common.publishers.ntfy"].NtfyPublisher = _boom

    ok, detail = cdf._send_one({"title": "t", "body": "b"})

    assert ok is False
    assert "boom" in detail


def test_corrupt_pending_file_does_not_crash(tmp_path):
    path = cdf._pending_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")

    assert cdf._load_pending(tmp_path) == []
