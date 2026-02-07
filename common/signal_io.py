"""Signal CSV discovery and IO helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

import pandas as pd

from config.settings import get_settings

_EXIT_PREFIX = "signals_exit_"


def get_signals_dir(create_dirs: bool = False) -> Path:
    settings = get_settings(create_dirs=create_dirs)
    return Path(settings.outputs.signals_dir)


def find_latest_final_signals(signals_dir: Path) -> Path | None:
    if not signals_dir.exists():
        return None
    files = sorted(signals_dir.glob("signals_final_*.csv"))
    return files[-1] if files else None


def select_signal_files(signals_dir: Path, date_str: str) -> list[Path]:
    def _sort(paths: Iterable[Path]) -> list[Path]:
        return sorted(list(paths), key=lambda p: p.stat().st_mtime if p.exists() else 0)

    final_files = _sort(signals_dir.glob(f"signals_final_{date_str}*.csv"))
    if final_files:
        return [final_files[-1]]

    system_files = _sort(signals_dir.glob(f"signals_system*_{date_str}*.csv"))
    if system_files:
        return system_files

    fallback = [
        p
        for p in _sort(signals_dir.glob(f"signals_*{date_str}*.csv"))
        if _EXIT_PREFIX not in p.name
    ]
    return fallback


def read_signal_frames(files: Iterable[Path]) -> list[pd.DataFrame]:
    frames: list[pd.DataFrame] = []
    for f in files:
        try:
            df = pd.read_csv(f)
            frames.append(df)
        except Exception:
            continue
    return frames


__all__ = [
    "get_signals_dir",
    "find_latest_final_signals",
    "select_signal_files",
    "read_signal_frames",
]
