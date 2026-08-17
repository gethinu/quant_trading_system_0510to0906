"""オープン後 fill 再 reconcile の検証 (2026-08-12 observability fix ②)。

狙い:
  - 寄り直後スナップ (成行 PENDING_NEW) は fill 0。約定確定後に order status を再取得して
    entry ``filled`` と台帳を更新すると、fill 表示が実約定を反映する。
  - broker へは read のみ (本テストは status_map を注入し I/O を模擬)。発注しない。
  - **flag-gate OFF で byte-parity**: FILL_RECONCILE_ENABLED 未設定なら CLI は何も書かない。
  - 既存の exit / funnel を壊さない (entry filled のみ更新)。idempotent。
"""

from __future__ import annotations

import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.reconcile_fills import fill_reconcile_enabled
from scripts.reconcile_fills import main as reconcile_main  # noqa: E402
from scripts.reconcile_fills import patch_recon_fills, recompute_fills_from_status


def _paper_orders() -> dict:
    """system2 ショート 10 件 submit (order_id あり) + system1 ロング 2 件 skip。"""
    orders = []
    for i in range(10):
        orders.append(
            {
                "symbol": f"S{i}",
                "side": "sell",
                "system": "system2",
                "client_order_id": f"system2-S{i}-20260812",
                "order_id": f"oid-s2-{i}",
                "status": "pending_new",
                "skip_reason": None,
                "error": None,
            }
        )
    for i in range(2):
        orders.append(
            {
                "symbol": f"L{i}",
                "side": "buy",
                "system": "system1",
                "client_order_id": f"system1-L{i}-20260812",
                "order_id": None,
                "status": None,
                "skip_reason": "already_held:x",
                "error": None,
            }
        )
    return {"date": "2026-08-12", "orders": orders}


def _recon() -> dict:
    return {
        "date": "2026-08-12",
        "systems": {
            "system1": {
                "long": {"entry_submitted": 0, "filled": 0},
                "short": {"entry_submitted": 0, "filled": 0},
                "exit": {"submitted": 2, "close": 0, "protect": 2},
            },
            "system2": {
                "long": {"entry_submitted": 0, "filled": 0},
                "short": {"entry_submitted": 10, "filled": 0},
                "exit": {"submitted": 10, "close": 10, "protect": 0},
            },
        },
        "portfolio": {
            "entry_submitted": 10,
            "entry_filled": 0,
            "exit_submitted": 12,
            "exit_close": 10,
            "exit_protect": 2,
        },
    }


# --- 寄り直後 (全 PENDING_NEW) は fill 0 ------------------------------------------
def test_open_snapshot_is_zero_fill():
    po = _paper_orders()
    at_open = {f"oid-s2-{i}": "pending_new" for i in range(10)}
    fills = recompute_fills_from_status(po, at_open)
    assert fills["portfolio"]["entry_submitted"] == 10
    assert fills["portfolio"]["entry_filled"] == 0


# --- 約定確定後は fill が実数を反映 (enum status も解釈) ---------------------------
def test_after_settlement_fills_reflected():
    po = _paper_orders()
    settled = {f"oid-s2-{i}": "OrderStatus.FILLED" for i in range(10)}
    fills = recompute_fills_from_status(po, settled)
    assert fills["portfolio"]["entry_filled"] == 10
    assert fills["portfolio"]["short_entry_filled"] == 10
    assert fills["portfolio"]["long_entry_filled"] == 0
    assert fills["systems"]["system2"]["short"]["filled"] == 10


# --- 部分約定: 未確定 order は filled に数えない (正直さ) ---------------------------
def test_partial_settlement_only_counts_filled():
    po = _paper_orders()
    partial = {f"oid-s2-{i}": ("filled" if i < 4 else "pending_new") for i in range(10)}
    fills = recompute_fills_from_status(po, partial)
    assert fills["portfolio"]["entry_filled"] == 4


# --- recon patch: filled のみ更新、exit/submitted は不変 + idempotent -------------
def test_patch_recon_updates_only_fill():
    po = _paper_orders()
    settled = {f"oid-s2-{i}": "filled" for i in range(10)}
    fills = recompute_fills_from_status(po, settled)
    recon = _recon()
    _, n, status = patch_recon_fills(recon, fills)
    assert status == "ok"
    assert recon["portfolio"]["entry_filled"] == 10
    assert recon["systems"]["system2"]["short"]["filled"] == 10
    # exit / submitted は壊さない
    assert recon["portfolio"]["exit_submitted"] == 12
    assert recon["systems"]["system2"]["short"]["entry_submitted"] == 10
    assert recon["systems"]["system2"]["exit"]["close"] == 10
    # 監査メタ
    assert recon["meta"]["fill_reconcile"]["entry_filled"] == 10
    assert recon["meta"]["fill_reconcile"]["entry_filled_prev"] == 0
    # idempotent
    _, n2, _ = patch_recon_fills(recon, fills)
    assert n2 == 0


def test_patch_recon_no_fills_is_noop():
    recon = _recon()
    _, n, status = patch_recon_fills(
        recon, {"n_orders": 0, "portfolio": {}, "systems": {}}
    )
    assert status == "no_fills" and n == 0
    assert recon["portfolio"]["entry_filled"] == 0


