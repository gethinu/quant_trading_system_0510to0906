"""Verify the probe's numpy setup/entry/exit re-implementation is equivalent to
the repo's own code (common/system_setup_predicates.py + strategies/*).

Fails loudly on any mismatch. READ-ONLY.
"""
from __future__ import annotations

import os
import random
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
REPO = r"C:\Repos\quant_trading_system_0510to0906"
sys.path.insert(0, REPO)

from build_dataset import (  # noqa: E402
    LIMIT_ENTRY, NEEDED, ROLLING, SCORE_COL, SIDE, TRADE, load_symbol,
    resolve_configs, setup_masks,
)
from common.system_setup_predicates import (  # noqa: E402
    system1_setup_predicate_bool, system2_setup_predicate,
    system3_setup_predicate_bool, system4_setup_predicate,
    system5_setup_predicate, system6_setup_predicate,
)

PRED = {
    "system1": system1_setup_predicate_bool,
    "system2": system2_setup_predicate,
    "system3": system3_setup_predicate_bool,
    "system4": system4_setup_predicate,
    "system5": system5_setup_predicate,
    "system6": system6_setup_predicate,
}

STRAT_MOD = {
    "system1": ("strategies.system1_strategy", "System1Strategy"),
    "system2": ("strategies.system2_strategy", "System2Strategy"),
    "system3": ("strategies.system3_strategy", "System3Strategy"),
    "system4": ("strategies.system4_strategy", "System4Strategy"),
    "system5": ("strategies.system5_strategy", "System5Strategy"),
    "system6": ("strategies.system6_strategy", "System6Strategy"),
}


def main():
    random.seed(11)
    uni = [ln.strip() for ln in open(os.path.join(REPO, "data", "universe_auto.txt")) if ln.strip()]
    avail = [s for s in uni if os.path.exists(os.path.join(ROLLING, s + ".feather"))]
    sample = random.sample(avail, 220)

    strategies = {}
    for sysname, (mod, cls) in STRAT_MOD.items():
        m = __import__(mod, fromlist=[cls])
        strategies[sysname] = getattr(m, cls)()
    cfgs = resolve_configs()
    cfgs["system1"] = dict(cfgs["system1"])
    cfgs["system1"].setdefault("stop_atr_multiple", 5.0)

    setup_checked = 0
    setup_bad = 0
    trade_checked = 0
    trade_bad = 0
    nan_stop_drops = [0]
    bad_examples = []

    for sym in sample:
        loaded = load_symbol(os.path.join(ROLLING, sym + ".feather"))
        if loaded is None:
            continue
        idx, d = loaded
        n = len(idx)
        pdf = pd.DataFrame({c: d[c] for c in NEEDED}, index=idx)
        masks = setup_masks(d)

        # ---- setup equivalence on a random subset of bars
        bars = random.sample(range(n), min(25, n))
        for sysname, mask in masks.items():
            fn = PRED[sysname]
            for b in bars:
                row = pdf.iloc[b]
                mine = bool(mask[b])
                theirs = bool(fn(row))
                setup_checked += 1
                if mine != theirs:
                    setup_bad += 1
                    if len(bad_examples) < 10:
                        bad_examples.append(("setup", sysname, sym, str(idx[b]), mine, theirs))

        # ---- entry/exit equivalence on this symbol's actual candidates
        for sysname, mask in masks.items():
            sc_col, _ = SCORE_COL[sysname]
            sig = np.where(mask & np.isfinite(d[sc_col]))[0]
            sig = sig[(sig >= 1) & (sig + 1 < n)]
            if sig.size == 0:
                continue
            take = sig if sig.size <= 4 else np.array(random.sample(list(sig), 4))
            st = strategies[sysname]
            for s in take:
                e = int(s) + 1
                cand = {"symbol": sym, "entry_date": idx[e]}
                try:
                    ce = st.compute_entry(pdf, cand, 100000.0)
                except Exception:
                    ce = None
                mine = TRADE[sysname](d, e, n, cfgs[sysname])
                trade_checked += 1
                if ce is None:
                    if mine is not None:
                        trade_bad += 1
                        if len(bad_examples) < 20:
                            bad_examples.append(("entry-None", sysname, sym, str(idx[e]), mine, None))
                    continue
                if not np.isfinite(float(ce[1])):
                    # repo code lets a NaN ATR through and yields a NaN stop.
                    # the probe drops these rows on purpose (degenerate trade).
                    nan_stop_drops[0] += 1
                    continue
                if mine is None:
                    trade_bad += 1
                    if len(bad_examples) < 20:
                        bad_examples.append(("entry-mineNone", sysname, sym, str(idx[e]), None, ce))
                    continue
                ep_t, stop_t = float(ce[0]), float(ce[1])
                cx = st.compute_exit(pdf, e, ep_t, stop_t)
                if cx is None:
                    continue
                xp_t, xd_t = float(cx[0]), pd.Timestamp(cx[1])
                ep_m, xp_m, xi_m = mine
                ok = (
                    abs(ep_t - ep_m) < 1e-9
                    and abs(xp_t - xp_m) < 1e-9
                    and pd.Timestamp(idx[xi_m]) == xd_t
                )
                if not ok:
                    trade_bad += 1
                    if len(bad_examples) < 20:
                        bad_examples.append((
                            "exit", sysname, sym, str(idx[e]),
                            (ep_m, xp_m, str(idx[xi_m])), (ep_t, xp_t, str(xd_t)),
                        ))

    print("setup checks : %d   mismatches: %d" % (setup_checked, setup_bad))
    print("trade checks : %d   mismatches: %d" % (trade_checked, trade_bad))
    print("nan-stop rows dropped by probe (repo would emit NaN stop): %d" % nan_stop_drops[0])
    for b in bad_examples:
        print("  MISMATCH", b)
    if setup_bad or trade_bad:
        raise SystemExit(1)
    print("EQUIVALENCE OK")


if __name__ == "__main__":
    main()
