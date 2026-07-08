"""Alpaca Paper 口座の現状 (position + system rule + exit 距離) を可視化する。

実発注は一切行わない。exit_check の pre-flight / dashboard 用の read-only ツール。

出力: results_csv/paper_status_YYYYMMDD.json
    {
      "version": "1.0",
      "date": "2026-07-03",
      "positions": [
        {
          "symbol": "AAPL", "system": "system1", "side": "long",
          "qty": 10, "avg_entry_price": 195.5,
          "current_price": 200.1,
          "unrealized_pl_pct": 2.35,
          "holding_days": 3, "max_holding_days": 0,
          "stop_price_est": 180.4, "distance_to_stop_pct": -9.83,
          "target_price_est": null,
          "trailing_stop_pct": 0.25,
          "exit_expected": null   # or "time_based" / "spy_breakout" / null
        },
        ...
      ]
    }
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd  # noqa: E402

from common import broker_alpaca as ba  # noqa: E402
from common.alpaca_trading import (  # noqa: E402
    LiveAccountGuardError,
    PositionSnapshot,
    assert_paper_env,
    compute_holding_days,
    fetch_position_snapshots,
    hydrate_system_tags,
    parse_entry_date_from_client_order_id,
    parse_system_from_client_order_id,
)
from common.position_tracker import load_tracker  # noqa: E402
from common.trade_management import SYSTEM_TRADE_RULES  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SPY_ROLLING = ROOT / "data_cache" / "rolling" / "SPY.csv"


def _collect_entry_orders_index(
    results_dir: Path, lookback_days: int = 30
) -> dict[str, dict[str, Any]]:
    idx: dict[str, dict[str, Any]] = {}
    if not results_dir.exists():
        return idx
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
            idx.setdefault(sym, {"system": sys_tag, "entry_date": ed})
    return idx


def _load_atr_by_symbol(symbols: list[str]) -> dict[str, dict[int, float]]:
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


def _load_current_price(symbol: str) -> float | None:
    f = ROOT / "data_cache" / "rolling" / f"{symbol}.csv"
    if not f.exists():
        return None
    try:
        df = pd.read_csv(f)
    except Exception:
        return None
    if df.empty:
        return None
    try:
        return float(df.iloc[-1].get("Close", 0) or 0) or None
    except (TypeError, ValueError):
        return None


def _safe_pos_float(val: Any) -> float | None:
    """0/NaN/None は None、正値は float を返す。"""
    try:
        f = float(val)
    except (TypeError, ValueError):
        return None
    if not (f > 0):  # NaN も 0 も負値もここで弾かれる
        return None
    return f


def _load_spy_context() -> tuple[float | None, float | None]:
    if not SPY_ROLLING.exists():
        return None, None
    try:
        df = pd.read_csv(SPY_ROLLING)
    except Exception:
        return None, None
    if df.empty:
        return None, None
    tail = df.iloc[-1]
    return _safe_pos_float(tail.get("High")), _safe_pos_float(tail.get("max_70"))


def _build_status_row(
    snap: PositionSnapshot,
    *,
    today: str,
    atr_by_symbol: dict[str, dict[int, float]],
    spy_high: float | None,
    spy_max70: float | None,
) -> dict[str, Any]:
    rules = SYSTEM_TRADE_RULES.get(snap.system) if snap.system else None
    current_price = _load_current_price(snap.symbol)
    holding_days = compute_holding_days(snap.entry_date, today)

    row: dict[str, Any] = {
        "symbol": snap.symbol,
        "system": snap.system,
        "side": snap.side,
        "qty": snap.qty,
        "avg_entry_price": snap.avg_entry_price,
        "current_price": current_price,
        "market_value": snap.market_value,
        "unrealized_pl": snap.unrealized_pl,
        "unrealized_pl_pct": None,
        "entry_date": snap.entry_date,
        "holding_days": holding_days,
        "max_holding_days": int(getattr(rules, "max_holding_days", 0)) if rules else 0,
        "trailing_stop_pct": (
            float(rules.trailing_stop_pct)
            if rules and rules.use_trailing_stop
            else None
        ),
        "profit_target_type": (
            getattr(rules, "profit_target_type", None) if rules else None
        ),
        "stop_price_est": None,
        "target_price_est": None,
        "distance_to_stop_pct": None,
        "distance_to_target_pct": None,
        "exit_expected": None,
    }

    if current_price and snap.avg_entry_price > 0:
        if snap.side == "long":
            row["unrealized_pl_pct"] = round(
                (current_price - snap.avg_entry_price) / snap.avg_entry_price * 100.0, 3
            )
        else:
            row["unrealized_pl_pct"] = round(
                (snap.avg_entry_price - current_price) / snap.avg_entry_price * 100.0, 3
            )

    if rules is None:
        return row

    # stop / target 見積 (ATR + rules から)
    atr_lookup = atr_by_symbol.get(snap.symbol, {})
    atr_stop = atr_lookup.get(int(rules.stop_atr_period))
    if atr_stop:
        stop_dist = atr_stop * rules.stop_atr_multiplier
        if snap.side == "long":
            row["stop_price_est"] = round(
                max(0.01, snap.avg_entry_price - stop_dist), 4
            )
        else:
            row["stop_price_est"] = round(snap.avg_entry_price + stop_dist, 4)
        if current_price and row["stop_price_est"]:
            row["distance_to_stop_pct"] = round(
                (row["stop_price_est"] - current_price) / current_price * 100.0, 3
            )

    if rules.profit_target_type == "percentage" and rules.profit_target_value > 0:
        mult = 1.0 + (rules.profit_target_value / 100.0)
        if snap.side == "long":
            row["target_price_est"] = round(snap.avg_entry_price * mult, 4)
        else:
            row["target_price_est"] = round(snap.avg_entry_price / mult, 4)
    elif rules.profit_target_type == "atr" and rules.profit_target_value > 0:
        atr_t = atr_lookup.get(int(rules.profit_target_atr_period))
        if atr_t:
            dist = atr_t * rules.profit_target_value
            if snap.side == "long":
                row["target_price_est"] = round(snap.avg_entry_price + dist, 4)
            else:
                row["target_price_est"] = round(snap.avg_entry_price - dist, 4)

    if current_price and row["target_price_est"]:
        row["distance_to_target_pct"] = round(
            (row["target_price_est"] - current_price) / current_price * 100.0, 3
        )

    # exit_expected プレビュー
    if (
        rules.max_holding_days > 0
        and holding_days is not None
        and holding_days >= rules.max_holding_days
    ):
        row["exit_expected"] = "time_based"
    elif (
        snap.system == "system7"
        and spy_high is not None
        and spy_max70 is not None
        and spy_high >= spy_max70
    ):
        row["exit_expected"] = "spy_breakout"

    return row


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", default=None)
    parser.add_argument("--output-json", default=None)
    parser.add_argument("--results-dir", default=str(ROOT / "results_csv"))
    parser.add_argument("--no-alpaca", action="store_true")
    args = parser.parse_args(argv)

    date_str = args.date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    date_compact = date_str.replace("-", "")
    results_dir = Path(args.results_dir)
    output_path = (
        Path(args.output_json)
        if args.output_json
        else results_dir / f"paper_status_{date_compact}.json"
    )

    snapshots: list[PositionSnapshot] = []
    client: Any | None = None
    if not args.no_alpaca:
        try:
            assert_paper_env()
        except LiveAccountGuardError as exc:
            print(f"[SAFETY ABORT] {exc}")
            return 2
        try:
            client = ba.get_client(paper=True)
            snapshots = fetch_position_snapshots(client)
        except Exception as exc:
            print(f"[warn] Alpaca 接続失敗 (offline mode): {exc}")

    tracker = load_tracker()
    entry_orders_index = _collect_entry_orders_index(results_dir)
    hydrate_system_tags(
        snapshots, tracker=tracker, entry_orders_index=entry_orders_index
    )

    spy_high, spy_max70 = _load_spy_context()
    atr_by_symbol = _load_atr_by_symbol([s.symbol for s in snapshots])

    rows = [
        _build_status_row(
            s,
            today=date_str,
            atr_by_symbol=atr_by_symbol,
            spy_high=spy_high,
            spy_max70=spy_max70,
        )
        for s in snapshots
    ]

    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": "1.0",
        "date": date_str,
        "count": len(rows),
        "spy_high": spy_high,
        "spy_max70": spy_max70,
        "positions": rows,
    }
    with output_path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2, default=str)

    time_cnt = sum(1 for r in rows if r.get("exit_expected") == "time_based")
    breakout_cnt = sum(1 for r in rows if r.get("exit_expected") == "spy_breakout")
    print(
        f"[status] positions={len(rows)} exit_expected(time={time_cnt}, breakout={breakout_cnt}) "
        f"spy_high={spy_high} spy_max70={spy_max70}"
    )
    print(f"[write] {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
