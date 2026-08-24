#!/usr/bin/env python3
"""Flag-gated CPCV / bootstrap / Deflated-Sharpe validation entrypoint.

This wires the anti-overfitting toolkit (:mod:`common.validation`) into the
existing backtest engines. It is **opt-in and OFF by default**: unless
``VALIDATION_ENABLED=1`` (or ``--force``) is set, the script prints guidance and
exits without running anything, so it can never affect the daily pipeline.

Modes
-----
* ``--trades PATH``   Evaluate an existing trades CSV (columns include
  ``exit_date``, ``pnl``) with a moving-block bootstrap + Deflated Sharpe.
* ``--integrated``    Build the integrated multi-system backtest over a symbol
  set and run full CPCV (purge + embargo) + bootstrap + DSR, plus a
  survivorship audit. Reuses ``build_system_states`` / ``run_integrated_backtest``
  unchanged.

Reports are written (dated) to ``results_csv/`` and ``logs/``.

Examples
--------
    VALIDATION_ENABLED=1 python -m scripts.run_validation \
        --trades results_csv/System1_trades.csv --capital 100000 --n-trials 20

    VALIDATION_ENABLED=1 python -m scripts.run_validation --integrated \
        --symbols AAPL,MSFT,NVDA,AMD,TSLA --limit 200 --n-groups 6 --k-test 2
"""

from __future__ import annotations

import argparse
import os
import sys

# Ensure repo root on path when run as a file.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def _resolve_dirs():
    results_dir = os.path.join(_ROOT, "results_csv")
    logs_dir = os.path.join(_ROOT, "logs")
    try:
        from config.settings import get_settings

        s = get_settings(create_dirs=False)
        results_dir = str(getattr(s.outputs, "results_csv_dir", results_dir))
        logs_dir = str(getattr(s.outputs, "logs_dir", logs_dir))
    except Exception:
        pass
    return results_dir, logs_dir


def _run_trades_mode(args) -> int:
    import pandas as pd

    from common.validation.evaluate import evaluate_trades

    df = pd.read_csv(args.trades)
    results_dir, logs_dir = _resolve_dirs()
    label = args.label or os.path.splitext(os.path.basename(args.trades))[0]
    report = evaluate_trades(
        df,
        args.capital,
        n_trials=args.n_trials,
        label=label,
        n_boot=args.n_boot,
        seed=args.seed,
    )
    written = report.save(results_dir, logs_dir)
    print(report.verdict())
    for k, v in written.items():
        print(f"  {k}: {v}")
    return 0


def _run_integrated_mode(args) -> int:
    from common.integrated_backtest import (  # noqa: F401
        build_system_states,
        run_integrated_backtest,
    )
    from common.validation.evaluate import make_integrated_runner, run_cpcv_evaluation
    from common.validation.survivorship import survivorship_guard

    symbols = [s.strip().upper() for s in (args.symbols or "").split(",") if s.strip()]
    if not symbols:
        # Fall back to the project's universe loader if available.
        try:
            from common.universe import load_universe_file

            symbols = load_universe_file() or []
        except Exception:
            symbols = []
    if args.limit:
        symbols = symbols[: args.limit]
    if not symbols:
        print("no symbols to evaluate (pass --symbols or provide a universe file)")
        return 2

    # Explicit survivorship guard (OFF/WARN/ENFORCE via SURVIVORSHIP_GUARD).
    survivorship_guard(symbols, root=_ROOT)

    states = build_system_states(symbols)
    run_on_dates, signal_dates = make_integrated_runner(states, args.capital)
    results_dir, logs_dir = _resolve_dirs()
    report = run_cpcv_evaluation(
        run_on_dates,
        signal_dates,
        args.capital,
        n_groups=args.n_groups,
        k_test=args.k_test,
        embargo_pct=args.embargo,
        label=args.label or "integrated",
        n_boot=args.n_boot,
        seed=args.seed,
        universe_symbols=symbols,
        survivorship_root=_ROOT,
        results_dir=results_dir,
        logs_dir=logs_dir,
    )
    print(report.verdict())
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--trades", help="evaluate an existing trades CSV")
    p.add_argument(
        "--integrated", action="store_true", help="run CPCV on the integrated engine"
    )
    p.add_argument("--symbols", help="comma-separated symbols (integrated mode)")
    p.add_argument("--limit", type=int, default=0, help="cap number of symbols")
    p.add_argument("--capital", type=float, default=100000.0)
    p.add_argument("--n-trials", dest="n_trials", type=int, default=1)
    p.add_argument("--n-groups", dest="n_groups", type=int, default=6)
    p.add_argument("--k-test", dest="k_test", type=int, default=2)
    p.add_argument("--embargo", type=float, default=0.01)
    p.add_argument("--n-boot", dest="n_boot", type=int, default=2000)
    p.add_argument("--seed", type=int, default=12345)
    p.add_argument("--label", default="")
    p.add_argument(
        "--force", action="store_true", help="run even if VALIDATION_ENABLED is unset"
    )
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    from common.validation.flags import validation_enabled

    if not (validation_enabled() or args.force):
        print(
            "validation is disabled. Set VALIDATION_ENABLED=1 (or pass --force) "
            "to run. This entrypoint never runs as part of the daily pipeline."
        )
        return 0

    if args.trades:
        return _run_trades_mode(args)
    if args.integrated:
        return _run_integrated_mode(args)

    print("nothing to do: pass --trades PATH or --integrated. See --help.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
