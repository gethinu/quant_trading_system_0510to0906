"""Pipeline logging utilities - extracted from run_all_systems_today.py.

This module provides centralized logging functionality for the pipeline,
including console output formatting, UI callback integration, and file logging.
"""

from __future__ import annotations

# unicodedata reserved for future use
from contextvars import ContextVar
from datetime import datetime
import re
import sys
import time
from typing import Any

# --- Constants for log filtering ---

# Keywords to skip from all logs (CLI/UI)
GLOBAL_SKIP_KEYWORDS = (
    "バッチ時間",
    "batch time",
    "銘柄:",
)

# Keywords to skip from UI only
UI_ONLY_SKIP_KEYWORDS = (
    "進捗",
    "候補抽出",
    "候補日数",
)

# Indicator log keywords (conditionally hidden)
INDICATOR_SKIP_KEYWORDS = (
    "インジケーター計算",
    "指標計算",
    "共有指標",
    "指標データロード",
    "📊 指標計算",
    "🧮 共有指標",
)

# Emoji regex pattern for stripping
EMOJI_PATTERN = re.compile(
    "["
    "\U0001f600-\U0001f64f"
    "\U0001f300-\U0001f5ff"
    "\U0001f680-\U0001f6ff"
    "\U0001f1e0-\U0001f1ff"
    "\U00002702-\U000027b0"
    "\U000024c2-\U0001f251"
    "]+",
    flags=re.UNICODE,
)

# Context variable for log forwarding state
LOG_FORWARDING: ContextVar[bool] = ContextVar("LOG_FORWARDING", default=False)


def console_supports_utf8() -> bool:
    """Check if current console supports UTF-8."""
    try:
        encoding = getattr(sys.stdout, "encoding", "") or ""
        return encoding.lower().replace("-", "").startswith("utf")
    except Exception:
        return False


def strip_emojis(text: str) -> str:
    """Remove emoji and special Unicode characters from text."""
    try:
        cleaned = EMOJI_PATTERN.sub("", str(text))
        return "".join(c for c in cleaned if ord(c) < 0x10000)
    except Exception:
        return str(text)


def format_log_prefix(
    start_ts: float | None,
    level: str = "INFO",
    error_code: str | None = None,
    no_timestamp: bool = False,
) -> str:
    """Format log prefix with timestamp and elapsed time.

    Args:
        start_ts: Start timestamp for elapsed time calculation
        level: Log level (INFO, WARNING, ERROR, DEBUG)
        error_code: Optional error code
        no_timestamp: If True, skip timestamp prefix

    Returns:
        Formatted prefix string
    """
    if no_timestamp:
        prefix = ""
    else:
        now = time.strftime("%H:%M:%S")
        elapsed = 0 if start_ts is None else max(0, time.time() - start_ts)
        m, s = divmod(int(elapsed), 60)
        prefix = f"[{now} | {m}分{s:02d}秒] "

    if level != "INFO":
        prefix += f"[{level}] "
    if error_code:
        prefix += f"[{error_code}] "

    return prefix


def safe_print(message: str, prefix: str = "") -> None:
    """Print message to console with encoding fallback.

    Args:
        message: Message to print
        prefix: Optional prefix
    """
    out = f"{prefix}{message}"
    try:
        print(out, flush=True)
    except UnicodeEncodeError:
        try:
            encoding = getattr(sys.stdout, "encoding", "") or "utf-8"
            safe = out.encode(encoding, errors="replace").decode(
                encoding, errors="replace"
            )
            print(safe, flush=True)
        except Exception:
            try:
                safe = out.encode("ascii", errors="replace").decode(
                    "ascii", errors="replace"
                )
                print(safe, flush=True)
            except Exception:
                pass


def should_skip_log(
    msg: str,
    skip_keywords: tuple[str, ...],
    hide_indicator_logs: bool = False,
) -> bool:
    """Check if log message should be skipped based on keywords.

    Args:
        msg: Log message
        skip_keywords: Keywords to check
        hide_indicator_logs: If True, also skip indicator-related logs

    Returns:
        True if message should be skipped
    """
    all_skip = skip_keywords + (INDICATOR_SKIP_KEYWORDS if hide_indicator_logs else ())
    return any(k in str(msg) for k in all_skip)


def should_skip_ui_log(msg: str, ui_skip_keywords: tuple[str, ...]) -> bool:
    """Check if UI log should be skipped.

    Args:
        msg: Log message
        ui_skip_keywords: Keywords that skip UI output

    Returns:
        True if UI output should be skipped
    """
    return any(k in str(msg) for k in ui_skip_keywords)


# Phase patterns for structured logging
PHASE_PATTERNS: list[tuple[str, list[str]]] = [
    ("universe", [r"universe", r"load symbols", r"symbol universe"]),
    ("indicators", [r"indicator", r"precompute", r"adx", r"rsi"]),
    ("filter", [r"filter", r"phase2 filter", r"screening"]),
    ("setup", [r"setup", r"prepare setup"]),
    ("ranking", [r"ranking", r"rank "]),
    ("signals", [r" signal", r"signals", r"generate signal"]),
    ("allocation", [r"allocation", r"alloc ", r"allocating", r"final allocation"]),
]


def extract_system_from_message(msg: str) -> str | None:
    """Extract system identifier from log message.

    Args:
        msg: Log message

    Returns:
        System identifier like 'system1' or None
    """
    m = re.search(r"\bSystem([1-9]|1[0-9])\b", str(msg))
    return f"system{m.group(1)}" if m else None


def extract_phase_from_message(msg: str) -> str | None:
    """Extract phase identifier from log message.

    Args:
        msg: Log message

    Returns:
        Phase identifier or None
    """
    lower = str(msg).lower()
    for phase, patterns in PHASE_PATTERNS:
        if any(pat in lower for pat in patterns):
            return phase
    return None


def extract_phase_status(msg: str) -> str | None:
    """Extract phase status (start/end) from log message.

    Args:
        msg: Log message

    Returns:
        'start', 'end', or None
    """
    lower = str(msg).lower()
    if re.search(r"\b(start|begin|開始)\b", lower):
        return "start"
    if re.search(r"\b(done|complete|completed|終了|end|finished)\b", lower):
        return "end"
    return None


def build_structured_log_object(
    msg: str,
    start_ts: float,
    last_phases: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Build structured log object for NDJSON/UI output.

    Args:
        msg: Log message
        start_ts: Process start timestamp
        last_phases: Dict tracking last phase per system

    Returns:
        Structured log object
    """
    now = time.time()
    iso = datetime.utcfromtimestamp(now).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    system = extract_system_from_message(msg)
    phase = extract_phase_from_message(msg)
    phase_status = extract_phase_status(msg) if phase else None

    # Use last phase if current message only has end status
    if system and not phase and last_phases:
        if re.search(
            r"\b(done|complete|completed|終了|end|finished)\b", str(msg).lower()
        ):
            last = last_phases.get(system)
            if last:
                phase = last
                phase_status = phase_status or "end"

    # Update last phase tracking
    if system and phase and last_phases is not None:
        last_phases[system] = phase

    obj: dict[str, Any] = {
        "v": 1,
        "ts": int(now * 1000),
        "iso": iso,
        "lvl": "INFO",
        "msg": str(msg),
    }
    if system:
        obj["system"] = system
    if phase:
        obj["phase"] = phase
    if phase_status:
        obj["phase_status"] = phase_status

    return obj