# --- flag OFF: CLI は完全に inert (byte-parity) -----------------------------------
def test_cli_off_by_default_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.delenv("FILL_RECONCILE_ENABLED", raising=False)
    assert fill_reconcile_enabled() is False
    # paper_orders を置くが、flag OFF なので触られないはず
    rd = tmp_path
    (rd / "paper_orders_20260812.json").write_text(
        json.dumps(_paper_orders(), ensure_ascii=False), encoding="utf-8"
    )
    before = set(p.name for p in rd.iterdir())
    rc = reconcile_main(["--date", "2026-08-12", "--results-dir", str(rd)])
    assert rc == 0
    after = set(p.name for p in rd.iterdir())
    assert before == after  # fills_*.json 等を作っていない = byte-parity


# --- flag ON + status_map 注入: durable に fills 台帳 + recon 更新 -----------------
def test_cli_on_with_injected_status_map(tmp_path, monkeypatch):
    monkeypatch.setenv("FILL_RECONCILE_ENABLED", "1")
    rd = tmp_path
    (rd / "paper_orders_20260812.json").write_text(
        json.dumps(_paper_orders(), ensure_ascii=False), encoding="utf-8"
    )
    (rd / "recon_20260812.json").write_text(
        json.dumps(_recon(), ensure_ascii=False), encoding="utf-8"
    )
    smap = {f"oid-s2-{i}": "filled" for i in range(10)}
    (rd / "smap.json").write_text(json.dumps(smap), encoding="utf-8")
    rc = reconcile_main(
        [
            "--date",
            "2026-08-12",
            "--results-dir",
            str(rd),
            "--status-map-json",
            str(rd / "smap.json"),
        ]
    )
    assert rc == 0
    fills = json.loads((rd / "fills_20260812.json").read_text(encoding="utf-8"))
    assert fills["portfolio"]["entry_filled"] == 10
    recon = json.loads((rd / "recon_20260812.json").read_text(encoding="utf-8"))
    assert recon["portfolio"]["entry_filled"] == 10


# --- 不確定な lookup で durable な filled を 0 へ後退させない -----------------------
# Codex review (PR#153): get_orders_status_map() は broker 例外を None に潰すため、
# transient な API 失敗時に「未 fill が確定した」と誤解して台帳を巻き戻し得た。
def test_unknown_status_is_counted_and_marks_incomplete():
    po = _paper_orders()
    # 10 件中 3 件が lookup 失敗 (None)、残りは filled
    partial = {f"oid-s2-{i}": (None if i < 3 else "filled") for i in range(10)}
    fills = recompute_fills_from_status(po, partial)
    assert fills["n_unknown"] == 3
    assert fills["complete"] is False
    # 既知の filled は 7 件だけ数える (unknown を filled とは数えない)
    assert fills["portfolio"]["entry_filled"] == 7


def test_missing_order_id_in_status_map_is_unknown_not_unfilled():
    po = _paper_orders()
    fills = recompute_fills_from_status(po, {})  # 1 件も引けなかった
    assert fills["n_unknown"] == 10
    assert fills["complete"] is False
    assert fills["portfolio"]["entry_filled"] == 0


def test_incomplete_status_does_not_regress_recorded_fills():
    """全件 filled を記録した後、lookup 全失敗で再実行しても 0 に戻らない。"""
    po = _paper_orders()
    settled = recompute_fills_from_status(
        po, {f"oid-s2-{i}": "filled" for i in range(10)}
    )
    recon = _recon()
    patch_recon_fills(recon, settled)
    assert recon["portfolio"]["entry_filled"] == 10

    # broker が全滅 (全 order で例外 -> None)
    blackout = recompute_fills_from_status(po, {f"oid-s2-{i}": None for i in range(10)})
    _, n, status = patch_recon_fills(recon, blackout)

    assert status == "incomplete_status"
    assert n == 0
    # durable な実績は保持される
    assert recon["portfolio"]["entry_filled"] == 10
    assert recon["systems"]["system2"]["short"]["filled"] == 10
    # 監査メタに理由が残る
    assert recon["meta"]["fill_reconcile"]["status"] == "incomplete_status"
    assert recon["meta"]["fill_reconcile"]["n_unknown"] == 10


def test_partial_blackout_also_refuses_to_overwrite():
    """一部だけ引けない場合も下振れ値で上書きしない。"""
    po = _paper_orders()
    settled = recompute_fills_from_status(
        po, {f"oid-s2-{i}": "filled" for i in range(10)}
    )
    recon = _recon()
    patch_recon_fills(recon, settled)

    partial = recompute_fills_from_status(
        po, {f"oid-s2-{i}": (None if i < 2 else "filled") for i in range(10)}
    )
    _, n, status = patch_recon_fills(recon, partial)
    assert status == "incomplete_status" and n == 0
    assert recon["portfolio"]["entry_filled"] == 10


def test_complete_status_still_updates_normally():
    """全件 status が引ければ従来どおり更新する (fail-safe が過剰に効かない)。"""
    po = _paper_orders()
    fills = recompute_fills_from_status(
        po, {f"oid-s2-{i}": ("filled" if i < 6 else "canceled") for i in range(10)}
    )
    assert fills["complete"] is True
    recon = _recon()
    _, _, status = patch_recon_fills(recon, fills)
    assert status == "ok"
    assert recon["portfolio"]["entry_filled"] == 6
    assert recon["meta"]["fill_reconcile"]["status"] == "ok"
