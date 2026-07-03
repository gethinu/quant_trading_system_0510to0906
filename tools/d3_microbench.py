"""D3 micro-benchmark: System5 filter/setup candidate counts under 3 regimes.

Cases:
  B (impl 現状):  Close>=5 & adx7>55 & atr_pct>0.025
  A (docs 準拠):  Close>=5 & adx7>55 & atr_pct>0.04 & avgvolume50>500k & dollarvolume50>2.5M
  C (hybrid):    Close>=5 & adx7>55 & atr_pct>0.025 & avgvolume50>500k & dollarvolume50>2.5M

Setup on top of filter:
  Close > sma100 + atr10  AND  rsi3 < 50

Ranking: adx7 descending, top_n=20 per day.

出力: 各 case の候補総数、シンボル被覆、日次候補分布、setup 通過数、
      trade proxy として ATR10 ベースの forward-return R multiple (次日 open→next-day close) を
      利用可能なら計算。
"""
from __future__ import annotations

import os
import random
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path("/sessions/brave-magical-brahmagupta/mnt/quant_trading_system_0510to0906")
ROLLING = ROOT / "data_cache" / "rolling"
random.seed(42)

# Sample size — trade-off between runtime and universe coverage
SAMPLE_N = int(os.environ.get("D3_SAMPLE_N", "1500"))
TOP_N = 20
# Analysis window (adjust with env var)
WINDOW_YEARS = int(os.environ.get("D3_YEARS", "3"))

REQUIRED_COLS = [
    "date", "Open", "High", "Low", "Close", "Volume",
    "adx7", "atr_pct", "sma100", "atr10", "rsi3",
    "avgvolume50", "dollarvolume50",
]


def load_symbol(sym_path: Path) -> pd.DataFrame | None:
    try:
        df = pd.read_csv(sym_path, usecols=lambda c: c in REQUIRED_COLS or c == "Unnamed: 0")
    except Exception:
        return None
    if "date" not in df.columns:
        return None
    for c in REQUIRED_COLS:
        if c != "date" and c not in df.columns:
            return None
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"]).set_index("date").sort_index()
    return df


def apply_filters(df: pd.DataFrame) -> pd.DataFrame:
    """Return DataFrame with boolean columns fB, fA, fC, sB, sA, sC."""
    close = pd.to_numeric(df["Close"], errors="coerce")
    adx7 = pd.to_numeric(df["adx7"], errors="coerce")
    atr_pct = pd.to_numeric(df["atr_pct"], errors="coerce")
    avgvol = pd.to_numeric(df["avgvolume50"], errors="coerce")
    dv50 = pd.to_numeric(df["dollarvolume50"], errors="coerce")
    sma100 = pd.to_numeric(df["sma100"], errors="coerce")
    atr10 = pd.to_numeric(df["atr10"], errors="coerce")
    rsi3 = pd.to_numeric(df["rsi3"], errors="coerce")

    base = (close >= 5.0) & (adx7 > 55.0)
    fB = (base & (atr_pct > 0.025)).fillna(False)
    fA = (base & (atr_pct > 0.04) & (avgvol > 500_000) & (dv50 > 2_500_000)).fillna(False)
    fC = (base & (atr_pct > 0.025) & (avgvol > 500_000) & (dv50 > 2_500_000)).fillna(False)

    setup_common = ((close > (sma100 + atr10)) & (rsi3 < 50)).fillna(False)
    sB = fB & setup_common
    sA = fA & setup_common
    sC = fC & setup_common

    out = pd.DataFrame(
        {
            "adx7": adx7,
            "atr_pct": atr_pct,
            "Close": close,
            "Open": pd.to_numeric(df["Open"], errors="coerce"),
            "High": pd.to_numeric(df["High"], errors="coerce"),
            "Low": pd.to_numeric(df["Low"], errors="coerce"),
            "atr10": atr10,
            "fB": fB, "fA": fA, "fC": fC,
            "sB": sB, "sA": sA, "sC": sC,
        }
    )
    return out


