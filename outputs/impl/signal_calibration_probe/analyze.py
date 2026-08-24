"""READ-ONLY calibration probe analysis.

Purged/embargoed forward split (repo convention: purge = drop training rows
whose label span [entry, exit] overlaps the test window; embargo = extra gap
after the train block). Fits temperature / Platt / isotonic calibration on
train, evaluates ECE + reliability + discrimination (AUC, rank-IC) OOS, and
simulates a confidence gate OOS.

No scipy (repo constraint: numpy + pandas + stdlib only).
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys

import numpy as np
import pandas as pd

REPO = r"C:\Repos\quant_trading_system_0510to0906"
if REPO not in sys.path:
    sys.path.insert(0, REPO)

# higher score = better, per core/system*.py rank() direction
SCORE_ASC = {"system4": True}  # only System4 ranks ascending (low RSI4 first)

MIN_OOS_ROWS = 200  # per-system floor for a verdict
MIN_BUCKET_ROWS = 50  # floor for reporting a gated bucket
N_BINS = 10


# ------------------------------------------------------------------ metrics
def auc_score(y: np.ndarray, s: np.ndarray) -> float:
    """Mann-Whitney AUC with tie correction. Returns nan if degenerate."""
    y = np.asarray(y, dtype=float)
    s = np.asarray(s, dtype=float)
    n1 = float(y.sum())
    n0 = float(len(y) - n1)
    if n1 == 0 or n0 == 0:
        return float("nan")
    order = np.argsort(s, kind="mergesort")
    ss = s[order]
    ranks = np.empty(len(s), dtype=float)
    i = 0
    while i < len(ss):
        j = i
        while j + 1 < len(ss) and ss[j + 1] == ss[i]:
            j += 1
        ranks[i : j + 1] = (i + j) / 2.0 + 1.0
        i = j + 1
    r = np.empty(len(s), dtype=float)
    r[order] = ranks
    return float((r[y == 1].sum() - n1 * (n1 + 1) / 2.0) / (n1 * n0))


def within_day_auc(dates, y, s):
    """Stratified (within signal-date) AUC: pools concordant/discordant pairs
    formed only *inside* the same day, so it measures whether the ranking key
    orders same-day candidates, free of any across-day regime drift."""
    dates = np.asarray(dates)
    y = np.asarray(y, float)
    s = np.asarray(s, float)
    num = 0.0
    den = 0.0
    n_days = 0
    for d in np.unique(dates):
        m = dates == d
        yy, ss = y[m], s[m]
        n1 = float(yy.sum())
        n0 = float(len(yy) - n1)
        if n1 == 0 or n0 == 0:
            continue
        a = auc_score(yy, ss)
        if not np.isfinite(a):
            continue
        w = n1 * n0
        num += a * w
        den += w
        n_days += 1
    if den == 0:
        return float("nan"), 0
    return float(num / den), n_days


def spearman(a: np.ndarray, b: np.ndarray) -> float:
    def rank(x):
        order = np.argsort(x, kind="mergesort")
        xs = x[order]
        ranks = np.empty(len(x), dtype=float)
        i = 0
        while i < len(xs):
            j = i
            while j + 1 < len(xs) and xs[j + 1] == xs[i]:
                j += 1
            ranks[i : j + 1] = (i + j) / 2.0 + 1.0
            i = j + 1
        out = np.empty(len(x), dtype=float)
        out[order] = ranks
        return out

    ra, rb = rank(np.asarray(a, float)), rank(np.asarray(b, float))
    ra -= ra.mean()
    rb -= rb.mean()
    den = math.sqrt(float((ra**2).sum()) * float((rb**2).sum()))
    return float((ra * rb).sum() / den) if den > 0 else float("nan")


def ece(y: np.ndarray, p: np.ndarray, n_bins: int = N_BINS):
    """Equal-count (quantile) binned Expected Calibration Error + reliability."""
    y = np.asarray(y, float)
    p = np.asarray(p, float)
    n = len(y)
    if n == 0:
        return float("nan"), []
    qs = np.quantile(p, np.linspace(0, 1, n_bins + 1))
    qs[0] -= 1e-12
    qs[-1] += 1e-12
    edges = np.unique(qs)
    idx = np.clip(np.searchsorted(edges, p, side="left") - 1, 0, len(edges) - 2)
    rows = []
    tot = 0.0
    for b in range(len(edges) - 1):
        m = idx == b
        cnt = int(m.sum())
        if cnt == 0:
            continue
        conf = float(p[m].mean())
        acc = float(y[m].mean())
        tot += cnt / n * abs(acc - conf)
        rows.append(
            {
                "bin": b,
                "n": cnt,
                "p_lo": float(edges[b]),
                "p_hi": float(edges[b + 1]),
                "mean_pred": round(conf, 4),
                "obs_rate": round(acc, 4),
            }
        )
    return float(tot), rows


def brier(y, p):
    return float(np.mean((np.asarray(p, float) - np.asarray(y, float)) ** 2))


# ------------------------------------------------------------- calibrators
def _sigmoid(z):
    return 1.0 / (1.0 + np.exp(-np.clip(z, -60, 60)))


def fit_logistic(x: np.ndarray, y: np.ndarray, fit_bias: bool = True, iters: int = 400):
    """Newton/IRLS logistic fit on a single feature. Returns (a, b)."""
    x = np.asarray(x, float)
    y = np.asarray(y, float)
    a, b = 0.0, 0.0
    if fit_bias:
        base = float(np.clip(y.mean(), 1e-6, 1 - 1e-6))
        b = math.log(base / (1 - base))
    for _ in range(iters):
        p = _sigmoid(a * x + b)
        w = np.clip(p * (1 - p), 1e-9, None)
        g_a = float(((y - p) * x).sum())
        g_b = float((y - p).sum()) if fit_bias else 0.0
        h_aa = float((w * x * x).sum()) + 1e-9
        h_ab = float((w * x).sum())
        h_bb = float(w.sum()) + 1e-9
        if fit_bias:
            det = h_aa * h_bb - h_ab * h_ab
            if abs(det) < 1e-12:
                break
            da = (h_bb * g_a - h_ab * g_b) / det
            db = (h_aa * g_b - h_ab * g_a) / det
        else:
            da = g_a / h_aa
            db = 0.0
        a += da
        b += db
        if max(abs(da), abs(db)) < 1e-10:
            break
    return a, b


def fit_isotonic(x: np.ndarray, y: np.ndarray):
    """PAVA isotonic regression. Returns (knot_x, knot_y) for step lookup."""
    order = np.argsort(x, kind="mergesort")
    xs = x[order].astype(float)
    ys = y[order].astype(float)
    vals = list(ys)
    wts = [1.0] * len(ys)
    xr = list(xs)
    i = 0
    while i < len(vals) - 1:
        if vals[i] <= vals[i + 1]:
            i += 1
            continue
        w = wts[i] + wts[i + 1]
        v = (vals[i] * wts[i] + vals[i + 1] * wts[i + 1]) / w
        vals[i : i + 2] = [v]
        wts[i : i + 2] = [w]
        xr[i : i + 2] = [xr[i + 1]]
        if i > 0:
            i -= 1
    return np.asarray(xr, float), np.asarray(vals, float)


def apply_isotonic(knot_x, knot_y, x):
    i = np.searchsorted(knot_x, np.asarray(x, float), side="left")
    i = np.clip(i, 0, len(knot_y) - 1)
    return knot_y[i]


# ---------------------------------------------------------------- pipeline
def oriented(df: pd.DataFrame, system: str) -> np.ndarray:
    s = df["score"].to_numpy(dtype=float)
    return -s if SCORE_ASC.get(system, False) else s


def ecdf_transform(train_scores: np.ndarray):
    srt = np.sort(train_scores)
    n = len(srt)

    def f(x):
        return np.searchsorted(srt, np.asarray(x, float), side="right") / max(n, 1)

    return f


def purged_forward_split(df: pd.DataFrame, train_frac: float, embargo_pct: float):
    dates = np.sort(df["signal_date"].unique())
    n = len(dates)
    cut = max(1, int(round(train_frac * n)))
    embargo = int(round(embargo_pct * n))
    train_end = dates[cut - 1]
    oos_start_i = min(n - 1, cut + embargo)
    oos_start = dates[oos_start_i]
    tr = df[df["signal_date"] <= train_end].copy()
    te = df[df["signal_date"] >= oos_start].copy()
    # purge: drop train rows whose label span reaches into the test window
    n_pre = len(tr)
    tr = tr[tr["exit_date"] < oos_start]
    return (
        tr,
        te,
        {
            "n_dates": int(n),
            "train_end": str(pd.Timestamp(train_end).date()),
            "embargo_dates": int(embargo),
            "oos_start": str(pd.Timestamp(oos_start).date()),
            "oos_end": str(pd.Timestamp(dates[-1]).date()),
            "train_rows_before_purge": int(n_pre),
            "train_rows_after_purge": int(len(tr)),
            "purged_rows": int(n_pre - len(tr)),
        },
    )


def block_bootstrap_auc(te: pd.DataFrame, y, s, n_boot=300, seed=7):
    """Resample whole signal-dates (blocks) to respect within-day dependence."""
    rng = np.random.default_rng(seed)
    dates = te["signal_date"].to_numpy()
    uniq = np.unique(dates)
    by = {d: np.where(dates == d)[0] for d in uniq}
    out = []
    for _ in range(n_boot):
        pick = rng.choice(uniq, size=len(uniq), replace=True)
        idx = np.concatenate([by[d] for d in pick])
        a = auc_score(y[idx], s[idx])
        if np.isfinite(a):
            out.append(a)
    if not out:
        return (float("nan"), float("nan"))
    return (float(np.quantile(out, 0.025)), float(np.quantile(out, 0.975)))


def gate_sim(te: pd.DataFrame, p: np.ndarray, thresholds):
    rows = []
    ret = te["ret"].to_numpy(float)
    base_n = len(ret)
    for t in thresholds:
        m = p >= t
        n = int(m.sum())
        if n == 0:
            rows.append({"threshold": round(float(t), 4), "n": 0})
            continue
        r = ret[m]
        rows.append(
            {
                "threshold": round(float(t), 4),
                "n": n,
                "kept_pct": round(100.0 * n / base_n, 1),
                "hit_rate": round(float((r > 0).mean()), 4),
                "mean_ret": round(float(r.mean()), 5),
                "median_ret": round(float(np.median(r)), 5),
                "std_ret": round(float(r.std(ddof=1)) if n > 1 else float("nan"), 5),
                "ret_per_unit_risk": (
                    round(float(r.mean() / r.std(ddof=1)), 4)
                    if n > 1 and r.std(ddof=1) > 0
                    else None
                ),
                "total_ret": round(float(r.sum()), 3),
                "below_floor": n < MIN_BUCKET_ROWS,
            }
        )
    return rows


def analyse_system(df: pd.DataFrame, system: str, args) -> dict:
    d = df[df["system"] == system].sort_values(["signal_date", "symbol"]).copy()
    res: dict = {"system": system, "n_rows": int(len(d))}
    if args.filled_only and system in ("system3", "system5", "system6"):
        d = d[d["limit_filled"]]
        res["filled_only"] = True
        res["n_rows_filled"] = int(len(d))
    if len(d) < 50:
        res["skipped"] = "fewer than 50 rows"
        return res

    tr, te, split = purged_forward_split(d, args.train_frac, args.embargo_pct)
    res["split"] = split
    res["n_train"] = int(len(tr))
    res["n_oos"] = int(len(te))
    res["oos_censored_pct"] = round(100.0 * float(te["censored_at_data_end"].mean()), 1)
    res["train_base_rate"] = (
        round(float((tr["ret"] > 0).mean()), 4) if len(tr) else None
    )
    res["oos_base_rate"] = round(float((te["ret"] > 0).mean()), 4) if len(te) else None
    if len(tr) < 100 or len(te) < 30:
        res["skipped"] = "train<100 or oos<30 rows after purge"
        return res

    s_tr, s_te = oriented(tr, system), oriented(te, system)
    y_tr = (tr["ret"].to_numpy(float) > 0).astype(float)
    y_te = (te["ret"].to_numpy(float) > 0).astype(float)

    # ---- discrimination (invariant to any monotone calibration map)
    auc = auc_score(y_te, s_te)
    res["auc_oos"] = round(auc, 4) if np.isfinite(auc) else None
    lo, hi = block_bootstrap_auc(te, y_te, s_te, n_boot=args.n_boot)
    res["auc_oos_ci95"] = [round(lo, 4), round(hi, 4)]
    res["auc_train"] = round(auc_score(y_tr, s_tr), 4)
    res["rank_ic_oos_vs_ret"] = round(spearman(s_te, te["ret"].to_numpy(float)), 4)
    res["rank_ic_train_vs_ret"] = round(spearman(s_tr, tr["ret"].to_numpy(float)), 4)
    wa, wd = within_day_auc(te["signal_date"].to_numpy(), y_te, s_te)
    res["within_day_auc_oos"] = round(wa, 4) if np.isfinite(wa) else None
    res["within_day_auc_n_days"] = int(wd)
    # within-day rank-IC of score vs return, averaged over days with >=5 rows
    ics = []
    for dd, sub in te.groupby("signal_date"):
        if len(sub) < 5:
            continue
        v = spearman(oriented(sub, system), sub["ret"].to_numpy(float))
        if np.isfinite(v):
            ics.append(v)
    res["within_day_rank_ic_oos"] = round(float(np.mean(ics)), 4) if ics else None
    res["within_day_rank_ic_n_days"] = len(ics)
    # decile table of the oriented score vs outcome (OOS)
    try:
        qq = pd.qcut(pd.Series(s_te).rank(method="first"), 10, labels=False)
        dec = (
            pd.DataFrame({"d": qq, "y": y_te, "r": te["ret"].to_numpy(float)})
            .groupby("d")
            .agg(
                n=("y", "size"),
                hit=("y", "mean"),
                mean_ret=("r", "mean"),
                median_ret=("r", "median"),
            )
        )
        res["oos_score_deciles"] = [
            {
                "decile": int(i) + 1,
                "n": int(row.n),
                "hit_rate": round(float(row.hit), 4),
                "mean_ret": round(float(row.mean_ret), 5),
                "median_ret": round(float(row.median_ret), 5),
            }
            for i, row in dec.iterrows()
        ]
    except Exception:
        pass

    # ---- feature: train-ECDF percentile of the oriented score (robust, monotone)
    f = ecdf_transform(s_tr)
    x_tr, x_te = f(s_tr), f(s_te)
    # centred, unit-ish scale so temperature has a meaningful zero
    z_tr, z_te = (x_tr - 0.5) * 2.0, (x_te - 0.5) * 2.0

    # uncalibrated "confidence" = the naive reading of the score percentile
    p_raw_te = x_te

    # constant base-rate reference (the honest null model)
    p_const_te = np.full(len(x_te), float(y_tr.mean()))

    # temperature-only: p = sigmoid(z / T), no bias
    a_t, _ = fit_logistic(z_tr, y_tr, fit_bias=False)
    temp = (1.0 / a_t) if abs(a_t) > 1e-12 else float("inf")
    p_temp_te = _sigmoid(a_t * z_te)

    # Platt: p = sigmoid(a*z + b)
    a_p, b_p = fit_logistic(z_tr, y_tr, fit_bias=True)
    p_platt_te = _sigmoid(a_p * z_te + b_p)

    # isotonic on the percentile feature
    kx, ky = fit_isotonic(x_tr, y_tr)
    p_iso_te = apply_isotonic(kx, ky, x_te)

    res["temperature_T"] = round(float(temp), 4) if np.isfinite(temp) else None
    res["platt_a"] = round(float(a_p), 4)
    res["platt_b"] = round(float(b_p), 4)
    res["platt_prob_range_oos"] = [
        round(float(p_platt_te.min()), 4),
        round(float(p_platt_te.max()), 4),
    ]
    res["iso_prob_range_oos"] = [
        round(float(p_iso_te.min()), 4),
        round(float(p_iso_te.max()), 4),
    ]

    cal = {}
    for name, p in (
        ("raw_score_percentile", p_raw_te),
        ("const_base_rate", p_const_te),
        ("temperature", p_temp_te),
        ("platt", p_platt_te),
        ("isotonic", p_iso_te),
    ):
        e, rel = ece(y_te, p, args.n_bins)
        cal[name] = {"ece_oos": round(e, 4), "brier_oos": round(brier(y_te, p), 4)}
        if name in ("raw_score_percentile", "platt", "isotonic"):
            cal[name]["reliability"] = rel
    res["calibration"] = cal

    # ---- gating simulation on the OOS split
    prim = p_platt_te
    qs = [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9]
    ths = sorted({float(np.quantile(prim, q)) for q in qs})
    res["gate_all_candidates"] = gate_sim(te, prim, ths)

    # ---- gating restricted to the actually-traded subset (top-N by score/day)
    top_n = int(args.top_n)
    te2 = te.copy()
    te2["_ord"] = oriented(te2, system)
    te2 = (
        te2.sort_values(["signal_date", "_ord"], ascending=[True, False])
        .groupby("signal_date", sort=False)
        .head(top_n)
    )
    if len(te2) >= 30:
        s2 = oriented(te2, system)
        y2 = (te2["ret"].to_numpy(float) > 0).astype(float)
        p2 = _sigmoid(a_p * ((f(s2) - 0.5) * 2.0) + b_p)
        a2 = auc_score(y2, s2)
        res["topn_subset"] = {
            "top_n": top_n,
            "n_oos": int(len(te2)),
            "auc_oos": round(a2, 4) if np.isfinite(a2) else None,
            "base_hit_rate": round(float(y2.mean()), 4),
            "mean_ret": round(float(te2["ret"].mean()), 5),
            "gate": gate_sim(
                te2,
                p2,
                sorted({float(np.quantile(p2, q)) for q in [0.0, 0.25, 0.5, 0.75]}),
            ),
        }
    else:
        res["topn_subset"] = {
            "top_n": top_n,
            "n_oos": int(len(te2)),
            "skipped": "fewer than 30 OOS rows",
        }

    # ---- CPCV stability of AUC using the repo's own fold builder
    try:
        from common.validation.cpcv import cpcv_date_folds

        lab = (d.groupby("signal_date")["exit_date"].max()).to_dict()
        folds = cpcv_date_folds(
            sorted(d["signal_date"].unique()),
            n_groups=6,
            k_test=2,
            embargo_pct=0.01,
            label_end_by_date=lab,
        )
        aucs = []
        for fo in folds:
            sub = d[d["signal_date"].isin(fo.test_dates)]
            if len(sub) < 50:
                continue
            a = auc_score(
                (sub["ret"].to_numpy(float) > 0).astype(float), oriented(sub, system)
            )
            if np.isfinite(a):
                aucs.append(a)
        if aucs:
            res["cpcv_auc"] = {
                "n_folds": len(aucs),
                "mean": round(float(np.mean(aucs)), 4),
                "std": round(float(np.std(aucs, ddof=1)), 4) if len(aucs) > 1 else None,
                "min": round(float(np.min(aucs)), 4),
                "max": round(float(np.max(aucs)), 4),
            }
    except Exception as exc:  # pragma: no cover
        res["cpcv_auc"] = {"error": str(exc)}

    res["meets_floor"] = bool(len(te) >= MIN_OOS_ROWS)
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--train-frac", type=float, default=0.6)
    ap.add_argument("--embargo-pct", type=float, default=0.01)
    ap.add_argument("--n-bins", type=int, default=N_BINS)
    ap.add_argument("--n-boot", type=int, default=300)
    ap.add_argument("--top-n", type=int, default=10)
    ap.add_argument("--filled-only", action="store_true")
    ap.add_argument("--tag", default="all_rows")
    args = ap.parse_args()

    df = pd.read_parquet(args.data)
    out = {
        "tag": args.tag,
        "params": {
            "train_frac": args.train_frac,
            "embargo_pct": args.embargo_pct,
            "n_bins": args.n_bins,
            "n_boot": args.n_boot,
            "top_n": args.top_n,
            "filled_only": bool(args.filled_only),
            "min_oos_rows": MIN_OOS_ROWS,
            "min_bucket_rows": MIN_BUCKET_ROWS,
        },
        "dataset": {
            "rows": int(len(df)),
            "span": [
                str(pd.Timestamp(df["signal_date"].min()).date()),
                str(pd.Timestamp(df["signal_date"].max()).date()),
            ],
        },
        "systems": [],
    }
    for system in sorted(df["system"].unique()):
        out["systems"].append(analyse_system(df, system, args))

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2, ensure_ascii=False)
    print("wrote", args.out)
    for r in out["systems"]:
        print(
            json.dumps(
                {
                    k: v
                    for k, v in r.items()
                    if k
                    in (
                        "system",
                        "n_rows",
                        "n_train",
                        "n_oos",
                        "auc_oos",
                        "auc_oos_ci95",
                        "within_day_auc_oos",
                        "rank_ic_oos_vs_ret",
                        "within_day_rank_ic_oos",
                        "oos_base_rate",
                        "meets_floor",
                        "skipped",
                    )
                }
            )
        )


if __name__ == "__main__":
    main()
