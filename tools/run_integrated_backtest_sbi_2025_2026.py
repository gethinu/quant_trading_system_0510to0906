import argparse
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.settings import get_settings
from common.cache_manager import load_base_cache
from common.integrated_backtest import run_integrated_backtest, SystemState
from common.performance_summary import summarize

from strategies.system1_strategy import System1Strategy
from strategies.system2_strategy import System2Strategy
from strategies.system3_strategy import System3Strategy
from strategies.system4_strategy import System4Strategy
from strategies.system5_strategy import System5Strategy
from strategies.system6_strategy import System6Strategy
from strategies.system7_strategy import System7Strategy

# ----------------------
# Config (Defaults)
# ----------------------
DEFAULT_START = "2025-01-01"
DEFAULT_END = "2026-01-01"
DEFAULT_BUFFER_DAYS = 400

INITIAL_CAPITAL_JPY = 300000.0
FX_RATE = 150.0  # JPY per USD
FX_MODE = "real_time_0"

SLIPPAGE_BPS = 5.0  # per side

# SBI米国株 信用取引（公式ページに記載の代表値）
# Checked: 2026-02-11
# Sources:
# - https://go.sbisec.co.jp/learn/kabu/margin/us-margin-beginner-250130.html
# - https://faq.sbisec.co.jp/answer/5f3334d7e37d38001148757f/
# - 売買手数料: 約定代金×0.33%（税込） 上限16.5USD（片道）
# - 買方金利: 年4.5%
# - 貸株料（売方）: 年2.0%
COMMISSION_RATE = 0.0033  # per side
COMMISSION_CAP_USD = 16.5  # per side
LONG_INTEREST_ANNUAL = 0.045
SHORT_BORROW_ANNUAL = 0.02
LONG_BORROW_RATIO = 1.0  # interest charged on full notional per SBI formula

DEFAULT_UNIVERSE_PATH = str(Path("data") / "universe_auto.txt")
OUTPUT_DIR = Path("results_csv")
LOG_DIR = Path("logs")


def log(msg: str, out_log: Path) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with out_log.open("a", encoding="utf-8") as fh:
        fh.write(f"[{ts}] {msg}\n")


def normalize_df(df: pd.DataFrame) -> pd.DataFrame:
    x = df.copy()
    # set datetime index
    if "date" in x.columns:
        x["date"] = pd.to_datetime(x["date"], errors="coerce")
        x = x.dropna(subset=["date"]).set_index("date")
    elif "Date" in x.columns:
        x["Date"] = pd.to_datetime(x["Date"], errors="coerce")
        x = x.dropna(subset=["Date"]).set_index("Date")
    else:
        try:
            x.index = pd.to_datetime(x.index, errors="coerce")
            x = x[~x.index.isna()]
        except Exception:
            pass

    # ensure OHLCV title-case columns exist (System1 expects Close/Open/High/Low)
    mapping = {
        "open": "Open",
        "high": "High",
        "low": "Low",
        "close": "Close",
        "volume": "Volume",
        "adjclose": "AdjClose",
    }
    for src, dest in mapping.items():
        if dest in x.columns:
            continue
        if src in x.columns:
            x[dest] = x[src]
        elif src.upper() in x.columns:
            x[dest] = x[src.upper()]

    # sort, drop duplicates
    try:
        x = x.sort_index()
        if getattr(x.index, "has_duplicates", False):
            x = x[~x.index.duplicated(keep="last")]
    except Exception:
        pass
    return x


def _filter_candidates_by_date(
    cands: object, *, start: pd.Timestamp, end: pd.Timestamp
) -> dict[pd.Timestamp, list[dict]]:
    if isinstance(cands, tuple) and len(cands) == 2:
        cands = cands[0]
    if not isinstance(cands, dict):
        return {}

    filtered: dict[pd.Timestamp, list[dict]] = {}
    for dt, entries in cands.items():
        ts = pd.to_datetime(dt)
        if ts < start or ts > end:
            continue
        norm: list[dict] = []
        if isinstance(entries, dict):
            for sym, payload in entries.items():
                if not sym:
                    continue
                item = {"symbol": str(sym), "entry_date": ts}
                if isinstance(payload, dict):
                    item.update(payload)
                norm.append(item)
        else:
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                item = dict(entry)
                if "symbol" not in item:
                    if "ticker" in item:
                        item["symbol"] = item.get("ticker")
                    elif "Symbol" in item:
                        item["symbol"] = item.get("Symbol")
                item.setdefault("entry_date", ts)
                if item.get("symbol"):
                    norm.append(item)
        if norm:
            filtered[ts] = norm
    return filtered


