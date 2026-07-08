"""Alpaca Paper 口座の read-only スナップショットを Vercel monitor 用 JSON に書き出す。

**厳守 (safety):**
    - Alpaca は **read-only / GET のみ**。発注・cancel・reset は一切しない。
    - paper 固定 (``assert_paper_env`` + ``get_client(paper=True)``)。live URL は使わない
      (portfolio-history も ``paper-api.alpaca.markets`` のみ)。
    - ``--execute`` の類は存在しない。この script は口座を **観測** するだけ。

daily_pipeline.ps1 への配線は Phase 2 (別セッションと競合回避のため後追い)。
まずは手動実行 → ``results_csv/alpaca_snapshot_YYYYMMDD.json`` を生成し、
``scripts/publish_data_to_vercel.ps1`` で Vercel に載せて Alpaca タブ単体を公開する。

出力 schema (``alpaca_snapshot/v1``):
    {
      "schema": "alpaca_snapshot/v1",
      "date": "YYYY-MM-DD",
      "generated_at": "...Z",
      "provider": "alpaca-paper",
      "account": {equity, last_equity, cash, buying_power, long_market_value,
                  short_market_value, pnl_today_abs, pnl_today_pct,
                  unrealized_pl_total, status, trading_blocked, pattern_day_trader},
      "equity_curve": {timeframe, period, base_value, points:[{t,equity,pl,pl_pct,
                       peak,dd_pct}], peak_equity, max_drawdown_pct,
                       period_return_pct, source},
      "exposure": {long_usd, short_usd, gross_usd, net_usd, gross_pct, net_pct,
                   gross_cap_pct, net_cap_pct, by_system:{...}},
      "summary": {n_positions, n_long, n_short, n_winning, n_losing, win_rate_pct,
                  unrealized_pl_total, exit_soon_count, biggest_winner, biggest_loser},
      "positions": [ {symbol, system, side, qty, avg_entry_price, current_price,
                      lastday_price, market_value, cost_basis, unrealized_pl,
                      unrealized_pl_pct, intraday_pl, intraday_pl_pct, entry_date,
                      holding_days, max_holding_days, days_remaining, exit_date,
                      exit_type, exit_expected, stop_price_est, target_price_est,
                      distance_to_stop_pct, distance_to_target_pct} ],
      "reconciliation": {signals_date, signals_total, signals_buy, signals_sell,
                         orders_date, orders_submitted, held_now,
                         held_from_signals, note}
    }
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd  # noqa: E402

from common import broker_alpaca as ba  # noqa: E402
from common.alpaca_trading import (  # noqa: E402
    LiveAccountGuardError,
    assert_paper_env,
    compute_holding_days,
    parse_entry_date_from_client_order_id,
    parse_system_from_client_order_id,
)
from common.position_tracker import load_tracker  # noqa: E402
from common.trade_management import SYSTEM_TRADE_RULES  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "alpaca_snapshot/v1"
PROVIDER = "alpaca-paper"

# paper endpoint 固定 (live URL は絶対に書かない: test_alpaca_no_live_url ガード)。
PAPER_BASE = "https://paper-api.alpaca.markets"

# config.yaml::risk.portfolio の上限 (可視化用。無ければ既定へフォールバック)。
_DEFAULT_NET_CAP_PCT = 50.0
_DEFAULT_GROSS_CAP_PCT = 100.0


# --------------------------------------------------------------------------
# small utils
# --------------------------------------------------------------------------
def _f(val: Any) -> float | None:
    try:
        if val is None or val == "":
            return None
        return float(val)
    except (TypeError, ValueError):
        return None


def _pos_f(val: Any) -> float | None:
    """正値のみ (0/NaN/負値/None は None)。ATR や価格の sanity guard。"""
    f = _f(val)
    if f is None or not (f > 0):
        return None
    return f


def _side_of(p: Any, qty: float) -> str:
    """PositionSide enum ("PositionSide.LONG") でも str でも long/short に正規化。"""
    raw = getattr(p, "side", None)
    val = getattr(raw, "value", raw)  # enum なら .value == "long"
    s = str(val or "").lower()
    if s in ("long", "short"):
        return s
    return "long" if qty >= 0 else "short"


def _today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# --------------------------------------------------------------------------
# system tag / entry date resolution (Alpaca orders > tracker > map/file)
# --------------------------------------------------------------------------
def _fetch_orders_index(client: Any) -> dict[str, dict[str, Any]]:
    """Alpaca の全 orders (ALL, limit 500) から symbol -> {system, entry_date} を作る。

    entry order の client_order_id ('system{N}-{SYM}-{YYYYMMDD}') を優先解析し、
    より確度の高い entry_date として order.filled_at を使う。
    """
    idx: dict[str, dict[str, Any]] = {}
    if client is None:
        return idx
    try:
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        raw = client.get_orders(
            GetOrdersRequest(status=QueryOrderStatus.ALL, limit=500)
        )
    except Exception:
        return idx
    for o in raw or []:
        try:
            sym = str(getattr(o, "symbol", "") or "").upper()
            coid = str(getattr(o, "client_order_id", "") or "")
            if not sym:
                continue
            sys_tag = parse_system_from_client_order_id(coid)
            if sys_tag is None:
                continue  # entry order 由来のみ (exit/protect は除外)
            filled_at = getattr(o, "filled_at", None)
            ed_from_fill = None
            if filled_at is not None:
                try:
                    ed_from_fill = str(pd.Timestamp(filled_at).date())
                except Exception:
                    ed_from_fill = None
            ed = ed_from_fill or parse_entry_date_from_client_order_id(coid)
            # 最初にヒットしたもの (最新 fill) を採用。
            idx.setdefault(sym, {"system": sys_tag, "entry_date": ed})
        except Exception:
            continue
    return idx


def _load_symbol_system_map() -> dict[str, str]:
    p = ROOT / "data" / "symbol_system_map.json"
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}
    out: dict[str, str] = {}
    for k, v in raw.items():
        key = str(k).upper()
        if isinstance(v, list):
            out[key] = str(v[0]).lower() if v else "unknown"
        else:
            out[key] = str(v).lower()
    return out


def _load_entry_dates_file() -> dict[str, str]:
    p = ROOT / "data" / "position_entry_dates.json"
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return {str(k).upper(): str(v)[:10] for k, v in raw.items()}


def _resolve_tags(
    symbol: str,
    *,
    orders_index: dict[str, dict[str, Any]],
    tracker: dict[str, Any],
    symbol_map: dict[str, str],
    entry_file: dict[str, str],
) -> tuple[str | None, str | None]:
    """(system, entry_date) を優先順位付きで解決。"""
    sym = symbol.upper()
    system: str | None = None
    entry_date: str | None = None

    for src in (orders_index.get(sym), tracker.get(sym)):
        if isinstance(src, dict):
            if system is None and src.get("system"):
                system = str(src["system"]).lower()
            if entry_date is None and src.get("entry_date"):
                entry_date = str(src["entry_date"])[:10]
    if system is None:
        system = symbol_map.get(sym)
    if entry_date is None:
        entry_date = entry_file.get(sym)
    return system, entry_date


# --------------------------------------------------------------------------
# ATR + stop/target estimation (paper_trading_status と同じ考え方)
# --------------------------------------------------------------------------
def _load_atr(symbols: list[str]) -> dict[str, dict[int, float]]:
    out: dict[str, dict[int, float]] = {}
    rolling = ROOT / "data_cache" / "rolling"
    if not rolling.exists():
        return out
    for sym in symbols:
        f = rolling / f"{sym}.csv"
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
                    v = _pos_f(tail.get(col))
                    if v:
                        per[period] = v
                        break
        if per:
            out[sym] = per
    return out


def _estimate_stop_target(
    *, side: str, avg_entry: float, rules: Any, atr: dict[int, float]
) -> tuple[float | None, float | None]:
    """rules + ATR から stop / target の見積り価格を返す (どちらも best-effort)。"""
    stop = target = None
    if rules is None or not (avg_entry > 0):
        return stop, target
    atr_stop = atr.get(int(getattr(rules, "stop_atr_period", 20)))
    if atr_stop:
        dist = atr_stop * float(getattr(rules, "stop_atr_multiplier", 0) or 0)
        if dist > 0:
            stop = round(
                max(0.01, avg_entry - dist) if side == "long" else avg_entry + dist, 4
            )
    ptype = getattr(rules, "profit_target_type", "none")
    pval = float(getattr(rules, "profit_target_value", 0) or 0)
    if ptype == "percentage" and pval > 0:
        mult = 1.0 + pval / 100.0
        target = round(avg_entry * mult if side == "long" else avg_entry / mult, 4)
    elif ptype == "atr" and pval > 0:
        atr_t = atr.get(int(getattr(rules, "profit_target_atr_period", 10)))
        if atr_t:
            d = atr_t * pval
            target = round(avg_entry + d if side == "long" else avg_entry - d, 4)
    return stop, target


def _exit_type(system: str | None, rules: Any) -> str:
    if system == "system7":
        return "spy_hedge"
    if rules is None:
        return "unknown"
    if getattr(rules, "max_holding_days", 0) > 0:
        return "time"
    if getattr(rules, "use_trailing_stop", False):
        return "trailing"
    return "stop"


# --------------------------------------------------------------------------
# portfolio history (equity curve) — GET only, paper REST
# --------------------------------------------------------------------------
def _fetch_equity_curve(period: str, timeframe: str) -> dict[str, Any]:
    import requests

    headers = {
        "APCA-API-KEY-ID": os.getenv("APCA_API_KEY_ID", ""),
        "APCA-API-SECRET-KEY": os.getenv("APCA_API_SECRET_KEY", ""),
    }
    out: dict[str, Any] = {
        "timeframe": timeframe,
        "period": period,
        "base_value": None,
        "points": [],
        "source": "portfolio_history_api",
    }
    try:
        r = requests.get(
            f"{PAPER_BASE}/v2/account/portfolio/history",
            headers=headers,
            params={
                "period": period,
                "timeframe": timeframe,
                "extended_hours": "false",
            },
            timeout=20,
        )
        j = r.json() if r.content else {}
    except Exception as exc:  # pragma: no cover - network
        out["error"] = str(exc)
        return out

    ts = j.get("timestamp") or []
    eq = j.get("equity") or []
    pl = j.get("profit_loss") or []
    plpc = j.get("profit_loss_pct") or []
    out["base_value"] = _f(j.get("base_value"))

    points: list[dict[str, Any]] = []
    for i, t in enumerate(ts):
        e = _f(eq[i]) if i < len(eq) else None
        # 口座開設前の leading zero-equity は捨てる (1A 等で先頭に混ざる)。
        if e is None or e <= 0:
            continue
        try:
            day = str(
                pd.Timestamp(int(t), unit="s", tz="UTC")
                .tz_convert("America/New_York")
                .date()
            )
        except Exception:
            day = str(pd.Timestamp(int(t), unit="s").date())
        points.append(
            {
                "t": day,
                "equity": round(e, 2),
                "pl": round(_f(pl[i]) or 0.0, 2) if i < len(pl) else None,
                "pl_pct": (
                    round((_f(plpc[i]) or 0.0) * 100.0, 3) if i < len(plpc) else None
                ),
            }
        )
    out["points"] = points
    return out


def _augment_curve(
    curve: dict[str, Any], live_equity: float | None, today: str
) -> None:
    """running peak / drawdown を各点に付与し、live equity を末尾 point に足す。"""
    points: list[dict[str, Any]] = curve.get("points") or []
    # live 現在値を末尾に (API の 1D last は前営業日 close なので当日 intraday を足す)。
    if live_equity is not None and live_equity > 0:
        if not points or points[-1]["t"] != today:
            points.append(
                {
                    "t": today,
                    "equity": round(live_equity, 2),
                    "pl": None,
                    "pl_pct": None,
                    "live": True,
                }
            )
        else:
            points[-1]["equity"] = round(live_equity, 2)
            points[-1]["live"] = True

    peak = None
    max_dd = 0.0
    for pt in points:
        e = pt["equity"]
        peak = e if peak is None else max(peak, e)
        dd = (e - peak) / peak * 100.0 if peak else 0.0
        pt["peak"] = round(peak, 2)
        pt["dd_pct"] = round(dd, 3)
        max_dd = min(max_dd, dd)

    curve["points"] = points
    curve["peak_equity"] = round(peak, 2) if peak is not None else None
    curve["max_drawdown_pct"] = round(max_dd, 3)
    if len(points) >= 2 and points[0]["equity"]:
        curve["period_return_pct"] = round(
            (points[-1]["equity"] - points[0]["equity"]) / points[0]["equity"] * 100.0,
            3,
        )
    else:
        curve["period_return_pct"] = None


def _accumulate_equity(results_dir: Path, today: str, equity: float | None) -> None:
    """従: 日次 equity を results_csv/alpaca_equity_history.json に upsert 蓄積。

    portfolio-history API が主だが、将来 API 不通でも自前の記録が積み上がるよう保険。
    """
    if equity is None or equity <= 0:
        return
    path = results_dir / "alpaca_equity_history.json"
    hist: list[dict[str, Any]] = []
    if path.exists():
        try:
            hist = json.loads(path.read_text(encoding="utf-8")) or []
        except Exception:
            hist = []
    by_date = {row.get("t"): row for row in hist if isinstance(row, dict)}
    by_date[today] = {"t": today, "equity": round(equity, 2)}
    merged = [by_date[k] for k in sorted(by_date)]
    try:
        path.write_text(
            json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        pass


# --------------------------------------------------------------------------
# reconciliation: signals -> orders -> currently held
# --------------------------------------------------------------------------
def _latest_json(results_dir: Path, prefix: str) -> Path | None:
    """prefix_YYYYMMDD.json のうち日付が最大のものを返す (数値比較)。"""
    best: tuple[int, Path] | None = None
    for f in results_dir.glob(f"{prefix}*.json"):
        digits = "".join(ch for ch in f.stem[len(prefix) :] if ch.isdigit())[:8]
        if len(digits) != 8:
            continue
        n = int(digits)
        if best is None or n > best[0]:
            best = (n, f)
    return best[1] if best else None


def _build_reconciliation(results_dir: Path, held_symbols: set[str]) -> dict[str, Any]:
    rec: dict[str, Any] = {
        "signals_date": None,
        "signals_total": None,
        "signals_buy": None,
        "signals_sell": None,
        "orders_date": None,
        "orders_submitted": None,
        "held_now": len(held_symbols),
        "held_from_signals": None,
        "note": None,
    }
    sig_path = _latest_json(results_dir, "today_signals_")
    signal_symbols: set[str] = set()
    if sig_path is not None:
        try:
            sj = json.loads(sig_path.read_text(encoding="utf-8"))
        except Exception:
            sj = {}
        rec["signals_date"] = sj.get("date")
        port = sj.get("portfolio") or {}
        rec["signals_total"] = port.get("total_signals")
        buy = sell = 0
        for sysobj in (sj.get("systems") or {}).values():
            for s in sysobj.get("signals", []) or []:
                sym = str(s.get("symbol", "")).upper()
                if sym:
                    signal_symbols.add(sym)
                if str(s.get("side", "")).upper() == "BUY":
                    buy += 1
                elif str(s.get("side", "")).upper() == "SELL":
                    sell += 1
        rec["signals_buy"] = buy
        rec["signals_sell"] = sell

    ord_path = _latest_json(results_dir, "paper_orders_")
    if ord_path is not None:
        try:
            oj = json.loads(ord_path.read_text(encoding="utf-8"))
        except Exception:
            oj = {}
        rec["orders_date"] = oj.get("date")
        orders = oj.get("orders", []) or []
        submitted = sum(
            1
            for o in orders
            if o.get("order_id")
            or str(o.get("status") or "").lower()
            in ("filled", "accepted", "new", "partially_filled")
        )
        rec["orders_submitted"] = (
            submitted
            if submitted
            else (len(orders) if oj.get("mode") == "submitted" else 0)
        )

    if signal_symbols:
        rec["held_from_signals"] = len(signal_symbols & held_symbols)
    rec["note"] = (
        "signals=最新 today_signals の配信数 / orders=最新 paper_orders の送信数 / "
        "held_now=現在の実保有数 (過去日ぶんの累積)。held_from_signals は最新シグナル銘柄のうち現在保有中の数。"
    )
    return rec


# --------------------------------------------------------------------------
# main build
# --------------------------------------------------------------------------
def _load_net_cap() -> tuple[float, float]:
    """config.yaml から net/gross cap(%) を読む。失敗時は既定。"""
    try:
        import yaml  # type: ignore

        cfg = yaml.safe_load(
            (ROOT / "config" / "config.yaml").read_text(encoding="utf-8")
        )
        pf = ((cfg or {}).get("risk") or {}).get("portfolio") or {}
        net = _f(pf.get("max_net_exposure_pct"))
        gross = _f(pf.get("max_gross_exposure_pct"))
        return (
            (net * 100.0) if net is not None else _DEFAULT_NET_CAP_PCT,
            (gross * 100.0) if gross is not None else _DEFAULT_GROSS_CAP_PCT,
        )
    except Exception:
        return _DEFAULT_NET_CAP_PCT, _DEFAULT_GROSS_CAP_PCT


def build_snapshot(
    client: Any, *, date_str: str, results_dir: Path, period: str
) -> dict[str, Any]:
    account = client.get_account()
    raw_positions = list(client.get_all_positions())

    orders_index = _fetch_orders_index(client)
    tracker = load_tracker() or {}
    symbol_map = _load_symbol_system_map()
    entry_file = _load_entry_dates_file()

    symbols = [str(getattr(p, "symbol", "") or "").upper() for p in raw_positions]
    atr_by_symbol = _load_atr([s for s in symbols if s])

    positions: list[dict[str, Any]] = []
    long_usd = short_usd = 0.0
    by_system: dict[str, dict[str, Any]] = {}
    unrealized_total = 0.0
    n_win = n_loss = exit_soon = 0
    biggest_win: dict[str, Any] | None = None
    biggest_loss: dict[str, Any] | None = None
    held_symbols: set[str] = set()

    for p in raw_positions:
        sym = str(getattr(p, "symbol", "") or "").upper()
        if not sym:
            continue
        held_symbols.add(sym)
        qty = _f(getattr(p, "qty", 0)) or 0.0
        side = _side_of(p, qty)
        avg = _f(getattr(p, "avg_entry_price", 0)) or 0.0
        cur = _f(getattr(p, "current_price", None))
        mv = _f(getattr(p, "market_value", None)) or 0.0
        upl = _f(getattr(p, "unrealized_pl", None)) or 0.0
        uplpc = _f(getattr(p, "unrealized_plpc", None))
        intr = _f(getattr(p, "unrealized_intraday_pl", None))
        intrpc = _f(getattr(p, "unrealized_intraday_plpc", None))

        system, entry_date = _resolve_tags(
            sym,
            orders_index=orders_index,
            tracker=tracker,
            symbol_map=symbol_map,
            entry_file=entry_file,
        )
        rules = SYSTEM_TRADE_RULES.get(system) if system else None
        max_hold = int(getattr(rules, "max_holding_days", 0)) if rules else 0
        holding_days = compute_holding_days(entry_date, date_str)

        days_remaining = None
        exit_date = None
        if max_hold > 0 and entry_date:
            try:
                exit_dt = datetime.fromisoformat(entry_date[:10]).date()
                from datetime import timedelta

                exit_dt = exit_dt + timedelta(days=max_hold)
                exit_date = exit_dt.isoformat()
                if holding_days is not None:
                    days_remaining = max_hold - holding_days
            except Exception:
                pass

        atr = atr_by_symbol.get(sym, {})
        stop_est, target_est = _estimate_stop_target(
            side=side, avg_entry=avg, rules=rules, atr=atr
        )
        dist_stop = dist_target = None
        if cur and stop_est:
            dist_stop = round((stop_est - cur) / cur * 100.0, 3)
        if cur and target_est:
            dist_target = round((target_est - cur) / cur * 100.0, 3)

        exit_expected = None
        if max_hold > 0 and holding_days is not None and holding_days >= max_hold:
            exit_expected = "time_based"

        row = {
            "symbol": sym,
            "system": system or "unknown",
            "side": side,
            "qty": round(qty, 6),
            "avg_entry_price": round(avg, 4),
            "current_price": round(cur, 4) if cur else None,
            "lastday_price": _f(getattr(p, "lastday_price", None)),
            "market_value": round(mv, 2),
            "cost_basis": _f(getattr(p, "cost_basis", None)),
            "unrealized_pl": round(upl, 2),
            "unrealized_pl_pct": round(uplpc * 100.0, 3) if uplpc is not None else None,
            "intraday_pl": round(intr, 2) if intr is not None else None,
            "intraday_pl_pct": round(intrpc * 100.0, 3) if intrpc is not None else None,
            "entry_date": entry_date,
            "holding_days": holding_days,
            "max_holding_days": max_hold,
            "days_remaining": days_remaining,
            "exit_date": exit_date,
            "exit_type": _exit_type(system, rules),
            "exit_expected": exit_expected,
            "stop_price_est": stop_est,
            "target_price_est": target_est,
            "distance_to_stop_pct": dist_stop,
            "distance_to_target_pct": dist_target,
        }
        positions.append(row)

        # aggregates
        abs_mv = abs(mv)
        if side == "long":
            long_usd += abs_mv
        else:
            short_usd += abs_mv
        unrealized_total += upl
        if upl > 0:
            n_win += 1
        elif upl < 0:
            n_loss += 1
        if days_remaining is not None and days_remaining <= 1:
            exit_soon += 1
        if biggest_win is None or upl > biggest_win["pl"]:
            biggest_win = {
                "symbol": sym,
                "pl": round(upl, 2),
                "pl_pct": row["unrealized_pl_pct"],
            }
        if biggest_loss is None or upl < biggest_loss["pl"]:
            biggest_loss = {
                "symbol": sym,
                "pl": round(upl, 2),
                "pl_pct": row["unrealized_pl_pct"],
            }

        sysk = system or "unknown"
        b = by_system.setdefault(
            sysk, {"long_usd": 0.0, "short_usd": 0.0, "count": 0, "unrealized_pl": 0.0}
        )
        b["count"] += 1
        b["unrealized_pl"] = round(b["unrealized_pl"] + upl, 2)
        if side == "long":
            b["long_usd"] = round(b["long_usd"] + abs_mv, 2)
        else:
            b["short_usd"] = round(b["short_usd"] + abs_mv, 2)

    # sort positions: exit_expected first, then by |unrealized_pl| desc
    positions.sort(
        key=lambda r: (0 if r["exit_expected"] else 1, -(abs(r["unrealized_pl"] or 0)))
    )

    equity = _f(getattr(account, "equity", None))
    last_equity = _f(getattr(account, "last_equity", None))
    cash = _f(getattr(account, "cash", None))
    bp = _f(getattr(account, "buying_power", None))
    acct_long_mv = _f(getattr(account, "long_market_value", None))
    acct_short_mv = _f(getattr(account, "short_market_value", None))

    pnl_today_abs = pnl_today_pct = None
    if equity is not None and last_equity:
        pnl_today_abs = round(equity - last_equity, 2)
        pnl_today_pct = round((equity - last_equity) / last_equity * 100.0, 3)

    net_cap_pct, gross_cap_pct = _load_net_cap()
    gross_usd = long_usd + short_usd
    net_usd = long_usd - short_usd
    for b in by_system.values():
        base = b["long_usd"] + b["short_usd"]
        b["pct_of_gross"] = round(base / gross_usd * 100.0, 2) if gross_usd else 0.0

    # equity curve
    curve = _fetch_equity_curve(period, "1D")
    _augment_curve(curve, equity, date_str)
    _accumulate_equity(results_dir, date_str, equity)

    n_pos = len(positions)
    snapshot = {
        "schema": SCHEMA,
        "date": date_str,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "provider": PROVIDER,
        "account": {
            "equity": round(equity, 2) if equity is not None else None,
            "last_equity": round(last_equity, 2) if last_equity is not None else None,
            "cash": round(cash, 2) if cash is not None else None,
            "buying_power": round(bp, 2) if bp is not None else None,
            "long_market_value": (
                round(acct_long_mv, 2)
                if acct_long_mv is not None
                else round(long_usd, 2)
            ),
            "short_market_value": (
                round(acct_short_mv, 2)
                if acct_short_mv is not None
                else round(short_usd, 2)
            ),
            "pnl_today_abs": pnl_today_abs,
            "pnl_today_pct": pnl_today_pct,
            "unrealized_pl_total": round(unrealized_total, 2),
            "status": str(
                getattr(
                    getattr(account, "status", None),
                    "value",
                    getattr(account, "status", ""),
                )
                or ""
            ),
            "trading_blocked": bool(getattr(account, "trading_blocked", False)),
            "pattern_day_trader": bool(getattr(account, "pattern_day_trader", False)),
        },
        "equity_curve": curve,
        "exposure": {
            "long_usd": round(long_usd, 2),
            "short_usd": round(short_usd, 2),
            "gross_usd": round(gross_usd, 2),
            "net_usd": round(net_usd, 2),
            "gross_pct": round(gross_usd / equity * 100.0, 3) if equity else None,
            "net_pct": round(net_usd / equity * 100.0, 3) if equity else None,
            "gross_cap_pct": gross_cap_pct,
            "net_cap_pct": net_cap_pct,
            "by_system": by_system,
        },
        "summary": {
            "n_positions": n_pos,
            "n_long": sum(1 for r in positions if r["side"] == "long"),
            "n_short": sum(1 for r in positions if r["side"] == "short"),
            "n_winning": n_win,
            "n_losing": n_loss,
            "win_rate_pct": round(n_win / n_pos * 100.0, 1) if n_pos else None,
            "unrealized_pl_total": round(unrealized_total, 2),
            "exit_soon_count": exit_soon,
            "biggest_winner": biggest_win,
            "biggest_loser": biggest_loss,
        },
        "positions": positions,
        "reconciliation": _build_reconciliation(results_dir, held_symbols),
    }
    return snapshot


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--date", default=None, help="対象日 YYYY-MM-DD (default: today UTC)"
    )
    parser.add_argument("--output-json", default=None)
    parser.add_argument("--results-dir", default=str(ROOT / "results_csv"))
    parser.add_argument(
        "--period", default="3M", help="equity curve 期間 (portfolio-history API)"
    )
    parser.add_argument(
        "--no-alpaca", action="store_true", help="Alpaca に接続しない (offline test)"
    )
    args = parser.parse_args(argv)

    date_str = args.date or _today_str()
    date_compact = date_str.replace("-", "")
    results_dir = Path(args.results_dir)
    output_path = (
        Path(args.output_json)
        if args.output_json
        else results_dir / f"alpaca_snapshot_{date_compact}.json"
    )

    if args.no_alpaca:
        print("[info] --no-alpaca 指定: 接続せず終了 (snapshot 未生成)")
        return 0

    # --- safety: paper 固定を強制 (read-only でも live 口座は観測しない) ---
    try:
        assert_paper_env()
    except LiveAccountGuardError as exc:
        print(f"[SAFETY ABORT] {exc}")
        return 2

    try:
        client = ba.get_client(paper=True)
    except Exception as exc:
        print(f"[ERROR] Alpaca client 取得失敗: {exc}")
        return 1

    try:
        snapshot = build_snapshot(
            client, date_str=date_str, results_dir=results_dir, period=args.period
        )
    except Exception as exc:  # pragma: no cover - defensive
        print(f"[ERROR] snapshot 生成失敗: {exc}")
        return 1

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as fh:
        json.dump(snapshot, fh, ensure_ascii=False, indent=2, default=str)

    acct = snapshot["account"]
    summ = snapshot["summary"]
    print(
        f"[alpaca_snapshot] equity=${acct['equity']:,.0f} "
        f"pnl_today={acct['pnl_today_abs']} ({acct['pnl_today_pct']}%) "
        f"positions={summ['n_positions']} (L{summ['n_long']}/S{summ['n_short']}) "
        f"win_rate={summ['win_rate_pct']}% exit_soon={summ['exit_soon_count']} "
        f"curve_points={len(snapshot['equity_curve'].get('points', []))} "
        f"max_dd={snapshot['equity_curve'].get('max_drawdown_pct')}%"
    )
    print(f"[write] {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
