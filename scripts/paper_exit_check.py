"""Alpaca Paper 口座の position を pull し、system 別 exit rule 照合 → exit 発注案を生成。

daily_pipeline.ps1 の [exit_check] step (5c) から呼ばれる:
    default:                dry-run (JSON 出力のみ、実発注なし)
    -AutoSubmitPaper 付き:  Paper 口座へ実発注 (成行 close / stop / trail / target)

安全設計:
    - ALPACA_PAPER=true 強制 (live 口座禁止、assert_paper_env)
    - --confirm が無いと dry-run と等価 (誤爆防止)
    - Alpaca API が使えない/SDK 未導入環境では position を空 list として扱い skip
    - protection 発注は client_order_id で idempotent (再実行で重複しない)

出力: results_csv/exit_orders_YYYYMMDD.json
    {
      "version": "1.0",
      "date": "2026-07-03",
      "mode": "dry_run" | "submitted",
      "count": <int>,
      "submitted": <int>,
      "failed": <int>,
      "positions": [ ... snapshot dicts ... ],
      "exits": [ ... PreparedExit rows ... ]
    }
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd  # noqa: E402

from common import broker_alpaca as ba  # noqa: E402
from common.alpaca_trading import (  # noqa: E402
    ExitReasonCode,
    LiveAccountGuardError,
    PositionsFetchError,
    PositionSnapshot,
    PreparedExit,
    assert_paper_env,
    build_exit_orders_from_positions,
    fetch_existing_exit_coids,
    fetch_existing_protect_coids,
    fetch_position_snapshots,
    parse_entry_date_from_client_order_id,
    parse_system_from_client_order_id,
    submit_paper_exit_order,
)
from common.position_tracker import load_tracker  # noqa: E402
from common.trade_management import SYSTEM_TRADE_RULES  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SPY_ROLLING = ROOT / "data_cache" / "rolling" / "SPY.csv"


# -------------------------------------------------------------------------
# entry_orders_index: paper_orders_YYYYMMDD.json 群から symbol -> system,
# entry_date を集めて hydrate に使う。tracker.json が空の case でも system
# tag が拾えるようにする secondary source。
# -------------------------------------------------------------------------


def _collect_entry_orders_index(
    results_dir: Path, lookback_days: int = 30
) -> dict[str, dict[str, Any]]:
    idx: dict[str, dict[str, Any]] = {}
    if not results_dir.exists():
        return idx
    # 新しい方から見る (最新 entry_date で上書き)
    files = sorted(results_dir.glob("paper_orders_*.json"), reverse=True)
    for f in files[:lookback_days]:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        for row in (data or {}).get("orders", []) or []:
            sym = str(row.get("symbol", "")).upper()
            if not sym:
                continue
            sys_tag = row.get("system") or parse_system_from_client_order_id(
                row.get("client_order_id")
            )
            ed = row.get("entry_date") or parse_entry_date_from_client_order_id(
                row.get("client_order_id")
            )
            if sym not in idx:
                idx[sym] = {"system": sys_tag, "entry_date": ed}
            else:
                # 既存より新しい entry_date が入ってきたら update しない (先に見た方=新しい)
                pass
    return idx


def _hydrate_from_alpaca_coids(snapshots: list[PositionSnapshot], client: Any) -> None:
    """Alpaca の直近 orders から client_order_id を pull し、system/entry_date を補う。"""
    if client is None:
        return
    # broker_alpaca の get_open_orders は open だけなので、全 order は client 直呼び。
    try:
        # QueryOrderStatus.ALL で最近の orders 取得
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        raw = client.get_orders(
            GetOrdersRequest(status=QueryOrderStatus.ALL, limit=500)
        )
    except Exception:
        return
    coid_by_symbol: dict[str, str] = {}
    for o in raw or []:
        try:
            sym = str(getattr(o, "symbol", "") or "").upper()
            coid = str(getattr(o, "client_order_id", "") or "")
            if not sym or not coid:
                continue
            # entry order 由来 (system... prefix) のみ拾う
            sys_tag = parse_system_from_client_order_id(coid)
            if sys_tag is None:
                continue
            # 最初にヒットしたものを採用 (新しい fill から順に返ってくる想定)
            coid_by_symbol.setdefault(sym, coid)
        except Exception:
            continue
    for snap in snapshots:
        if snap.system and snap.entry_date:
            continue
        coid = coid_by_symbol.get(snap.symbol)
        if not coid:
            continue
        if not snap.system:
            snap.system = parse_system_from_client_order_id(coid)
        if not snap.entry_date:
            snap.entry_date = parse_entry_date_from_client_order_id(coid)


# -------------------------------------------------------------------------
# SPY breakout data (system7 exit trigger)
# -------------------------------------------------------------------------


def _load_spy_context() -> tuple[float | None, float | None]:
    """SPY.csv 最新行の High と max_70 を返す。file 無い / column 欠損なら None。"""
    if not SPY_ROLLING.exists():
        return None, None
    try:
        df = pd.read_csv(SPY_ROLLING)
    except Exception:
        return None, None
    if df.empty:
        return None, None
    tail = df.iloc[-1]
    try:
        high = float(tail.get("High", 0) or 0)
    except (TypeError, ValueError):
        high = 0.0
    try:
        m70 = float(tail.get("max_70", 0) or 0)
    except (TypeError, ValueError):
        m70 = 0.0
    return (high if high > 0 else None), (m70 if m70 > 0 else None)


# -------------------------------------------------------------------------
# ATR lookup (protection stop price)
# -------------------------------------------------------------------------


def _load_atr_by_symbol(symbols: list[str]) -> dict[str, dict[int, float]]:
    """必要な symbol の rolling CSV から atr10/20/40/50 を latest 行で取得。"""
    out: dict[str, dict[int, float]] = {}
    rolling_dir = ROOT / "data_cache" / "rolling"
    if not rolling_dir.exists():
        return out
    for sym in symbols:
        f = rolling_dir / f"{sym}.csv"
        if not f.exists():
            continue
        try:
            df = pd.read_csv(f)
        except Exception:
            continue
        if df.empty:
            continue
        tail = df.iloc[-1]
        per: dict[int, float] = {}
        for period in (10, 14, 20, 40, 50):
            for col in (f"atr{period}", f"ATR{period}", f"atr_{period}"):
                if col in df.columns:
                    try:
                        val = float(tail.get(col, 0) or 0)
                        if val > 0:
                            per[period] = val
                            break
                    except (TypeError, ValueError):
                        continue
        if per:
            out[sym] = per
    return out


def _load_price_by_symbol(symbols: list[str]) -> dict[str, float]:
    """rolling CSV の latest Close を symbol 別に取得 (端株 synthetic の現値 fallback)。

    通常は snapshot.current_price (market_value/qty) が優先されるが、Alpaca が
    market_value を返さない場合の fallback に使う。取得不能な symbol は省く。
    """
    out: dict[str, float] = {}
    rolling_dir = ROOT / "data_cache" / "rolling"
    if not rolling_dir.exists():
        return out
    for sym in symbols:
        f = rolling_dir / f"{sym}.csv"
        if not f.exists():
            continue
        try:
            df = pd.read_csv(f)
        except Exception:
            continue
        if df.empty:
            continue
        tail = df.iloc[-1]
        for col in ("Close", "close", "adj_close", "Adj Close"):
            if col in df.columns:
                try:
                    val = float(tail.get(col, 0) or 0)
                    if val > 0:
                        out[sym] = val
                        break
                except (TypeError, ValueError):
                    continue
    return out


# -------------------------------------------------------------------------
# main
# -------------------------------------------------------------------------


def _today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _write_output(
    exits: list[PreparedExit],
    snapshots: list[PositionSnapshot],
    meta: dict,
    output_path: Path,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": "1.0",
        **meta,
        "positions": [
            {
                "symbol": s.symbol,
                "qty": s.qty,
                "side": s.side,
                "avg_entry_price": s.avg_entry_price,
                "market_value": s.market_value,
                "unrealized_pl": s.unrealized_pl,
                "system": s.system,
                "entry_date": s.entry_date,
            }
            for s in snapshots
        ],
        "exits": [e.to_row() for e in exits],
    }
    with output_path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2, default=str)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--date", default=None, help="対象日 YYYY-MM-DD (default: today)"
    )
    parser.add_argument(
        "--output-json",
        default=None,
        help="exit_orders_YYYYMMDD.json 出力先 (default: results_csv/exit_orders_<date>.json)",
    )
    parser.add_argument(
        "--results-dir",
        default=str(ROOT / "results_csv"),
        help="paper_orders_*.json を集める directory",
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="実発注する (無指定は dry-run と等価: 誤爆防止)。",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Reserved: interactive 確認をスキップ (現状 script は無人動作)。",
    )
    parser.add_argument(
        "--no-alpaca",
        action="store_true",
        help="Alpaca API を叩かず tracker のみで動く (test / offline 用)。",
    )
    parser.add_argument(
        "--no-cancel-before-close",
        action="store_true",
        help=(
            "time/breakout の成行 close 前に対象銘柄の resting protective 注文を "
            "cancel する挙動を無効化する (既定は有効)。"
        ),
    )
    parser.add_argument(
        "--cancel-settle-seconds",
        type=float,
        default=2.5,
        help="cancel 後、qty 解放を待つ秒数 (default 2.5)。",
    )
    args = parser.parse_args(argv)

    date_str = args.date or _today_str()
    date_compact = date_str.replace("-", "")

    results_dir = Path(args.results_dir)
    output_path = (
        Path(args.output_json)
        if args.output_json
        else results_dir / f"exit_orders_{date_compact}.json"
    )

    # --- 1) Alpaca client + positions -----------------------------------
    client: Any | None = None
    snapshots: list[PositionSnapshot] = []
    existing_protect_coids: set[str] = set()
    existing_exit_coids: set[str] = set()
    # broker が到達不能で positions を取れなかった場合、「0 exits = 成功」と誤認
    # させないための anomaly フラグ (--no-alpaca の意図的 offline とは区別する)。
    broker_unreachable = False

    if not args.no_alpaca:
        if args.confirm:
            try:
                assert_paper_env()
            except LiveAccountGuardError as exc:
                print(f"[SAFETY ABORT] {exc}")
                return 2
        try:
            client = ba.get_client(paper=True)
        except Exception as exc:
            print(f"[warn] Alpaca client 取得失敗 (offline mode で継続): {exc}")
            client = None
            broker_unreachable = True

    if client is not None:
        try:
            snapshots = fetch_position_snapshots(client, raise_on_error=True)
        except PositionsFetchError as exc:
            # client は取れたが positions fetch が失敗 (transient outage 等)。
            # silent [] に畳むと「flat book」と区別できず exit が全 skip されても
            # exit0 で成功に見える → anomaly として surface する。
            print(f"[warn] positions 取得失敗 (broker unreachable): {exc}")
            snapshots = []
            broker_unreachable = True
        else:
            existing_protect_coids = fetch_existing_protect_coids(client)
            existing_exit_coids = fetch_existing_exit_coids(client)
            _hydrate_from_alpaca_coids(snapshots, client)

    # --- 2) tracker / entry_orders_index --------------------------------
    tracker = load_tracker()
    entry_orders_index = _collect_entry_orders_index(results_dir)

    # --- 3) context (SPY, ATR, 現値) ------------------------------------
    spy_high, spy_max70 = _load_spy_context()
    symbols = [s.symbol for s in snapshots]
    atr_by_symbol = _load_atr_by_symbol(symbols) if symbols else {}
    # 端株 synthetic の現値 fallback (snapshot.current_price を優先)。
    price_by_symbol = _load_price_by_symbol(symbols) if symbols else {}

    # --- 4) build exit proposals ----------------------------------------
    exits = build_exit_orders_from_positions(
        snapshots,
        today=date_str,
        tracker=tracker,
        entry_orders_index=entry_orders_index,
        existing_protect_coids=existing_protect_coids,
        existing_exit_coids=existing_exit_coids,
        spy_high=spy_high,
        spy_max70=spy_max70,
        atr_by_symbol=atr_by_symbol,
        price_by_symbol=price_by_symbol,
    )

    dry_run = not args.confirm
    submitted_ok = 0
    submit_failed = 0

    if dry_run:
        for po in exits:
            po.dry_run = True
    else:
        # --- cancel-before-close: 有害な held_for_orders 失敗を防ぐ -----------
        # time/breakout の full-close (成行) を出す銘柄は、resting protective 注文
        # (stop/limit/trailing) が qty を握って code 40310000 を招く。close 対象銘柄
        # だけ先に protective を cancel して qty を解放する。保有継続銘柄は不変。
        if not getattr(args, "no_cancel_before_close", False):
            close_syms = {
                po.symbol.upper()
                for po in exits
                if po.reason in (ExitReasonCode.TIME, ExitReasonCode.BREAKOUT)
            }
            if close_syms:
                canc = ba.cancel_open_orders_for_symbols(client, close_syms)
                print(
                    f"[exit_check] cancel-before-close: canceled "
                    f"{canc['canceled']} resting order(s) on {len(canc['symbols'])} "
                    f"symbol(s) before market close"
                )
                if canc["canceled"]:
                    # cancel は非同期。qty が解放されるまで短く待つ (best-effort)。
                    time.sleep(float(getattr(args, "cancel_settle_seconds", 2.5)))

        # 実発注 pass
        for po in exits:
            try:
                result = submit_paper_exit_order(po, dry_run=False, client=client)
                if result.error:
                    submit_failed += 1
                elif result.order_id:
                    submitted_ok += 1
            except Exception as exc:
                po.error = str(exc)
                submit_failed += 1

    mode = "submitted" if not dry_run else "dry_run"

    _write_output(
        exits,
        snapshots,
        {
            "date": date_str,
            "mode": mode,
            "count": len(exits),
            "submitted": submitted_ok,
            "failed": submit_failed,
            "broker_unreachable": broker_unreachable,
            "spy_high": spy_high,
            "spy_max70": spy_max70,
            "systems": {
                sys: {
                    "max_holding_days": rule.max_holding_days,
                    "trailing_stop_pct": rule.trailing_stop_pct,
                    "profit_target_type": rule.profit_target_type,
                    "profit_target_value": rule.profit_target_value,
                }
                for sys, rule in SYSTEM_TRADE_RULES.items()
            },
        },
        output_path,
    )

    # summary
    time_cnt = sum(1 for e in exits if e.reason == "time_based")
    breakout_cnt = sum(1 for e in exits if e.reason == "spy_breakout")
    protect_cnt = sum(1 for e in exits if e.reason.startswith("protect_"))
    print(
        f"[exit_check] positions={len(snapshots)} exits={len(exits)} "
        f"(time={time_cnt}, breakout={breakout_cnt}, protect={protect_cnt}) "
        f"mode={mode} submitted={submitted_ok} failed={submit_failed}"
    )
    print(f"[write] {output_path}")
    # broker 到達不能で positions を確認できなかった場合、exit が 0 件でも「成功
    # (flat book)」と誤認させない。distinct code 3 で daily_pipeline に surface
    # する (entry 側の no_orders_generated=3 と同じ観測性方針)。市場が閉じた後の
    # 沈黙 exit 失敗 = position 滞留の温床なので必ず flag する。
    if broker_unreachable:
        print(
            "[WARN] broker (Alpaca) に到達できず現保有 positions を確認できませんでした。"
            "exit 判定は行われていません (0 exits は flat book ではなく取得失敗)。"
            "接続を確認し、必要なら再実行してください。"
        )
        return 3
    return 0 if submit_failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