def _apply_costs(
    trades_df: pd.DataFrame,
    *,
    slippage_bps: float,
    commission_rate: float,
    commission_cap_usd: float,
    long_interest_annual: float,
    short_borrow_annual: float,
    long_borrow_ratio: float,
) -> tuple[pd.DataFrame, dict[str, float]]:
    if trades_df is None or trades_df.empty:
        return pd.DataFrame(), {
            "slippage_total": 0.0,
            "commission_total": 0.0,
            "long_interest_total": 0.0,
            "short_borrow_total": 0.0,
            "total_cost": 0.0,
        }

    net_df = trades_df.copy()
    net_df["entry_date"] = pd.to_datetime(net_df["entry_date"])
    net_df["exit_date"] = pd.to_datetime(net_df["exit_date"])
    net_df["entry_notional"] = (net_df["entry_price"].abs() * net_df["shares"]).astype(
        float
    )
    net_df["exit_notional"] = (net_df["exit_price"].abs() * net_df["shares"]).astype(
        float
    )
    net_df["hold_days"] = (net_df["exit_date"] - net_df["entry_date"]).dt.days.clip(
        lower=1
    )

    # Slippage per side (entry + exit)
    net_df["slip_entry"] = net_df["entry_notional"] * (slippage_bps / 10000.0)
    net_df["slip_exit"] = net_df["exit_notional"] * (slippage_bps / 10000.0)

    # Commission per side with cap
    net_df["comm_entry"] = (net_df["entry_notional"] * commission_rate).clip(
        upper=commission_cap_usd
    )
    net_df["comm_exit"] = (net_df["exit_notional"] * commission_rate).clip(
        upper=commission_cap_usd
    )

    # Interest/borrow
    net_df["long_interest"] = 0.0
    net_df["short_borrow"] = 0.0
    short_mask = net_df["side"].str.lower() == "short"
    long_mask = ~short_mask
    net_df.loc[long_mask, "long_interest"] = (
        net_df.loc[long_mask, "entry_notional"]
        * long_borrow_ratio
        * long_interest_annual
        * net_df.loc[long_mask, "hold_days"]
        / 365.0
    )
    net_df.loc[short_mask, "short_borrow"] = (
        net_df.loc[short_mask, "entry_notional"]
        * short_borrow_annual
        * net_df.loc[short_mask, "hold_days"]
        / 365.0
    )

    net_df["cost_total"] = (
        net_df["slip_entry"]
        + net_df["slip_exit"]
        + net_df["comm_entry"]
        + net_df["comm_exit"]
        + net_df["long_interest"]
        + net_df["short_borrow"]
    )
    net_df["pnl"] = net_df["pnl"] - net_df["cost_total"]

    costs = {
        "slippage_total": float((net_df["slip_entry"] + net_df["slip_exit"]).sum()),
        "commission_total": float((net_df["comm_entry"] + net_df["comm_exit"]).sum()),
        "long_interest_total": float(net_df["long_interest"].sum()),
        "short_borrow_total": float(net_df["short_borrow"].sum()),
        "total_cost": float(net_df["cost_total"].sum()),
    }
    return net_df, costs


def _dd_pct(enriched: pd.DataFrame, initial_capital_usd: float) -> float:
    if enriched is None or enriched.empty:
        return 0.0
    try:
        return float(
            ((enriched["drawdown"] / (initial_capital_usd + enriched["cum_max"])).min())
            * 100.0
        )
    except Exception:
        return 0.0


def _monthly_pnl(net_df: pd.DataFrame, initial_capital_usd: float) -> pd.DataFrame:
    if net_df is None or net_df.empty:
        return pd.DataFrame(
            columns=["month", "pnl", "return_pct", "pnl_jpy", "breakeven_jpy"]
        )
    monthly = (
        net_df.groupby(net_df["exit_date"].dt.to_period("M"))["pnl"]
        .sum()
        .reset_index()
    )
    monthly["month"] = monthly["exit_date"].astype(str)
    monthly = monthly.drop(columns=["exit_date"])
    monthly["return_pct"] = monthly["pnl"] / initial_capital_usd * 100.0
    monthly["pnl_jpy"] = monthly["pnl"] * FX_RATE
    monthly["breakeven_jpy"] = monthly["pnl_jpy"] >= 3000.0
    return monthly


