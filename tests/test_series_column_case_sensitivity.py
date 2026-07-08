"""Source-level guard against the F2 audit P0#1 paradigm reappearing.

The SPY frame produced by ``utils_spy.get_spy_with_indicators`` uses uppercase
column names (``Close`` / ``SMA100`` / ``SMA200``). The historical bug was a
lowercase ``Series.get("sma100")`` against that frame, which silently returned
``None`` and disabled the System1 SPY gate.

This test statically scans ``common/today_signals.py`` for the anti-pattern so
a future refactor cannot silently reintroduce it. It is intentionally narrow —
scanning all ``.get("smaN")`` sites repo-wide would false-positive on per-symbol
frames whose columns are legitimately lowercase.
"""

from __future__ import annotations

import inspect
import re

from common import today_signals

# The buggy paradigm: `_make_spy_gate` is only called from ``common/today_signals``
# and only against the uppercase SPY frame. If any caller passes a lowercase gate
# column the gate will fail open. Enforce uppercase at the source level.
_LOWERCASE_SPY_COLUMNS = ("sma100", "sma200")


def test_no_lowercase_column_passed_to_make_spy_gate() -> None:
    """No caller may pass a lowercase SPY column name to _make_spy_gate."""
    src = inspect.getsource(today_signals)
    for col in _LOWERCASE_SPY_COLUMNS:
        # Match `column="smaXXX"` and `column='smaXXX'`
        pattern = rf'column\s*=\s*["\']{re.escape(col)}["\']'
        matches = re.findall(pattern, src)
        assert not matches, (
            f"F2 P0#1 regression: common/today_signals.py contains a lowercase "
            f"`column={col!r}` passed to _make_spy_gate. SPY frame columns are "
            f"uppercase ({col.upper()}); Series.get is case-sensitive; the SPY "
            f"gate will silently fail open. Occurrences: {matches}"
        )


def test_make_spy_gate_uses_indexable_case_insensitive_resolution() -> None:
    """The gate helper must implement case-insensitive column resolution.

    The primary defense (uppercase call sites) plus this belt-and-suspenders
    resolution inside ``_make_spy_gate`` means a future cache-casing regression
    won't silently disable the gate — instead a WARN is logged.
    """
    src = inspect.getsource(today_signals._make_spy_gate)
    assert "lower()" in src, (
        "_make_spy_gate must resolve its `column` argument case-insensitively "
        "against the SPY frame's index. Look for a `.lower()` comparison."
    )
    assert "logger.warning" in src or "logger.warn" in src, (
        "_make_spy_gate must emit a WARN log when the requested column cannot "
        "be resolved, so a broken cache surfaces instead of silently returning "
        "None and reopening the gate."
    )


def test_line_980_call_site_uses_uppercase() -> None:
    """The specific historical bug site must call _make_spy_gate with uppercase."""
    src = inspect.getsource(today_signals)
    # Grab the region around the historical bug site — anchor on the setup_pass
    # gate predicate for stability across refactors.
    marker = "setup_pass = final_pass_count if spy_gate != 0 else 0"
    # Use rfind: the marker appears both in an explanatory comment (added by the
    # G1 fix) and in the actual code below it. We want the code site, which is
    # the last occurrence.
    idx = src.rfind(marker)
    assert idx > 0, (
        f"expected sentinel {marker!r} to still exist in today_signals.py — "
        "if the gate predicate has been rewritten, update this regression test."
    )
    # Search backward ~1500 chars — the call to _make_spy_gate lives immediately
    # above, but the G1 explanatory comment block pushes it further up.
    window = src[max(0, idx - 1500) : idx]
    assert "_make_spy_gate(" in window, (
        "expected _make_spy_gate(...) call immediately above the System1 gate "
        "predicate. Refactor moved things? Update this test."
    )
    assert 'column="SMA100"' in window, (
        "F2 P0#1 regression: the System1 SPY gate call site must pass "
        '`column="SMA100"` (uppercase). Anything else silently reopens the '
        "gate under real cache column casing."
    )
