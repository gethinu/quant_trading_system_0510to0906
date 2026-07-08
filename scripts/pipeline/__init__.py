"""Pipeline package for today signals generation.

This package contains modular components extracted from run_all_systems_today.py
for better maintainability and testability.

Public API (backward compatible):
- compute_today_signals: Main signal computation function
- TodayRunContext: Execution context dataclass
- LightweightBenchmark: Performance benchmarking
- StageReporter: Progress reporting
- BaseCachePool: Cache management
- logging_utils: Logging helpers (format_log_prefix, safe_print, etc.)
"""

from __future__ import annotations

# Re-export public APIs for backward compatibility
from scripts.pipeline.benchmark import LightweightBenchmark
from scripts.pipeline.cache_pool import BaseCachePool
from scripts.pipeline.context import TodayRunContext
from scripts.pipeline.logging_utils import (
    GLOBAL_SKIP_KEYWORDS,
    INDICATOR_SKIP_KEYWORDS,
    UI_ONLY_SKIP_KEYWORDS,
    build_structured_log_object,
    console_supports_utf8,
    format_log_prefix,
    safe_print,
    should_skip_log,
    should_skip_ui_log,
    strip_emojis,
)
from scripts.pipeline.stage_reporter import (
    StageReporter,
    register_stage_callback,
    register_stage_exit_callback,
    register_universe_target_callback,
)

__all__ = [
    "LightweightBenchmark",
    "BaseCachePool",
    "TodayRunContext",
    "StageReporter",
    "register_stage_callback",
    "register_stage_exit_callback",
    "register_universe_target_callback",
    # logging_utils
    "GLOBAL_SKIP_KEYWORDS",
    "UI_ONLY_SKIP_KEYWORDS",
    "INDICATOR_SKIP_KEYWORDS",
    "format_log_prefix",
    "safe_print",
    "should_skip_log",
    "should_skip_ui_log",
    "strip_emojis",
    "console_supports_utf8",
    "build_structured_log_object",
]