def _canon(k: str) -> str:
    s = str(k)
    try:
        if s.lower().startswith("system"):
            num = "".join(ch for ch in s if ch.isdigit())
            return f"System{num}" if num else s.title()
        if s.isdigit():
            return f"System{s}"
        return s
    except Exception:
        return s


def _norm_map(d: dict, default_map: dict) -> dict:
    try:
        f = {k: float(v) for k, v in (d or {}).items() if float(v) > 0}
        s = sum(f.values())
        if s <= 0:
            f = default_map
            s = sum(f.values())
        return {_canon(k): v / s for k, v in f.items()}
    except Exception:
        s = sum(default_map.values())
        return {_canon(k): v / s for k, v in default_map.items()}


def _parse_systems_arg(raw: str) -> list[str]:
    if raw is None:
        raw = ""
    text = str(raw).strip()
    if not text or text.lower() == "all":
        return [f"System{i}" for i in range(1, 8)]

    tokens = [t for t in re.split(r"[,\s]+", text) if t]
    selected_nums: set[int] = set()
    for tok in tokens:
        s = tok.strip()
        if s.lower().startswith("system"):
            s = "".join(ch for ch in s if ch.isdigit())
        if not s.isdigit():
            raise ValueError(f"invalid system token: {tok}")
        n = int(s)
        if n < 1 or n > 7:
            raise ValueError(f"system out of range 1..7: {tok}")
        selected_nums.add(n)
    if not selected_nums:
        return [f"System{i}" for i in range(1, 8)]
    return [f"System{i}" for i in range(1, 8) if i in selected_nums]


def _cap_daily_candidates(
    cands: dict[pd.Timestamp, list[dict]], daily_entry_cap: int
) -> dict[pd.Timestamp, list[dict]]:
    if daily_entry_cap <= 0:
        return cands
    out: dict[pd.Timestamp, list[dict]] = {}
    for dt, entries in cands.items():
        if not isinstance(entries, list):
            continue
        capped = entries[:daily_entry_cap]
        if capped:
            out[dt] = capped
    return out


