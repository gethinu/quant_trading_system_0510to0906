"""exit artifact の role 分離 (提案 vs 実発注) の契約 test。

守りたい不変条件:
    - 同じ日に朝の提案 run と夜の実発注 run が走っても、実発注記録が消えない
    - 「直近の実発注」を訊いたら、当日の提案ではなく前営業日夜の実発注が返る
    - role 未記載の legacy artifact も mode から実発注と判定できる
      (新 writer が回る前の過去日を遡って検証できる)
"""

from __future__ import annotations

import json
from pathlib import Path

from common.exit_artifacts import (
    ROLE_EXECUTION,
    ROLE_PROPOSAL,
    artifact_role,
    latest_execution,
    role_for,
    sidecar_path,
    write_with_sidecar,
)


def _write(path: Path, **payload) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_role_follows_whether_anything_was_submitted():
    assert role_for(dry_run=True) == ROLE_PROPOSAL
    assert role_for(dry_run=False) == ROLE_EXECUTION


def test_sidecar_name_is_derived_from_canonical_path(tmp_path: Path):
    canonical = tmp_path / "exit_orders_20260818.json"
    assert (
        sidecar_path(canonical, ROLE_EXECUTION).name
        == "exit_orders_20260818_execution.json"
    )


def test_write_stamps_role_and_written_at(tmp_path: Path):
    canonical = tmp_path / "exit_orders_20260818.json"
    payload = {"date": "2026-08-18", "mode": "submitted", "exits": []}
    side = write_with_sidecar(canonical, payload, ROLE_EXECUTION)

    written = json.loads(side.read_text(encoding="utf-8"))
    assert written["role"] == ROLE_EXECUTION
    assert written["written_at"].startswith("20")
    # canonical は「最後に走った run」なので同じ内容で残る
    assert json.loads(canonical.read_text(encoding="utf-8"))["role"] == ROLE_EXECUTION


def test_morning_proposal_does_not_clobber_the_execution_record(tmp_path: Path):
    """これが本丸。夜の実発注のあとに翌朝の提案が同じ date へ書いても消えない。"""
    canonical = tmp_path / "exit_orders_20260818.json"
    write_with_sidecar(
        canonical,
        {"date": "2026-08-18", "mode": "submitted", "submitted": 12, "exits": []},
        ROLE_EXECUTION,
    )
    # 同じ営業日に再度 dry-run が走る (手動再実行 / pipeline の重複起動)
    write_with_sidecar(
        canonical,
        {"date": "2026-08-18", "mode": "dry_run", "submitted": 0, "exits": []},
        ROLE_PROPOSAL,
    )

    execution = json.loads(
        sidecar_path(canonical, ROLE_EXECUTION).read_text(encoding="utf-8")
    )
    assert execution["submitted"] == 12
    # canonical だけ見ると提案で上書きされている = これが従来の壊れ方
    assert json.loads(canonical.read_text(encoding="utf-8"))["submitted"] == 0


def test_latest_execution_skips_todays_proposal_for_last_nights_submit(tmp_path: Path):
    """朝 07:20 の状況: 当日は提案しかなく、前営業日夜に実発注がある。"""
    write_with_sidecar(
        tmp_path / "exit_orders_20260817.json",
        {"date": "2026-08-17", "mode": "submitted", "exits": []},
        ROLE_EXECUTION,
    )
    write_with_sidecar(
        tmp_path / "exit_orders_20260818.json",
        {"date": "2026-08-18", "mode": "dry_run", "exits": []},
        ROLE_PROPOSAL,
    )

    found = latest_execution(tmp_path, on_or_before="2026-08-18")
    assert found is not None
    _path, payload = found
    assert payload["date"] == "2026-08-17"
    assert artifact_role(payload) == ROLE_EXECUTION


def test_legacy_artifact_without_role_is_classified_by_mode(tmp_path: Path):
    """role 導入前の artifact (08-10..08-17) も遡って検証できること。"""
    _write(tmp_path / "exit_orders_20260814.json", date="2026-08-14", mode="submitted")
    _write(tmp_path / "exit_orders_20260815.json", date="2026-08-15", mode="dry_run")

    found = latest_execution(tmp_path, on_or_before="2026-08-15")
    assert found is not None
    assert found[1]["date"] == "2026-08-14"


def test_latest_execution_respects_the_upper_bound(tmp_path: Path):
    write_with_sidecar(
        tmp_path / "exit_orders_20260818.json",
        {"date": "2026-08-18", "mode": "submitted", "exits": []},
        ROLE_EXECUTION,
    )
    assert latest_execution(tmp_path, on_or_before="2026-08-17") is None


def test_no_execution_anywhere_returns_none(tmp_path: Path):
    write_with_sidecar(
        tmp_path / "exit_orders_20260818.json",
        {"date": "2026-08-18", "mode": "dry_run", "exits": []},
        ROLE_PROPOSAL,
    )
    assert latest_execution(tmp_path) is None


def test_unparseable_artifact_is_skipped_not_fatal(tmp_path: Path):
    (tmp_path / "exit_orders_20260818.json").write_text("{ broken", encoding="utf-8")
    write_with_sidecar(
        tmp_path / "exit_orders_20260817.json",
        {"date": "2026-08-17", "mode": "submitted", "exits": []},
        ROLE_EXECUTION,
    )
    found = latest_execution(tmp_path, on_or_before="2026-08-18")
    assert found is not None and found[1]["date"] == "2026-08-17"
