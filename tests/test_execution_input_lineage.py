"""execution input (paper_orders / exit_orders) の lineage 検証。

Codex review (PR#153) の指摘に対応する回帰テスト:

同日 signals rerun で Step5b/5c が skip / 失敗すると、``publish_execution_summary``
は前 run の paper_orders / exit_orders を読んだまま recon を作り直す。以前は
recon に current signals の run_id を **無条件で** 貼っていたため、bundle
preflight がそれを current と信じ、古い execution / Exit を新しい run として
publish し得た。

契約:
  - producer が書く ``source_signals_run_id`` と signals の run_id が一致した
    input だけ ``verified``。
  - 不一致 = ``stale`` / field 不在 = ``unverified`` (推測で昇格させない)。
  - 1 つでも verified/missing 以外があれば recon に run_id を stamp しない。
  - ``--require-exit`` の preflight はその recon を fail-closed で弾く。
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
from scripts.prepare_dashboard_bundle import (  # noqa: E402
    BundleContractError,
    materialize_dashboard_bundle,
)

RUN = "20260817_223505_current"
OLD = "20260817_060721_morning"


def _signals(run_id: str = RUN) -> dict:
    return {"date": "2026-08-17", "meta": {"run_id": run_id}, "systems": {}}


def _orders(run_id: str | None) -> dict:
    payload: dict = {"date": "2026-08-17", "orders": []}
    if run_id is not None:
        payload["source_signals_run_id"] = run_id
    return payload


# --- lineage 判定 ---------------------------------------------------------
def test_matching_run_id_is_verified():
    lineage = execution_input_lineage(_signals(), _orders(RUN), _orders(RUN))
    assert lineage == {"paper_orders": "verified", "exit_orders": "verified"}
    assert execution_lineage_ok(lineage) is True


def test_previous_run_id_is_stale():
    lineage = execution_input_lineage(_signals(), _orders(OLD), _orders(RUN))
    assert lineage["paper_orders"] == "stale"
    assert execution_lineage_ok(lineage) is False


def test_missing_field_is_unverified_not_verified():
    """旧 producer 出力を推測で verified に昇格させない。"""
    lineage = execution_input_lineage(_signals(), _orders(None), _orders(None))
    assert lineage == {"paper_orders": "unverified", "exit_orders": "unverified"}
    assert execution_lineage_ok(lineage) is False


def test_absent_input_is_missing_and_tolerated():
    """その段が動かなかった = 突合対象なしなので許容する。"""
    lineage = execution_input_lineage(_signals(), None, _orders(RUN))
    assert lineage["paper_orders"] == "missing"
    assert execution_lineage_ok(lineage) is True


def test_signals_without_run_id_cannot_verify_anything():
    lineage = execution_input_lineage({"meta": {}}, _orders(RUN), _orders(RUN))
    assert set(lineage.values()) == {"stale"}
    assert execution_lineage_ok(lineage) is False


# --- build_recon の stamp 挙動 -------------------------------------------
def test_recon_stamps_run_id_only_when_lineage_verified():
    recon = build_recon(_signals(), _orders(RUN), _orders(RUN), date_str="2026-08-17")
    assert recon["source_signals_run_id"] == RUN
    assert recon["execution_lineage_ok"] is True


@pytest.mark.parametrize(
    "paper_run,exit_run",
    [(OLD, RUN), (RUN, OLD), (None, RUN), (RUN, None), (None, None)],
)
def test_recon_withholds_run_id_when_any_input_unbound(paper_run, exit_run):
    recon = build_recon(
        _signals(),
        _orders(paper_run),
        _orders(exit_run),
        date_str="2026-08-17",
    )
    assert recon["source_signals_run_id"] is None
    assert recon["execution_lineage_ok"] is False


# --- preflight fail-closed ----------------------------------------------
# 既存 bundle テストの fixture を再利用する (funnel/Exit が揃った現実的な形)。
# 最小 dict では funnel materialization が先に落ち、lineage check に到達しない。
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_prepare_dashboard_bundle import (  # noqa: E402
    COMPACT,
    DATE,
    _fixtures,
    _recon,
    _write,
)


def _stage(tmp_path: Path, lineage: dict | None) -> None:
    _fixtures(tmp_path)
    recon = _recon()
    if lineage is None:
        recon.pop("execution_lineage", None)
        recon.pop("execution_lineage_ok", None)
    else:
        recon["execution_lineage"] = lineage
        recon["execution_lineage_ok"] = all(
            v in {"verified", "missing"} for v in lineage.values()
        )
        if not recon["execution_lineage_ok"]:
            recon["source_signals_run_id"] = None
    _write(tmp_path / f"recon_{COMPACT}.json", recon)


@pytest.mark.parametrize(
    "lineage",
    [
        {"paper_orders": "stale", "exit_orders": "verified"},
        {"paper_orders": "unverified", "exit_orders": "unverified"},
        {"paper_orders": "verified", "exit_orders": "stale"},
    ],
)
def test_preflight_rejects_unbound_execution_inputs(tmp_path: Path, lineage: dict):
    _stage(tmp_path, lineage)
    with pytest.raises(BundleContractError) as exc:
        materialize_dashboard_bundle(
            results_dir=tmp_path, date_str=DATE, require_exit=True
        )
    assert "not bound to the current signals run" in str(exc.value)


def test_preflight_accepts_fully_verified_execution_inputs(tmp_path: Path):
    _stage(tmp_path, {"paper_orders": "verified", "exit_orders": "verified"})
    manifest = materialize_dashboard_bundle(
        results_dir=tmp_path, date_str=DATE, require_exit=True
    )
    assert manifest["date"] == DATE


def test_preflight_tolerates_missing_execution_stage(tmp_path: Path):
    """その段が動かなかった (missing) だけなら publish を止めない。"""
    _stage(tmp_path, {"paper_orders": "missing", "exit_orders": "verified"})
    manifest = materialize_dashboard_bundle(
        results_dir=tmp_path, date_str=DATE, require_exit=True
    )
    assert manifest["date"] == DATE


def test_preflight_still_accepts_legacy_recon_without_lineage(tmp_path: Path):
    """旧 producer の recon (lineage field なし) を lineage 理由で落とさない。"""
    _stage(tmp_path, None)
    manifest = materialize_dashboard_bundle(
        results_dir=tmp_path, date_str=DATE, require_exit=True
    )
    assert manifest["date"] == DATE
