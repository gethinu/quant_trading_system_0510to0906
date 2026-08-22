"""READ-ONLY research probe: build (score -> realized outcome) dataset per system.

Nothing here is imported by the runtime. It only reads data_cache/ and writes
to the output dir passed on the command line.

Fidelity: entry/exit rules are numpy re-implementations of
strategies/systemN_strategy.py compute_entry/compute_exit, using the *live*
resolved strategy config. Equivalence against the real strategy methods is
asserted on a random sample by verify_equivalence.py.
"""
from __future__ import annotations

import argparse
import os
import sys
import time

import numpy as np
import pandas as pd

REPO = r"C:\Repos\quant_trading_system_0510to0906"
if REPO not in sys.path:
    sys.path.insert(0, REPO)

ROLLING = os.path.join(REPO, "data_cache", "rolling")

# ---------------------------------------------------------------- setup masks
# Mirrors common/system_setup_predicates.py (verified in verify_equivalence.py).


def setup_masks(d):
    def nn(*arrs):
        m = np.ones(len(arrs[0]), dtype=bool)
        for a in arrs:
            m &= np.isfinite(a)
        return m

    close, low = d["Close"], d["Low"]
    out = {}

    # system1: Close>=5, dv20>=50M, sma25>sma50, roc200>0
    out["system1"] = (
        nn(close, d["dollarvolume20"], d["sma25"], d["sma50"], d["roc200"])
        & (close >= 5.0)
        & (d["dollarvolume20"] >= 50e6)
        & (d["sma25"] > d["sma50"])
        & (d["roc200"] > 0.0)
    )

    # system2: Close>=5, dv20>25M, atr_ratio>0.03, rsi3>90, twodayup
    out["system2"] = (
        nn(close, d["dollarvolume20"], d["atr_ratio"], d["rsi3"])
        & (close >= 5.0)
        & (d["dollarvolume20"] > 25e6)
        & (d["atr_ratio"] > 0.03)
        & (d["rsi3"] > 90.0)
        & d["twodayup"]
    )

    # system3: Low>=1, avgvol50>=1M, atr_ratio>=0.05, Close>sma150, drop3d>=0.125
    out["system3"] = (
        nn(low, d["avgvolume50"], d["atr_ratio"], close, d["sma150"], d["drop3d"])
        & (low >= 1.0)
        & (d["avgvolume50"] >= 1e6)
        & (d["atr_ratio"] >= 0.05)
        & (close > d["sma150"])
        & (d["drop3d"] >= 0.125)
    )

    # system4: dv50>100M, 10<=hv50<=40, Close>sma200
    out["system4"] = (
        nn(d["dollarvolume50"], d["hv50"], close, d["sma200"])
        & (d["dollarvolume50"] > 100e6)
        & (d["hv50"] >= 10.0)
        & (d["hv50"] <= 40.0)
        & (close > d["sma200"])
    )

    # system5: Close>=5, adx7>55, atr_pct>0.04, avgvol50>500k, dv50>2.5M,
    #          Close>sma100+atr10, rsi3<50
    out["system5"] = (
        nn(close, d["adx7"], d["atr_pct"], d["sma100"], d["atr10"], d["rsi3"],
           d["avgvolume50"], d["dollarvolume50"])
        & (close >= 5.0)
        & (d["adx7"] > 55.0)
        & (d["atr_pct"] > 0.04)
        & (d["avgvolume50"] > 500_000.0)
        & (d["dollarvolume50"] > 2_500_000.0)
        & (close > (d["sma100"] + d["atr10"]))
        & (d["rsi3"] < 50.0)
    )

    # system6: return_6d>0.20, uptwodays
    out["system6"] = (
        np.isfinite(d["return_6d"]) & (d["return_6d"] > 0.20) & d["uptwodays"]
    )
    return out


SCORE_COL = {
    "system1": ("roc200", False),
    "system2": ("adx7", False),
    "system3": ("drop3d", False),
    "system4": ("rsi4", True),
    "system5": ("adx7", False),
    "system6": ("return_6d", False),
}

