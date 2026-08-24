"""producer -> recon の実経路で lineage が成立することを固定する。

runner (claude/open-auto-run) は open_auto_run.py から
``scripts/paper_exit_check.py`` と ``scripts/paper_trading_submit.py`` を
**自分の worktree の実体で** 呼ぶ。その出力に ``source_signals_run_id`` が乗って
いなければ、下流 (publish 側の bundle preflight) は lineage を検証できず、
同日 rerun で前 run の execution 実績が current run として publish され得る。

ここでは実際の producer 実装を通して:
  - exit producer が同日 today_signals の run_id を出力に stamp すること
  - 新形式入力 (両方 stamped かつ一致) で recon が verified になり run_id を持つこと
  - run_id 不一致 / 未 stamp では recon が run_id を **付けない** こと
を検証する。発注は一切行わない (broker I/O 無しの純関数のみ)。
"""

from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.build_execution_recon import (  # noqa: E402
    build_recon,
    execution_input_lineage,
    execution_lineage_ok,
)
from scripts.paper_exit_check import _signals_run_id  # noqa: E402

DATE = "2026-08-18"
COMPACT = DATE.replace("-", "")
RUN = "20260818_223505_night"
MORNING = "20260818_060721_morning"


def _write_signals(results_dir: Path, run_id: str) -> None:
    (results_dir / f"today_signals_{COMPACT}.json").write_text(
        json.dumps({"date": DATE, "meta": {"run_id": run_id}, "systems": {}}),
        encoding="utf-8",
    )


# --- exit producer が実際に run_id を解決できる -----------------------------
def test_exit_producer_resolves_current_signals_run_id(tmp_path: Path) -> None:
    _write_signals(tmp_path, RUN)
    assert _signals_run_id(tmp_path, DATE) == RUN


def test_exit_producer_returns_none_when_signals_absent(tmp_path: Path) -> None:
    """signals が読めなくても exit 処理は止めない (None = 検証不能)。"""
    assert _signals_run_id(tmp_path, DATE) is None


def test_exit_producer_returns_none_on_broken_signals(tmp_path: Path) -> None:
    (tmp_path / f"today_signals_{COMPACT}.json").write_text(
        "{ not json", encoding="utf-8"
    )
    assert _signals_run_id(tmp_path, DATE) is None


# --- producer 出力 -> recon -------------------------------------------------
def _orders(run_id: str | None) -> dict:
    payload: dict = {"date": DATE, "orders": [], "exits": []}
    if run_id is not None:
        payload["source_signals_run_id"] = run_id
    return payload


def _signals(run_id: str = RUN) -> dict:
    return {"date": DATE, "meta": {"run_id": run_id}, "systems": {}}


def test_new_format_inputs_are_verified_and_stamped() -> None:
    """両 producer が current run を stamp していれば verified + run_id 付き。"""
    recon = build_recon(_signals(), _orders(RUN), _orders(RUN), date_str=DATE)
    assert recon["execution_lineage"] == {
        "paper_orders": "verified",
        "exit_orders": "verified",
    }
    assert recon["execution_lineage_ok"] is True
    assert recon["source_signals_run_id"] == RUN


@pytest.mark.parametrize(
    "paper_run,exit_run,expected",
    [
        (MORNING, RUN, "stale"),
        (RUN, MORNING, "stale"),
        (None, RUN, "unverified"),
        (RUN, None, "unverified"),
    ],
)
def test_run_id_mismatch_or_missing_withholds_stamp(
    paper_run, exit_run, expected
) -> None:
    """同日 rerun で片方が前 run の残骸なら current として stamp しない。"""
    recon = build_recon(
        _signals(), _orders(paper_run), _orders(exit_run), date_str=DATE
    )
    assert recon["execution_lineage_ok"] is False
    assert recon["source_signals_run_id"] is None
    assert expected in recon["execution_lineage"].values()


def test_absent_stage_is_missing_and_tolerated() -> None:
    """entry 段が動かなかっただけなら突合対象なし = 許容する。"""
    recon = build_recon(_signals(), None, _orders(RUN), date_str=DATE)
    assert recon["execution_lineage"]["paper_orders"] == "missing"
    assert recon["execution_lineage_ok"] is True
    assert recon["source_signals_run_id"] == RUN


def test_lineage_helpers_agree_with_recon() -> None:
    s, po, eo = _signals(), _orders(MORNING), _orders(RUN)
    lineage = execution_input_lineage(s, po, eo)
    assert execution_lineage_ok(lineage) is False
    assert build_recon(s, po, eo, date_str=DATE)["execution_lineage"] == lineage


# --- producer source contract ----------------------------------------------
# stamping が将来削られたら recon は永久に unverified になり、publish が
# fail-closed し続ける。source 側にも歯止めを置く。
@pytest.mark.parametrize(
    "relpath",
    ["scripts/paper_trading_submit.py", "scripts/paper_exit_check.py"],
)
def test_producers_stamp_source_signals_run_id(relpath: str) -> None:
    text = (ROOT / relpath).read_text(encoding="utf-8", errors="replace")
    assert "source_signals_run_id" in text, f"{relpath} が provenance を書いていない"
