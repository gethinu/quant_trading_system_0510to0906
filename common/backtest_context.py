"""Process-local marker for "this code is running inside a backtest".

Why this exists
---------------
A few engine fast paths exist purely to keep the *daily (today) signal run*
cheap — most notably System6's forced ``latest_only`` (see
``core/system6.generate_candidates_system6``). Those fast paths collapse a
historical scan down to the most recent bar, which is exactly what today's run
wants and exactly what a backtest must not do.

Historically the only way to opt out was to flip a global default
(``SYSTEM6_FORCE_LATEST_ONLY=0`` / ``FULL_SCAN_TODAY=1``), which also changes the
live daily run. This module gives backtest/validation entry points a way to say
"I am a backtest" *locally*, so the live defaults stay untouched.

Contract
--------
- Live (``scripts/run_all_systems_today.py`` and everything it calls) never
  enters this context, so :func:`in_backtest_context` is ``False`` there and all
  today-oriented fast paths behave byte-identically to before.
- Backtest entry points wrap their work in :func:`backtest_context`.
- The context manager also mirrors the state into ``QTS_BACKTEST_CONTEXT`` so
  worker *processes* spawned while inside the context inherit it
  (``contextvars`` do not cross a process boundary). The previous value is
  restored on exit.
"""

from __future__ import annotations

import contextlib
import contextvars
import os
from typing import Iterator

__all__ = [
    "BACKTEST_CONTEXT_ENV",
    "backtest_context",
    "in_backtest_context",
]

BACKTEST_CONTEXT_ENV = "QTS_BACKTEST_CONTEXT"

_TRUTHY = {"1", "true", "yes", "on", "y", "t"}

_depth: contextvars.ContextVar[int] = contextvars.ContextVar(
    "qts_backtest_depth", default=0
)


def _env_flag_set() -> bool:
    raw = os.environ.get(BACKTEST_CONTEXT_ENV)
    if raw is None:
        return False
    return str(raw).strip().lower() in _TRUTHY


def in_backtest_context() -> bool:
    """Return True when the caller runs inside a backtest/validation run."""
    try:
        if _depth.get() > 0:
            return True
    except Exception:  # pragma: no cover - contextvar lookup should not fail
        pass
    return _env_flag_set()


@contextlib.contextmanager
def backtest_context() -> Iterator[None]:
    """Mark the enclosed block as backtest execution (re-entrant)."""
    token = _depth.set(_depth.get() + 1)
    previous = os.environ.get(BACKTEST_CONTEXT_ENV)
    os.environ[BACKTEST_CONTEXT_ENV] = "1"
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(BACKTEST_CONTEXT_ENV, None)
        else:
            os.environ[BACKTEST_CONTEXT_ENV] = previous
        try:
            _depth.reset(token)
        except Exception:  # pragma: no cover - reset across contexts
            _depth.set(max(0, _depth.get() - 1))