NEEDED = [
    "Open", "High", "Low", "Close", "Volume",
    "adx7", "avgvolume50", "atr_ratio", "atr_pct", "return_6d", "uptwodays",
    "twodayup", "drop3d", "sma25", "sma50", "sma100", "sma150", "sma200",
    "atr10", "atr20", "atr40", "rsi3", "rsi4", "roc200", "hv50",
    "dollarvolume20", "dollarvolume50",
]


def load_symbol(path):
    try:
        df = pd.read_feather(path)
    except Exception:
        return None
    if df is None or len(df) < 30 or "date" not in df.columns:
        return None
    df = df.sort_values("date")
    idx = pd.DatetimeIndex(pd.to_datetime(df["date"]).values).normalize()
    d = {}
    lower = {c.lower(): c for c in df.columns}
    for col in NEEDED:
        src = lower.get(col.lower())
        if src is None:
            if col in ("uptwodays", "twodayup"):
                d[col] = np.zeros(len(df), dtype=bool)
            else:
                d[col] = np.full(len(df), np.nan)
            continue
        v = df[src]
        if col in ("uptwodays", "twodayup"):
            d[col] = v.fillna(False).astype(bool).to_numpy()
        else:
            d[col] = pd.to_numeric(v, errors="coerce").astype("float64").to_numpy()
    return idx, d


# ------------------------------------------------------------ entry/exit sims
# e = entry bar index (signal bar is e-1). Returns (entry_px, exit_px, exit_i)
# or None when the strategy would reject the entry.


def trade_sys1(d, e, n, cfg):
    atr = d["atr20"][e - 1]
    ep = d["Open"][e]
    if not (np.isfinite(atr) and np.isfinite(ep)):
        return None
    stop = ep - float(cfg.get("stop_atr_multiple", 5.0)) * atr
    if ep - stop <= 0:
        return None
    trail = float(cfg.get("trailing_pct", 0.25))
    C = d["Close"]
    highest = ep
    for i in range(e + 1, n):
        c = C[i]
        if c > highest:
            highest = c
        if c <= highest * (1 - trail) or c <= stop:
            return ep, c, i
    return ep, C[n - 1], n - 1


def trade_sys4(d, e, n, cfg):
    atr = d["atr40"][e - 1]
    ep = d["Open"][e]
    if not (np.isfinite(atr) and np.isfinite(ep)):
        return None
    stop = ep - float(cfg.get("stop_atr_multiple", 1.5)) * atr
    if ep - stop <= 0:
        return None
    trail = float(cfg.get("trailing_pct", 0.20))
    C = d["Close"]
    highest = ep
    for i in range(e + 1, n):
        c = C[i]
        if c > highest:
            highest = c
        if c <= highest * (1 - trail) or c <= stop:
            return ep, c, i
    return ep, C[n - 1], n - 1


def trade_sys2(d, e, n, cfg):
    atr = d["atr10"][e - 1]
    prev_close = d["Close"][e - 1]
    ep = d["Open"][e]
    if not (np.isfinite(atr) and np.isfinite(prev_close) and np.isfinite(ep)):
        return None
    min_gap = float(cfg.get("entry_min_gap_pct", 0.04))
    if ep < prev_close * (1 + min_gap):
        return None
    stop = ep + float(cfg.get("stop_atr_multiple", 3.0)) * atr
    pt = float(cfg.get("profit_take_pct", 0.04))
    mh = int(cfg.get("max_hold_days", 3))
    H, C = d["High"], d["Close"]
    for off in range(mh):
        i = e + off
        if i >= n:
            break
        if H[i] >= stop:
            return ep, stop, i
        if (ep - C[i]) / ep >= pt:
            j = min(i + 1, n - 1)
            return ep, C[j], j
    j = min(e + mh, n - 1)
    return ep, C[j], j