def main() -> None:
    # Integrated backtests should use full-scan behavior for System6 (avoid env-forced latest_only)
    try:
        import os

        os.environ["FULL_SCAN_TODAY"] = "true"
        from config.environment import reset_env_config_cache

        reset_env_config_cache()
    except Exception:
        pass

    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default=DEFAULT_START, help="YYYY-MM-DD")
    parser.add_argument("--end", default=DEFAULT_END, help="YYYY-MM-DD")
    parser.add_argument("--buffer-days", type=int, default=DEFAULT_BUFFER_DAYS)
    parser.add_argument("--universe", default=DEFAULT_UNIVERSE_PATH)
    parser.add_argument("--long-share", type=float, default=0.5)
    parser.add_argument("--short-share", type=float, default=0.5)
    parser.add_argument(
        "--initial-capital-jpy",
        type=float,
        default=INITIAL_CAPITAL_JPY,
        help=f"Initial capital in JPY (default: {INITIAL_CAPITAL_JPY:.0f})",
    )
    parser.add_argument(
        "--systems",
        default="all",
        help="Target systems. Examples: 'all', '6', '1,2,3,4,5,6,7', 'System2,System6'",
    )
    parser.add_argument(
        "--daily-entry-cap",
        type=int,
        default=0,
        help="If >0, cap number of new entries per system per day (0=disabled)",
    )
    parser.add_argument(
        "--min-hold-days",
        type=int,
        default=0,
        help="If >0, enforce minimum holding days before exit (0=disabled)",
    )
    parser.add_argument(
        "--engine",
        default="auto",
        choices=["python", "rust", "auto"],
        help="Integrated backtest core engine",
    )
    parser.add_argument(
        "--output-tag",
        default="",
        help="Optional suffix added to output filenames (safe chars: A-Za-z0-9._-)",
    )
    args = parser.parse_args()

    start = pd.Timestamp(args.start)
    end = pd.Timestamp(args.end)
    buffer_days = int(args.buffer_days)
    universe_path = Path(args.universe)
    long_share = float(args.long_share)
    short_share = float(args.short_share)
    initial_capital_jpy = float(args.initial_capital_jpy)
    daily_entry_cap = max(0, int(args.daily_entry_cap))
    min_hold_days = max(0, int(args.min_hold_days))
    selected_systems = _parse_systems_arg(args.systems)
    engine = str(args.engine or "auto").strip().lower()
    raw_tag = str(args.output_tag or "").strip()
    safe_tag = re.sub(r"[^A-Za-z0-9._-]+", "_", raw_tag).strip("_")

    start_key = start.strftime("%Y%m%d")
    end_key = end.strftime("%Y%m%d")
    out_key = f"{start_key}_{end_key}"
    if safe_tag:
        out_key = f"{out_key}_{safe_tag}"
    out_log = LOG_DIR / f"integrated_backtest_{out_key}.log"
    out_json = OUTPUT_DIR / f"integrated_backtest_sbi_{out_key}.json"
    out_monthly = OUTPUT_DIR / f"integrated_backtest_sbi_{out_key}_monthly.csv"

    settings = get_settings(create_dirs=False)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    # Load universe
    if universe_path.exists():
        symbols = [
            line.strip().upper()
            for line in universe_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    else:
        symbols = ["SPY"]
    if "SPY" not in symbols:
        symbols.insert(0, "SPY")

    symbol_limit = len(symbols)
    buffer_start = start - pd.Timedelta(days=buffer_days)

    start_time = time.time()
    log(f"Start integrated backtest: start={start.date()} end={end.date()} symbols={symbol_limit}", out_log)

    # Load cached data
    used_symbols: list[str] = []
    missing_symbols: list[str] = []
    data_dict: dict[str, pd.DataFrame] = {}

    for i, sym in enumerate(symbols, start=1):
        try:
            df = load_base_cache(
                sym,
                rebuild_if_missing=False,
                prefer_precomputed_indicators=False,
            )
        except Exception:
            df = None
        if df is None or df.empty:
            missing_symbols.append(sym)
            continue
        try:
            df = normalize_df(df)
        except Exception:
            missing_symbols.append(sym)
            continue
        try:
            df = df[(df.index >= buffer_start) & (df.index <= end)]
        except Exception:
            missing_symbols.append(sym)
            continue
        if df is None or df.empty:
            missing_symbols.append(sym)
            continue
        data_dict[sym] = df
        used_symbols.append(sym)

        if i % 200 == 0:
            elapsed = time.time() - start_time
            log(
                f"Loaded {i}/{symbol_limit} symbols (used={len(used_symbols)}) elapsed={elapsed:.1f}s",
                out_log,
            )

    log(f"Load done: used={len(used_symbols)} missing={len(missing_symbols)}", out_log)

    # Allocation map (same as UI defaults)
    la = getattr(settings.ui, "long_allocations", {}) or {}
    sa = getattr(settings.ui, "short_allocations", {}) or {}
    alloc_map: dict[str, float] = {}
    alloc_map.update(
        _norm_map(
            la,
            {"system1": 0.25, "system3": 0.25, "system4": 0.25, "system5": 0.25},
        )
    )
    alloc_map.update(_norm_map(sa, {"system2": 0.40, "system6": 0.40, "system7": 0.20}))

    # Prepare systems
    strategies = {
        "System1": System1Strategy(),
        "System2": System2Strategy(),
        "System3": System3Strategy(),
        "System4": System4Strategy(),
        "System5": System5Strategy(),
        "System6": System6Strategy(),
        "System7": System7Strategy(),
    }
    short_set = {"System2", "System6", "System7"}

    states: list[SystemState] = []
    signal_counts: dict[str, int] = {}

    spy_fallback = data_dict.get("SPY")
    for sys_name in selected_systems:
        strat = strategies[sys_name]
        t0 = time.time()
        log(f"Prepare {sys_name}: start", out_log)

        prepared = strat.prepare_data(data_dict, reuse_indicators=True)
        spy_df = None
        if isinstance(prepared, dict):
            spy_df = prepared.get("SPY")
        if spy_df is None:
            spy_df = spy_fallback
        try:
            cands = strat.generate_candidates(prepared, market_df=spy_df)
        except TypeError:
            cands = strat.generate_candidates(prepared)

        filtered = _filter_candidates_by_date(cands, start=start, end=end)
        filtered = _cap_daily_candidates(filtered, daily_entry_cap=daily_entry_cap)
        signal_counts[sys_name] = int(sum(len(v) for v in filtered.values()))
        states.append(
            SystemState(
                name=sys_name,
                side="short" if sys_name in short_set else "long",
                strategy=strat,
                prepared=prepared if isinstance(prepared, dict) else {},
                candidates_by_date=filtered,
            )
        )
        log(
            f"Prepare {sys_name}: done signals={signal_counts[sys_name]} elapsed={time.time()-t0:.1f}s",
            out_log,
        )

    # Run integrated backtest
    initial_capital_usd = initial_capital_jpy / FX_RATE
    log("Run integrated backtest: start", out_log)
    trades_df, _sig = run_integrated_backtest(
        states,
        initial_capital_usd,
        allocations=alloc_map,
        long_share=long_share,
        short_share=short_share,
        allow_gross_leverage=False,
        min_hold_days=min_hold_days,
        engine=engine,
    )
    log(
        f"Run integrated backtest: done trades={0 if trades_df is None else len(trades_df)}",
        out_log,
    )

    result = {
        "meta": {
            "start": str(start.date()),
            "end": str(end.date()),
            "buffer_start": str(buffer_start.date()),
            "initial_capital_jpy": initial_capital_jpy,
            "initial_capital_usd": initial_capital_usd,
            "fx_rate": FX_RATE,
            "fx_mode": FX_MODE,
            "symbol_limit": symbol_limit,
            "symbols_requested": len(symbols),
            "symbols_used": len(used_symbols),
            "missing_symbols": len(missing_symbols),
            "long_share": long_share,
            "short_share": short_share,
            "systems": selected_systems,
            "daily_entry_cap": daily_entry_cap,
            "min_hold_days": min_hold_days,
            "engine": engine,
        },
        "signals_per_system": signal_counts,
        "cost_assumptions": {
            "slippage_bps_per_side": SLIPPAGE_BPS,
            "commission_rate_per_side": COMMISSION_RATE,
            "commission_cap_usd_per_side": COMMISSION_CAP_USD,
            "long_interest_annual": LONG_INTEREST_ANNUAL,
            "short_borrow_annual": SHORT_BORROW_ANNUAL,
            "long_borrow_ratio": LONG_BORROW_RATIO,
        },
    }

    if trades_df is None or trades_df.empty:
        result["trades"] = 0
        result["error"] = "no trades"
        out_json.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        log("No trades. Finished.", out_log)
        raise SystemExit(0)

    # Gross summary
    summary_gross, _enriched_gross = summarize(trades_df.copy(), initial_capital_usd)

    # Apply cost model
    net_df, costs_usd = _apply_costs(
        trades_df,
        slippage_bps=SLIPPAGE_BPS,
        commission_rate=COMMISSION_RATE,
        commission_cap_usd=COMMISSION_CAP_USD,
        long_interest_annual=LONG_INTEREST_ANNUAL,
        short_borrow_annual=SHORT_BORROW_ANNUAL,
        long_borrow_ratio=LONG_BORROW_RATIO,
    )

    summary_net, enriched_net = summarize(net_df.copy(), initial_capital_usd)
    monthly = _monthly_pnl(net_df, initial_capital_usd)
    dd_pct = _dd_pct(enriched_net, initial_capital_usd)

    result.update(
        {
            "trades": int(len(net_df)),
            "gross": summary_gross.to_dict(),
            "net": summary_net.to_dict(),
            "net_max_drawdown_pct": float(dd_pct) if pd.notna(dd_pct) else 0.0,
            "costs_usd": costs_usd,
            "costs_jpy": {"total_cost": float(costs_usd["total_cost"] * FX_RATE)},
            "monthly": monthly.to_dict(orient="records"),
            "breakeven_months": int(monthly["breakeven_jpy"].sum())
            if not monthly.empty
            else 0,
            "months": int(len(monthly)),
            "total_net_pnl_usd": float(net_df["pnl"].sum()),
            "total_net_pnl_jpy": float(net_df["pnl"].sum() * FX_RATE),
        }
    )

    out_json.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    monthly.to_csv(out_monthly, index=False)
    log("Finished. Output saved.", out_log)


if __name__ == "__main__":
    main()
