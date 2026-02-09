"""Apply manual entry/exit confirmations to position tracker.

Usage:
  python tools/position_tracker_apply.py
  python tools/position_tracker_apply.py --date 2026-02-09
  python tools/position_tracker_apply.py --entries data/entry_confirmations_YYYY-MM-DD.csv --exits data/exit_confirmations_YYYY-MM-DD.csv
"""

from __future__ import annotations

from datetime import datetime
import logging
from pathlib import Path
import sys

import pandas as pd

from common.position_tracker import remove_positions, update_positions_from_signals


def _default_paths(date_str: str) -> tuple[Path, Path]:
    data_dir = Path(__file__).resolve().parents[1] / "data"
    return (
        data_dir / f"entry_confirmations_{date_str}.csv",
        data_dir / f"exit_confirmations_{date_str}.csv",
    )


def _load_csv(path: Path) -> pd.DataFrame:
    try:
        return pd.read_csv(path)
    except Exception as exc:
        raise RuntimeError(f"Failed to read CSV: {path} ({exc})") from exc


def _normalize_entries(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    cols = {c.lower(): c for c in df.columns}
    required = {"symbol", "system", "entry_date", "entry_price"}
    if not required.issubset(cols.keys()):
        missing = ", ".join(sorted(required - set(cols.keys())))
        raise ValueError(f"entry_confirmations missing columns: {missing}")
    return df


def _normalize_exits(df: pd.DataFrame) -> list[str]:
    if df is None or df.empty:
        return []
    cols = {c.lower(): c for c in df.columns}
    sym_col = cols.get("symbol") or cols.get("ticker")
    if not sym_col:
        raise ValueError("exit_confirmations missing column: symbol")
    symbols = [
        str(sym).upper()
        for sym in df[sym_col].tolist()
        if str(sym).strip()
    ]
    return symbols


def apply_confirmations(
    entries_path: Path | None,
    exits_path: Path | None,
    *,
    dry_run: bool = False,
) -> None:
    entries_df = pd.DataFrame()
    exits_symbols: list[str] = []

    if entries_path and entries_path.exists():
        entries_df = _normalize_entries(_load_csv(entries_path))
    elif entries_path:
        logging.info("entries CSV not found: %s", entries_path)

    if exits_path and exits_path.exists():
        exits_symbols = _normalize_exits(_load_csv(exits_path))
    elif exits_path:
        logging.info("exits CSV not found: %s", exits_path)

    logging.info(
        "entries=%d exits=%d (dry_run=%s)",
        0 if entries_df is None else len(entries_df),
        len(exits_symbols),
        dry_run,
    )

    if dry_run:
        return

    if entries_df is not None and not entries_df.empty:
        update_positions_from_signals(entries_df)
        logging.info("position tracker updated: %d entries", len(entries_df))
    if exits_symbols:
        remove_positions(exits_symbols)
        logging.info("position tracker updated: %d exits", len(exits_symbols))


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--date",
        default=None,
        help="Date suffix (YYYY-MM-DD) for default confirmation files.",
    )
    parser.add_argument("--entries", default=None, help="Entry confirmations CSV path.")
    parser.add_argument("--exits", default=None, help="Exit confirmations CSV path.")
    parser.add_argument("--dry-run", action="store_true", help="Do not write updates.")

    args = parser.parse_args(argv)

    date_str = args.date or datetime.today().strftime("%Y-%m-%d")
    default_entries, default_exits = _default_paths(date_str)
    entries_path = Path(args.entries) if args.entries else default_entries
    exits_path = Path(args.exits) if args.exits else default_exits

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    apply_confirmations(entries_path, exits_path, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
