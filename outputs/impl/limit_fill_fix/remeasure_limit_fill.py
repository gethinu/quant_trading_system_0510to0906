"""Re-measure S3/S5/S6 with the REPO's (fixed) compute_entry.

For every candidate row the probe produced, call the real
strategies/systemN_strategy.compute_entry on the same bar and record whether the
repo now books the trade. Reports win rate / trade count before vs after and
cross-checks the repo's decision against the probe's own limit_filled flag.
"""

import os
import sys

import numpy as np
import pandas as pd

REPO = r"C:\Repos\quant_trading_system_0510to0906"
sys.path.insert(0, REPO)
ROLLING = os.path.join(REPO, "data_cache", "rolling")

from strategies.system3_strategy import System3Strategy  # noqa: E402
from strategies.system5_strategy import System5Strategy  # noqa: E402
from strategies.system6_strategy import System6Strategy  # noqa: E402

STRAT = {
    "system3": System3Strategy(),
    "system5": System5Strategy(),
    "system6": System6Strategy(),
}

data = pd.read_parquet(sys.argv[1])
data = data[data["system"].isin(STRAT)]

out = []
for system, sub in data.groupby("system"):
    strat = STRAT[system]
    filled_mask = np.zeros(len(sub), dtype=bool)
    entry_px = np.full(len(sub), np.nan)
    pos = 0
    for sym, g in sub.groupby("symbol", sort=False):
        path = os.path.join(ROLLING, sym + ".feather")
        raw = pd.read_feather(path).sort_values("date")
        lower = {c.lower(): c for c in raw.columns}
        df = pd.DataFrame(
            {
                "Open": pd.to_numeric(raw[lower["open"]], errors="coerce"),
                "High": pd.to_numeric(raw[lower["high"]], errors="coerce"),
                "Low": pd.to_numeric(raw[lower["low"]], errors="coerce"),
                "Close": pd.to_numeric(raw[lower["close"]], errors="coerce"),
                "atr10": pd.to_numeric(raw[lower["atr10"]], errors="coerce"),
            }
        )
        df.index = pd.DatetimeIndex(pd.to_datetime(raw["date"]).values).normalize()
        for i, ed in zip(g.index, g["entry_date"]):
            r = strat.compute_entry(df, {"entry_date": pd.Timestamp(ed)}, 1e6)
            loc = sub.index.get_loc(i)
            if r is not None:
                filled_mask[loc] = True
                entry_px[loc] = r[0]
        pos += len(g)

    sub = sub.assign(repo_fills=filled_mask, repo_entry_px=entry_px)
    agree = int((sub["repo_fills"] == sub["limit_filled"]).sum())
    px_ok = bool(
        np.allclose(
            sub.loc[sub["repo_fills"], "repo_entry_px"],
            sub.loc[sub["repo_fills"], "entry_price"],
            atol=1e-9,
        )
    )
    booked = sub[sub["repo_fills"]]
    out.append(
        {
            "system": system,
            "candidates": len(sub),
            "win_before": float((sub["ret"] > 0).mean()),
            "mean_ret_before": float(sub["ret"].mean()),
            "trades_after": len(booked),
            "fill_rate": len(booked) / len(sub),
            "win_after": float((booked["ret"] > 0).mean()),
            "mean_ret_after": float(booked["ret"].mean()),
            "probe_fillable_win": float(
                (sub.loc[sub["limit_filled"], "ret"] > 0).mean()
            ),
            "agree_with_probe_flag": f"{agree}/{len(sub)}",
            "entry_px_identical": px_ok,
        }
    )

res = pd.DataFrame(out)
pd.set_option("display.width", 220)
pd.set_option("display.max_columns", 50)
print(res.to_string(index=False))
res.to_json(sys.argv[2], orient="records", indent=2)
