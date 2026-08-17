"""薄シグナルゲートを cap 前の候補数で判定する回帰テスト (2026-07-27)。

背景
----
薄シグナルゲートの目的は「データがまだ来ていない状態で発注しない」こと
(`logs/design_open_auto_run_20260708.md` L15: 06:00 は Polygon 403 / EODHD 401 で
今日 1 件しか出ない)。ところが判定に使っていた ``systems[*].signals`` は
**portfolio cap 適用後**の本数で、``core/final_allocation.py::_apply_portfolio_caps``
が ``allow_total = max_total_positions(70) - held_total`` で上から抑えた残枠でしかない。

実測 (logs/open_run_*/): 建玉が 61 (L51/S10) まで積み上がった結果 残枠が 9 に固定され、
候補数は 44-48 件で健全なままなのに ``thin_signals:9<10`` で 2026-07-22..27 の
**5 営業日連続** entry が SKIP された。さらに entry が止まると建玉が減らないため
残枠も戻らず、cap の最後の 9 枠が構造的に使えない (自己強化ループ)。

ここで固定する契約:
  1. cap で絞られただけ (候補は健全) なら entry を通す = 本バグの回帰。
  2. 本物のデータ欠測 (候補自体が薄い) では従来どおり entry を止める = 保護の維持。
  3. funnel が無い旧 JSON では cap 後の本数にフォールバックする = 後方互換。
  4. 候補は健全でも cap 後 0 件なら、専用理由で SKIP する (黙って 0 件 submit しない)。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_module():
    path = ROOT / "scripts" / "open_auto_run.py"
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    spec = importlib.util.spec_from_file_location("open_auto_run_precap_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


oar = _load_module()


def _write_signals(root: Path, date_compact: str, systems: dict) -> None:
    """systems ブロックをそのまま書き出す (funnel 有無を試験ごとに変えるため)。"""
    results = root / "results_csv"
    results.mkdir(parents=True, exist_ok=True)
    payload = {
        "date": f"{date_compact[:4]}-{date_compact[4:6]}-{date_compact[6:8]}",
        "systems": systems,
    }
    (results / f"today_signals_{date_compact}.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )


def _sys_block(n_out: int, candidate_count: int | None, *, legacy_key: bool = False):
    """cap 後 n_out 本 / cap 前 candidate_count 件の system ブロックを作る。"""
    blk: dict = {"signals": [{"symbol": f"S{i}"} for i in range(n_out)]}
    if candidate_count is not None:
        if legacy_key:
            blk["n_candidates_input"] = candidate_count
        else:
            blk["funnel"] = {"candidate_count": candidate_count}
    return blk


def _args(**kw):
    base = dict(
        date=None,
        min_signals=10,
        poll_timeout=1.0,
        dry_run=False,
        skip_signals=True,
        allow_closed=True,
        force=True,
        flatten_all=False,
        no_publish=True,
        primary_root=".",
        thin_aborts_run=False,
    )
    base.update(kw)
    return argparse.Namespace(**base)


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[str] = []


@pytest.fixture()
def runner(tmp_path, monkeypatch):
    monkeypatch.setattr(oar, "ROOT", tmp_path)

    def _make(systems: dict, **overrides):
        date = "2026-07-27"
        _write_signals(tmp_path, date.replace("-", ""), systems)

        r = oar.Runner(_args(date=date, **overrides))
        rec = _Recorder()

        monkeypatch.setattr(r, "_assert_paper", lambda: None)
        monkeypatch.setattr(r, "_ntfy_warn", lambda *a, **k: None)
        monkeypatch.setattr(r, "equity", lambda: 100_000.0)
        monkeypatch.setattr(r, "wait_exit_fills", lambda ids: None)
        monkeypatch.setattr(r, "record_stage", lambda: None)
        monkeypatch.setattr(r, "publish", lambda: 0)
        monkeypatch.setattr(r, "notify", lambda eq: 0)
        monkeypatch.setattr(r, "gate", lambda: True)

        def _exit_stage():
            rec.calls.append("exit")
            r.record["exit_count"] = 24
            return []

        def _entry_stage(eq):
            rec.calls.append("entry")

        monkeypatch.setattr(r, "exit_stage", _exit_stage)
        monkeypatch.setattr(r, "entry_stage", _entry_stage)
        return r, rec

    return _make


# ---------------------------------------------------------------------------
# 1) 本丸: cap で絞られただけなら entry を通す
# ---------------------------------------------------------------------------


def test_cap_limited_but_healthy_candidates_allows_entry(runner):
    """2026-07-27 実測の再現: 候補 47 件 / cap 後 9 本 -> entry は通すべき。

    sys1/3/4/5 は long cap (held_long=51 > max_long=40) で全滅し、
    残った sys2 の 9 本は allow_total = 70 - 61 = 9 の残枠そのもの。
    データは健全なので新規 entry を止める理由が無い。
    """
    systems = {
        "sys1": _sys_block(0, 10),
        "sys2": _sys_block(9, 10),
        "sys3": _sys_block(0, 10),
        "sys4": _sys_block(0, 10),
        "sys5": _sys_block(0, 7),
        "sys6": _sys_block(0, 0),
        "sys7": _sys_block(0, 0),
    }
    r, rec = runner(systems)

    code = r.main()

    assert r.record["candidate_count"] == 47
    assert r.record["signal_count"] == 9, "cap 後の本数は観測値として残すこと"
    assert r.entry_allowed is True, (
        "候補 47 件は健全。cap 後 9 本という残枠を理由に entry を止めるのは、"
        "07-22..27 に 5 営業日 entry を殺したバグそのもの"
    )
    assert "entry" in rec.calls
    assert rec.calls == ["exit", "entry"], "exit->entry の順序契約は維持"
    assert code == 0
    assert r.record.get("entry_skip_reason") is None


# ---------------------------------------------------------------------------
# 2) 保護の維持: 本物のデータ欠測では止める
# ---------------------------------------------------------------------------


def test_genuine_data_outage_still_blocks_entry(runner):
    """2026-07-07 実測の再現: 候補 2 件 / cap 後 1 本 = Polygon 403 の薄データ。"""
    systems = {
        "sys1": _sys_block(1, 2),
        "sys2": _sys_block(0, 0),
    }
    r, rec = runner(systems)

    code = r.main()

    assert r.record["candidate_count"] == 2
    assert (
        r.entry_allowed is False
    ), "候補自体が 2 件 = データがまだ来ていない。ゲート本来の目的なので止める"
    assert r.record["entry_skip_reason"] == "thin_signals:2<10"
    assert "entry" not in rec.calls
    assert "exit" in rec.calls, "データ欠測でも exit は止めない (A1 契約)"
    assert code == 0


def test_zero_candidates_blocks_entry(runner):
    """2026-07-09 実測の再現: 候補 0 件 (データ皆無) でも exit は通す。"""
    r, rec = runner({"sys1": _sys_block(0, 0)})

    r.main()

    assert r.entry_allowed is False
    assert r.record["entry_skip_reason"] == "thin_signals:0<10"
    assert rec.calls == ["exit"]


# ---------------------------------------------------------------------------
# 3) 後方互換: funnel が無ければ cap 後の本数で判定 (従来挙動)
# ---------------------------------------------------------------------------


def test_missing_funnel_falls_back_to_postcap_count(runner):
    """旧 JSON (funnel も n_candidates_input も無い) では従来どおり cap 後で判定。"""
    systems = {"sys1": _sys_block(9, None)}
    r, rec = runner(systems)

    r.main()

    assert r.record["candidate_count"] is None
    assert r.record["thin_gate_basis"] == "signals(post-cap)"
    assert r.entry_allowed is False, "候補数が取れない旧 JSON では従来挙動を維持"
    assert r.record["entry_skip_reason"] == "thin_signals:9<10"


def test_legacy_n_candidates_input_is_used(runner):
    """funnel が無くても n_candidates_input があればそれを候補数として使う。"""
    systems = {"sys1": _sys_block(9, 40, legacy_key=True)}
    r, rec = runner(systems)

    r.main()

    assert r.record["candidate_count"] == 40
    assert r.record["thin_gate_basis"] == "candidates(pre-cap)"
    assert r.entry_allowed is True


# ---------------------------------------------------------------------------
# 4) 候補は健全でも cap 後 0 件なら専用理由で SKIP
# ---------------------------------------------------------------------------


def test_healthy_candidates_but_zero_after_caps_skips_with_reason(runner):
    """cap が全部刈った場合、submit は no-op なので黙って通さず理由を残す。"""
    systems = {
        "sys1": _sys_block(0, 30),
        "sys2": _sys_block(0, 10),
    }
    r, rec = runner(systems)

    code = r.main()

    assert r.record["candidate_count"] == 40
    assert r.record["signal_count"] == 0
    assert r.entry_allowed is False
    assert r.record["entry_skip_reason"] == "no_submittable_signals_after_caps"
    assert "entry" not in rec.calls
    assert "exit" in rec.calls
    assert code == 0


# ---------------------------------------------------------------------------
# 5) 潤沢シグナルの非退行
# ---------------------------------------------------------------------------


def test_healthy_day_unchanged(runner):
    """2026-07-21 実測の再現: 候補 44 / cap 後 39 -> 従来どおり entry を通す。"""
    systems = {
        "sys1": _sys_block(7, 10),
        "sys2": _sys_block(10, 10),
        "sys3": _sys_block(8, 10),
        "sys4": _sys_block(10, 10),
        "sys5": _sys_block(4, 4),
    }
    r, rec = runner(systems)

    r.main()

    assert r.record["candidate_count"] == 44
    assert r.record["signal_count"] == 39
    assert r.entry_allowed is True
    assert rec.calls == ["exit", "entry"]