def main() -> None:
    all_files = sorted(ROLLING.glob("*.csv"))
    n = min(SAMPLE_N, len(all_files))
    sample = random.sample(all_files, n)
    print(f"[D3] sampling {n}/{len(all_files)} symbols; window={WINDOW_YEARS}y", flush=True)

    cutoff = pd.Timestamp.now() - pd.DateOffset(years=WINDOW_YEARS)
    frames = []
    used = 0
    for i, p in enumerate(sample):
        sym = p.stem
        df = load_symbol(p)
        if df is None or df.empty:
            continue
        df = df[df.index >= cutoff]
        if len(df) < 100:
            continue
        stats = apply_filters(df)
        stats["symbol"] = sym
        stats["date"] = df.index
        frames.append(stats)
        used += 1
        if i % 200 == 0:
            print(f"  loaded {used} usable so far ({i+1}/{n})", flush=True)
    if not frames:
        print("no usable data", flush=True)
        return
    print(f"[D3] loaded {used} symbols with data", flush=True)

    all_ = pd.concat(frames, ignore_index=True)
    print(f"[D3] total row-days: {len(all_):,}", flush=True)

    # --------- Filter-level counts ---------
    print("\n=== FILTER PASS COUNTS (row-days) ===")
    print(f"  Case B (impl):   {int(all_['fB'].sum()):,}")
    print(f"  Case A (docs):   {int(all_['fA'].sum()):,}  ({all_['fA'].sum()/max(all_['fB'].sum(),1):.1%} of impl)")
    print(f"  Case C (hybrid): {int(all_['fC'].sum()):,}  ({all_['fC'].sum()/max(all_['fB'].sum(),1):.1%} of impl)")

    # --------- Setup-level counts ---------
    print("\n=== SETUP PASS COUNTS (row-days) ===")
    for lbl, col in [("B (impl)", "sB"), ("A (docs)", "sA"), ("C (hybrid)", "sC")]:
        print(f"  Case {lbl}:  {int(all_[col].sum()):,}")

    # --------- Daily candidate count (top-N per day, adx7 desc) ---------
    def top_n_per_day(mask_col: str) -> pd.DataFrame:
        sel = all_[all_[mask_col]].copy()
        if sel.empty:
            return sel
        sel = sel.sort_values(["date", "adx7"], ascending=[True, False])
        sel = sel.groupby("date").head(TOP_N)
        return sel

    tB = top_n_per_day("sB")
    tA = top_n_per_day("sA")
    tC = top_n_per_day("sC")

    print("\n=== TOP-{} PER DAY CANDIDATE COUNTS ===".format(TOP_N))
    for lbl, t in [("B (impl)", tB), ("A (docs)", tA), ("C (hybrid)", tC)]:
        days = t["date"].nunique() if not t.empty else 0
        rows = len(t)
        avg = (rows / days) if days else 0.0
        syms = t["symbol"].nunique() if not t.empty else 0
        print(f"  Case {lbl}: total_candidates={rows:,}  days_with_signal={days}  avg/day={avg:.2f}  unique_symbols={syms}")

    # --------- Daily distribution of setup counts ---------
    print("\n=== DAILY SETUP-COUNT DISTRIBUTION ===")
    daily = all_.groupby("date")[["sB", "sA", "sC"]].sum()
    for lbl, col in [("B", "sB"), ("A", "sA"), ("C", "sC")]:
        s = daily[col]
        print(f"  Case {lbl}: median={int(s.median())}  mean={s.mean():.1f}  p90={int(s.quantile(0.9))}  p99={int(s.quantile(0.99))}  max={int(s.max())}  zero_days={(s==0).sum()}/{len(s)}")

    # --------- ATR-scaled forward return (proxy trade P/L) ---------
    # System5 entry rule (spec): buy at 3% below prev close (limit order).
    # Approximation for proxy: assume fill only if next-day Low <= entry_limit; otherwise no trade.
    # Exit: profit target = entry + 1 * atr10 -> hit if High >= target during holding window.
    # Stop: entry - 3 * atr10 -> hit if Low <= stop.
    # Time exit: 6 days -> exit at Open of day 7.
    # We simulate with per-symbol daily frames for a quick R-multiple estimate.
    print("\n=== PROXY R-MULTIPLE (System5 spec entry+exit rules) ===")

    def simulate(mask_col: str) -> dict:
        sim_cash_R = []
        sim_hits = 0
        sim_fills = 0
        sim_signals = 0
        # Group per symbol for holding-window lookups
        for sym, dfg in all_.groupby("symbol"):
            dfg = dfg.sort_values("date").reset_index(drop=True)
            mask = dfg[mask_col].values
            highs = dfg["High"].values
            lows = dfg["Low"].values
            opens = dfg["Open"].values
            closes = dfg["Close"].values
            atr10 = dfg["atr10"].values
            n = len(dfg)
            for i in range(n - 8):
                if not mask[i]:
                    continue
                sim_signals += 1
                # Rank filter is applied at group-day level externally; per-symbol
                # sim treats every setup as if it made top-N (upper bound of trades)
                entry_limit = closes[i] * 0.97
                # Next-day fill check
                j = i + 1
                if lows[j] > entry_limit:
                    continue  # no fill
                sim_fills += 1
                entry = entry_limit
                stop = entry - 3.0 * atr10[i]
                target = entry + 1.0 * atr10[i]
                # walk holding window: day j (entry day) is skipped for exit, exits from j+1..j+6
                # spec: fill at limit on next day; profit target at open next day if reached
                exit_R = None
                # System5 spec: profit target at NEXT day OPEN if target reached (not intraday)
                # Simplify: check open of subsequent days
                for k in range(j + 1, min(j + 7, n)):
                    if lows[k] <= stop:
                        exit_R = (stop - entry) / atr10[i] if atr10[i] > 0 else -3.0
                        break
                    # Spec: 利食い = open of next day when target reached the previous day
                    # We approximate: if High >= target on day k, exit at open of day k+1
                    if k + 1 < n and highs[k] >= target:
                        exit_R = (opens[k + 1] - entry) / atr10[i] if atr10[i] > 0 else 1.0
                        break
                if exit_R is None:
                    # Time exit: open of day j+7 (or last available)
                    k = min(j + 6, n - 1)
                    exit_R = (opens[k] - entry) / atr10[i] if atr10[i] > 0 else 0.0
                sim_cash_R.append(exit_R)
                if exit_R > 0:
                    sim_hits += 1
        if not sim_cash_R:
            return {"n": 0, "avg_R": 0.0, "median_R": 0.0, "win": 0.0, "expectancy": 0.0, "fills": 0, "signals": sim_signals}
        arr = np.array(sim_cash_R)
        return {
            "n": len(arr),
            "avg_R": float(arr.mean()),
            "median_R": float(np.median(arr)),
            "win": float((arr > 0).mean()),
            "expectancy": float(arr.sum() / max(len(arr), 1)),
            "fills": sim_fills,
            "signals": sim_signals,
            "sharpe": float(arr.mean() / arr.std() * np.sqrt(252 / 4)) if arr.std() > 0 else 0.0,  # rough
        }

    for lbl, col in [("B (impl)", "sB"), ("A (docs)", "sA"), ("C (hybrid)", "sC")]:
        r = simulate(col)
        print(
            f"  Case {lbl}: signals={r['signals']:,} fills={r['fills']:,} trades(R)={r['n']:,} "
            f"win={r['win']:.1%} avg_R={r['avg_R']:.3f} median_R={r['median_R']:.3f} "
            f"expectancy={r['expectancy']:.3f} rough_sharpe={r.get('sharpe',0):.2f}"
        )

    print("\n[D3] done.")


if __name__ == "__main__":
    main()