def trade_sys3(d, e, n, cfg):
    atr = d["atr10"][e - 1]
    prev_close = d["Close"][e - 1]
    if not (np.isfinite(atr) and np.isfinite(prev_close)):
        return None
    ep = round(float(prev_close) * float(cfg.get("entry_price_ratio_vs_prev_close", 0.93)), 2)
    stop = ep - float(cfg.get("stop_atr_multiple", 2.5)) * atr
    if ep - stop <= 0:
        return None
    pt = float(cfg.get("profit_take_pct", 0.04))
    mh = int(cfg.get("max_hold_days", 3))
    L, C = d["Low"], d["Close"]
    for off in range(mh + 1):
        i = e + off
        if i >= n:
            break
        if L[i] <= stop:
            return ep, stop, i
        if (C[i] - ep) / ep >= pt:
            j = min(i + 1, n - 1)
            return ep, C[j], j
    j = min(e + mh + 1, n - 1)
    return ep, C[j], j


def trade_sys5(d, e, n, cfg):
    atr = d["atr10"][e - 1]
    prev_close = d["Close"][e - 1]
    if not (np.isfinite(atr) and np.isfinite(prev_close)):
        return None
    ep = round(float(prev_close) * float(cfg.get("entry_price_ratio_vs_prev_close", 0.97)), 2)
    stop = ep - float(cfg.get("stop_atr_multiple", 3.0)) * atr
    if ep - stop <= 0:
        return None
    target = ep + float(cfg.get("target_atr_multiple", 1.0)) * atr
    fb = int(cfg.get("fallback_exit_after_days", 6))
    L, H, C, OPEN = d["Low"], d["High"], d["Close"], d["Open"]
    for off in range(1, fb + 1):
        i = e + off
        if i >= n:
            break
        if L[i] <= stop:
            return ep, stop, i
        if H[i] >= target:
            j = i + 1
            if j < n:
                return ep, OPEN[j], j
            return ep, C[i], i
    j = e + fb + 1
    if j < n:
        return ep, OPEN[j], j
    k = min(e + fb, n - 1)
    return ep, C[k], k


def trade_sys6(d, e, n, cfg):
    atr = d["atr10"][e - 1]
    prev_close = d["Close"][e - 1]
    if not (np.isfinite(atr) and np.isfinite(prev_close)):
        return None
    ep = round(float(prev_close) * float(cfg.get("entry_price_ratio_vs_prev_close", 1.05)), 2)
    stop = ep + float(cfg.get("stop_atr_multiple", 3.0)) * atr
    if stop <= ep:
        return None
    pt = float(cfg.get("profit_take_pct", 0.05))
    md = int(cfg.get("profit_take_max_days", 3))
    H, C = d["High"], d["Close"]
    for off in range(1, md + 1):
        i = e + off
        if i >= n:
            break
        if H[i] >= stop:
            return ep, stop, i
        if (ep - C[i]) / ep >= pt:
            j = i + 1
            if j < n:
                return ep, C[j], j
            return ep, C[i], i
    j = e + md
    if j < n:
        return ep, C[j], j
    return ep, C[n - 1], n - 1


TRADE = {
    "system1": trade_sys1, "system2": trade_sys2, "system3": trade_sys3,
    "system4": trade_sys4, "system5": trade_sys5, "system6": trade_sys6,
}
SIDE = {"system1": 1, "system2": -1, "system3": 1, "system4": 1,
        "system5": 1, "system6": -1}
# systems whose entry is a limit order placed away from the prior close
LIMIT_ENTRY = {"system3": "long", "system5": "long", "system6": "short"}


