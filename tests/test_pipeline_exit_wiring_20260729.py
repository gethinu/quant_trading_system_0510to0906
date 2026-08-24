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
                {
                    "name": "Tgt",
                    "label": "Tgt",
                    "condition": "u",
                    "count": 5000,
                    "measured": True,
                    "ratio_of_prev": None,
                    "ratio_of_universe": 1.0,
                },
                {
                    "name": "Entry",
                    "label": "Entry",
                    "condition": "allocation 後エントリ発火",
                    "count": entry,
                    "measured": False,
                    "ratio_of_prev": None,
                    "ratio_of_universe": None,
                },
                {
                    "name": "Exit",
                    "label": "Exit",
                    "condition": "本日手仕舞い発火",
                    "count": None,
                    "measured": False,
                    "ratio_of_prev": None,
                    "ratio_of_universe": None,
                },
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
        None,
        None,
        _exit_orders(
            [
                {"system": "system2", "reason": "protect_stop", "order_id": "o1"},
                {"system": "system2", "reason": "close_time", "order_id": "o2"},
            ]
        ),
        date_str="2026-07-29",
    )
    ec = exit_counts_from_recon(recon)
    # 2026-08-19: rejected / suppressed を armed から分離したため schema が増えた。
    assert ec["sys2"] == {
        "submitted": 2,
        "close": 1,
        "protect": 1,
        "armed": 0,
        "armed_close": 0,
        "armed_protect": 0,
        "rejected": 0,
        "rejected_close": 0,
        "rejected_protect": 0,
        "suppressed": 0,
        "suppressed_close": 0,
        "suppressed_protect": 0,
    }


def test_patch_fills_exit_and_matches_ntfy_headline() -> None:
    """今夜の実測 `exit 1 (close 0 / protect 1)` が漏斗にも同じ数字で出ること。"""
    recon = build_recon(
        None,
        None,
        _exit_orders(
            [
                {"system": "system1", "reason": "protect_stop", "order_id": "o1"},
            ]
        ),
        date_str="2026-07-29",
    )
    # ntfy 本文の見出し exit 行 (新表示: fired を明示、armed 0 なので suffix 無し)
    body = build_body(recon)
    assert "exit 1 fired (close 0 / protect 1)" in body
    assert "armed" not in body

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


# ---------------------------------------------------------------------------
# 実データ回帰 (裏取りで使った 07-27 / 07-28 / 07-29 の分布を再現)。
# 新表示ロジック `exit N fired (close Cs / protect Ps) · M armed` と
# fired/armed の分離が期待通り出ることを固定する。
# 数値は results_csv/exit_orders_*.json の実測 (新セマンティクス) に一致:
#   07-27: fired 23 (close 20 / protect 3) · armed 1
#   07-28: fired 1  (close 0  / protect 1) · armed 0
#   07-29: fired 0  (close 0  / protect 0) · armed 25
# ---------------------------------------------------------------------------


def _rows(system: str, reason: str, n: int, *, submitted: bool) -> list[dict]:
    out = []
    for i in range(n):
        r = {"system": system, "reason": reason}
        if submitted:
            r["order_id"] = f"{reason}-{i}"
        out.append(r)  # submitted=False は order_id 無し → armed
    return out


def test_regression_20260727_real_fire_day() -> None:
    """07-27: 実発火日。fired 23 = close 20 + protect 3、armed 1 は別枠。"""
    rows = (
        _rows("system2", "time_based", 20, submitted=True)  # fired close
        + _rows("system2", "protect_stop", 3, submitted=True)  # fired protect
        + _rows("system2", "protect_target", 1, submitted=False)  # armed protect
    )
    recon = build_recon(None, None, _exit_orders(rows), date_str="2026-07-27")
    p = recon["portfolio"]
    assert p["exit_submitted"] == 23
    assert p["exit_close"] == 20 and p["exit_protect"] == 3
    assert p["exit_submitted"] == p["exit_close"] + p["exit_protect"]  # N=Cs+Ps
    assert p["exit_armed"] == 1 and p["exit_armed_protect"] == 1

    body = build_body(recon)
    assert "exit 23 fired (close 20 / protect 3) · 1 armed" in body

    pipeline = _pipeline()
    _, n_filled, status = patch_pipeline_exit(pipeline, recon)
    assert status == "ok" and n_filled == 3
    ex = _exit_phase(pipeline, "sys2")
    assert ex["count"] == 23 and ex["fired"] == 23 and ex["measured"] is True
    assert ex["exit_close"] == 20 and ex["exit_protect"] == 3
    assert ex["armed"] == 1 and ex["armed_protect"] == 1
    assert ex["condition"].endswith("(close 20 / protect 3) · 1 armed")


