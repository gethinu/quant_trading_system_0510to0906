#!/usr/bin/env python3
"""Turn a ``revalidate_summary_*.json`` into the before/after markdown tables.

Reads the A/B summary written by ``revalidate_limit_fill.py`` and emits the
per-system CPCV / bootstrap / DSR comparison used in the durable report.
"""

from __future__ import annotations

import argparse
import json


def _f(v, nd=3, dash="-"):
    if v is None:
        return dash
    try:
        fv = float(v)
    except (TypeError, ValueError):
        return dash
    if fv != fv:  # NaN
        return "n/a"
    return f"{fv:.{nd}f}"


def _arm(rep):
    if not rep:
        return None
    c = rep.get("cpcv") or {}
    b = rep.get("bootstrap") or {}
    d = rep.get("deflated_sharpe") or {}
    return {
        "full_sharpe": c.get("full_sample_sharpe"),
        "fold_mean": c.get("fold_sharpe_mean"),
        "fold_std": c.get("fold_sharpe_std"),
        "fold_min": c.get("fold_sharpe_min"),
        "fold_max": c.get("fold_sharpe_max"),
        "frac_pos": c.get("frac_folds_positive"),
        "n_comb": c.get("n_combinations"),
        "boot_lo": b.get("ci_low"),
        "boot_hi": b.get("ci_high"),
        "boot_p": b.get("p_value_le_zero"),
        "dsr": d.get("deflated_sharpe"),
        "passed": d.get("passed"),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("summary")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    with open(args.summary, encoding="utf-8") as fh:
        s = json.load(fh)

    L: list[str] = []
    cp = s["cpcv"]
    L.append(
        f"universe={s['universe_size']} symbols | capital={s['capital']:,.0f} | "
        f"CPCV n_groups={cp['n_groups']} k_test={cp['k_test']} "
        f"embargo={cp['embargo_pct']} | n_boot={cp['n_boot']} seed={cp['seed']}"
    )
    L.append("")

    # ---- fills ----------------------------------------------------------- #
    L.append("### Fill realism (full-sample, single-system engine)")
    L.append("")
    L.append(
        "| system | limit entry | candidates | trades (pre-fix) | trades (fixed) | "
        "fill rate | win rate pre → fixed | mean trade return pre → fixed | "
        "cum. PnL % of capital pre → fixed |"
    )
    L.append("|---|---|---:|---:|---:|---:|---|---|---|")
    for name, blk in s["systems"].items():
        if blk.get("skipped"):
            L.append(f"| {name} | - | - | - | - | - | - | - | {blk['skipped']} |")
            continue
        fs = blk["trades_full_sample"]
        pre, fix = fs["prefix"], fs["fixed"]
        L.append(
            f"| {name} | {'yes' if blk['is_limit_system'] else 'no'} | "
            f"{blk['n_candidates']:,} | {pre.get('n_trades', 0):,} | "
            f"{fix.get('n_trades', 0):,} | "
            f"{_f(fs.get('fill_rate'), 3)} | "
            f"{_f(pre.get('win_rate'), 4)} → {_f(fix.get('win_rate'), 4)} | "
            f"{_f(pre.get('mean_trade_return'), 4)} → "
            f"{_f(fix.get('mean_trade_return'), 4)} | "
            f"{_f(pre.get('pnl_pct_of_capital'), 1)}% → "
            f"{_f(fix.get('pnl_pct_of_capital'), 1)}% |"
        )
    L.append("")
    L.append("### Sharpe interpretability guard (equity crossing zero)")
    L.append("")
    L.append(
        "``common/validation/metrics.py`` builds equity as "
        "``capital + cumsum(pnl)`` and takes ``pct_change()``. Once that series "
        "touches zero the returns flip sign and the Sharpe/DSR below stop being "
        "performance statements. Flagged here rather than silently reported."
    )
    L.append("")
    L.append(
        "| system | arm | min equity | final equity | equity crossed 0 | Sharpe interpretable |"
    )
    L.append("|---|---|---:|---:|---|---|")

    def _eqrow(name, pre, fix):
        for arm_label, d in (("pre-fix", pre), ("fixed", fix)):
            if not d or d.get("equity_min") is None:
                continue
            crossed = d.get("equity_crossed_zero")
            L.append(
                f"| {name} | {arm_label} | {_f(d.get('equity_min'), 0)} | "
                f"{_f(d.get('equity_final'), 0)} | "
                f"{'**yes**' if crossed else 'no'} | "
                f"{'**NO**' if crossed else 'yes'} |"
            )

    for name, blk in s["systems"].items():
        if blk.get("skipped"):
            continue
        fs = blk["trades_full_sample"]
        _eqrow(name, fs.get("prefix"), fs.get("fixed"))
    if s.get("integrated"):
        _eqrow(
            "Integrated (7)",
            s["integrated"].get("prefix_trades_full_sample"),
            s["integrated"].get("fixed_trades_full_sample"),
        )
    L.append("")

    # ---- validation ------------------------------------------------------ #
    L.append(
        "### CPCV / bootstrap / Deflated Sharpe — before (pre-fix) vs after (fixed)"
    )
    L.append("")
    L.append(
        "| system | arm | full-sample Sharpe | fold Sharpe mean ± std | "
        "fold min / max | folds > 0 | bootstrap 95% CI | P(SR≤0) | DSR (N) | verdict |"
    )
    L.append("|---|---|---:|---|---|---:|---|---:|---:|---|")

    def emit(name, reports):
        if not reports:
            L.append(f"| {name} | - | - | - | - | - | - | - | - | not evaluated |")
            return
        for arm_key, arm_label in (("prefix", "pre-fix"), ("fixed", "**fixed**")):
            a = _arm(reports.get(arm_key))
            if a is None:
                continue
            verdict = "PASS" if a["passed"] else "FAIL"
            L.append(
                f"| {name} | {arm_label} | {_f(a['full_sharpe'])} | "
                f"{_f(a['fold_mean'])} ± {_f(a['fold_std'])} | "
                f"{_f(a['fold_min'])} / {_f(a['fold_max'])} | "
                f"{_f(a['frac_pos'], 2)} | "
                f"[{_f(a['boot_lo'])}, {_f(a['boot_hi'])}] | "
                f"{_f(a['boot_p'])} | {_f(a['dsr'])} (N={a['n_comb']}) | {verdict} |"
            )

    for name, blk in s["systems"].items():
        if blk.get("skipped"):
            L.append(f"| {name} | - | - | - | - | - | - | - | - | {blk['skipped']} |")
            continue
        emit(name, blk.get("reports"))
    if s.get("integrated"):
        emit("**Integrated (7)**", s["integrated"])

    text = "\n".join(L)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