def resolve_configs():
    from strategies.system1_strategy import System1Strategy
    from strategies.system2_strategy import System2Strategy
    from strategies.system3_strategy import System3Strategy
    from strategies.system4_strategy import System4Strategy
    from strategies.system5_strategy import System5Strategy
    from strategies.system6_strategy import System6Strategy

    return {
        "system1": System1Strategy().config, "system2": System2Strategy().config,
        "system3": System3Strategy().config, "system4": System4Strategy().config,
        "system5": System5Strategy().config, "system6": System6Strategy().config,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--limit-symbols", type=int, default=0)
    ap.add_argument(
        "--max-hold", type=int, default=0,
        help="Cap the holding period at H bars (mark to market at the close of "
             "bar e+H) and require H bars of forward data for every signal. "
             "0 = the system's own unbounded exit rule (repo convention). "
             "A cap makes labels comparable across time splits: System1/4 use "
             "trailing exits with no time stop, so with a 2-year cache a large "
             "share of late signals would otherwise be unresolved MTM rows.")
    args = ap.parse_args()

    cfgs = resolve_configs()
    # System1 has no stop_atr_multiple in yaml -> strategy uses the module
    # constant STOP_ATR_MULTIPLE_SYSTEM1 = 5.0. Make that explicit here.
    cfgs["system1"] = dict(cfgs["system1"])
    cfgs["system1"].setdefault("stop_atr_multiple", 5.0)

    uni_path = os.path.join(REPO, "data", "universe_auto.txt")
    uni = [ln.strip() for ln in open(uni_path) if ln.strip()]
    paths = []
    for s in uni:
        p = os.path.join(ROLLING, s + ".feather")
        if os.path.exists(p):
            paths.append((s, p))
    if args.limit_symbols:
        paths = paths[: args.limit_symbols]
    print("symbols: %d" % len(paths), flush=True)

    rows = []
    t0 = time.time()
    for k, (sym, p) in enumerate(paths, 1):
        loaded = load_symbol(p)
        if loaded is None:
            continue
        idx, d = loaded
        n = len(idx)
        masks = setup_masks(d)
        for system, mask in masks.items():
            sc_col, _asc = SCORE_COL[system]
            sc = d[sc_col]
            sig = np.where(mask & np.isfinite(sc))[0]
            sig = sig[(sig >= 1) & (sig + 1 < n)]
            if sig.size == 0:
                continue
            fn = TRADE[system]
            side = SIDE[system]
            lim = LIMIT_ENTRY.get(system)
            H = int(args.max_hold)
            for s in sig:
                e = s + 1
                if H > 0 and e + H >= n:
                    continue
                r = fn(d, e, n, cfgs[system])
                if r is None:
                    continue
                ep, xp, xi = r
                if H > 0 and xi > e + H:
                    xi = e + H
                    xp = d["Close"][xi]
                if not (np.isfinite(ep) and np.isfinite(xp)) or ep <= 0:
                    continue
                ret = side * (xp - ep) / ep
                filled = True
                if lim == "long":
                    filled = bool(d["Low"][e] <= ep)
                elif lim == "short":
                    filled = bool(d["High"][e] >= ep)
                rows.append((
                    system, sym, idx[s], idx[e], idx[xi], float(sc[s]),
                    float(ep), float(xp), float(ret), int(xi - e), filled,
                    bool(xi == n - 1),
                ))
        if k % 500 == 0:
            print("  %d/%d rows=%d %.0fs" % (k, len(paths), len(rows), time.time() - t0),
                  flush=True)

    df = pd.DataFrame(rows, columns=[
        "system", "symbol", "signal_date", "entry_date", "exit_date", "score",
        "entry_price", "exit_price", "ret", "hold_bars", "limit_filled",
        "censored_at_data_end",
    ])
    os.makedirs(args.out, exist_ok=True)
    fp = os.path.join(args.out, "candidates.parquet")
    df.to_parquet(fp, index=False)
    print("wrote %s  rows=%d" % (fp, len(df)), flush=True)
    if len(df):
        g = df.groupby("system").agg(
            n=("ret", "size"),
            win=("ret", lambda x: float((x > 0).mean())),
            mean_ret=("ret", "mean"),
        )
        print(g)


if __name__ == "__main__":
    main()
