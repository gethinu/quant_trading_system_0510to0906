"""exit E2E 検証 — 「本日 exit 予定」vs「実 fill」を突合し durable ログ + 異常時 ntfy。

exit 側 (SYSTEM_TRADE_RULES の max_holding_days S2=2/S3=3/S5=6/S6=3・利確・ATR ストップ)
が **実際に paper で発火して決済したか** を観測・検証する。paper_exit_check が生成した
``exit_orders_YYYYMMDD.json`` (planned exits + position snapshot) を single source にし、

    1. [expected] position snapshot から time-based 満期 (holding_days >= max_holding_days)
                  を **独立再計算** = 「本日 exit すべき」建玉 (paper_exit_check の漏れ検知)。
    2. [planned]  exit_orders.json の exits[] = paper_exit_check が実際に planned/submit した exit。
    3. [fill]     各 planned close の Alpaca order status を照合 (--no-alpaca なら JSON 記録値)。
    4. [reconcile] close 未 fill / 満期なのに未計画 / reject を discrepancy として抽出。

**read-only**: Alpaca へは GET のみ (order status / positions)。発注・cancel は一切しない。
月曜以降の建玉で time-exit が実発火するのを、この durable ログ (logs/exit_verify_<date>.json)
で日次に追える。異常 (close 未 fill / 満期漏れ) があれば ntfy WARN。

Usage:
    python scripts/exit_verify.py --date 2026-07-13            # 当日を検証 (Alpaca 照合あり)
    python scripts/exit_verify.py --date 2026-07-13 --no-alpaca # JSON 記録値だけで検証
    python scripts/exit_verify.py --date 2026-07-13 --dry-run   # ntfy を送らず本文表示

Exit codes: 0=乖離なし, 2=discrepancy あり (WARN), 1=入力 (exit_orders JSON) が無い。
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.alpaca_trading import compute_holding_days  # noqa: E402
from common.trade_management import SYSTEM_TRADE_RULES  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]

# fill とみなす Alpaca order status
_FILLED = {"filled", "partially_filled"}
# まだ約定待ち (成行は市場休場中 accepted のまま。失敗ではない)
_PENDING = {
    "new",
    "accepted",
    "pending_new",
    "held",
    "accepted_for_bidding",
    "pending_replace",
    "calculated",
    "partially_filled",
}
_DEAD = {"canceled", "cancelled", "rejected", "expired", "done_for_day", "replaced"}
# position を実際に閉じる exit reason (resting protection は fill 待ちが正常なので除外)
_CLOSE_REASONS = {"time_based", "spy_breakout", "flatten_all"}


def _today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _norm_status(raw: Any) -> str:
    return str(raw or "").lower().split(".")[-1].strip()


def _is_close(row: dict[str, Any]) -> bool:
    reason = str(row.get("reason") or "").lower()
    if reason in _CLOSE_REASONS:
        return True
    # reason が protect_* でない market は close 扱い
    return str(row.get("order_type")) == "market" and not reason.startswith("protect")


def _expected_time_exits(
    positions: list[dict[str, Any]], today: str
) -> list[dict[str, Any]]:
    """position snapshot から time-based 満期の建玉を独立再計算 (paper_exit_check の漏れ検知)。"""
    due: list[dict[str, Any]] = []
    for p in positions or []:
        system = str(p.get("system") or "").lower()
        rule = SYSTEM_TRADE_RULES.get(system)
        max_hold = int(getattr(rule, "max_holding_days", 0) or 0) if rule else 0
        if max_hold <= 0:
            continue  # S1/S4/S7 は time-exit 無し
        hd = compute_holding_days(p.get("entry_date"), today)
        if hd is None:
            continue
        if hd >= max_hold:
            due.append(
                {
                    "symbol": str(p.get("symbol") or "").upper(),
                    "system": system,
                    "holding_days": hd,
                    "max_holding_days": max_hold,
                    "entry_date": p.get("entry_date"),
                }
            )
    return due


def _live_status_map(order_ids: list[str]) -> dict[str, str]:
    """Alpaca から order_id -> status を GET (read-only)。失敗時は空 map。"""
    ids = [o for o in order_ids if o]
    if not ids:
        return {}
    try:
        from common import broker_alpaca as ba
        from common.broker_alpaca import get_orders_status_map

        client = ba.get_client(paper=True)
        raw = get_orders_status_map(client, ids)
        return {k: _norm_status(v) for k, v in raw.items()}
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] Alpaca order status 取得失敗 (JSON 記録値で継続): {exc}")
        return {}


def verify(
    exit_orders: dict[str, Any], today: str, status_map: dict[str, str]
) -> dict[str, Any]:
    """planned exits と実 fill を突合し検証 dict を返す (pure、I/O は status_map に外出し)。"""
    exits = exit_orders.get("exits") or []
    positions = exit_orders.get("positions") or []

    expected = _expected_time_exits(positions, today)

    planned_rows: list[dict[str, Any]] = []
    planned_close_symbols: set[str] = set()
    closes_not_filled: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []

    for e in exits:
        sym = str(e.get("symbol") or "").upper()
        oid = e.get("order_id")
        is_close = _is_close(e)
        # status: live 優先、無ければ JSON 記録値
        st = status_map.get(str(oid)) if oid else None
        if not st:
            st = _norm_status(e.get("status"))
        submitted = bool(oid) and not e.get("dry_run", True) and not e.get("error")
        filled = st in _FILLED
        row = {
            "symbol": sym,
            "system": e.get("system"),
            "reason": e.get("reason"),
            "order_type": e.get("order_type"),
            "is_close": is_close,
            "order_id": oid,
            "submitted": submitted,
            "status": st or None,
            "filled": filled,
            "dry_run": bool(e.get("dry_run", True)),
            "error": e.get("error"),
        }
        planned_rows.append(row)
        if is_close:
            planned_close_symbols.add(sym)
            if submitted and not filled:
                if st in _DEAD:
                    rejected.append(row)
                elif st not in _PENDING:
                    closes_not_filled.append(row)
                else:
                    closes_not_filled.append(
                        row
                    )  # pending も「まだ閉じてない」として記録

    # 満期なのに planned に居ない建玉 = paper_exit_check の漏れ
    due_not_planned = [e for e in expected if e["symbol"] not in planned_close_symbols]

    # discrepancy = WARN 対象 (pending は info、rejected/漏れ が本命)
    hard_closes_unfilled = [
        r for r in closes_not_filled if (r["status"] or "") not in _PENDING
    ]
    discrepancies = {
        "due_not_planned": due_not_planned,
        "closes_rejected": rejected,
        "closes_unfilled_nonpending": hard_closes_unfilled,
        "closes_pending": [
            r for r in closes_not_filled if (r["status"] or "") in _PENDING
        ],
    }
    n_warn = len(due_not_planned) + len(rejected) + len(hard_closes_unfilled)

    filled_closes = [r for r in planned_rows if r["is_close"] and r["filled"]]
    return {
        "date": today,
        "mode": exit_orders.get("mode"),
        "n_positions": len(positions),
        "n_planned_exits": len(exits),
        "n_planned_closes": len(planned_close_symbols),
        "n_filled_closes": len(filled_closes),
        "n_expected_time_exits": len(expected),
        "expected_time_exits": expected,
        "planned": planned_rows,
        "discrepancies": discrepancies,
        "n_warn": n_warn,
    }


def _summary_lines(v: dict[str, Any]) -> list[str]:
    lines = [
        f"positions={v['n_positions']} planned_closes={v['n_planned_closes']} "
        f"filled_closes={v['n_filled_closes']} expected_time_exits={v['n_expected_time_exits']}",
    ]
    d = v["discrepancies"]
    if d["due_not_planned"]:
        syms = ", ".join(
            f"{e['symbol']}({e['system']} {e['holding_days']}/{e['max_holding_days']}d)"
            for e in d["due_not_planned"]
        )
        lines.append(f"[WARN] 満期だが未計画: {syms}")
    if d["closes_rejected"]:
        lines.append(
            f"[WARN] close reject: {', '.join(r['symbol'] for r in d['closes_rejected'])}"
        )
    if d["closes_unfilled_nonpending"]:
        lines.append(
            f"[WARN] close 未 fill: {', '.join(r['symbol']+'('+str(r['status'])+')' for r in d['closes_unfilled_nonpending'])}"
        )
    if d["closes_pending"]:
        lines.append(
            f"[..] close pending(市場休場等): {', '.join(r['symbol'] for r in d['closes_pending'])}"
        )
    if v["n_warn"] == 0:
        lines.append("[OK] 乖離なし (planned close は fill 済 / 満期漏れなし)")
    return lines


def _notify(date_str: str, v: dict[str, Any], dry_run: bool) -> None:
    n_warn = v["n_warn"]
    head = (
        f"ExitVerify {date_str}: WARN ({n_warn})"
        if n_warn
        else f"ExitVerify {date_str}: OK"
    )
    body = head + "\n" + "\n".join(_summary_lines(v))
    if dry_run:
        print("--- ntfy (dry-run) ---")
        print(body)
        return
    try:
        from common.publishers.ntfy import NtfyPublisher

        pub = NtfyPublisher()
        if not pub.is_configured():
            print("[ntfy] NTFY_TOPIC 未設定のため送信スキップ")
            return
        tags = "warning" if n_warn else "heavy_check_mark"
        res = pub.send_text(head, body, tags=tags, priority=(5 if n_warn else None))
        print(f"[ntfy] 送信 ok={getattr(res, 'ok', '?')}")
    except Exception as exc:  # noqa: BLE001
        print(f"[ntfy] 送信失敗: {exc}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--date", default=None, help="対象日 YYYY-MM-DD (default: today UTC)"
    )
    parser.add_argument(
        "--exit-orders-json",
        default=None,
        help="exit_orders JSON path (default: results_csv/exit_orders_<date>.json)",
    )
    parser.add_argument("--results-dir", default=str(ROOT / "results_csv"))
    parser.add_argument("--log-dir", default=str(ROOT / "logs"))
    parser.add_argument("--output-json", default=None, help="検証結果 JSON の出力先")
    parser.add_argument(
        "--no-alpaca", action="store_true", help="Alpaca を叩かず JSON 記録値だけで検証"
    )
    parser.add_argument("--dry-run", action="store_true", help="ntfy を送らず本文表示")
    parser.add_argument(
        "--no-notify", action="store_true", help="ntfy 送信を完全に無効化"
    )
    args = parser.parse_args(argv)

    date_str = args.date or _today_str()
    compact = date_str.replace("-", "")
    results_dir = Path(args.results_dir)
    exit_path = (
        Path(args.exit_orders_json)
        if args.exit_orders_json
        else results_dir / f"exit_orders_{compact}.json"
    )
    if not exit_path.exists():
        print(f"[error] exit_orders JSON が無い: {exit_path}")
        return 1
    try:
        exit_orders = json.loads(exit_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        print(f"[error] exit_orders JSON を読めない: {exc}")
        return 1

    order_ids = [
        str(e.get("order_id"))
        for e in (exit_orders.get("exits") or [])
        if e.get("order_id") and not e.get("dry_run", True)
    ]
    status_map = {} if args.no_alpaca else _live_status_map(order_ids)

    v = verify(exit_orders, date_str, status_map)
    for ln in _summary_lines(v):
        print(ln)

    record = {
        "version": "1.0",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "exit_orders_source": str(exit_path),
        "alpaca_checked": not args.no_alpaca,
        **v,
    }
    out_path = (
        Path(args.output_json)
        if args.output_json
        else Path(args.log_dir) / f"exit_verify_{compact}.json"
    )
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(record, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        print(f"[write] {out_path}")
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] JSON 書き出し失敗 (無視): {exc}")

    if not args.no_notify:
        _notify(date_str, v, dry_run=args.dry_run)

    return 2 if v["n_warn"] > 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
