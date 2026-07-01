"""当日シグナルの standardize JSON 出力ユーティリティ (Phase 1 事業化基盤)。

`scripts.run_all_systems_today.compute_today_signals` が返す
``(final_df, per_system)`` を、Phase 2/3 で subscribers に配信できる
安定した JSON schema (version 1.0) に変換する。

Streamlit UI (`apps/app_today_signals.py`) の ``--headless`` mode と、
将来の配信 (`scripts/publish_signals.py`) の双方から re-use される。

JSON schema (version 1.0):
    {
      "version": "1.0",
      "date": "2026-07-01",
      "generated_at": "2026-07-01T06:15:23+09:00",
      "provider": "polygon",
      "systems": {
        "sys1": {
          "signals": [{"symbol","side","entry_price","weight","rank","reason"}],
          "n_candidates_input": 15,
          "n_signals_output": 5,
          "gate_survival_ratio": 0.33
        }, ...
      },
      "portfolio": {"total_signals","total_notional_usd","hedge"},
      "meta": {"cli_version","run_id","elapsed_seconds"}
    }

`run_id` は同一シグナルの重複配信/発注を検出するための鍵。
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import time
from typing import Any
import uuid

logger = logging.getLogger(__name__)

CLI_VERSION = "0.1.0"
SCHEMA_VERSION = "1.0"

# sys7 = SPY 空売りヘッジ (short-only)
HEDGE_SYSTEM = "sys7"

_SIDE_MAP = {
    "long": "BUY",
    "buy": "BUY",
    "b": "BUY",
    "short": "SELL",
    "sell": "SELL",
    "s": "SELL",
}


def _try_tokyo_now() -> datetime:
    """JST の現在時刻。zoneinfo が無い環境では UTC+9 手組みで fallback。"""
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo("Asia/Tokyo"))
    except Exception:  # pragma: no cover - Windows で tzdata 欠損時
        from datetime import timedelta

        return datetime.now(timezone.utc).astimezone(
            timezone(timedelta(hours=9))
        )


def generate_run_id(now: datetime | None = None) -> str:
    """``YYYYMMDD_HHMMSS_<hex6>`` 形式の run_id を生成する。"""
    stamp = (now or _try_tokyo_now()).strftime("%Y%m%d_%H%M%S")
    return f"{stamp}_{uuid.uuid4().hex[:6]}"


def normalize_system_key(name: Any) -> str | None:
    """"System1" / "system1" / "sys1" / "SPY(sys7)" などを ``sysN`` に正規化。"""
    if name is None:
        return None
    s = str(name).lower()
    for ch in s:
        if ch.isdigit():
            # 最初に現れる数字の連なりを system 番号とみなす
            idx = s.index(ch)
            num = ""
            for c in s[idx:]:
                if c.isdigit():
                    num += c
                else:
                    break
            if num:
                return f"sys{int(num)}"
    return None


def _first(row: Any, *keys: str) -> Any:
    """Mapping-like row から最初に見つかった非 None/非 NaN 値を返す。"""
    for k in keys:
        try:
            v = row.get(k) if hasattr(row, "get") else row[k]
        except Exception:
            continue
        if v is None:
            continue
        try:
            # pandas NaN 判定
            import math

            if isinstance(v, float) and math.isnan(v):
                continue
        except Exception:
            pass
        return v
    return None


def _to_float(v: Any) -> float | None:
    try:
        if v is None:
            return None
        f = float(v)
        import math

        if math.isnan(f):
            return None
        return f
    except Exception:
        return None


def _map_side(v: Any) -> str:
    if v is None:
        return "BUY"
    return _SIDE_MAP.get(str(v).strip().lower(), str(v).upper())


def _notional(row: Any, entry_price: float | None) -> float:
    """1 signal の想定約定金額 (USD)。position_value 優先、無ければ shares*entry。"""
    pv = _to_float(_first(row, "position_value", "position_value_usd", "notional"))
    if pv is not None and pv > 0:
        return pv
    shares = _to_float(_first(row, "shares", "qty", "quantity"))
    if shares is not None and entry_price is not None:
        return abs(shares) * entry_price
    return 0.0


def _row_signal(row: Any, system_key: str) -> dict[str, Any]:
    symbol = _first(row, "symbol", "ticker", "Symbol")
    entry_price = _to_float(
        _first(row, "entry_price", "entry_price_final", "Close", "close", "price")
    )
    side = _map_side(_first(row, "side", "position_side"))
    rank = _first(row, "rank", "no")
    try:
        rank = int(rank) if rank is not None else None
    except Exception:
        rank = None
    reason = _first(row, "reason", "setup", "entry_type", "signal_type")
    if reason is not None:
        reason = str(reason)
    score = _to_float(_first(row, "score"))
    return {
        "symbol": str(symbol) if symbol is not None else None,
        "side": side,
        "entry_price": entry_price,
        "rank": rank,
        "reason": reason,
        "score": score,
        "_notional": _notional(row, entry_price),
        "_system": system_key,
    }


def _iter_rows(df: Any) -> list[dict[str, Any]]:
    """DataFrame を行 dict の list に (空/None 安全)。"""
    if df is None:
        return []
    try:
        if getattr(df, "empty", True):
            return []
        return [dict(rec) for rec in df.to_dict(orient="records")]
    except Exception:
        return []


def build_signals_json(
    final_df: Any,
    per_system: dict[str, Any] | None,
    *,
    date_str: str | None = None,
    provider: str = "polygon",
    run_id: str | None = None,
    elapsed_seconds: float | None = None,
    cli_version: str = CLI_VERSION,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    """``(final_df, per_system)`` を version 1.0 の JSON dict に変換する。"""
    now = generated_at or _try_tokyo_now()
    run_id = run_id or generate_run_id(now)
    per_system = per_system or {}

    # --- 最終シグナル (final_df) を system 別に振り分け -------------------
    final_rows = _iter_rows(final_df)
    signals_by_sys: dict[str, list[dict[str, Any]]] = {}
    for row in final_rows:
        sk = normalize_system_key(_first(row, "system", "System", "strategy"))
        if sk is None:
            continue
        signals_by_sys.setdefault(sk, []).append(_row_signal(row, sk))

    # --- 候補数 (per_system) を集計 -------------------------------------
    candidate_counts: dict[str, int] = {}
    all_sys_keys: set[str] = set(signals_by_sys.keys())
    for raw_name, df in per_system.items():
        sk = normalize_system_key(raw_name)
        if sk is None:
            continue
        candidate_counts[sk] = len(_iter_rows(df))
        all_sys_keys.add(sk)

    # --- portfolio 集計 (weight 計算のため total notional を先に) --------
    total_notional = 0.0
    for sigs in signals_by_sys.values():
        for s in sigs:
            total_notional += float(s.get("_notional") or 0.0)

    def _weight(s: dict[str, Any]) -> float | None:
        if total_notional <= 0:
            return None
        return round(float(s.get("_notional") or 0.0) / total_notional, 4)

    systems_out: dict[str, Any] = {}
    for sk in sorted(all_sys_keys, key=lambda x: int(x[3:]) if x[3:].isdigit() else 99):
        sigs = signals_by_sys.get(sk, [])
        # rank 未設定なら score 降順で採番、それも無ければ入力順
        ranked = sorted(
            sigs,
            key=lambda s: (
                s["rank"] if s.get("rank") is not None else 10_000,
                -(s.get("score") or 0.0),
            ),
        )
        clean_sigs = []
        for i, s in enumerate(ranked, start=1):
            clean_sigs.append(
                {
                    "symbol": s["symbol"],
                    "side": s["side"],
                    "entry_price": s["entry_price"],
                    "weight": _weight(s),
                    "rank": s["rank"] if s.get("rank") is not None else i,
                    "reason": s["reason"],
                }
            )
        n_in = candidate_counts.get(sk, len(sigs))
        n_out = len(clean_sigs)
        ratio = round(n_out / n_in, 4) if n_in else 0.0
        systems_out[sk] = {
            "signals": clean_sigs,
            "n_candidates_input": n_in,
            "n_signals_output": n_out,
            "gate_survival_ratio": ratio,
        }

    # --- hedge (sys7 = SPY short) ---------------------------------------
    hedge: dict[str, Any] | None = None
    hedge_sigs = systems_out.get(HEDGE_SYSTEM, {}).get("signals", [])
    if hedge_sigs:
        h = hedge_sigs[0]
        hedge = {
            "symbol": h.get("symbol"),
            "side": h.get("side"),
            "entry_price": h.get("entry_price"),
        }

    payload = {
        "version": SCHEMA_VERSION,
        "date": date_str or now.strftime("%Y-%m-%d"),
        "generated_at": now.isoformat(timespec="seconds"),
        "provider": provider,
        "systems": systems_out,
        "portfolio": {
            "total_signals": sum(v["n_signals_output"] for v in systems_out.values()),
            "total_notional_usd": round(total_notional, 2),
            "hedge": hedge,
        },
        "meta": {
            "cli_version": cli_version,
            "run_id": run_id,
            "elapsed_seconds": round(float(elapsed_seconds), 1)
            if elapsed_seconds is not None
            else None,
        },
    }
    return payload


def default_output_path(date_str: str) -> Path:
    return Path("results_csv") / f"today_signals_{date_str.replace('-', '')}.json"


def write_signals_json(payload: dict[str, Any], output_path: Path) -> Path:
    """JSON を atomic write (tmp -> replace) で書き出す。"""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = output_path.with_suffix(output_path.suffix + f".{payload['meta']['run_id']}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(output_path)
    return output_path


# --------------------------------------------------------------------------
# Headless CLI  (apps/app_today_signals.py --headless から dispatch される)
# --------------------------------------------------------------------------

def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="app_today_signals.py --headless",
        description="当日シグナルを生成し standardize JSON を出力する (Streamlit UI 非起動)。",
    )
    p.add_argument("--headless", action="store_true", help="Streamlit UI を起動せず core logic のみ実行。")
    p.add_argument(
        "--output-json",
        type=str,
        default=None,
        help="出力先 JSON path (default: results_csv/today_signals_YYYYMMDD.json)。",
    )
    p.add_argument(
        "--symbols",
        type=str,
        default=None,
        help="対象シンボルを comma 区切りで指定 (省略時は full universe)。例: AAPL,SPY",
    )
    p.add_argument("--date", type=str, default=None, help="シグナル対象日 (YYYY-MM-DD)。省略時は今日(JST)。")
    p.add_argument("--capital-long", type=float, default=None, help="ロング側資金 (USD)。")
    p.add_argument("--capital-short", type=float, default=None, help="ショート側資金 (USD)。")
    p.add_argument("--parallel", action="store_true", help="システム別抽出を並列実行する。")
    p.add_argument(
        "--skip-latest-check",
        action="store_true",
        help="rolling cache の最新営業日チェックを skip (cache 未更新環境での adhoc 実行用)。",
    )
    p.add_argument("--log-level", default="INFO", help="ログレベル (default: INFO)。")
    return p


def run_headless(argv: list[str]) -> int:
    """``--headless`` エントリ本体。0=成功, 1=エラー。"""
    args = build_arg_parser().parse_args(argv)
    logging.basicConfig(
        level=str(args.log_level).upper(),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    now = _try_tokyo_now()
    date_str = args.date or now.strftime("%Y-%m-%d")
    run_id = generate_run_id(now)

    symbols: list[str] | None = None
    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]

    logger.info(
        "headless run: date=%s symbols=%s run_id=%s",
        date_str,
        symbols if symbols else "<full-universe>",
        run_id,
    )

    t0 = time.time()
    try:
        from scripts.run_all_systems_today import compute_today_signals

        final_df, per_system = compute_today_signals(
            symbols,
            capital_long=args.capital_long,
            capital_short=args.capital_short,
            save_csv=False,
            notify=False,
            parallel=bool(args.parallel),
            skip_latest_check=bool(args.skip_latest_check),
        )
    except SystemExit as exc:
        # compute 側は「全銘柄 stale で除外」時に SystemExit(1) を raise する。
        # headless では空 payload を書き出し、後段 (配信) が空を検知できるようにする。
        logger.warning(
            "compute aborted (SystemExit=%s): rolling cache が未更新の可能性。空 payload を出力します。",
            getattr(exc, "code", exc),
        )
        final_df, per_system = None, {}
    except Exception as exc:  # noqa: BLE001
        logger.exception("compute_today_signals failed: %s", exc)
        return 1
    elapsed = time.time() - t0

    payload = build_signals_json(
        final_df,
        per_system,
        date_str=date_str,
        run_id=run_id,
        elapsed_seconds=elapsed,
    )

    out_path = Path(args.output_json) if args.output_json else default_output_path(date_str)
    write_signals_json(payload, out_path)

    logger.info(
        "wrote %s (total_signals=%d, notional=$%.0f, elapsed=%.1fs)",
        out_path,
        payload["portfolio"]["total_signals"],
        payload["portfolio"]["total_notional_usd"],
        elapsed,
    )
    print(str(out_path))
    return 0
