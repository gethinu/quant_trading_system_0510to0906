"""exit E2E 検証 (scripts/exit_verify.verify) の突合ロジック検証。

- time-based 満期の独立再計算 (positions snapshot から holding_days>=max)。
- planned close の fill/pending/reject 分類。
- 満期なのに未計画 (paper_exit_check 漏れ) の検知。
status_map を注入できる純関数なので Alpaca 無しで検証できる。
"""

from __future__ import annotations

import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.exit_verify import _expected_time_exits, verify  # noqa: E402


def _pos(symbol, system, entry_date, side="long", qty=1.0):
    return {
        "symbol": symbol,
        "system": system,
        "entry_date": entry_date,
        "side": side,
        "qty": qty,
    }


def _exit(
    symbol, system, reason, order_type, order_id=None, dry_run=False, status=None
):
    return {
        "symbol": symbol,
        "system": system,
        "reason": reason,
        "order_type": order_type,
        "order_id": order_id,
        "dry_run": dry_run,
        "status": status,
    }


# --- expected time exits recompute ----------------------------------------
def test_expected_time_exits_by_rule():
    # S3 max 3d: 07-08 entry, today 07-12 -> 4d -> due。S1 は time-exit 無し。
    positions = [
        _pos("AAA", "system3", "2026-07-08"),
        _pos("BBB", "system3", "2026-07-11"),  # 1d -> not due
        _pos("CCC", "system1", "2026-07-01"),  # S1: max_hold=0 -> never
    ]
    due = _expected_time_exits(positions, "2026-07-12")
    syms = {d["symbol"] for d in due}
    assert syms == {"AAA"}


def test_expected_time_exits_boundary_equal():
    # holding_days == max_holding_days は due (>=)
    positions = [_pos("AAA", "system2", "2026-07-10")]  # 2d == max2 -> due
    due = _expected_time_exits(positions, "2026-07-12")
    assert len(due) == 1


# --- reconcile: due but not planned ---------------------------------------
def test_due_not_planned_flagged():
    exit_orders = {
        "mode": "dry_run",
        "positions": [
            _pos("AAA", "system3", "2026-07-08"),  # due
            _pos("BBB", "system3", "2026-07-08"),  # due
        ],
        "exits": [
            # AAA だけ計画、BBB は漏れ
            _exit(
                "AAA", "system3", "time_based", "market", order_id="o1", status="filled"
            ),
        ],
    }
    v = verify(exit_orders, "2026-07-12", status_map={})
    assert v["n_expected_time_exits"] == 2
    dnp = {d["symbol"] for d in v["discrepancies"]["due_not_planned"]}
    assert dnp == {"BBB"}
    assert v["n_warn"] >= 1


def test_all_due_planned_and_filled_no_warn():
    exit_orders = {
        "mode": "submitted",
        "positions": [_pos("AAA", "system3", "2026-07-08")],
        "exits": [
            _exit(
                "AAA", "system3", "time_based", "market", order_id="o1", dry_run=False
            )
        ],
    }
    # live status_map が fill を返す
    v = verify(exit_orders, "2026-07-12", status_map={"o1": "filled"})
    assert v["n_filled_closes"] == 1
    assert v["n_warn"] == 0


# --- artifact 選択: 朝の提案ではなく前営業日夜の実発注を検証する -------------
def _artifact(tmp_path: Path, date: str, role: str, **extra):
    from common.exit_artifacts import write_with_sidecar

    payload = {
        "date": date,
        "mode": "submitted" if role == "execution" else "dry_run",
        "positions": [_pos("AAA", "system3", "2026-07-08")],
        "exits": [],
        **extra,
    }
    compact = date.replace("-", "")
    write_with_sidecar(tmp_path / f"exit_orders_{compact}.json", payload, role)


