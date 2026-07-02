"""Unit + regression tests for audit-remediation 2026-07-02.

Covers the fixes tracked in docs/AUDIT_REMEDIATION_20260702.md:
  - P0  System7 unused stub removed from SYSTEM_TRADE_RULES
  - P0  System5 setup 乖離 (ADX7>55, RSI3<50, Close>SMA100+ATR10) enforced
  - P1  System3 filter/setup reverted to spec (Low>=1, AvgVolume50>=1M,
        Close>sma150, drop3d>=0.125)
  - P2  system5/system3 predicate unified with core setup logic
  - P2  signal-count regression (old vs new setup selectivity)
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from common.system_setup_predicates import (
    system3_setup_predicate,
    system5_setup_predicate,
)
from common.trade_management import SYSTEM_TRADE_RULES
from core.system3 import _apply_filter_conditions as _s3_filter
from core.system3 import _apply_setup_conditions as _s3_setup
from core.system5 import (
    DEFAULT_ATR_PCT_THRESHOLD,
    MAX_RSI3,
    MIN_ADX,
    MIN_PRICE,
    _apply_filter_conditions,
    _apply_setup_conditions,
)


# --- P0: System7 stub removal ------------------------------------------------
def test_system7_stub_removed():
    """System7 must not be present in SYSTEM_TRADE_RULES (unused 20d/5ATR stub)."""
    assert "system7" not in SYSTEM_TRADE_RULES
    assert SYSTEM_TRADE_RULES.get("system7") is None


def test_other_systems_still_present():
    """Removing system7 must not disturb the other six systems."""
    for name in ("system1", "system2", "system3", "system4", "system5", "system6"):
        assert name in SYSTEM_TRADE_RULES


# --- P0: System5 setup conditions --------------------------------------------
def _base_frame(**overrides) -> pd.DataFrame:
    row = {
        "Close": 100.0,
        "adx7": 60.0,  # > 55
        "atr_pct": 0.05,  # > 2.5%
        "sma100": 90.0,
        "atr10": 5.0,  # Close(100) > sma100(90)+atr10(5)=95 -> True
        "rsi3": 30.0,  # < 50
    }
    row.update(overrides)
    return pd.DataFrame([row])


def _setup_bool(df: pd.DataFrame) -> bool:
    out = _apply_setup_conditions(_apply_filter_conditions(df))
    return bool(out["setup"].iloc[0])


def test_system5_setup_passes_when_all_conditions_met():
    assert _setup_bool(_base_frame()) is True


def test_system5_setup_rejects_low_adx():
    # ADX7 = 50 (<=55) must fail (old code accepted >35)
    assert _setup_bool(_base_frame(adx7=50.0)) is False


def test_system5_setup_rejects_high_rsi3():
    # RSI3 = 60 (>=50) must fail
    assert _setup_bool(_base_frame(rsi3=60.0)) is False


def test_system5_setup_rejects_below_price_band():
    # Close below SMA100+ATR10 band must fail
    assert _setup_bool(_base_frame(Close=94.0)) is False


def test_system5_setup_rejects_low_price():
    assert _setup_bool(_base_frame(Close=4.0)) is False


def test_system5_setup_rejects_low_atr_pct():
    assert _setup_bool(_base_frame(atr_pct=0.01)) is False


# --- P2: predicate unified with core setup -----------------------------------
def test_predicate_matches_core_setup():
    """system5_setup_predicate must agree with core setup on representative rows."""
    frames = [
        _base_frame(),
        _base_frame(adx7=50.0),
        _base_frame(rsi3=70.0),
        _base_frame(Close=94.0),
        _base_frame(Close=4.0),
        _base_frame(atr_pct=0.01),
    ]
    for df in frames:
        core_setup = _setup_bool(df)
        pred = bool(system5_setup_predicate(df.iloc[0]))
        assert core_setup == pred, f"mismatch on {df.iloc[0].to_dict()}"


# --- P2: signal-count regression (old vs new selectivity) --------------------
def test_system5_setup_is_more_selective_than_old():
    """New spec-compliant setup must be a strict subset of the old (looser) setup.

    Old logic: Close>=5 & adx7>35 & atr_pct>2.5% (setup == filter).
    New logic adds ADX>55, Close>SMA100+ATR10, RSI3<50 -> strictly fewer.
    """
    rng = np.random.default_rng(20260702)
    n = 3000
    df = pd.DataFrame(
        {
            "Close": rng.uniform(4, 200, n),
            "adx7": rng.uniform(10, 90, n),
            "atr_pct": rng.uniform(0.005, 0.10, n),
            "sma100": rng.uniform(4, 200, n),
            "atr10": rng.uniform(0.5, 8, n),
            "rsi3": rng.uniform(0, 100, n),
        }
    )
    old_setup = (
        (df["Close"] >= MIN_PRICE) & (df["adx7"] > 35.0) & (df["atr_pct"] > 0.025)
    )
    new_setup = _apply_setup_conditions(_apply_filter_conditions(df.copy()))["setup"]

    # New must be a subset of old (no new candidate that old rejected)
    assert (new_setup & ~old_setup).sum() == 0
    # And strictly fewer on a broad random universe
    assert int(new_setup.sum()) < int(old_setup.sum())

    # Every surviving row satisfies the full spec
    sub = df[new_setup]
    assert (sub["adx7"] > MIN_ADX).all()
    assert (sub["rsi3"] < MAX_RSI3).all()
    assert (sub["Close"] > sub["sma100"] + sub["atr10"]).all()
    assert (sub["atr_pct"] > DEFAULT_ATR_PCT_THRESHOLD).all()


# --- P1: System3 spec-compliant filter/setup --------------------------------
def _s3_base_frame(**overrides) -> pd.DataFrame:
    """A row that PASSES the spec filter+setup (docs/systems/システム3.txt)."""
    row = {
        "Low": 2.0,  # >= 1
        "avgvolume50": 1_500_000,  # >= 1M shares
        "atr_ratio": 0.06,  # >= 5%
        "Close": 20.0,
        "sma150": 10.0,  # Close(20) > sma150(10)
        "drop3d": 0.20,  # >= 12.5%
    }
    row.update(overrides)
    return pd.DataFrame([row])


def _s3_setup_bool(df: pd.DataFrame) -> bool:
    return bool(_s3_setup(_s3_filter(df))["setup"].iloc[0])


def test_system3_setup_passes_spec_row():
    assert _s3_setup_bool(_s3_base_frame()) is True


def test_system3_accepts_low_priced_dollar_stock():
    # spec: price >= $1 (old code required Close>=5). Low=1.2 must pass filter.
    df = _s3_base_frame(Low=1.2, Close=1.5, sma150=1.0)
    assert bool(_s3_filter(df)["filter"].iloc[0]) is True


def test_system3_rejects_sub_dollar_price():
    assert bool(_s3_filter(_s3_base_frame(Low=0.5))["filter"].iloc[0]) is False


def test_system3_uses_share_volume_not_dollar_volume():
    # avgvolume50 below 1M shares must fail even if dollar volume is huge.
    df = _s3_base_frame(avgvolume50=500_000)
    df["dollarvolume20"] = 999_000_000
    assert bool(_s3_filter(df)["filter"].iloc[0]) is False


def test_system3_setup_requires_close_above_sma150():
    # spec setup: Close > 150SMA (previously not enforced)
    assert _s3_setup_bool(_s3_base_frame(Close=9.0, sma150=10.0)) is False


def test_system3_setup_requires_drop3d():
    assert _s3_setup_bool(_s3_base_frame(drop3d=0.05)) is False


def test_system3_predicate_matches_core_setup():
    frames = [
        _s3_base_frame(),
        _s3_base_frame(Low=0.5),
        _s3_base_frame(avgvolume50=500_000),
        _s3_base_frame(Close=9.0, sma150=10.0),
        _s3_base_frame(drop3d=0.05),
        _s3_base_frame(atr_ratio=0.01),
    ]
    for df in frames:
        core_setup = _s3_setup_bool(df)
        pred = bool(system3_setup_predicate(df.iloc[0]))
        assert core_setup == pred, f"mismatch on {df.iloc[0].to_dict()}"
