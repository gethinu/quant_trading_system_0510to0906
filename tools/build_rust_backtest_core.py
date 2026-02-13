from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path


def _resolve_cargo_binary() -> str | None:
    found = shutil.which("cargo")
    if found:
        return found
    home = Path.home()
    candidates = [
        home / ".cargo" / "bin" / "cargo",
        home / ".cargo" / "bin" / "cargo.exe",
    ]
    for path in candidates:
        if path.exists():
            return str(path)
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Build debug binary instead of release",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Show full cargo output",
    )
    args = parser.parse_args()

    cargo = _resolve_cargo_binary()
    if not cargo:
        print("cargo is not installed or not on PATH.", file=sys.stderr)
        return 2

    root = Path(__file__).resolve().parents[1]
    crate_dir = root / "rust" / "integrated_backtest_core"
    if not crate_dir.exists():
        print(f"crate directory not found: {crate_dir}", file=sys.stderr)
        return 2

    cmd = [cargo, "build"]
    release_build = not args.debug
    if release_build:
        cmd.append("--release")

    proc = subprocess.run(
        cmd,
        cwd=str(crate_dir),
        capture_output=not args.verbose,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        if not args.verbose:
            if proc.stdout:
                print(proc.stdout, file=sys.stderr)
            if proc.stderr:
                print(proc.stderr, file=sys.stderr)
        return proc.returncode

    target_dir = crate_dir / "target" / ("release" if release_build else "debug")
    print(f"Build succeeded: {target_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