def test_morning_run_verifies_last_nights_execution_not_todays_proposal(
    tmp_path: Path, capsys
):
    """07:20 の定例 run が 06:00 の提案を実発注として検証する回帰を防ぐ。

    提案を掴むと planned close が毎日「未送信」に見え、警報が常時赤になる。
    """
    from scripts.exit_verify import main

    _artifact(
        tmp_path,
        "2026-07-11",
        "execution",
        exits=[
            _exit(
                "AAA", "system3", "time_based", "market", order_id="o1", dry_run=False
            )
        ],
    )
    _artifact(tmp_path, "2026-07-12", "proposal")

    rc = main(
        [
            "--date",
            "2026-07-12",
            "--results-dir",
            str(tmp_path),
            "--log-dir",
            str(tmp_path / "logs"),
            "--no-alpaca",
            "--no-notify",
        ]
    )
    out = capsys.readouterr().out
    assert "role=execution" in out
    assert "date=2026-07-11" in out
    # 満期判定は artifact 自身の日付基準 (07-11 なら 3d でちょうど満期)
    record = json.loads(
        (tmp_path / "logs" / "exit_verify_20260711.json").read_text(encoding="utf-8")
    )
    assert record["requested_date"] == "2026-07-12"
    assert record["exit_orders_role"] == "execution"
    assert record["date"] == "2026-07-11"
    assert rc in (0, 2)


def test_missing_execution_artifact_is_distinct_from_a_clean_run(tmp_path: Path):
    """提案しか無い状態を「乖離なし」と言わない。"""
    from scripts.exit_verify import main

    _artifact(tmp_path, "2026-07-12", "proposal")
    rc = main(
        [
            "--date",
            "2026-07-12",
            "--results-dir",
            str(tmp_path),
            "--log-dir",
            str(tmp_path / "logs"),
            "--no-alpaca",
            "--no-notify",
        ]
    )
    assert rc == 1


# --- reconcile: close fill classification ---------------------------------
def test_rejected_close_is_warn():
    exit_orders = {
        "positions": [],
        "exits": [
            _exit(
                "AAA", "system2", "time_based", "market", order_id="o1", dry_run=False
            )
        ],
    }
    v = verify(exit_orders, "2026-07-12", status_map={"o1": "rejected"})
    assert len(v["discrepancies"]["closes_rejected"]) == 1
    assert v["n_warn"] >= 1


def test_pending_close_is_info_not_warn():
    # 市場休場中の成行は accepted のまま = pending (失敗ではない)
    exit_orders = {
        "positions": [],
        "exits": [
            _exit(
                "AAA", "system2", "time_based", "market", order_id="o1", dry_run=False
            )
        ],
    }
    v = verify(exit_orders, "2026-07-12", status_map={"o1": "accepted"})
    assert len(v["discrepancies"]["closes_pending"]) == 1
    assert len(v["discrepancies"]["closes_unfilled_nonpending"]) == 0
    # pending だけなら n_warn に数えない
    assert v["n_warn"] == 0


def test_protection_orders_not_counted_as_close():
    # resting protection (stop/limit) は close ではない → 未 fill でも WARN しない
    exit_orders = {
        "positions": [],
        "exits": [
            _exit(
                "AAA", "system5", "protect_stop", "stop", order_id="o1", dry_run=False
            ),
            _exit(
                "AAA",
                "system5",
                "protect_target",
                "limit",
                order_id="o2",
                dry_run=False,
            ),
        ],
    }
    v = verify(exit_orders, "2026-07-12", status_map={"o1": "new", "o2": "new"})
    assert v["n_planned_closes"] == 0
    assert v["n_warn"] == 0


def test_fractional_gap_regression():
    """qty<1 の満期建玉が exits に無いと due_not_planned で必ず立つ (07-12 実データの回帰)。"""
    exit_orders = {
        "positions": [
            _pos("FRAC", "system3", "2026-07-08", qty=0.18),  # 4d due だが fractional
        ],
        "exits": [],  # 端株は exit builder が abs_qty=0 で skip → 未計画
    }
    v = verify(exit_orders, "2026-07-12", status_map={})
    assert [d["symbol"] for d in v["discrepancies"]["due_not_planned"]] == ["FRAC"]
