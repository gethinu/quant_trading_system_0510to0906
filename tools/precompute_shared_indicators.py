#!/usr/bin/env python3
"""Warm shared indicators by rebuilding rolling cache with indicators.

This tool is used by the scheduler job `precompute_shared_indicators` and
simply delegates to `scripts/build_rolling_with_indicators.py` so the
rolling cache always contains indicator-enriched data for daily signals.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from scripts.build_rolling_with_indicators import main as build_main  # noqa: E402


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Rebuild rolling cache with indicators (shared precompute)",
    )
    parser.add_argument(
        "--symbols",
        nargs="+",
        help="Process only specified symbols (default: all in manifest)",
    )
    parser.add_argument(
        "--target-days",
        type=int,
        help="Rolling lookback days (default: settings base+buffer)",
    )
    parser.add_argument(
        "--max-symbols",
        type=int,
        help="Maximum number of symbols to process (default: settings)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        help="Max worker threads (default: settings/auto)",
    )
    parser.add_argument(
        "--nan-warnings",
        action="store_true",
        help="Enable indicator NaN warnings",
    )
    parser.add_argument(
        "--no-adaptive",
        action="store_true",
        help="Disable adaptive worker tuning",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    forward: list[str] = []
    if args.symbols:
        forward.extend(["--symbols", *args.symbols])
    if args.target_days is not None:
        forward.extend(["--target-days", str(args.target_days)])
    if args.max_symbols is not None:
        forward.extend(["--max-symbols", str(args.max_symbols)])
    if args.workers is not None:
        forward.extend(["--workers", str(args.workers)])
    if args.nan_warnings:
        forward.append("--nan-warnings")
    if args.no_adaptive:
        forward.append("--no-adaptive")

    return build_main(forward)


if __name__ == "__main__":
    raise SystemExit(main())
