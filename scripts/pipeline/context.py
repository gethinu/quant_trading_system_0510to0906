"""TodayRunContext - Execution context for today signals pipeline.

Extracted from run_all_systems_today.py for better modularity.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

import pandas as pd

if TYPE_CHECKING:
    from common.cache_manager import CacheManager


@dataclass(slots=True)
class TodayRunContext:
    """保持共有状態とコールバックを集約した当日シグナル実行用コンテキスト。"""

    settings: Any
    cache_manager: "CacheManager"
    signals_dir: Path
    cache_dir: Path
    slots_long: int | None = None
    slots_short: int | None = None
    capital_long: float | None = None
    capital_short: float | None = None
    save_csv: bool = False
    csv_name_mode: str | None = None
    notify: bool = True
    log_callback: Callable[[str], None] | None = None
    progress_callback: Callable[[int, int, str], None] | None = None
    per_system_progress: Callable[[str, str], None] | None = None
    symbol_data: dict[str, pd.DataFrame] | None = None
    parallel: bool = False
    run_start_time: datetime = field(default_factory=datetime.now)
    start_equity: float = 0.0
    run_id: str = ""
    today: pd.Timestamp | None = None
    symbol_universe: list[str] = field(default_factory=list)
    basic_data: dict[str, pd.DataFrame] = field(default_factory=dict)
    base_cache: dict[str, pd.DataFrame] = field(default_factory=dict)
    system_filters: dict[str, list[str]] = field(default_factory=dict)
    per_system_frames: dict[str, pd.DataFrame] = field(default_factory=dict)
    final_signals: pd.DataFrame | None = None
    system_diagnostics: dict[str, dict[str, Any]] = field(default_factory=dict)
    freshness_summary: dict[str, Any] = field(default_factory=dict)
    # テスト高速化オプション
    test_mode: str | None = None  # mini/quick/sample
    skip_external: bool = False  # 外部API呼び出しをスキップ
    # latest_only グローバル制御
    signal_base_day: pd.Timestamp | None = None
    entry_day: pd.Timestamp | None = None
    max_date_lag_days: int = 1
    run_namespace: str | None = None
