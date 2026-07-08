"""Regression tests for the F2 audit P0#1 SPY gate case-sensitivity bug.

Historical bug (fixed 2026-07-03):
    ``common/today_signals.py`` line 980 called ``_make_spy_gate(spy_df, column="sma100")``.
    The SPY frame produced by ``utils_spy.get_spy_with_indicators`` uses uppercase
    column names (``SMA100`` / ``SMA200``). ``pd.Series.get`` is case-sensitive, so
    the lookup silently returned ``None`` → ``spy_gate`` stayed ``None`` → the
    surrounding predicate ``setup_pass = final_pass_count if spy_gate != 0 else 0``
    evaluated ``None != 0`` as ``True`` and the gate **failed open**. System1 emitted
    long buy signals every day for roughly a year — including days SPY was below
    its 100-day SMA (trend down).

    Business impact: this is the single most direct money-losing bug in the
    F2 refactor audit. Regression coverage must guarantee it stays fixed.

Coverage:
    * ``_make_spy_gate`` returns ``False`` when SPY closes below SMA100 (uppercase col).
    * ``_make_spy_gate`` is case-insensitive: passing "SMA100" against a frame whose
      column is spelled ``sma100`` still resolves correctly (belt-and-suspenders
      defense in case a cache writer changes casing again).
    * The integrated System1 setup-pass computation (call site at line 980) yields
      ``setup_pass == 0`` on a SPY-below-SMA100 day.
    * A ``logger.warning`` is emitted when the requested SPY-gate column cannot be
      resolved at all, so a broken cache surfaces instead of silently failing open.
"""

from __future__ import annotations

import logging

import pandas as pd
import pytest

from common import today_signals
from common.today_signals import _make_spy_gate

# ---------------------------------------------------------------------------
# Direct _make_spy_gate coverage (source of truth for the bug)
# ---------------------------------------------------------------------------


def test_spy_gate_returns_false_when_close_below_uppercase_sma100() -> None:
    """This is the exact scenario that used to fail open — assert it now closes."""
    spy = pd.DataFrame(
        {"Close": [100.0], "SMA100": [110.0]},
        index=[pd.Timestamp("2026-07-02")],
    )
    assert _make_spy_gate(spy, column="SMA100") is False


def test_spy_gate_returns_true_when_close_above_uppercase_sma100() -> None:
    spy = pd.DataFrame(
        {"Close": [120.0], "SMA100": [110.0]},
        index=[pd.Timestamp("2026-07-02")],
    )
    assert _make_spy_gate(spy, column="SMA100") is True


def test_spy_gate_case_insensitive_fallback_when_column_is_lowercase() -> None:
    """Belt-and-suspenders: even if a cache writer regressed to lowercase spelling,
    passing the canonical uppercase name must still resolve the value."""
    spy = pd.DataFrame(
        {"Close": [120.0], "sma100": [110.0]},
        index=[pd.Timestamp("2026-07-02")],
    )
    assert _make_spy_gate(spy, column="SMA100") is True

    spy_below = pd.DataFrame(
        {"Close": [100.0], "sma100": [110.0]},
        index=[pd.Timestamp("2026-07-02")],
    )
    assert _make_spy_gate(spy_below, column="SMA100") is False


def test_spy_gate_missing_column_returns_none_and_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A completely absent gate column should return None **and** log a WARN.

    Without the log, a broken cache would silently disable the gate again.
    """
    spy = pd.DataFrame({"Close": [120.0]}, index=[pd.Timestamp("2026-07-02")])
    with caplog.at_level(logging.WARNING, logger=today_signals.logger.name):
        result = _make_spy_gate(spy, column="SMA100")
    assert result is None
    assert any(
        "SPY gate column" in rec.getMessage() and "SMA100" in rec.getMessage()
        for rec in caplog.records
    ), f"expected WARN about missing SPY gate column, got: {[r.getMessage() for r in caplog.records]}"


def test_spy_gate_returns_none_on_empty_or_missing_frame() -> None:
    assert _make_spy_gate(None, column="SMA100") is None
    assert _make_spy_gate(pd.DataFrame(), column="SMA100") is None


# ---------------------------------------------------------------------------
# Integration: line 980 call site must yield setup_pass == 0 when gate is off
# ---------------------------------------------------------------------------


def test_line980_call_site_passes_uppercase_column() -> None:
    """The caller at common/today_signals.py:980 must pass "SMA100" (uppercase).

    This is a source-level guard so the exact regression that shipped can never
    silently return. If someone touches the call site with the wrong case, this
    test dies immediately.
    """
    import inspect

    src = inspect.getsource(today_signals)
    # Regression: the exact buggy call `column="sma100"` must not reappear.
    assert 'column="sma100"' not in src, (
        "F2 P0#1 regression: common/today_signals.py contains a lowercase "
        '`column="sma100"` again — SPY gate will silently fail open. Use '
        '`column="SMA100"` (uppercase to match utils_spy.get_spy_with_indicators).'
    )
    # Positive assertion: the canonical uppercase call exists at least once.
    assert 'column="SMA100"' in src, (
        "expected common/today_signals.py to call _make_spy_gate with the "
        "uppercase SMA100 column at the System1 gate call site"
    )


def test_gated_setup_pass_is_zero_when_spy_below_sma100() -> None:
    """End-to-end sanity check on the gate arithmetic that shipped the bug.

    We don't drive the whole ``get_today_signals`` pipeline here — that would
    require a huge amount of prepared-cache scaffolding. Instead we assert the
    exact predicate at line 988 (``setup_pass = final_pass_count if spy_gate != 0 else 0``)
    reduces to zero when spy_gate reflects a SPY-below-SMA100 day, using the
    real ``_make_spy_gate`` -> uppercase-column path we just fixed.
    """
    spy = pd.DataFrame(
        {"Close": [100.0], "SMA100": [110.0]},
        index=[pd.Timestamp("2026-07-02")],
    )
    spy_gate_bool = _make_spy_gate(spy, column="SMA100")
    # mimic the mapping at today_signals.py:981-986
    if spy_gate_bool is True:
        spy_gate = 1
    elif spy_gate_bool is False:
        spy_gate = 0
    else:
        spy_gate = None

    final_pass_count = 42  # pretend 42 symbols passed system1 setup pre-gate
    setup_pass = final_pass_count if spy_gate != 0 else 0

    assert spy_gate == 0, "gate must be OFF when SPY < SMA100"
    assert setup_pass == 0, (
        "F2 P0#1 regression: with SPY below SMA100 the System1 setup_pass must "
        "collapse to zero. Under the historical bug this stayed at 42."
    )


def test_gated_setup_pass_passes_through_when_spy_above_sma100() -> None:
    """Same predicate, positive-path: gate ON must let the candidate count through."""
    spy = pd.DataFrame(
        {"Close": [120.0], "SMA100": [110.0]},
        index=[pd.Timestamp("2026-07-02")],
    )
    spy_gate_bool = _make_spy_gate(spy, column="SMA100")
    if spy_gate_bool is True:
        spy_gate = 1
    elif spy_gate_bool is False:
        spy_gate = 0
    else:
        spy_gate = None

    final_pass_count = 42
    setup_pass = final_pass_count if spy_gate != 0 else 0

    assert spy_gate == 1
    assert setup_pass == 42
