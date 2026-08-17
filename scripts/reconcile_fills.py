"""オープン発注後の fill 再 reconcile (成行が寄りで PENDING_NEW → 約定確定後に再計測)。

背景 (2026-08-12 observability fix):
  オープン run は成行 entry を寄りで送信し、その *直後* に recon スナップを取る。
  Alpaca 成行は submit 直後 ``PENDING_NEW`` (未約定) なので ``entry N → fill 0`` が
  毎回出る。これは「約定失敗」ではなく **スナップ時点が早すぎる** だけ。約定が確定した
  タイミングで Alpaca の orders/positions を *read-only* で取り直し、fill 数と台帳
  (recon_YYYYMMDD.json + fills_YYYYMMDD.json) を durable に更新する follow-up step。

ガードレール (厳守):
  - **発注しない**。broker へは read (get_order_by_id / positions) と記帳のみ。
  - **flag-gate**: ``FILL_RECONCILE_ENABLED`` が truthy でなければ完全に inert
    (通知を出して exit 0、ファイルは一切触らない = byte-parity)。
  - paper のみ (``ALPACA_PAPER`` 既定 True)。
  - 既存の exit 計測を壊さない (recon の exit / funnel には触れず、entry ``filled`` のみ更新)。

pure 関数 (I/O 無し, テスト対象):
  - ``recompute_fills_from_status(paper_orders, status_map)``
  - ``patch_recon_fills(recon, fills)``

CLI (flag on 時のみ broker I/O):
  ``FILL_RECONCILE_ENABLED=1 python -m scripts.reconcile_fills --date 2026-08-12``
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import sys
from typing import Any

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from scripts.build_execution_recon import (  # noqa: E402
    _FILLED_STATUSES,
    _default_path,
    _load_json,
    _norm_side,
    _norm_system,
)

logger = logging.getLogger(__name__)

_TRUTHY = ("1", "true", "yes", "y", "on")


def fill_reconcile_enabled() -> bool:
    """master flag。既定 OFF。設定されない限り本 step は完全に inert。"""
    return os.getenv("FILL_RECONCILE_ENABLED", "").strip().lower() in _TRUTHY


def _norm_status(status: Any) -> str:
    """Alpaca status (enum ``OrderStatus.FILLED`` or str 'filled') を小文字 tail に正規化。"""
    text = str(status if status is not None else "").strip().lower()
    # enum 表現 'orderstatus.filled' → 'filled'
    if "." in text:
        text = text.rsplit(".", 1)[-1]
    return text


def _is_filled(status: Any) -> bool:
    return _norm_status(status) in _FILLED_STATUSES


def recompute_fills_from_status(
    paper_orders: dict[str, Any] | None,
    status_map: dict[str, Any] | None,
) -> dict[str, Any]:
    """paper_orders の submit 済み entry を、再取得した order_id -> status で fill 再計測。

    ``status_map`` は order_id -> (Alpaca status; enum/str/None)。マップに無い / None の
    order は「まだ未確定」として filled に数えない (submitted は据え置き)。

    戻り値::

        {
          "as_of": "<UTC ISO>",
          "n_orders": <再取得対象の submit 済み entry 数>,
          "systems": {"system1": {"long": {"entry_submitted": N, "filled": M}, "short": {...}}},
          "portfolio": {"entry_submitted", "entry_filled",
                        "long_entry_filled", "short_entry_filled"},
          "orders": [{"client_order_id","order_id","system","side","status","filled"}],
        }
    """
    status_map = status_map or {}
    systems: dict[str, dict[str, dict[str, int]]] = {}
    orders_out: list[dict[str, Any]] = []
    tot_sub = tot_fill = long_fill = short_fill = 0

    def _bucket(name: str | None, side: str) -> dict[str, int]:
        key = _norm_system(name) or "__unassigned__"
        sysobj = systems.setdefault(
            key,
            {
                "long": {"entry_submitted": 0, "filled": 0},
                "short": {"entry_submitted": 0, "filled": 0},
            },
        )
        return sysobj[side]

    for o in (paper_orders or {}).get("orders", []) or []:
        order_id = o.get("order_id")
        # submit 済み entry のみ対象 (skip/fail は fill 対象外)。
        if not order_id or o.get("skip_reason") or o.get("error"):
            continue
        side = _norm_side(o.get("side"))
        name = o.get("system")
        refreshed = status_map.get(order_id, None)
        filled = _is_filled(refreshed)
        sb = _bucket(name, side)
        sb["entry_submitted"] += 1
        tot_sub += 1
        if filled:
            sb["filled"] += 1
            tot_fill += 1
            if side == "short":
                short_fill += 1
            else:
                long_fill += 1
        orders_out.append(
            {
                "client_order_id": o.get("client_order_id"),
                "order_id": order_id,
                "system": _norm_system(name) or o.get("system"),
                "side": side,
                "status": _norm_status(refreshed) if refreshed is not None else None,
                "filled": bool(filled),
            }
        )

    return {
        "as_of": datetime.now(timezone.utc).isoformat(),
        "n_orders": tot_sub,
        "systems": systems,
        "portfolio": {
            "entry_submitted": tot_sub,
            "entry_filled": tot_fill,
            "long_entry_filled": long_fill,
            "short_entry_filled": short_fill,
        },
        "orders": orders_out,
    }


def patch_recon_fills(
    recon: dict[str, Any] | None,
    fills: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, int, str]:
    """recon_*.json の entry ``filled`` を再計測 fills で上書き (in-place)。

    - ``recon.portfolio.entry_filled`` を fills 総数に更新。
    - 各 ``recon.systems[systemN][side].filled`` を fills の per-system 値に更新。
    - submitted / skipped / exit / funnel は **触らない** (fill だけを直す)。
    - 監査のため ``recon.meta.fill_reconcile`` に as_of / 更新前後を残す。

    戻り値 ``(recon, n_updated, status)``:
      - ``status="ok"``          : filled を更新した
      - ``status="no_recon"``    : recon 無効
      - ``status="no_fills"``    : fills 無効 / 対象 order 0 → 何もしない
    idempotent: 同じ fills を再度当てても同じ結果。
    """
    if not isinstance(recon, dict):
        return recon, 0, "no_recon"
    if not isinstance(fills, dict) or not fills.get("n_orders"):
        return recon, 0, "no_fills"

    n_updated = 0
    fsys = fills.get("systems") or {}
    for name, sysobj in (recon.get("systems") or {}).items():
        norm = _norm_system(name)
        fs = fsys.get(norm) if norm else None
        if not isinstance(sysobj, dict) or not isinstance(fs, dict):
            continue
        for side in ("long", "short"):
            side_bucket = sysobj.get(side)
            f_side = fs.get(side)
            if isinstance(side_bucket, dict) and isinstance(f_side, dict):
                new_filled = int(f_side.get("filled") or 0)
                if side_bucket.get("filled") != new_filled:
                    side_bucket["filled"] = new_filled
                    n_updated += 1
                else:
                    side_bucket["filled"] = new_filled

    port = recon.get("portfolio")
    prev_port_fill = None
    if isinstance(port, dict):
        prev_port_fill = port.get("entry_filled")
        port["entry_filled"] = int(fills["portfolio"].get("entry_filled") or 0)
        port["long_entry_filled"] = int(
            fills["portfolio"].get("long_entry_filled") or 0
        )
        port["short_entry_filled"] = int(
            fills["portfolio"].get("short_entry_filled") or 0
        )

    meta = recon.setdefault("meta", {})
    if isinstance(meta, dict):
        meta["fill_reconcile"] = {
            "as_of": fills.get("as_of"),
            "n_orders": fills.get("n_orders"),
            "entry_filled": fills["portfolio"].get("entry_filled"),
            "entry_filled_prev": prev_port_fill,
        }
    return recon, n_updated, "ok"


# ---------------------------------------------------------------------------
# broker I/O (flag on 時のみ)。read-only: get_order_by_id のみ。発注しない。
# ---------------------------------------------------------------------------
def fetch_status_map(order_ids: list[str]) -> dict[str, Any]:
    """order_id -> status を Alpaca から read-only 取得 (paper)。"""
    from common.broker_alpaca import get_client, get_orders_status_map

    client = get_client()  # ALPACA_PAPER 既定 True
    return get_orders_status_map(client, order_ids)


def _entry_order_ids(paper_orders: dict[str, Any] | None) -> list[str]:
    ids: list[str] = []
    for o in (paper_orders or {}).get("orders", []) or []:
        oid = o.get("order_id")
        if oid and not o.get("skip_reason") and not o.get("error"):
            ids.append(str(oid))
    return ids


def _write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--date", help="対象日 (YYYY-MM-DD)。default path 解決に使う。")
    parser.add_argument("--results-dir", default="results_csv")
    parser.add_argument("--paper-orders-json", help="paper_orders JSON path (明示)。")
    parser.add_argument("--recon-json", help="recon JSON path (明示)。")
    parser.add_argument(
        "--status-map-json",
        help="order_id->status の JSON (broker を叩かずテスト/オフライン再計測する用)。",
    )
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=str(args.log_level).upper(), format="%(levelname)s: %(message)s"
    )

    # --- master flag: 既定 OFF なら完全に inert (byte-parity) --------------
    if not fill_reconcile_enabled():
        logger.info(
            "FILL_RECONCILE_ENABLED が未設定のため fill 再 reconcile は無効 "
            "(何も変更しません)。有効化するには FILL_RECONCILE_ENABLED=1。"
        )
        return 0

    results_dir = Path(args.results_dir)
    date_str = args.date or datetime.now().strftime("%Y-%m-%d")
    paper_path = (
        Path(args.paper_orders_json)
        if args.paper_orders_json
        else _default_path(results_dir, "paper_orders", date_str)
    )
    recon_path = (
        Path(args.recon_json)
        if args.recon_json
        else _default_path(results_dir, "recon", date_str)
    )
    paper_orders = _load_json(paper_path)
    if paper_orders is None:
        logger.error("paper_orders が読めません: %s", paper_path)
        return 1

    # status_map: 明示 JSON があればそれ (オフライン再計測)、無ければ broker から read-only。
    if args.status_map_json:
        status_map = _load_json(Path(args.status_map_json)) or {}
    else:
        order_ids = _entry_order_ids(paper_orders)
        if not order_ids:
            logger.info("submit 済み entry order_id が無いため再計測不要。")
            return 0
        try:
            status_map = fetch_status_map(order_ids)
        except Exception as exc:  # noqa: BLE001
            logger.error("Alpaca status 取得に失敗 (発注はしていません): %s", exc)
            return 1

    fills = recompute_fills_from_status(paper_orders, status_map)
    logger.info(
        "fill 再計測: submitted=%d filled=%d (long=%d short=%d) as_of=%s",
        fills["portfolio"]["entry_submitted"],
        fills["portfolio"]["entry_filled"],
        fills["portfolio"]["long_entry_filled"],
        fills["portfolio"]["short_entry_filled"],
        fills["as_of"],
    )

    # durable 台帳
    fills_path = _default_path(results_dir, "fills", date_str)
    _write_json(fills_path, fills)
    logger.info("fills 台帳を書き出し: %s", fills_path)

    # recon の entry filled を更新 (exit/funnel は不変)
    recon = _load_json(recon_path)
    if recon is not None:
        _, n_updated, status = patch_recon_fills(recon, fills)
        if status == "ok":
            _write_json(recon_path, recon)
            logger.info("recon fill 更新: %s (%d side 更新)", recon_path, n_updated)
        else:
            logger.info("recon fill 更新せず (%s)", status)
    else:
        logger.info("recon が無いため fills 台帳のみ更新: %s", recon_path)

    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
