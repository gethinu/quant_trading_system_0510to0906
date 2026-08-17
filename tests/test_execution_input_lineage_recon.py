"""recon が execution input の lineage を判定することの検証 (bundle 非依存)。

同日 rerun で entry(5b)/exit(5c) が skip / 失敗すると、publish_execution_summary は
**前 run の** paper_orders / exit_orders を読んだまま recon を作り直す。従来は current
signals の run_id を無条件に stamp していたため、下流 (dashboard publish) がそれを
current と信じ、古い execution 実績を新しい run として公開し得た。

契約:
  - producer が書く ``source_signals_run_id`` が current signals と一致した input
    だけ ``verified``。
  - 不一致 = ``stale`` / field 不在 = ``unverified`` (**推測で verified に昇格させない**)。
  - 段が動かなかった = ``missing`` は突合対象なしなので許容。
  - verified/missing 以外が 1 つでもあれば recon に run_id を **stamp しない**。

publish 側の fail-closed gate はこの判定を読むだけなので、判定そのものをここで固定する。
"""

from __future__ import annotations

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

DATE = "2026-08-18"
RUN = "20260818_223505_night"
MORNING = "20260818_060721_morning"


def _signals(run_id: str = RUN) -> dict:
    return {"date": DATE, "meta": {"run_id": run_id}, "systems": {}}


def _orders(run_id: str | None) -> dict:
    payload: dict = {"date": DATE, "orders": [], "exits": []}
    if run_id is not None:
        payload["source_signals_run_id"] = run_id
    return payload


# --- 判定 -----------------------------------------------------------------
def test_matching_run_id_is_verified():
    lineage = execution_input_lineage(_signals(), _orders(RUN), _orders(RUN))
    assert lineage == {"paper_orders": "verified", "exit_orders": "verified"}
    assert execution_lineage_ok(lineage) is True


def test_previous_run_id_is_stale():
    lineage = execution_input_lineage(_signals(), _orders(MORNING), _orders(RUN))
    assert lineage["paper_orders"] == "stale"
    assert execution_lineage_ok(lineage) is False


def test_missing_field_is_unverified_not_verified():
    """旧 producer の出力を推測で verified に昇格させない。"""
    lineage = execution_input_lineage(_signals(), _orders(None), _orders(None))
    assert lineage == {"paper_orders": "unverified", "exit_orders": "unverified"}
    assert execution_lineage_ok(lineage) is False


def test_absent_stage_is_missing_and_tolerated():
    """その段が動かなかっただけなら突合対象なし = 許容する。"""
    lineage = execution_input_lineage(_signals(), None, _orders(RUN))
    assert lineage["paper_orders"] == "missing"
    assert execution_lineage_ok(lineage) is True


def test_signals_without_run_id_cannot_verify_anything():
    lineage = execution_input_lineage({"meta": {}}, _orders(RUN), _orders(RUN))
    assert set(lineage.values()) == {"stale"}
    assert execution_lineage_ok(lineage) is False


# --- recon への反映 --------------------------------------------------------
def test_recon_stamps_run_id_only_when_verified():
    recon = build_recon(_signals(), _orders(RUN), _orders(RUN), date_str=DATE)
    assert recon["source_signals_run_id"] == RUN
    assert recon["execution_lineage_ok"] is True


@pytest.mark.parametrize(
    "paper_run,exit_run",
    [(MORNING, RUN), (RUN, MORNING), (None, RUN), (RUN, None), (None, None)],
)
def test_recon_withholds_run_id_when_any_input_unbound(paper_run, exit_run):
    """1 つでも current run に紐付かない input があれば current と名乗らない。"""
    recon = build_recon(
        _signals(), _orders(paper_run), _orders(exit_run), date_str=DATE
    )
    assert recon["source_signals_run_id"] is None
    assert recon["execution_lineage_ok"] is False


def test_recon_exposes_lineage_for_downstream_gate():
    """publish 側 gate が読む field をここで固定する (改名で gate が無言化しないため)。"""
    recon = build_recon(_signals(), _orders(MORNING), _orders(RUN), date_str=DATE)
    assert recon["execution_lineage"] == execution_input_lineage(
        _signals(), _orders(MORNING), _orders(RUN)
    )
    assert "execution_lineage_ok" in recon


def test_missing_only_still_stamps():
    """entry も exit も動かなかった run は突合対象が無いので stamp してよい。"""
    recon = build_recon(_signals(), None, None, date_str=DATE)
    assert recon["execution_lineage_ok"] is True
    assert recon["source_signals_run_id"] == RUN
