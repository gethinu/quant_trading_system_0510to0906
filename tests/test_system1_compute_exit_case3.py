"""D5 Case 3 regression: System1 compute_exit trailing-only rewrite.

Ref: docs/D5_SYSTEM_SPECIFIC_CONFIG_bug_20260702.md

Background:
    The old ``System1Strategy.compute_exit`` fell back to
    ``strategies/constants.py::MAX_HOLD_DAYS_DEFAULT=3``, forcing a 3-day
    exit on the long trend momentum strategy (spec: 25% trailing, no time
    limit).  A micro-bench showed -5.09% vs +54.43% (delta +59.52%).

This test locks in:
    1. ``System1Strategy.compute_exit`` no longer references
       ``MAX_HOLD_DAYS_DEFAULT`` in *code* (docstring mentions are fine).
    2. Same trailing-only structure as System4 (``highest = entry_price``,
       Close-based drop check, Close-based hard stop, no time cap).
    3. Behavior: trailing hit / initial stop hit / no-trigger hold-to-end.
"""

from __future__ import annotations

import inspect
import io
from pathlib import Path
import re
import tokenize

import pandas as pd
import pytest

from strategies.system1_strategy import System1Strategy
from strategies.system4_strategy import System4Strategy


def _code_tokens(src: str) -> set[str]:
    """Return NAME tokens in ``src``, excluding tokens inside strings/comments.

    Docstrings and comments are legitimate places to *mention* symbols the
    impl must not *reference*.  This helper lets us assert "no code
    reference" without banning historical explanations.
    """
    names: set[str] = set()
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type == tokenize.NAME:
                names.add(tok.string)
    except tokenize.TokenizeError:
        pass
    return names


class TestSystem1ComputeExitStructure:
    """Source-level guards to prevent dead-code re-entry."""

    def _source(self, cls) -> str:
        return inspect.getsource(cls.compute_exit)

    def test_no_max_hold_days_default_reference(self):
        names = _code_tokens(self._source(System1Strategy))
        assert "MAX_HOLD_DAYS_DEFAULT" not in names, (
            "D5 regression: System1Strategy.compute_exit must not go through "
            "the MAX_HOLD_DAYS_DEFAULT (3-day) fallback."
        )

    def test_no_max_hold_days_config_key(self):
        src = self._source(System1Strategy)
        assert (
            '"max_hold_days"' not in src and "'max_hold_days'" not in src
        ), "D5 regression: System1 has no time-based forced exit (spec)."

    def test_uses_trailing_pct_config(self):
        src = self._source(System1Strategy)
        assert (
            '"trailing_pct"' in src or "'trailing_pct'" in src
        ), "S1 trailing width must be resolved from config.trailing_pct."

    def test_default_trailing_pct_is_25_percent(self):
        src = self._source(System1Strategy)
        m = re.search(r'trailing_pct["\']\s*,\s*([0-9.]+)', src)
        assert m is not None, "trailing_pct default not extractable"
        default_val = float(m.group(1))
        assert (
            default_val == 0.25
        ), f"S1 spec is 25%% trailing; default={default_val} violates spec."

    def test_structure_matches_s4_trailing_only(self):
        s1_src = self._source(System1Strategy)
        s4_src = self._source(System4Strategy)
        for token in (
            "highest = entry_price",
            "highest * (1 - trail_pct)",
            "close <= stop_price",
        ):
            assert token in s1_src, f"S1.compute_exit missing S4-shape token {token!r}"
            assert (
                token in s4_src
            ), f"S4.compute_exit should also contain {token!r} (baseline drift)"


class TestSystem1ComputeExitBehavior:
    """Behavioral guards; the old 3-day forced exit would fail these."""

    def setup_method(self):
        self.strategy = System1Strategy()
        self.strategy.config = {}

    def _make_df(self, closes):
        n = len(closes)
        dates = pd.date_range("2025-01-01", periods=n, freq="B")
        return pd.DataFrame(
            {
                "Open": closes,
                "High": [c * 1.001 for c in closes],
                "Low": [c * 0.999 for c in closes],
                "Close": closes,
            },
            index=dates,
        )

    def test_no_forced_exit_at_day_3(self):
        # 100 -> 110 -> 121 -> 133 -> 146: monotonic up, no trigger
        df = self._make_df([100.0, 110.0, 121.0, 133.0, 146.0])
        exit_price, exit_date = self.strategy.compute_exit(
            df, entry_idx=0, entry_price=100.0, stop_price=80.0
        )
        assert exit_price == pytest.approx(146.0), (
            f"Time-based forced exit triggered (exit_price={exit_price}). "
            "The old 3-day forced-exit bug may have re-entered."
        )
        assert exit_date == df.index[-1]

    def test_trailing_stop_triggers(self):
        # 100 -> 200 -> 200 -> 149 (below 200*0.75=150)
        df = self._make_df([100.0, 200.0, 200.0, 149.0])
        exit_price, exit_date = self.strategy.compute_exit(
            df, entry_idx=0, entry_price=100.0, stop_price=50.0
        )
        assert exit_price == pytest.approx(149.0)
        assert exit_date == df.index[3]

    def test_initial_stop_triggers(self):
        # entry=100, stop=90; day2 close=89 (< 90)
        df = self._make_df([100.0, 95.0, 89.0, 91.0])
        exit_price, exit_date = self.strategy.compute_exit(
            df, entry_idx=0, entry_price=100.0, stop_price=90.0
        )
        assert exit_price == pytest.approx(89.0)
        assert exit_date == df.index[2]

    def test_trailing_pct_override_via_config(self):
        self.strategy.config = {"trailing_pct": 0.10}
        # 100 -> 200 -> 179 (< 200*0.90=180)
        df = self._make_df([100.0, 200.0, 179.0, 200.0])
        exit_price, exit_date = self.strategy.compute_exit(
            df, entry_idx=0, entry_price=100.0, stop_price=50.0
        )
        assert exit_price == pytest.approx(179.0)
        assert exit_date == df.index[2]

    def test_max_hold_days_config_is_ignored(self):
        self.strategy.config = {"max_hold_days": 1}
        df = self._make_df([100.0, 110.0, 121.0, 133.0, 146.0])
        exit_price, _ = self.strategy.compute_exit(
            df, entry_idx=0, entry_price=100.0, stop_price=80.0
        )
        assert exit_price == pytest.approx(
            146.0
        ), "config.max_hold_days is being read by the new impl by mistake."


def test_system1_strategy_file_has_no_max_hold_days_reference():
    """File-wide guard: no code reference to MAX_HOLD_DAYS_DEFAULT anywhere.

    Docstring/comment mentions are ignored via tokenize.
    """
    path = Path(__file__).resolve().parent.parent / "strategies" / "system1_strategy.py"
    text = path.read_text(encoding="utf-8")
    names = _code_tokens(text)
    assert "MAX_HOLD_DAYS_DEFAULT" not in names, (
        "D5 regression: strategies/system1_strategy.py has resurrected an "
        "import/reference to MAX_HOLD_DAYS_DEFAULT."
    )
