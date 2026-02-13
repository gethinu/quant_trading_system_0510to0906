import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common.cache_manager import load_base_cache
from common.integrated_backtest import SystemState, run_integrated_backtest
from common.performance_summary import summarize
from config.settings import get_settings

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

# SBI米国株 信用取引(想定): 値は変わり得るため定期確認推奨
# Checked: 2026-02-11
# Sources:
# - https://go.sbisec.co.jp/learn/kabu/margin/us-margin-beginner-250130.html
# - https://faq.sbisec.co.jp/answer/5f3334d7e37d38001148757f/
COMMISSION_RATE = 0.0033  # 0.33% per side
COMMISSION_CAP_USD = 16.5  # per side
LONG_INTEREST_ANNUAL = 0.045  # 買方金利（年率）
SHORT_BORROW_ANNUAL = 0.02  # 貸株料（年率）
LONG_BORROW_RATIO = 1.0  # ロング建玉のうち借入として金利計算する割合

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
    # datetime index
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

    # OHLCV を TitleCase に揃える（System1 などが Close/Open/High/Low を前提）
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

    try:
        x = x.sort_index()
        if getattr(x.index, "has_duplicates", False):
            x = x[~x.index.duplicated(keep="last")]
    except Exception:
        pass
    return x


def _costed_trades(
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

    # slippage: entry + exit
    net_df["slip_entry"] = net_df["entry_notional"] * (slippage_bps / 10000.0)
    net_df["slip_exit"] = net_df["exit_notional"] * (slippage_bps / 10000.0)

    # commission: entry + exit (cap per side)
    net_df["comm_entry"] = (net_df["entry_notional"] * commission_rate).clip(
        upper=commission_cap_usd
    )
    net_df["comm_exit"] = (net_df["exit_notional"] * commission_rate).clip(
        upper=commission_cap_usd
    )

    # interest/borrow
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
            (
                enriched["drawdown"]
                / (float(initial_capital_usd) + enriched["cum_max"])
            ).min()
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


def main() -> None:
    # Backtests should use full-scan behavior for System6 (avoid env-forced latest_only)
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
    args = parser.parse_args()

    start = pd.Timestamp(args.start)
    end = pd.Timestamp(args.end)
    buffer_days = int(args.buffer_days)
    universe_path = Path(args.universe)

    start_key = start.strftime("%Y%m%d")
    end_key = end.strftime("%Y%m%d")
    out_json = OUTPUT_DIR / f"single_system_backtests_sbi_{start_key}_{end_key}.json"
    out_summary_csv = (
        OUTPUT_DIR / f"single_system_backtests_sbi_{start_key}_{end_key}_summary.csv"
    )
    out_log = LOG_DIR / f"single_system_backtests_{start_key}_{end_key}.log"

    settings = get_settings(create_dirs=False)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    # Universe
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
    initial_capital_usd = INITIAL_CAPITAL_JPY / FX_RATE

    log(f"Start single-system backtests: symbols={symbol_limit}", out_log)

    # Load cached data
    start_time = time.time()
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

    log(
        f"Load done: used={len(used_symbols)} missing={len(missing_symbols)}",
        out_log,
    )

    spy_fallback = data_dict.get("SPY")

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

    out = {
        "meta": {
            "start": str(start.date()),
            "end": str(end.date()),
            "buffer_start": str(buffer_start.date()),
            "initial_capital_jpy": INITIAL_CAPITAL_JPY,
            "initial_capital_usd": initial_capital_usd,
            "fx_rate": FX_RATE,
            "fx_mode": FX_MODE,
            "symbols_requested": len(symbols),
            "symbols_used": len(used_symbols),
            "missing_symbols": len(missing_symbols),
        },
        "cost_assumptions": {
            "slippage_bps_per_side": SLIPPAGE_BPS,
            "commission_rate_per_side": COMMISSION_RATE,
            "commission_cap_usd_per_side": COMMISSION_CAP_USD,
            "long_interest_annual": LONG_INTEREST_ANNUAL,
            "short_borrow_annual": SHORT_BORROW_ANNUAL,
            "long_borrow_ratio": LONG_BORROW_RATIO,
        },
        "systems": {},
    }

    summary_rows: list[dict] = []

    for sys_name, strat in strategies.items():
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
        signals = int(sum(len(v) for v in filtered.values()))
        log(
            f"Prepare {sys_name}: done signals={signals} elapsed={time.time()-t0:.1f}s",
            out_log,
        )

        state = SystemState(
            name=sys_name,
            side="short" if sys_name in short_set else "long",
            strategy=strat,
            prepared=prepared if isinstance(prepared, dict) else {},
            candidates_by_date=filtered,
        )

        allocations = {sys_name: 1.0}
        if state.side == "short":
            long_share, short_share = 0.0, 1.0
        else:
            long_share, short_share = 1.0, 0.0

        log(f"Run backtest {sys_name}: start side={state.side}", out_log)
        trades_df, _sig = run_integrated_backtest(
            [state],
            initial_capital_usd,
            allocations=allocations,
            long_share=long_share,
            short_share=short_share,
            allow_gross_leverage=False,
        )
        trades = 0 if trades_df is None else int(len(trades_df))
        log(f"Run backtest {sys_name}: done trades={trades}", out_log)

        if trades_df is None or trades_df.empty:
            out["systems"][sys_name] = {
                "side": state.side,
                "signals": signals,
                "trades": 0,
                "gross": summarize(pd.DataFrame(), initial_capital_usd)[0].to_dict(),
                "net": summarize(pd.DataFrame(), initial_capital_usd)[0].to_dict(),
                "net_max_drawdown_pct": 0.0,
                "costs_usd": {
                    "slippage_total": 0.0,
                    "commission_total": 0.0,
                    "long_interest_total": 0.0,
                    "short_borrow_total": 0.0,
                    "total_cost": 0.0,
                },
                "total_net_pnl_usd": 0.0,
                "total_net_pnl_jpy": 0.0,
                "breakeven_months": 0,
                "months": 0,
                "monthly": [],
            }
            summary_rows.append(
                {
                    "system": sys_name,
                    "side": state.side,
                    "signals": signals,
                    "trades": 0,
                    "net_pnl_usd": 0.0,
                    "net_pnl_jpy": 0.0,
                    "net_return_pct": 0.0,
                    "net_cagr": None,
                    "net_mdd_pct": 0.0,
                    "win_rate": 0.0,
                    "sharpe": 0.0,
                    "profit_factor": 0.0,
                    "cost_total_usd": 0.0,
                }
            )
            continue

        gross_summary, _gross_enriched = summarize(trades_df.copy(), initial_capital_usd)

        net_df, costs = _costed_trades(
            trades_df,
            slippage_bps=SLIPPAGE_BPS,
            commission_rate=COMMISSION_RATE,
            commission_cap_usd=COMMISSION_CAP_USD,
            long_interest_annual=LONG_INTEREST_ANNUAL,
            short_borrow_annual=SHORT_BORROW_ANNUAL,
            long_borrow_ratio=LONG_BORROW_RATIO,
        )
        net_summary, net_enriched = summarize(net_df.copy(), initial_capital_usd)
        monthly = _monthly_pnl(net_df, initial_capital_usd)
        dd_pct = _dd_pct(net_enriched, initial_capital_usd)

        sys_out = {
            "side": state.side,
            "signals": signals,
            "trades": int(len(net_df)),
            "gross": gross_summary.to_dict(),
            "net": net_summary.to_dict(),
            "net_max_drawdown_pct": float(dd_pct),
            "costs_usd": costs,
            "total_net_pnl_usd": float(net_df["pnl"].sum()),
            "total_net_pnl_jpy": float(net_df["pnl"].sum() * FX_RATE),
            "breakeven_months": int(monthly["breakeven_jpy"].sum())
            if not monthly.empty
            else 0,
            "months": int(len(monthly)),
            "monthly": monthly.to_dict(orient="records"),
        }
        out["systems"][sys_name] = sys_out

        summary_rows.append(
            {
                "system": sys_name,
                "side": state.side,
                "signals": signals,
                "trades": int(len(net_df)),
                "net_pnl_usd": float(net_df["pnl"].sum()),
                "net_pnl_jpy": float(net_df["pnl"].sum() * FX_RATE),
                "net_return_pct": float(net_df["pnl"].sum() / initial_capital_usd * 100.0),
                "net_cagr": net_summary.cagr,
                "net_mdd_pct": float(dd_pct),
                "win_rate": net_summary.win_rate,
                "sharpe": net_summary.sharpe,
                "profit_factor": net_summary.profit_factor,
                "cost_total_usd": costs["total_cost"],
            }
        )

    out_json.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    pd.DataFrame(summary_rows).to_csv(out_summary_csv, index=False)
    log("Finished. Output saved.", out_log)


if __name__ == "__main__":
    main()
