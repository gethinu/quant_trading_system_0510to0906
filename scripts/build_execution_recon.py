"""実行 reconciliation JSON を生成する (signals → plan → entry → exit → fill の突合)。

daily_pipeline の 3 つの成果物を 1 本の ``recon_YYYYMMDD.json`` に join する:

    - ``today_signals_YYYYMMDD.json``  (Step2: signals + funnel)
    - ``paper_orders_YYYYMMDD.json``   (Step5b: entry 発注結果)
    - ``exit_orders_YYYYMMDD.json``    (Step5c: exit 発注結果)

出力は system × side (long/short) 粒度で

    signals → 生成 → entry 送信 → fill → exit 送信

を並べ、drop 内訳 (min_notional / wash / unsizable / fail) を集計する。
Vercel dashboard の execution funnel と、submit 後の execution summary 通知
(scripts/publish_execution_summary.py) の両方が本 JSON を single source にする。

**read-only**: Alpaca へは一切アクセスしない。既存 JSON を読むだけ。
入力が欠けても (dry-run 等) 部分 recon を出す (inputs フラグで明示)。
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# fill とみなす Alpaca order status (成行は submit 直後 accepted のことが多く、
# fill は非同期。ここに載るのは既に約定確認できた分のみ = best-effort)。
_FILLED_STATUSES = {"filled", "partially_filled"}
# exit protection の reason_code (それ以外の exit は close 扱い)
_PROTECT_REASONS = {"protect_stop", "protect_trailing", "protect_target"}

_SYSTEMS = tuple(f"system{i}" for i in range(1, 8))


def _norm_system(raw: Any) -> str | None:
    """'sys1' / 'system1' / '1' → 'system1' に正規化。'system1' はそのまま。

    注意: 単純な ``replace('sys','system')`` は 'system1'→'systemtem1' に化けるため
    startswith 判定で分岐する。
    """
    try:
        text = str(raw or "").strip().lower()
    except Exception:
        return None
    if not text:
        return None
    if text.startswith("system"):
        return text
    if text.startswith("sys"):
        rest = text[3:]
        return f"system{rest}" if rest else None
    if text.isdigit():
        return f"system{text}"
    return None


def _norm_side(raw: Any) -> str:
    """BUY/long → 'long'、SELL/short → 'short'。不明は 'long' 扱い (集計欠落を避ける)。"""
    s = str(raw or "").strip().lower()
    if s in ("sell", "short", "sell_short"):
        return "short"
    return "long"


def _empty_side_bucket() -> dict[str, int]:
    return {
        "signals": 0,
        "generated": 0,
        "entry_submitted": 0,
        "filled": 0,
        "skipped": 0,
        "failed": 0,
    }


def _load_json(path: Path | None) -> dict[str, Any] | None:
    if path is None or not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("recon: %s の読込に失敗 (無視して継続): %s", path, exc)
        return None


def build_recon(
    signals: dict[str, Any] | None,
    paper_orders: dict[str, Any] | None,
    exit_orders: dict[str, Any] | None,
    *,
    date_str: str | None = None,
    account_equity: float | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """3 つの JSON payload を突合し recon dict を返す (pure、I/O なし)。"""
    systems: dict[str, dict[str, Any]] = {
        name: {
            "long": _empty_side_bucket(),
            "short": _empty_side_bucket(),
            "funnel": None,
            "exit": {"submitted": 0, "close": 0, "protect": 0},
        }
        for name in _SYSTEMS
    }

    def _sys(name: str | None) -> dict[str, Any] | None:
        if name is None:
            return None
        return systems.setdefault(
            name,
            {
                "long": _empty_side_bucket(),
                "short": _empty_side_bucket(),
                "funnel": None,
                "exit": {"submitted": 0, "close": 0, "protect": 0},
            },
        )

    universe_target: int | None = None
    total_signals = 0

    # --- signals (Step2) -------------------------------------------------
    if signals:
        portfolio = signals.get("portfolio", {}) or {}
        universe_target = portfolio.get("universe_target")
        for raw_name, cfg in (signals.get("systems", {}) or {}).items():
            name = _norm_system(raw_name)
            bucket = _sys(name)
            if bucket is None or not isinstance(cfg, dict):
                continue
            if cfg.get("funnel") is not None:
                bucket["funnel"] = cfg.get("funnel")
            for sig in cfg.get("signals", []) or []:
                side = _norm_side(sig.get("side"))
                bucket[side]["signals"] += 1
                total_signals += 1

    # --- paper_orders (Step5b) ------------------------------------------
    drop_breakdown: dict[str, int] = {}
    if paper_orders:
        for o in paper_orders.get("orders", []) or []:
            name = _norm_system(o.get("system"))
            bucket = _sys(name)
            if bucket is None:
                continue
            side = _norm_side(o.get("side"))
            sb = bucket[side]
            sb["generated"] += 1
            skip_reason = o.get("skip_reason")
            error = o.get("error")
            order_id = o.get("order_id")
            status = str(o.get("status") or "").lower()
            if skip_reason:
                sb["skipped"] += 1
                kind = str(skip_reason).split(":", 1)[0]
                # "skip" prefix は冗長なので次の segment を使う
                if kind == "skip":
                    parts = str(skip_reason).split(":")
                    kind = parts[1] if len(parts) > 1 else "skip"
                drop_breakdown[kind] = drop_breakdown.get(kind, 0) + 1
            elif error:
                sb["failed"] += 1
                drop_breakdown["fail"] = drop_breakdown.get("fail", 0) + 1
            elif order_id:
                sb["entry_submitted"] += 1
                if status in _FILLED_STATUSES:
                    sb["filled"] += 1

    # --- exit_orders (Step5c) -------------------------------------------
    if exit_orders:
        for e in exit_orders.get("exits", []) or []:
            name = _norm_system(e.get("system"))
            bucket = _sys(name)
            if bucket is None:
                continue
            ex = bucket["exit"]
            # 送信済 (order_id あり & error なし) のみ submitted カウント
            if e.get("order_id") and not e.get("error"):
                ex["submitted"] += 1
            reason = str(e.get("reason") or "").lower()
            if reason in _PROTECT_REASONS:
                ex["protect"] += 1
            else:
                ex["close"] += 1

    # --- portfolio aggregate --------------------------------------------
    def _agg(field: str) -> int:
        return sum(
            b[side][field] for b in systems.values() for side in ("long", "short")
        )

    long_signals = sum(b["long"]["signals"] for b in systems.values())
    short_signals = sum(b["short"]["signals"] for b in systems.values())
    exit_submitted = sum(b["exit"]["submitted"] for b in systems.values())
    exit_close = sum(b["exit"]["close"] for b in systems.values())
    exit_protect = sum(b["exit"]["protect"] for b in systems.values())

    portfolio_out = {
        "universe_target": universe_target,
        "signals": total_signals,
        "long_signals": long_signals,
        "short_signals": short_signals,
        "orders_generated": _agg("generated"),
        "entry_submitted": _agg("entry_submitted"),
        "entry_filled": _agg("filled"),
        "entry_skipped": _agg("skipped"),
        "entry_failed": _agg("failed"),
        "long_entry_submitted": sum(
            b["long"]["entry_submitted"] for b in systems.values()
        ),
        "short_entry_submitted": sum(
            b["short"]["entry_submitted"] for b in systems.values()
        ),
        "exit_submitted": exit_submitted,
        "exit_close": exit_close,
        "exit_protect": exit_protect,
        "drop_breakdown": drop_breakdown,
        "account_equity": account_equity,
    }

    # 空 (全 0) の system は出力から落として dashboard を簡潔に保つ
    systems_out = {
        name: data
        for name, data in systems.items()
        if (
            data["long"]["signals"]
            or data["short"]["signals"]
            or data["long"]["generated"]
            or data["short"]["generated"]
            or data["exit"]["submitted"]
            or data["funnel"] is not None
        )
    }

    return {
        "version": "1.0",
        "date": date_str or (signals or {}).get("date") or "",
        "generated_at": generated_at
        or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "inputs": {
            "signals": signals is not None,
            "paper_orders": paper_orders is not None,
            "exit_orders": exit_orders is not None,
        },
        "portfolio": portfolio_out,
        "systems": systems_out,
    }


def _default_path(results_dir: Path, stem: str, date_str: str) -> Path:
    return results_dir / f"{stem}_{date_str.replace('-', '')}.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--date", help="対象日 (YYYY-MM-DD)。default paths の解決に使う。"
    )
    parser.add_argument("--signals-json", help="today_signals JSON path。")
    parser.add_argument("--paper-orders-json", help="paper_orders JSON path。")
    parser.add_argument("--exit-orders-json", help="exit_orders JSON path。")
    parser.add_argument(
        "--output-json",
        help="recon 出力先 (default: results_csv/recon_YYYYMMDD.json)。",
    )
    parser.add_argument(
        "--results-dir", default="results_csv", help="default path 解決の基準 dir。"
    )
    parser.add_argument(
        "--account-equity", type=float, default=None, help="口座残高 (通知表示用)。"
    )
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=str(args.log_level).upper(), format="%(levelname)s: %(message)s"
    )

    results_dir = Path(args.results_dir)
    date_str = args.date or datetime.now().strftime("%Y-%m-%d")

    signals_path = (
        Path(args.signals_json)
        if args.signals_json
        else _default_path(results_dir, "today_signals", date_str)
    )
    paper_path = (
        Path(args.paper_orders_json)
        if args.paper_orders_json
        else _default_path(results_dir, "paper_orders", date_str)
    )
    exit_path = (
        Path(args.exit_orders_json)
        if args.exit_orders_json
        else _default_path(results_dir, "exit_orders", date_str)
    )

    signals = _load_json(signals_path)
    paper_orders = _load_json(paper_path)
    exit_orders = _load_json(exit_path)

    if signals is None and paper_orders is None and exit_orders is None:
        logger.error(
            "recon 入力が 1 つも見つかりません (signals=%s paper=%s exit=%s)。",
            signals_path,
            paper_path,
            exit_path,
        )
        return 1

    recon = build_recon(
        signals,
        paper_orders,
        exit_orders,
        date_str=(args.date or (signals or {}).get("date") or None),
        account_equity=args.account_equity,
    )

    out_path = (
        Path(args.output_json)
        if args.output_json
        else _default_path(results_dir, "recon", date_str)
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    tmp.write_text(json.dumps(recon, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(out_path)

    p = recon["portfolio"]
    logger.info(
        "recon 書き出し: %s (Tgt=%s sig=%s gen=%s entry=%s exit=%s fill=%s)",
        out_path,
        p.get("universe_target"),
        p.get("signals"),
        p.get("orders_generated"),
        p.get("entry_submitted"),
        p.get("exit_submitted"),
        p.get("entry_filled"),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
