"""stale アラートは self-heal の **後** にだけ鳴る (2026-08-22 cry-wolf 修正)。

背景
----
``morning_brief.ps1`` は 08:00 JST に

    1. check_dashboard_freshness.py --notify  (検出 + 通知)
    2. publish_data_to_vercel.ps1 -AutoLatest (self-heal)

の順で走っていた。つまり **次の行が直そうとしている状態** に対して通知していた。
しかも 08:00 時点で gap があるのは異常ではなく **通常状態** である: 06:00 のデイリーは
``results_csv/`` を進めるだけで publish しないので、self-heal が push するまで origin の
``data/`` は前日のままだからだ。

実測 (logs/morning_brief/launch_*.log, 2026-08-09..08-22):
    14/14 朝が status=stale -> ntfy -> 数秒後に "verify OK: served date=<当日>"。
    つまり 14 連続で「オオカミが来た」。唯一 08-20 だけ self-heal が本当に失敗し
    (bundle preflight FAIL, exit=1)、そこでは通知が正しかった。

ここで固定する契約
------------------
  1. ``--defer-stale-notify`` の pass は stale を検出しても **通知しない**
     (exit code と print は従来どおり出す = 診断は失わない)。
  2. self-heal 後の pass (``--post-heal``) で fresh なら **誰も通知しない**
     = 通常運転の朝は静か。
  3. self-heal 後も stale なら **必ず** 通知する。文面で self-heal 済みと分かる。
  4. self-heal が存在しない構成 (defer しない) では従来どおり即通知する
     = アラートを無効化したわけではない。
  5. deploy watchdog (``--check-served`` -> deploy_missing) は self-heal と無関係
     なので defer しても通知が抑止されない。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_dashboard_freshness.py"


def _load():
    spec = importlib.util.spec_from_file_location("cdf_selfheal_order", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


cdf = _load()


@pytest.fixture()
def sent(monkeypatch):
    """ntfy を捕まえるだけのスタブ (送信は一切しない)。"""
    seen: list[dict] = []

    def _send(item):
        seen.append(item)
        return True, "ok=True"

    monkeypatch.setattr(cdf, "_send_one", _send)
    return seen


def _repo(tmp_path: Path, generated: str, served: str | None) -> Path:
    """results_csv が ``generated``、ローカル data/ が ``served`` の repo を作る。

    served=None で「publish 済みが 1 件も無い」= 必ず stale。
    """
    (tmp_path / "results_csv").mkdir(parents=True, exist_ok=True)
    (tmp_path / "results_csv" / f"today_signals_{generated}.json").write_text(
        json.dumps({"generated_at": f"{generated}T06:26:00"}), encoding="utf-8"
    )
    data = tmp_path / "apps" / "dashboards" / "alpaca-next" / "data"
    data.mkdir(parents=True, exist_ok=True)
    if served is not None:
        (data / f"today_signals_{served}.json").write_text(
            json.dumps({"generated_at": f"{served}T06:26:00"}), encoding="utf-8"
        )
    return tmp_path


def _run(root: Path, *extra: str) -> int:
    """git を介さずローカル data/ を served とみなして実行する (--data-dir 明示)。"""
    return cdf.main(
        [
            "--repo-root",
            str(root),
            "--data-dir",
            str(root / "apps" / "dashboards" / "alpaca-next" / "data"),
            "--notify",
            *extra,
        ]
    )


# --- 1) 1 パス目は検出しても黙る ------------------------------------------
def test_defer_suppresses_the_stale_ntfy(tmp_path, sent, capsys):
    root = _repo(tmp_path, "20260822", "20260821")

    code = _run(root, "--defer-stale-notify")

    assert code == 2, "stale の検出自体はやめていない (exit code は 2 のまま)"
    assert sent == [], "self-heal 前なのに通知してしまっている = cry wolf"
    out = capsys.readouterr().out
    assert "STALE" in out, "診断ログまで消してはいけない"
    assert "委譲" in out, "通知を先送りしたことがログから読み取れない"


def test_defer_does_not_queue_the_alert_for_later(tmp_path, sent):
    """先送りは『pending キューに積む』でもない (積むと翌朝に遅れて鳴る)。"""
    root = _repo(tmp_path, "20260822", "20260821")

    _run(root, "--defer-stale-notify")

    assert not cdf._pending_path(root).exists()


# --- 2) self-heal で解消した朝は誰も鳴らない (本丸) ------------------------
def test_no_alert_when_self_heal_fixes_it(tmp_path, sent):
    """08-22 の実データ再現: 検出 -> self-heal が当日ぶんを publish -> 静か。"""
    root = _repo(tmp_path, "20260822", "20260821")
    data = root / "apps" / "dashboards" / "alpaca-next" / "data"

    pre = _run(root, "--defer-stale-notify")
    # self-heal (publish_data_to_vercel.ps1 -AutoLatest) が当日ぶんを配信した状態
    (data / "today_signals_20260822.json").write_text(
        json.dumps({"generated_at": "2026-08-22T06:26:00"}), encoding="utf-8"
    )
    post = _run(root, "--post-heal")

    assert pre == 2 and post == 0
    assert sent == [], "self-heal で解消したのに通知が飛んでいる"


# --- 3) self-heal が効かなかったら必ず鳴る (08-20 の本物) ------------------
def test_alerts_when_self_heal_did_not_fix_it(tmp_path, sent):
    """08-20 のように bundle preflight が落ちて publish されなかったケース。"""
    root = _repo(tmp_path, "20260820", "20260819")

    pre = _run(root, "--defer-stale-notify")
    # self-heal は走ったが exit=1 で publish されず -> served は前日のまま
    post = _run(root, "--post-heal")

    assert pre == 2 and post == 2
    assert len(sent) == 1, "本物の publish 取りこぼしを黙らせてはいけない"
    assert "self-heal 後も" in sent[0]["title"]
    assert "解消しませんでした" in sent[0]["body"]


def test_post_heal_alert_is_distinguishable_from_the_old_one(tmp_path, sent):
    """タイトルが変わる = 通知の履歴で新旧を取り違えない。"""
    root = _repo(tmp_path, "20260820", "20260819")

    _run(root, "--post-heal")
    plain_title = "Dashboard STALE: served 2026-08-19 < generated 2026-08-20"
    assert sent[0]["title"] != plain_title


# --- 4) self-heal が無い構成では従来どおり即通知 --------------------------
def test_without_defer_it_still_alerts_immediately(tmp_path, sent):
    root = _repo(tmp_path, "20260822", "20260821")

    code = _run(root)

    assert code == 2
    assert len(sent) == 1, "アラートを無効化してしまっている"
    assert sent[0]["title"] == (
        "Dashboard STALE: served 2026-08-21 < generated 2026-08-22"
    )


# --- 5) deploy watchdog は defer の対象外 --------------------------------
def test_deploy_missing_still_alerts_under_defer(tmp_path, sent, monkeypatch):
    """Vercel build 不達は self-heal では治らないので、1 パス目で鳴らす。"""
    root = _repo(tmp_path, "20260822", "20260822")  # freshness 自体は fresh
    data = root / "apps" / "dashboards" / "alpaca-next" / "data"
    (data / "dashboard_bundle_20260822.json").write_text(
        json.dumps({"source_run_id": "20260822_062601_d80c7f"}), encoding="utf-8"
    )
    monkeypatch.setattr(
        cdf, "_fetch_served_html", lambda url, timeout: "<html>古い</html>"
    )
    monkeypatch.setattr(cdf, "_minutes_since", lambda p: 999.0)  # grace 超過

    code = cdf.main(
        [
            "--repo-root",
            str(root),
            "--data-dir",
            str(data),
            "--notify",
            "--check-served",
            "--defer-stale-notify",
        ]
    )

    assert code == 3
    assert len(sent) == 1, "deploy 不達まで先送りしてしまっている"


def test_fresh_run_is_silent_in_both_passes(tmp_path, sent):
    root = _repo(tmp_path, "20260822", "20260822")

    assert _run(root, "--defer-stale-notify") == 0
    assert _run(root, "--post-heal") == 0
    assert sent == []
