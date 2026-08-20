#!/usr/bin/env python3
"""Re-run the methodology-validation stack (CPCV / bootstrap / DSR) on the
limit-fill-corrected backtest engine, and reconstruct the pre-fix (inflated)
baseline on *identical* candidates so the before/after is apples-to-apples.

Why an A/B instead of "read the old reports": no per-system validation report
for System3/5/6 was ever produced (``results_csv/`` only ever held a 10-symbol
integrated run and a System1 run, both from 2026-08-11, and ``results_csv/`` is
gitignored so nothing durable survives). The honest before/after therefore has
to be *measured*, not quoted.

The fix (commit ``960487c``) is purely additive: each of System3/5/6 gained

    if not self._limit_entry_filled(df, entry_idx, entry_price, <side>):
        return None

and nothing else changed in the entry path. So forcing
``StrategyBase._limit_entry_filled`` to return ``True`` reproduces the pre-fix
``compute_entry`` **exactly** -- same candidates, same prices, same sizing --
which is what arm ``prefix`` does. Arm ``fixed`` runs the tree as-is.

Both arms share one ``build_system_states`` result, so candidate generation
(which the fix does not touch) is bit-identical across arms and every delta is
attributable to the fill check alone.

Usage
-----
    VALIDATION_ENABLED=1 python outputs/impl/limit_fill_fix/revalidate_limit_fill.py \
        --limit 1500 --n-groups 6 --k-test 2 --out <dir>
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
import warnings

warnings.filterwarnings("ignore")
logging.getLogger("streamlit").setLevel(logging.ERROR)

_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), *[".."] * 3))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

SYSTEMS = ["System1", "System2", "System3", "System4", "System5", "System6", "System7"]
LIMIT_SYSTEMS = {"System3", "System5", "System6"}  # the ones the fix touches


def _log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


class ForceFill:
    """Context manager: make every limit entry fill (== pre-fix behaviour)."""

    def __init__(self, active: bool):
        self.active = active
        self._orig = None

    def __enter__(self):
        if not self.active:
            return self
        from strategies.base_strategy import StrategyBase

        self._orig = StrategyBase._limit_entry_filled
        StrategyBase._limit_entry_filled = lambda self, df, i, p, s: True
        return self

    def __exit__(self, *exc):
        if self.active and self._orig is not None:
            from strategies.base_strategy import StrategyBase

            StrategyBase._limit_entry_filled = self._orig
        return False


def build_states(symbols):
    from common.integrated_backtest import build_system_states
    from common.utils_spy import get_spy_data_cached, get_spy_with_indicators

    spy = get_spy_with_indicators(get_spy_data_cached())
    _log(f"SPY rows={len(spy)} {spy.index.min().date()}..{spy.index.max().date()}")
    t0 = time.time()
    states = build_system_states(symbols, spy_df=spy)
    _log(f"build_system_states: {time.time() - t0:.1f}s")
    for st in states:
        n = sum(len(v) for v in st.candidates_by_date.values())
        _log(
            f"  {st.name} side={st.side} prepared={len(st.prepared)} "
            f"cand_dates={len(st.candidates_by_date)} candidates={n}"
        )
    return states


def normalize_list_candidates(states):
    """Convert any ``{date: [record, ...]}`` state to the engine's dict form.

    Both engines only inject ``entry_date`` when a date maps to a ``dict``
    (``{symbol: payload}``); a *list* of records is passed through untouched and
    every record is then dropped by ``df.index.get_loc(c["entry_date"])``.
    System3 is the only system that emits the list form (its records carry
    ``date``, not ``entry_date``), so it silently books **zero trades through
    both engines** -- a pre-existing schema gap unrelated to the limit-fill fix.

    This applies the repo's own ``normalize_candidates_by_date`` (the very
    normalizer core/system5 and core/system6 already call before returning), so
    System3 lands on exactly the same convention as System2/4/5/6: the
    candidate's date key becomes ``entry_date``. Nothing in production code is
    changed; this is a driver-side shim so that a System3 number can exist.
    """
    from common.system_candidates_utils import normalize_candidates_by_date

    touched = []
    for st in states:
        cbd = st.candidates_by_date
        if not cbd:
            continue
        if any(isinstance(v, list) for v in cbd.values()):
            st.candidates_by_date = normalize_candidates_by_date(cbd)
            touched.append(st.name)
    return touched


def prune_prepared(states):
    """Drop prepared frames for symbols no candidate references.

    Both engines only ever do ``data_dict.get(candidate["symbol"])``
    (``common/backtest_utils.py:200``, ``common/integrated_backtest.py:351``),
    so dropping unreferenced frames is result-preserving and turns a ~4,650
    symbol x 7 system working set into a few hundred frames.
    """
    freed = {}
    for st in states:
        used = set()
        for _dt, cands in st.candidates_by_date.items():
            if isinstance(cands, dict):
                used.update(str(s) for s in cands.keys())
            else:
                for rec in cands or []:
                    sym = rec.get("symbol")
                    if sym:
                        used.add(str(sym))
        before = len(st.prepared)
        st.prepared = {k: v for k, v in st.prepared.items() if k in used}
        freed[st.name] = (before, len(st.prepared))
    return freed


def trade_stats(trades, capital=None):
    """Win rate / mean per-trade return, comparable to the fix-note table."""
    if trades is None or trades.empty:
        return {"n_trades": 0}
    import numpy as np

    pnl = trades["pnl"].astype(float)
    out = {
        "n_trades": int(len(trades)),
        "win_rate": round(float((pnl > 0).mean()), 4),
        "total_pnl": round(float(pnl.sum()), 2),
    }
    if "entry_price" in trades.columns and "shares" in trades.columns:
        cost = trades["entry_price"].astype(float).abs() * trades["shares"].astype(float)
        r = np.where(cost > 0, pnl / cost.replace(0, np.nan), np.nan)
        r = r[~np.isnan(r)]
        if r.size:
            out["mean_trade_return"] = round(float(r.mean()), 6)
            out["median_trade_return"] = round(float(np.median(r)), 6)
    if capital:
        # Sharpe/DSR are only meaningful while the notional equity series
        # (capital + cumsum(pnl), how common/validation/metrics.py builds it)
        # stays positive. Once it crosses zero, pct_change flips sign and the
        # resulting Sharpe is an artifact, not a performance statement.
        from common.validation.metrics import equity_from_trades

        eq = equity_from_trades(trades, float(capital))
        out["equity_min"] = round(float(eq.min()), 2)
        out["equity_final"] = round(float(eq.iloc[-1]), 2)
        out["pnl_pct_of_capital"] = round(float(pnl.sum()) / float(capital) * 100, 2)
        out["equity_crossed_zero"] = bool(eq.min() <= 0)
    return out


def fill_stats(state, capital):
    """Full-sample trade counts + win rate under both arms (diagnostic)."""
    from common.backtest_utils import simulate_trades_with_risk

    out = {}
    for arm, force in (("prefix", True), ("fixed", False)):
        with ForceFill(force):
            trades, _ = simulate_trades_with_risk(
                state.candidates_by_date,
                state.prepared,
                capital,
                state.strategy,
                side=state.side,
            )
        out[arm] = trade_stats(trades, capital)
    pre = out["prefix"].get("n_trades", 0)
    fix = out["fixed"].get("n_trades", 0)
    out["fill_rate"] = round(fix / pre, 4) if pre else None
    return out


def run_system(state, capital, args, results_dir, logs_dir, stamp):
    from common.validation.evaluate import (
        make_single_system_runner,
        run_cpcv_evaluation,
    )

    rows = {}
    for arm, force in (("prefix", True), ("fixed", False)):
        label = f"limitfill_{arm}_{state.name.lower()}"
        with ForceFill(force):
            run_on_dates, signal_dates = make_single_system_runner(
                state.candidates_by_date,
                state.prepared,
                capital,
                state.strategy,
                side=state.side,
            )
            if len(set(signal_dates)) < args.n_groups:
                _log(
                    f"  {state.name}/{arm}: only {len(set(signal_dates))} "
                    "signal dates -> SKIP"
                )
                return None
            t0 = time.time()
            rep = run_cpcv_evaluation(
                run_on_dates,
                signal_dates,
                capital,
                n_groups=args.n_groups,
                k_test=args.k_test,
                embargo_pct=args.embargo,
                label=label,
                n_boot=args.n_boot,
                seed=args.seed,
                results_dir=results_dir,
                logs_dir=logs_dir,
            )
        _log(f"  {state.name}/{arm}: {rep.verdict()}  ({time.time() - t0:.1f}s)")
        rows[arm] = rep.to_dict()
        with open(
            os.path.join(args.out, f"{label}_{stamp}.json"), "w", encoding="utf-8"
        ) as fh:
            json.dump(rep.to_dict(), fh, ensure_ascii=False, indent=2, default=str)
    return rows


def run_integrated(states, capital, args, results_dir, logs_dir, stamp, symbols=None):
    from common.validation.evaluate import make_integrated_runner, run_cpcv_evaluation

    rows = {}
    for arm, force in (("prefix", True), ("fixed", False)):
        label = f"limitfill_{arm}_integrated"
        with ForceFill(force):
            run_on_dates, signal_dates = make_integrated_runner(states, capital)
            full = run_on_dates(set(signal_dates))
            rows[f"{arm}_trades_full_sample"] = trade_stats(full, capital)
            _log(f"  integrated/{arm} full-sample: {rows[f'{arm}_trades_full_sample']}")
            t0 = time.time()
            rep = run_cpcv_evaluation(
                run_on_dates,
                signal_dates,
                capital,
                n_groups=args.n_groups,
                k_test=args.k_test,
                embargo_pct=args.embargo,
                label=label,
                n_boot=args.n_boot,
                seed=args.seed,
                universe_symbols=symbols,
                survivorship_root=_ROOT,
                results_dir=results_dir,
                logs_dir=logs_dir,
            )
        _log(f"  integrated/{arm}: {rep.verdict()}  ({time.time() - t0:.1f}s)")
        rows[arm] = rep.to_dict()
        with open(
            os.path.join(args.out, f"{label}_{stamp}.json"), "w", encoding="utf-8"
        ) as fh:
            json.dump(rep.to_dict(), fh, ensure_ascii=False, indent=2, default=str)
    return rows


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--limit", type=int, default=0, help="cap universe size (0 = all)")
    p.add_argument("--capital", type=float, default=100000.0)
    p.add_argument("--n-groups", dest="n_groups", type=int, default=6)
    p.add_argument("--k-test", dest="k_test", type=int, default=2)
    p.add_argument("--embargo", type=float, default=0.01)
    p.add_argument("--n-boot", dest="n_boot", type=int, default=2000)
    p.add_argument("--seed", type=int, default=12345)
    p.add_argument("--out", required=True, help="directory for the A/B artifacts")
    p.add_argument("--systems", default="", help="comma list, default all 7")
    p.add_argument("--skip-integrated", action="store_true")
    p.add_argument(
        "--normalize-list-candidates",
        action="store_true",
        help="apply the repo's normalize_candidates_by_date to list-form states "
        "(System3) so the engines see an entry_date; see the docstring",
    )
    p.add_argument("--states-cache", default="", help="pickle path to reuse states")
    args = p.parse_args(argv)

    from common.validation.flags import validation_enabled

    if not validation_enabled():
        print("VALIDATION_ENABLED is not set; refusing to run (flag-gated by design).")
        return 2

    os.makedirs(args.out, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    results_dir = os.path.join(_ROOT, "results_csv")
    logs_dir = os.path.join(_ROOT, "logs")

    from common.universe import load_universe_file

    symbols = load_universe_file() or []
    if args.limit:
        symbols = symbols[: args.limit]
    _log(f"universe: {len(symbols)} symbols")

    import pickle

    if args.states_cache and os.path.exists(args.states_cache):
        _log(f"loading cached states from {args.states_cache}")
        with open(args.states_cache, "rb") as fh:
            states = pickle.load(fh)
    else:
        states = build_states(symbols)

    normalized = []
    if args.normalize_list_candidates:
        normalized = normalize_list_candidates(states)
        _log(f"list-form candidate states normalized: {normalized or 'none'}")

    freed = prune_prepared(states)
    _log(f"prepared pruned to referenced symbols: {freed}")

    if args.states_cache and not os.path.exists(args.states_cache):
        # Cached *after* normalize+prune, so the pickle is small. The cache is
        # therefore specific to the flags it was built with -- delete it if you
        # change --normalize-list-candidates or the universe.
        with open(args.states_cache, "wb") as fh:
            pickle.dump(states, fh, protocol=5)
        _log(f"states cached -> {args.states_cache}")

    by_name = {st.name: st for st in states}
    wanted = [s.strip() for s in args.systems.split(",") if s.strip()] or SYSTEMS

    summary = {
        "stamp": stamp,
        "universe_size": len(symbols),
        "capital": args.capital,
        "cpcv": {
            "n_groups": args.n_groups,
            "k_test": args.k_test,
            "embargo_pct": args.embargo,
            "n_boot": args.n_boot,
            "seed": args.seed,
        },
        "normalize_list_candidates": bool(args.normalize_list_candidates),
        "normalized_states": normalized,
        "env": {
            k: os.environ.get(k)
            for k in (
                "VALIDATION_ENABLED",
                "SYSTEM6_FORCE_LATEST_ONLY",
                "FULL_SCAN_TODAY",
                "SURVIVORSHIP_GUARD",
            )
        },
        "git_head": subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            cwd=_ROOT,
        ).stdout.strip(),
        "systems": {},
        "integrated": None,
    }

    for name in wanted:
        st = by_name.get(name)
        if st is None:
            continue
        n_cand = sum(len(v) for v in st.candidates_by_date.values())
        if n_cand == 0:
            _log(f"{name}: 0 candidates -> SKIP")
            summary["systems"][name] = {"skipped": "no candidates"}
            continue
        _log(f"{name}: candidates={n_cand} dates={len(st.candidates_by_date)}")
        fs = fill_stats(st, args.capital)
        _log(
            f"  trades prefix={fs['prefix']} fixed={fs['fixed']} "
            f"fill_rate={fs['fill_rate']}"
        )
        rows = run_system(st, args.capital, args, results_dir, logs_dir, stamp)
        summary["systems"][name] = {
            "is_limit_system": name in LIMIT_SYSTEMS,
            "n_candidates": n_cand,
            "n_signal_dates": len(st.candidates_by_date),
            "trades_full_sample": fs,
            "reports": rows,
        }

    if not args.skip_integrated:
        _log("integrated (all 7 systems together)")
        summary["integrated"] = run_integrated(
            states, args.capital, args, results_dir, logs_dir, stamp, symbols=symbols
        )

    out_path = os.path.join(args.out, f"revalidate_summary_{stamp}.json")
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=2, default=str)
    _log(f"summary -> {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