def test_regression_20260728_coincidental_n_equals_c_plus_p() -> None:
    """07-28: fired 1 が偶然 close+protect と一致した日。armed 0 で suffix 無し。"""
    rows = _rows("system1", "protect_stop", 1, submitted=True)
    recon = build_recon(None, None, _exit_orders(rows), date_str="2026-07-28")
    p = recon["portfolio"]
    assert p["exit_submitted"] == 1 and p["exit_close"] == 0 and p["exit_protect"] == 1
    assert p["exit_armed"] == 0
    body = build_body(recon)
    assert "exit 1 fired (close 0 / protect 1)" in body
    assert "armed" not in body


def test_regression_20260729_all_armed_no_fire() -> None:
    """07-29: pre-open。fired 0 だが 25 の保護注文が armed。0 張り付きではなく honest。"""
    rows = (
        _rows("system1", "protect_trailing", 2, submitted=False)
        + _rows("system1", "protect_stop", 13, submitted=False)
        + _rows("system1", "protect_target", 10, submitted=False)
    )
    recon = build_recon(None, None, _exit_orders(rows), date_str="2026-07-29")
    p = recon["portfolio"]
    assert p["exit_submitted"] == 0
    assert p["exit_close"] == 0 and p["exit_protect"] == 0  # fired 分は 0
    assert p["exit_armed"] == 25 and p["exit_armed_protect"] == 25
    body = build_body(recon)
    assert "exit 0 fired (close 0 / protect 0) · 25 armed" in body

    pipeline = _pipeline()
    _, _, status = patch_pipeline_exit(pipeline, recon)
    assert status == "ok"
    ex = _exit_phase(pipeline, "sys1")
    # funnel の発火バーは 0 (honest)、armed は別枠フィールドで 25
    assert ex["count"] == 0 and ex["fired"] == 0 and ex["measured"] is True
    assert ex["armed"] == 25 and ex["armed_protect"] == 25
    assert "· 25 armed" in ex["condition"]


def test_flatten_all_system_null_not_dropped() -> None:
    """system=null の exit (flatten_all 等) を drop せず __unassigned__ に集計する。"""
    rows = [
        {"system": None, "reason": "flatten_all", "order_id": "f1"},  # fired close
        {"system": None, "reason": "flatten_all"},  # armed close
    ]
    recon = build_recon(None, None, _exit_orders(rows), date_str="2026-07-10")
    p = recon["portfolio"]
    # 旧実装は system=None を drop し close=0 に取りこぼしていた。now 計上される。
    assert p["exit_submitted"] == 1 and p["exit_close"] == 1
    assert p["exit_armed"] == 1 and p["exit_armed_close"] == 1
    assert "__unassigned__" in recon["systems"]


def test_patch_is_idempotent() -> None:
    recon = build_recon(
        None,
        None,
        _exit_orders(
            [{"system": "system1", "reason": "protect_stop", "order_id": "o1"}]
        ),
        date_str="2026-07-29",
    )
    pipeline = _pipeline()
    patch_pipeline_exit(pipeline, recon)
    cond_once = _exit_phase(pipeline, "sys1")["condition"]
    patch_pipeline_exit(pipeline, recon)
    cond_twice = _exit_phase(pipeline, "sys1")["condition"]
    assert cond_once == cond_twice  # condition の内訳が二重付与されない
    assert cond_twice.count("(close") == 1
