"""Regression: SPY must never be an entry candidate for systems 1-6.

Root cause (2026-07-08, run_id 20260708_060309): the daily pipeline force-adds
SPY to the loaded universe (for the SPY>SMA100/SMA200 market-regime gate and for
System7's hedge). On a day where the EODHD common-stock filter failed with 401,
the universe degraded to the raw NASDAQ-Trader list (SPY prepended) and only
~290/7475 symbols had fresh rolling data. SPY — always kept fresh as the rolling
anchor — was the only symbol left in System1's ROC200 ranking and was emitted as
"SPY BUY rank1", producing a paper order (system1-SPY-20260708).

docs/systems/INDEX.md is authoritative: System7 is "SPY 固定のヘッジ戦略（変更禁止）"
and systems 1-6 trade common stocks only. These tests lock in the fix
(``common.today_signals._exclude_hedge_symbols_for_entry``) so SPY can never again
leak into a systems 1-6 entry list, while System7 keeps SPY.
"""

from __future__ import annotations

import pandas as pd
import pytest

from common.system_constants import HEDGE_INDEX_SYMBOLS, SYSTEM7_SYMBOL
from common.today_signals import _exclude_hedge_symbols_for_entry
from core.system1 import generate_candidates_system1


def _mk_frame(close: float, roc200: float, dv20: float = 60_000_000.0) -> pd.DataFrame:
    """Minimal frame with the precomputed indicators System1 reads."""
    idx = pd.to_datetime(["2026-07-06", "2026-07-07"])
    return pd.DataFrame(
        {
            "Open": [close, close],
            "High": [close, close],
            "Low": [close, close],
            "Close": [close, close],
            "sma25": [10.0, 10.0],
            "sma50": [9.0, 9.0],  # sma25 > sma50 => trend ok
            "sma200": [8.0, 8.0],
            "roc200": [roc200, roc200],
            "atr20": [1.0, 1.0],
            "dollarvolume20": [dv20, dv20],
            "setup": [True, True],
            "filter": [True, True],
        },
        index=idx,
    )


def test_hedge_index_symbols_contains_spy():
    assert SYSTEM7_SYMBOL == "SPY"
    assert "SPY" in HEDGE_INDEX_SYMBOLS


@pytest.mark.parametrize(
    "system_name",
    ["system1", "system2", "system3", "system4", "system5", "system6"],
)
def test_spy_removed_from_entry_universe_for_systems_1_to_6(system_name):
    prepared = {"SPY": _mk_frame(745.76, 0.5), "AAPL": _mk_frame(200.0, 0.3)}
    out, _market_df = _exclude_hedge_symbols_for_entry(
        system_name, prepared, market_df=pd.DataFrame({"x": [1]}), log_callback=None
    )
    assert "SPY" not in out, f"{system_name} must not keep SPY as an entry candidate"
    assert "AAPL" in out, f"{system_name} must keep ordinary stocks"


def test_spy_retained_for_system7():
    prepared = {"SPY": _mk_frame(745.76, 0.5)}
    out, _market_df = _exclude_hedge_symbols_for_entry(
        "system7", prepared, market_df=None, log_callback=None
    )
    assert "SPY" in out, "System7 is the SPY hedge and must keep SPY"


def test_market_df_fallback_uses_removed_spy_when_absent():
    spy_frame = _mk_frame(745.76, 0.5)
    prepared = {"SPY": spy_frame, "MSFT": _mk_frame(400.0, 0.2)}
    # market_df not provided -> the removed SPY frame is preserved for the gate
    out, market_df = _exclude_hedge_symbols_for_entry(
        "system1", prepared, market_df=None, log_callback=None
    )
    assert "SPY" not in out
    assert market_df is not None and not market_df.empty
    assert market_df.equals(spy_frame)


def test_provided_market_df_is_not_overridden():
    provided = pd.DataFrame({"Close": [1.0, 2.0]})
    prepared = {"SPY": _mk_frame(745.76, 0.5), "MSFT": _mk_frame(400.0, 0.2)}
    out, market_df = _exclude_hedge_symbols_for_entry(
        "system1", prepared, market_df=provided, log_callback=None
    )
    assert "SPY" not in out
    assert market_df is provided  # caller-supplied SPY market frame wins


def test_no_hedge_symbols_leaves_dict_untouched():
    prepared = {"AAPL": _mk_frame(200.0, 0.3), "MSFT": _mk_frame(400.0, 0.2)}
    out, market_df = _exclude_hedge_symbols_for_entry(
        "system1", prepared, market_df=None, log_callback=None
    )
    assert set(out) == {"AAPL", "MSFT"}
    assert market_df is None


def test_system1_ranking_does_not_surface_spy_after_exclusion():
    """End-to-end at the ranking boundary: with SPY as the top-ROC200 symbol,
    excluding it before ``generate_candidates_system1`` must yield AAPL (not SPY).

    This reproduces the 2026-07-08 shape where SPY (rank1 by ROC200) was the only
    survivor of a degraded universe.
    """
    prepared = {
        "SPY": _mk_frame(745.76, 0.99),  # would be rank1 by ROC200
        "AAPL": _mk_frame(200.0, 0.10),
    }
    # Sanity: without the exclusion SPY *would* be ranked first.
    by_date_raw, _df_raw, _diag_raw = generate_candidates_system1(
        dict(prepared), top_n=10, latest_only=True
    )
    raw_syms = {s for bucket in by_date_raw.values() for s in bucket}
    assert "SPY" in raw_syms  # confirms the bug shape exists pre-fix

    # Apply the production exclusion, then rank.
    filtered, _market_df = _exclude_hedge_symbols_for_entry(
        "system1", prepared, market_df=pd.DataFrame({"x": [1]}), log_callback=None
    )
    by_date, _df, _diag = generate_candidates_system1(
        filtered, top_n=10, latest_only=True
    )
    syms = {s for bucket in by_date.values() for s in bucket}
    assert "SPY" not in syms, "System1 must not rank SPY after hedge exclusion"
    assert "AAPL" in syms
