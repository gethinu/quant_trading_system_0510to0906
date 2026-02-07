from __future__ import annotations

import argparse
import csv
from pathlib import Path
import sys
from typing import Optional

# Ensure project root on sys.path for local imports when run directly
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from config.settings import get_settings
except Exception:
    get_settings = None  # fallback later

try:
    from common.signal_io import get_signals_dir
except Exception:
    get_signals_dir = None


def _safe_count_data_rows(csv_path: Path) -> int:
    """Count data rows (excluding header) from a CSV file.
    Returns 0 if file missing or unreadable.
    """
    try:
        with csv_path.open("r", encoding="utf-8", newline="") as f:
            reader = csv.reader(f)
            # consume header if present
            try:
                next(reader)
            except StopIteration:
                return 0
            return sum(1 for _ in reader)
    except Exception:
        return 0


def _find_validation_report(results_dir: Path, date_str: str) -> Optional[Path]:
    # Prefer the conventional subfolder first
    candidate = results_dir / "validation" / f"validation_report_{date_str}.json"
    if candidate.exists():
        return candidate
    # Fallback: recurse results_dir
    for p in results_dir.rglob(f"validation_report_{date_str}.json"):
        return p
    return None


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Show latest signals_final CSV info (path, data rows) and its "
            "validation report."
        )
    )
    parser.add_argument(
        "--date",
        help="Target date in YYYY-MM-DD (if omitted, use latest by mtime).",
        default=None,
    )
    parser.add_argument(
        "--latest",
        type=int,
        default=1,
        help="Show top-N latest files (ignored if --date is specified).",
    )
    args = parser.parse_args()

    # Settings with safe fallbacks
    signals_dir: Path
    results_dir: Path
    if get_settings is not None:
        s = get_settings(create_dirs=False)
        if get_signals_dir is not None:
            try:
                signals_dir = get_signals_dir(create_dirs=False)
            except Exception:
                signals_dir = Path(s.outputs.signals_dir)
        else:
            signals_dir = Path(s.outputs.signals_dir)
        results_dir = Path(s.outputs.results_csv_dir)
    else:
        # Fallback defaults
        signals_dir = ROOT / "data_cache" / "signals"
        results_dir = ROOT / "results_csv"

    if not signals_dir.exists():
        print(f"Signals directory not found: {signals_dir}")
        return 1

    # If a specific date is provided, show exactly that file
    if args.date:
        target = signals_dir / f"signals_final_{args.date}.csv"
        if not target.exists():
            print(f"Not found: {target}")
            return 2
        rows = _safe_count_data_rows(target)
        validation_path = _find_validation_report(results_dir, args.date)
        print(f"File: {target}")
        print(f"Rows: {rows}")
        vr = str(validation_path) if validation_path is not None else "(not found)"
        print(f"ValidationReport: {vr}")
        return 0

    # Otherwise, list by LastWriteTime (top-N)
    files = sorted(
        signals_dir.glob("signals_final_*.csv"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not files:
        print(f"No signals_final_*.csv found under: {signals_dir}")
        return 2

    show_n = max(1, int(args.latest))
    for i, f in enumerate(files[:show_n], start=1):
        rows = _safe_count_data_rows(f)
        date_token = f.stem.replace("signals_final_", "")
        validation_path = _find_validation_report(results_dir, date_token)
        prefix = "LatestFile" if i == 1 else f"File#{i}"
        print(f"{prefix}: {f}")
        print(f"Rows: {rows}")
        vr = str(validation_path) if validation_path is not None else "(not found)"
        print(f"ValidationReport: {vr}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
