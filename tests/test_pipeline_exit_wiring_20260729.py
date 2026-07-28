"""漏斗 (signal_pipeline/v1) の Exit phase を recon から配線するロジックの検証。

狙い (2026-07-29):
  - ダッシュボードの Exit ステージ「未計測」を、ntfy が使うのと **同一 recon** から
    実数で埋める。ntfy 本文 (`exit N (close C / protect P)`) と漏斗が一致すること。
  - 0 件の日は 0、内訳 (close/protect) も出す。取れない日は正直に「未計測」を維持
    (0 で誤魔化さない)。
"""

from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common.publishers.execution_summary import build_body  # noqa: E402
from scripts.build_execution_recon import (  # noqa: E402
    build_recon,
    exit_counts_from_recon,
    patch_pipeline_exit,
)


def _pipeline() -> dict:
    """Exit=null (未計測) の最小 pipeline (daily_polygon_monitor 出力形)。"""
    def sys_block(sysid: str, entry: int | None) -> dict:
        return {
            "system_id": sysid,
            "final_signals": entry,
            "phases": [
                {"name": "Tgt", "label": "Tgt", "condition": "u", "count": 5000,
                 "measured": True, "ratio_of_prev": None, "ratio_of_universe": 1.0},
                {"name": "Entry", "label": "Entry", "condition": "allocation 後エントリ発火",
                 "count": entry, "measured": False, "ratio_of_prev": None,
                 "ratio_of_universe": None},
                {"name": "Exit", "label": "Exit", "condition": "本日手仕舞い発火",
                 "count": None, "measured": False, "ratio_of_prev": None,
                 "ratio_of_universe": None},
            ],
        }

    return {
        "date": "2026-07-29",
        "schema": "signal_pipeline/v1",
        "systems": {
            "sys1": sys_block("sys1", 2),
            "sys2": sys_block("sys2", 10),
            "sys4": sys_block("sys4", 0),
        },
        "notes": ["phases are reference counts, not evaluation criteria."],
    }


def _exit_orders(rows: list[dict]) -> dict:
    return {"date": "2026-07-29", "exits": rows}


def _exit_phase(pipeline: dict, sysk: str) -> dict:
    return next(p for p in pipeline["systems"][sysk]["phases"] if p["name"] == "Exit")


def test_exit_counts_normalizes_system_to_sys_key() -> None:
    recon = build_recon(
        None, None,
        _exit_orders([
            {"system": "system2", "reason": "protect_stop", "order_id": "o1"},
            {"system": "system2", "reason": "close_time", "order_id": "o2"},
        ]),
        date_str="2026-07-29",
    )
    ec = exit_counts_from_recon(recon)
    assert ec["sys2"] == {"submitted": 2, "close": 1, "protect": 1}


def test_patch_fills_exit_and_matches_ntfy_headline() -> None:
    """今夜の実測 `exit 1 (close 0 / protect 1)` が漏斗にも同じ数字で出ること。"""
    recon = build_recon(
        None, None,
        _exit_orders([
            {"system": "system1", "reason": "protect_stop", "order_id": "o1"},
        ]),
        date_str="2026-07-29",
    )
    # ntfy 本文の見出し exit 行
    body = build_body(recon)
    assert "exit 1 (close 0 / protect 1)" in body

    pipeline = _pipeline()
    _, n_filled, status = patch_pipeline_exit(pipeline, recon)
    assert status == "ok" and n_filled == 3

    # 漏斗 Exit の合計 = ntfy 見出しの exit_submitted = 1 (乖離しない)
    total_exit = sum(_exit_phase(pipeline, s)["count"] for s in pipeline["systems"])
    assert total_exit == recon["portfolio"]["exit_submitted"] == 1

    ex1 = _exit_phase(pipeline, "sys1")
    assert ex1["count"] == 1 and ex1["measured"] is True
    assert ex1["exit_close"] == 0 and ex1["exit_protect"] == 1
    assert "(close 0 / protect 1)" in ex1["condition"]
    # ratio_of_prev = exit / entry(=2) の形で入る
    assert ex1["ratio_of_prev"] == 0.5


def test_zero_exit_day_shows_zero_not_unmeasured() -> None:
    """exit_orders はあるが発火 0 の日: 未計測ではなく 0 を出す。"""
    recon = build_recon(None, None, _exit_orders([]), date_str="2026-07-29")
    pipeline = _pipeline()
    _, _, status = patch_pipeline_exit(pipeline, recon)
    assert status == "ok"
    ex = _exit_phase(pipeline, "sys1")
    assert ex["count"] == 0 and ex["measured"] is True
    assert ex["exit_close"] == 0 and ex["exit_protect"] == 0


def test_no_recon_keeps_unmeasured() -> None:
    pipeline = _pipeline()
    _, n_filled, status = patch_pipeline_exit(pipeline, None)
    assert status == "no_recon" and n_filled == 0
    assert _exit_phase(pipeline, "sys1")["count"] is None  # 未計測を維持


def test_partial_recon_without_exit_orders_keeps_unmeasured() -> None:
    """exit_orders 入力が無い部分 recon は「発火 0」ではなく「未計測」を維持。"""
    recon = build_recon(None, None, None, date_str="2026-07-29")  # exit_orders=None
    assert recon["inputs"]["exit_orders"] is False
    pipeline = _pipeline()
    _, n_filled, status = patch_pipeline_exit(pipeline, recon)
    assert status == "exit_orders_input_missing" and n_filled == 0
    assert _exit_phase(pipeline, "sys1")["count"] is None  # 0 で埋めない


def test_patch_is_idempotent() -> None:
    recon = build_recon(
        None, None,
        _exit_orders([{"system": "system1", "reason": "protect_stop", "order_id": "o1"}]),
        date_str="2026-07-29",
    )
    pipeline = _pipeline()
    patch_pipeline_exit(pipeline, recon)
    cond_once = _exit_phase(pipeline, "sys1")["condition"]
    patch_pipeline_exit(pipeline, recon)
    cond_twice = _exit_phase(pipeline, "sys1")["condition"]
    assert cond_once == cond_twice  # condition の内訳が二重付与されない
    assert cond_twice.count("(close") == 1
