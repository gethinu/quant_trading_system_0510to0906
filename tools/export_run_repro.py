#!/usr/bin/env python3
"""Export a run payload for reproduction.

This utility collects:
 - diagnostics snapshot (latest or provided)
 - persisted per-system candidate frames (feather -> CSV)
 - final signals CSV (latest)
 - optional logs matching the run date

It writes them into an output directory and (optionally) a ZIP archive
that can be attached to an issue or shared with engineers for repro.

Usage:
  python tools/export_run_repro.py [--snapshot PATH] [--out OUTDIR] [--no-zip]

Examples:
    python tools/export_run_repro.py
            python tools/export_run_repro.py --snapshot repro_payloads/snap.json

Notes:
 - The script is defensive: if Feather cannot be read it will copy the
   original file instead of converting.
 - The ZIP file is created by default; pass --no-zip to skip compression.
"""
from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
import shutil
from typing import Optional

import pandas as pd

from common.io_utils import df_to_csv, write_json
from common.signal_io import find_latest_final_signals, get_signals_dir


def find_latest_snapshot(dirpath: Path) -> Optional[Path]:
    if not dirpath.exists():
        return None
    files = sorted(dirpath.glob("diagnostics_snapshot_*.json"))
    return files[-1] if files else None


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(
        description=(
            "Export repro payload for allocation runs. Collect diagnostics, "
            "per-system persisted frames and final signals into a single folder."
        )
    )
    parser.add_argument("--snapshot", help="Diagnostics snapshot path (optional)")
    default_out = str(repo_root / "repro_payloads")
    parser.add_argument("--out", default=default_out, help="Output directory")
    parser.add_argument("--no-zip", action="store_true", help="Do not compress to ZIP")
    args = parser.parse_args()

    snapshot_dir = repo_root / "results_csv_test" / "diagnostics_test"
    per_system_dir = repo_root / "results_csv_test"
    try:
        signals_dir = get_signals_dir(create_dirs=False)
    except Exception:
        signals_dir = repo_root / "data_cache" / "signals"
    logs_dir = repo_root / "logs"

    outdir = Path(args.out)
    if outdir.exists():
        # avoid accidental overwrite: create timestamped folder
        suffix = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        outdir = outdir.with_name(outdir.name + "_" + suffix)
    outdir.mkdir(parents=True, exist_ok=True)

    # Choose snapshot
    chosen_snapshot: Optional[Path]
    if args.snapshot:
        chosen_snapshot = Path(args.snapshot)
        if not chosen_snapshot.exists():
            print(f"Provided snapshot not found: {chosen_snapshot}")
            return 2
    else:
        chosen_snapshot = find_latest_snapshot(snapshot_dir)

    meta: dict = {
        "collected_at": datetime.utcnow().isoformat(),
        "snapshot": None,
        "per_system_files": [],
        "final_signals": None,
    }

    # Copy snapshot
    if chosen_snapshot is not None:
        try:
            target = outdir / chosen_snapshot.name
            shutil.copy2(chosen_snapshot, target)
            meta["snapshot"] = str(target.relative_to(repo_root))
            print(f"Copied snapshot: {chosen_snapshot} -> {target}")
        except Exception as e:
            print(f"Could not copy snapshot {chosen_snapshot}: {e}")
    else:
        print("No diagnostics snapshot found; continuing without it.")

    # Export per-system feathers -> CSV (if present)
    feather_files = sorted(per_system_dir.glob("per_system_*.feather"))
    if not feather_files:
        print("No per_system_*.feather files found in results_csv_test/")
    for p in feather_files:
        name = p.stem.replace("per_system_", "")
        out_csv = outdir / f"per_system_{name}.csv"
        try:
            df = pd.read_feather(p)
            # write CSV and small head sample
            # Explicitly write CSVs with UTF-8 to ensure cross-platform
            df_to_csv(df, out_csv, index=False)
            head_csv = outdir / f"per_system_{name}.head.csv"
            head_csv.write_text(df.head(5).to_csv(index=False), encoding="utf-8")
            meta_entry = {
                "system": name,
                "rows": int(len(df)),
                "csv": str(out_csv.relative_to(repo_root)),
            }
            meta["per_system_files"].append(meta_entry)
            print(f"Exported {p} -> {out_csv} ({len(df)} rows)")
        except Exception as e:
            # fallback: copy original file for debugging
            try:
                fallback = outdir / p.name
                shutil.copy2(p, fallback)
                meta_entry = {
                    "system": name,
                    "feather": str(fallback.relative_to(repo_root)),
                }
                meta["per_system_files"].append(meta_entry)
                print(f"Could not read feather {p}, copied raw file to {fallback}: {e}")
            except Exception as e2:
                print(f"Failed to copy feather {p}: {e2}")

    # Copy latest final signals CSV if present
    final_signals = find_latest_final_signals(signals_dir)
    if final_signals is not None:
        try:
            dst = outdir / final_signals.name
            shutil.copy2(final_signals, dst)
            meta["final_signals"] = str(dst.relative_to(repo_root))
            print(f"Copied final signals CSV: {final_signals} -> {dst}")
        except Exception as e:
            print(f"Could not copy final signals CSV {final_signals}: {e}")
    else:
        print("No final signals CSV found in data_cache/signals/")

    # Try to gather logs for the snapshot date (best-effort)
    try:
        if chosen_snapshot is not None:
            # attempt to extract date from snapshot filename
            sname = chosen_snapshot.name
            # look for YYYYMMDD pattern
            import re

            m = re.search(r"(\d{8})", sname)
            if m:
                datecode = m.group(1)
                log_glob = f"*{datecode}*"
                matched = list(sorted(logs_dir.glob(log_glob)))
                for f in matched[:5]:
                    try:
                        shutil.copy2(f, outdir / f.name)
                        print(f"Copied log: {f} -> {outdir / f.name}")
                    except Exception:
                        pass
    except Exception:
        pass

    # Write meta.json
    try:
        meta_path = outdir / "meta.json"
        write_json(meta_path, meta, ensure_ascii=False, indent=2)
        print(f"Wrote metadata: {meta_path}")
    except Exception as e:
        print(f"Failed to write meta.json: {e}")

    # Optionally compress
    zip_path = None
    if not args.no_zip:
        try:
            archive = shutil.make_archive(str(outdir), "zip", root_dir=str(outdir))
            zip_path = Path(archive)
            print(f"Created ZIP archive: {zip_path}")
        except Exception as e:
            print(f"Failed to create ZIP archive: {e}")

    print(f"Run payload exported to: {outdir}")
    if zip_path:
        print(f"ZIP file: {zip_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
