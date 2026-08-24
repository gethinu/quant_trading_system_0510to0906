"""Feature flags for the validation toolkit.

All flags default to *disabled*. The truthiness convention mirrors
``config/settings.py`` (``str(os.getenv(...)).lower() in {"1","true","yes","on"}``)
so behavior is consistent with the rest of the codebase.

Because every entry point in :mod:`common.validation` checks one of these flags
before doing any work, the default (all-unset) environment is byte-parity with
the pre-existing system: no code path changes, no files are written, no logs
are emitted.
"""

from __future__ import annotations

import os

_TRUTHY = {"1", "true", "yes", "on"}


def env_flag(name: str, default: bool = False) -> bool:
    """Return a boolean environment flag, defaulting to *disabled*.

    Matches the settings.py idiom; an unset or blank variable yields ``default``.
    """
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in _TRUTHY


def validation_enabled() -> bool:
    """Master switch. When False (default) the whole toolkit is inert."""
    return env_flag("VALIDATION_ENABLED", False)


def cpcv_enabled() -> bool:
    return validation_enabled() and env_flag("VALIDATION_CPCV", True)


def bootstrap_enabled() -> bool:
    return validation_enabled() and env_flag("VALIDATION_BOOTSTRAP", True)


def dsr_enabled() -> bool:
    return validation_enabled() and env_flag("VALIDATION_DSR", True)


def survivorship_guard_mode() -> str:
    """Return one of ``"off"`` / ``"warn"`` / ``"enforce"`` (default ``"off"``).

    Mirrors the OFF/WARN/ENFORCE staged-rollout pattern used by
    ``common/invariants/phase1_gates.py``.
    """
    raw = (os.getenv("SURVIVORSHIP_GUARD", "off") or "off").strip().lower()
    if raw in {"off", "warn", "enforce"}:
        return raw
    if raw in _TRUTHY:  # tolerate SURVIVORSHIP_GUARD=1 -> warn
        return "warn"
    return "off"
