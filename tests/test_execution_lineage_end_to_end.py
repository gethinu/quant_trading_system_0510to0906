"""producer -> recon -> bundle preflight の実経路を通しで検証する。

夜間 open run の実物の流れ:

    paper_trading_submit / paper_exit_check   (producer: run_id を stamp)
        -> build_recon                        (lineage を検証して stamp を決める)
        -> prepare_dashboard_bundle           (publish 直前の fail-closed gate)

個々の層は各 module のテストで固定済みだが、**層をまたいだ契約**
(producer が書く field 名 / recon が載せる field 名 / preflight が読む field 名)
がずれると、どのテストも緑のまま publish gate だけが無言で無効化される。
ここでは実装を跨いで実際に繋ぎ、

  - 新形式入力 (両 producer が current run を stamp) -> verified -> publish 可
  - run_id 不一致 (前 run の残骸) -> fail-closed
  - 未 stamp (旧 producer 出力) -> fail-closed

を確認する。発注は行わない。
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

from scripts.build_execution_recon import build_recon  # noqa: E402
from scripts.prepare_dashboard_bundle import (  # noqa: E402
    BundleContractError,
    materialize_dashboard_bundle,
)
from test_prepare_dashboard_bundle import (  # noqa: E402
    COMPACT,
    DATE,
    _pipeline,
    _signals,
    _write,
)

RUN = "20260813_223505_first"  # _signals() の既定 run_id
STALE = "20260813_060721_morning"


def _paper_orders(run_id: str | None) -> dict:
    payload: dict = {
        "date": DATE,
        "orders": [
            {
                "system": "system1",
                "side": "buy",
                "order_id": "o1",
                "status": "filled",
            }
        ],
    }
    if run_id is not None:
        payload["source_signals_run_id"] = run_id
    return payload


def _exit_orders(run_id: str | None) -> dict:
    payload: dict = {
        "date": DATE,
        "exits": [
            {"system": "system1", "reason": "time_exit", "order_id": "e1"},
        ],
    }
    if run_id is not None:
        payload["source_signals_run_id"] = run_id
    return payload


def _stage_chain(
    tmp_path: Path, *, paper_run: str | None, exit_run: str | None
) -> dict:
    """producer 出力 -> build_recon -> ディスク配置まで、実装を通して行う。"""
    signals = _signals()
    _write(tmp_path / f"today_signals_{COMPACT}.json", signals)
    _write(tmp_path / f"pipeline_{COMPACT}.json", _pipeline())

    recon = build_recon(
        signals,
        _paper_orders(paper_run),
        _exit_orders(exit_run),
        date_str=DATE,
        generated_at="2026-08-13T22:51:58+00:00",
    )
    _write(tmp_path / f"recon_{COMPACT}.json", recon)
    return recon


def test_new_format_chain_is_verified_and_publishable(tmp_path: Path) -> None:
    recon = _stage_chain(tmp_path, paper_run=RUN, exit_run=RUN)

    # recon 層: 両 input が current run 由来
    assert recon["execution_lineage"] == {
        "paper_orders": "verified",
        "exit_orders": "verified",
    }
    assert recon["source_signals_run_id"] == RUN

    # preflight 層: そのまま publish 可
    manifest = materialize_dashboard_bundle(
        results_dir=tmp_path, date_str=DATE, require_exit=True
    )
    assert manifest["date"] == DATE
    assert manifest["source_run_id"] == RUN
    # legacy 互換経路の warning が出ていない = 新形式として通っている
    assert not any("legacy recon" in w for w in manifest.get("warnings", []))


@pytest.mark.parametrize(
    "paper_run,exit_run,expected_state",
    [
        (STALE, RUN, "stale"),
        (RUN, STALE, "stale"),
        (None, RUN, "unverified"),
        (RUN, None, "unverified"),
        (None, None, "unverified"),
    ],
)
def test_unbound_execution_inputs_fail_closed_at_publish(
    tmp_path: Path, paper_run, exit_run, expected_state
) -> None:
    """前 run の残骸 / 未 stamp は publish を止める (古い実績を current にしない)。"""
    recon = _stage_chain(tmp_path, paper_run=paper_run, exit_run=exit_run)
    assert recon["source_signals_run_id"] is None
    assert expected_state in recon["execution_lineage"].values()

    with pytest.raises(BundleContractError) as exc:
        materialize_dashboard_bundle(
            results_dir=tmp_path, date_str=DATE, require_exit=True
        )
    assert "not bound to the current signals run" in str(exc.value)


def test_field_names_stay_aligned_across_layers(tmp_path: Path) -> None:
    """層をまたぐ field 名の契約を明示的に固定する。

    producer が書く key / recon が載せる key / preflight が読む key のいずれかが
    改名されると、gate が無言で無効化される (どの層の単体テストも緑のまま)。
    """
    recon = _stage_chain(tmp_path, paper_run=RUN, exit_run=RUN)
    assert "execution_lineage" in recon
    assert "execution_lineage_ok" in recon
    assert "source_signals_run_id" in recon

    preflight_src = (ROOT / "scripts" / "prepare_dashboard_bundle.py").read_text(
        encoding="utf-8"
    )
    assert "execution_lineage" in preflight_src

    for producer in ("paper_trading_submit.py", "paper_exit_check.py"):
        src = (ROOT / "scripts" / producer).read_text(
            encoding="utf-8", errors="replace"
        )
        assert "source_signals_run_id" in src, f"{producer} が provenance を書かない"


def test_recon_json_roundtrip_preserves_lineage(tmp_path: Path) -> None:
    """recon はディスク経由で preflight に渡るので JSON 往復で壊れないこと。"""
    _stage_chain(tmp_path, paper_run=RUN, exit_run=RUN)
    on_disk = json.loads(
        (tmp_path / f"recon_{COMPACT}.json").read_text(encoding="utf-8")
    )
    assert on_disk["execution_lineage"]["paper_orders"] == "verified"
    assert on_disk["execution_lineage_ok"] is True
