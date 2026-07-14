"""Regression tests for the cache-freshness guards (2026-07-14 dashboard freeze).

These lock in detection of the exact failure mode that froze the dashboard at
2026-07-12: the rolling cache stopped advancing while the pipeline still reported
success, so signals recomputed the same stale snapshot forever (a silent no-op),
and past bars must never be destroyed by a rebuild (monotonic non-decreasing).
"""

from __future__ import annotations

from common.cache_freshness import (
    count_advanced,
    detect_regressions,
    fraction_behind_upstream,
    is_silent_noop,
    lag_business_days,
    max_last_date,
    modal_last_date,
    symbols_behind_upstream,
)


def _mf(**kw):
    """Build a manifest {sym: {"last_date":..., "n_rows":...}} from sym=date pairs."""
    return {sym: {"last_date": d, "n_rows": 100} for sym, d in kw.items()}


def test_max_last_date_and_empty():
    assert max_last_date(_mf(SPY="2026-07-10", BNY="2026-07-08")) == "2026-07-10"
    assert max_last_date({}) is None


def test_silent_noop_detected_when_nothing_advances():
    # byte-identical before/after == the freeze: signals rehash the same day.
    before = _mf(SPY="2026-07-08", BNY="2026-07-08", AAPL="2026-07-08")
    after = _mf(SPY="2026-07-08", BNY="2026-07-08", AAPL="2026-07-08")
    assert is_silent_noop(before, after) is True
    assert count_advanced(before, after) == 0


def test_advancement_is_not_a_noop():
    before = _mf(SPY="2026-07-08", BNY="2026-07-08", AAPL="2026-07-08")
    after = _mf(SPY="2026-07-10", BNY="2026-07-10", AAPL="2026-07-10")
    assert is_silent_noop(before, after) is False
    assert count_advanced(before, after) == 3


def test_partial_advance_below_threshold_flagged():
    before = _mf(A="2026-07-08", B="2026-07-08", C="2026-07-08")
    after = _mf(A="2026-07-10", B="2026-07-08", C="2026-07-08")
    assert count_advanced(before, after) == 1
    assert is_silent_noop(before, after) is False  # 1 >= default min_advanced=1
    assert is_silent_noop(before, after, min_advanced=2) is True


def test_newly_present_symbol_counts_as_advanced():
    before = _mf(A="2026-07-08")
    after = _mf(A="2026-07-08", NEW="2026-07-10")
    assert count_advanced(before, after) == 1


def test_regression_backward_date_flagged():
    before = _mf(SPY="2026-07-10", BNY="2026-07-10")
    after = _mf(SPY="2026-07-08", BNY="2026-07-10")  # SPY lost 2 days
    assert detect_regressions(before, after) == ["SPY"]


def test_regression_row_shrink_flagged():
    before = {"SPY": {"last_date": "2026-07-10", "n_rows": 500}}
    after = {"SPY": {"last_date": "2026-07-10", "n_rows": 300}}  # bars destroyed
    assert detect_regressions(before, after) == ["SPY"]


def test_no_regression_on_healthy_append():
    before = {"SPY": {"last_date": "2026-07-08", "n_rows": 498}}
    after = {"SPY": {"last_date": "2026-07-10", "n_rows": 500}}
    assert detect_regressions(before, after) == []


def test_symbols_behind_upstream_ignores_delisted():
    # rolling frozen at 07-08 while full_backup advanced to 07-10 for live names;
    # DEAD is delisted (upstream also old) so it must NOT count as "behind".
    upstream = _mf(SPY="2026-07-10", BNY="2026-07-10", DEAD="2026-03-01")
    rolling = _mf(SPY="2026-07-08", BNY="2026-07-08", DEAD="2026-03-01")
    assert set(symbols_behind_upstream(rolling, upstream)) == {"SPY", "BNY"}
    assert abs(fraction_behind_upstream(rolling, upstream) - 2 / 3) < 1e-9


def test_fraction_behind_zero_when_synced():
    upstream = _mf(SPY="2026-07-10", BNY="2026-07-10")
    rolling = _mf(SPY="2026-07-10", BNY="2026-07-10")
    assert fraction_behind_upstream(rolling, upstream) == 0.0


def test_max_date_alone_is_fooled_by_one_fresh_file():
    # THE bug the guard originally had: bulk frozen, one file fresh -> max lag 0,
    # but fraction-behind correctly flags the freeze.
    upstream = {f"S{i}": {"last_date": "2026-07-10"} for i in range(100)}
    rolling = {f"S{i}": {"last_date": "2026-07-01"} for i in range(100)}
    rolling["S0"]["last_date"] = "2026-07-10"  # single fresh file
    assert lag_business_days(max_last_date(rolling), max_last_date(upstream)) == 0
    assert fraction_behind_upstream(rolling, upstream) == 0.99


def test_modal_last_date_robust_to_one_fresh_file():
    m = {f"S{i}": {"last_date": "2026-07-01"} for i in range(100)}
    m["S0"]["last_date"] = "2026-07-10"  # one fresh file
    assert max_last_date(m) == "2026-07-10"  # max is fooled
    assert modal_last_date(m) == "2026-07-01"  # mode is not


def test_universe_scoping_excludes_non_universe_etfs():
    # Post-rebuild reality: universe names rebuilt to 07-10; non-universe ETFs
    # (fetched into full_backup, not in the trading universe) stuck at 07-01.
    upstream = _mf(AAPL="2026-07-10", MSFT="2026-07-10", GLDETF="2026-07-10")
    rolling = _mf(AAPL="2026-07-10", MSFT="2026-07-10", GLDETF="2026-07-01")
    # unscoped: GLDETF drags fraction up (false freeze)
    assert fraction_behind_upstream(rolling, upstream) > 0.3
    # scoped to the trading universe: 0% behind (correct — not a freeze)
    uni = {"AAPL", "MSFT"}
    assert fraction_behind_upstream(rolling, upstream, universe=uni) == 0.0
    assert symbols_behind_upstream(rolling, upstream, universe=uni) == []


def test_universe_scoping_still_catches_real_freeze():
    upstream = _mf(AAPL="2026-07-10", MSFT="2026-07-10")
    rolling = _mf(AAPL="2026-07-08", MSFT="2026-07-08")  # universe frozen
    uni = {"AAPL", "MSFT"}
    assert fraction_behind_upstream(rolling, upstream, universe=uni) == 1.0


def test_lag_business_days_freeze_signature():
    # rolling frozen at 07-01 while upstream is 07-10 -> 7 business days behind.
    assert lag_business_days("2026-07-01", "2026-07-10") == 7
    # rolling 07-08 vs upstream 07-10 -> 2 business days behind (the 07-14 case).
    assert lag_business_days("2026-07-08", "2026-07-10") == 2
    # up to date.
    assert lag_business_days("2026-07-10", "2026-07-10") == 0
    # weekend gap is not counted: Fri 07-10 -> Mon 07-13 is 1 business day.
    assert lag_business_days("2026-07-10", "2026-07-13") == 1
    assert lag_business_days(None, "2026-07-10") is None
