"""D5 Case 3 regression: SYSTEM_SPECIFIC_CONFIG dead-code removal guard.

Ref: docs/D5_SYSTEM_SPECIFIC_CONFIG_bug_20260702.md

Background:
    ``strategies/constants.py::SYSTEM_SPECIFIC_CONFIG`` was fully dead code
    (zero references across repo) yet formed a shadow config surface that
    could drift away from YAML (``config/config.yaml::strategies.<systemN>``)
    and ``common/trade_management.py::SYSTEM_TRADE_RULES``.  D5 removed it.

This test locks in:
    1. ``SYSTEM_SPECIFIC_CONFIG`` is not importable from ``strategies.constants``.
    2. No ``.py`` source (excluding docs) contains a *code* reference to the
       symbol — docstring/comment mentions are permitted.
    3. Individual constants (``MAX_HOLD_DAYS_DEFAULT`` etc.) are preserved
       because S2/S3/S6 still import them.
"""

from __future__ import annotations

import io
import tokenize
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent


def _code_names(src: str) -> set[str]:
    """Return NAME tokens in ``src``, ignoring tokens inside strings/comments.

    Docstring / comment mentions of a removed symbol are legitimate history
    and must not fail this guard; only real code references should.
    """
    names: set[str] = set()
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type == tokenize.NAME:
                names.add(tok.string)
    except tokenize.TokenizeError:
        pass
    return names


def test_symbol_not_importable():
    """SYSTEM_SPECIFIC_CONFIG must not be an attribute of strategies.constants."""
    import strategies.constants as const

    assert not hasattr(const, "SYSTEM_SPECIFIC_CONFIG"), (
        "D5 regression: SYSTEM_SPECIFIC_CONFIG has been re-introduced in "
        "strategies.constants.  Config surface must stay unified to YAML "
        "and SYSTEM_TRADE_RULES."
    )


def test_individual_constants_preserved():
    """Cleanup must not sweep away constants that other systems still use."""
    from strategies.constants import (
        ENTRY_MIN_GAP_PCT_DEFAULT,
        FALLBACK_EXIT_DAYS_DEFAULT,
        MAX_HOLD_DAYS_DEFAULT,
        PROFIT_TAKE_PCT_DEFAULT_4,
        PROFIT_TAKE_PCT_DEFAULT_5,
        STOP_ATR_MULTIPLE_DEFAULT,
        STOP_ATR_MULTIPLE_SYSTEM1,
        STOP_ATR_MULTIPLE_SYSTEM3,
        STOP_ATR_MULTIPLE_SYSTEM4,
    )

    assert MAX_HOLD_DAYS_DEFAULT == 3
    assert FALLBACK_EXIT_DAYS_DEFAULT == 6
    assert STOP_ATR_MULTIPLE_DEFAULT == 3.0
    assert STOP_ATR_MULTIPLE_SYSTEM1 == 5.0
    assert STOP_ATR_MULTIPLE_SYSTEM3 == 2.5
    assert STOP_ATR_MULTIPLE_SYSTEM4 == 1.5
    assert PROFIT_TAKE_PCT_DEFAULT_4 == pytest.approx(0.04)
    assert PROFIT_TAKE_PCT_DEFAULT_5 == pytest.approx(0.05)
    assert ENTRY_MIN_GAP_PCT_DEFAULT == pytest.approx(0.04)


def _iter_py_files(root: Path):
    skip_dirs = {".git", ".venv", "venv", "__pycache__", ".mypy_cache",
                 ".pytest_cache", "node_modules", ".ruff_cache", "docs"}
    for p in root.rglob("*.py"):
        parts = set(p.parts)
        if parts & skip_dirs:
            continue
        yield p


def test_no_source_references_to_dead_symbol():
    """No .py file (excluding docs and this test) may reference the symbol in code.

    Docstring / comment mentions are ignored via tokenize.
    """
    self_path = Path(__file__).resolve()
    hits: list[str] = []
    for path in _iter_py_files(REPO_ROOT):
        if path.resolve() == self_path:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if "SYSTEM_SPECIFIC_CONFIG" not in text:
            continue
        names = _code_names(text)
        if "SYSTEM_SPECIFIC_CONFIG" in names:
            hits.append(str(path.relative_to(REPO_ROOT)))

    assert not hits, (
        "D5 regression: SYSTEM_SPECIFIC_CONFIG has resurrected as a code "
        "reference in:\n" + "\n".join(f"  {p}" for p in hits)
    )
