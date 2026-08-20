"""Regression tests for the three backtest-engine gaps closed on 2026-08-21.

Each gap made one or more of System1-7 produce *no* backtest history at all:

GAP 1  System3's full-scan candidates are list-form keyed by ``date`` (no
       ``entry_date``); both engines looked up ``entry_date`` inside a bare
       ``except Exception: continue`` and dropped every one of them silently.
GAP 2  ``SYSTEM6_FORCE_LATEST_ONLY`` defaults to True, collapsing System6 to a
       single date even in a backtest.
GAP 3  System1's batch prepare route produced RangeIndex/lowercase frames (no
       date index, ``setup`` always False) and System7's prepare could not read
       a base-cache SPY frame at all.

Every fix is backtest-only; the live (today) path is asserted to be untouched
where that is observable (System7's IMMEDIATE_STOP guard, System6's forced
latest_only outside a backtest context).
"""

from __future__ import annotations

import os

import pandas as pd
import pytest

from common.backtest_context import backtest_context, in_backtest_context
from common.backtest_utils import simulate_trades_with_risk
from common.candidate_schema import (
    CandidateSchemaError,
    normalize_candidates_for_date,
    resolve_entry_bar,
)
from common.integrated_backtest import (
    SystemState,
    run_integrated_backtest,
)
from common.system_common import normalize_ohlc_frame

CAPITAL = 100_000.0


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def _system3_frame() -> pd.DataFrame:
    """12 bars where a limit at prev_close*0.93 is reachable on bar 1."""
    idx = pd.bdate_range("2024-05-01", periods=12)
    close = [100, 95, 98, 99, 100, 101, 102, 103, 104, 105, 106, 107]
    low = [99, 90, 96, 97, 98, 99, 100, 101, 102, 103, 104, 105]
    high = [101, 101, 99, 100, 101, 102, 103, 104, 105, 106, 107, 108]
    open_ = [100, 99, 97, 98, 99, 100, 101, 102, 103, 104, 105, 106]
    return pd.DataFrame(
        {
            "Open": open_,
            "High": high,
            "Low": low,
            "Close": close,
            "Volume": [5_000_000] * 12,
            "atr10": [2.0] * 12,
            "ATR10": [2.0] * 12,
            "sma150": [80.0] * 12,
            "avgvolume50": [5_000_000] * 12,
            "atr_ratio": [0.06] * 12,
            "drop3d": [0.15] * 12,
            "filter": [True] * 12,
            "setup": [True] * 12,
        },
        index=idx,
    )


def _system3_candidate(signal_date: pd.Timestamp) -> dict:
    """Exactly the shape core/system3.py emits on the full-scan path."""
    return {
        "symbol": "AAA",
        "date": signal_date,
        "drop3d": 0.15,
        "atr_ratio": 0.06,
        "close": 100.0,
    }


def _system3_strategy():
    from strategies.system3_strategy import System3Strategy

    return System3Strategy()


# ---------------------------------------------------------------------------
# candidate schema unit tests
# ---------------------------------------------------------------------------


def test_dict_form_still_injects_entry_date():
    date = pd.Timestamp("2024-05-02")
    out = normalize_candidates_for_date({"AAA": {"rsi3": 1.0}}, date)
    assert out == [{"symbol": "AAA", "entry_date": date, "rsi3": 1.0}]


def test_list_form_with_only_date_is_accepted():
    date = pd.Timestamp("2024-05-01")
    out = normalize_candidates_for_date([_system3_candidate(date)], date)
    assert out[0]["symbol"] == "AAA"
    assert "entry_date" not in out[0]


def test_normalize_fails_loud_when_no_date_key():
    with pytest.raises(CandidateSchemaError) as excinfo:
        normalize_candidates_for_date(
            [{"symbol": "AAA", "roc200": 1.0}], pd.Timestamp("2024-05-01")
        )
    message = str(excinfo.value)
    assert "no usable entry date" in message
    assert "roc200" in message  # the offending keys are named


def test_normalize_fails_loud_on_non_mapping_candidate():
    with pytest.raises(CandidateSchemaError):
        normalize_candidates_for_date(["AAA"], pd.Timestamp("2024-05-01"))


def test_resolve_entry_bar_advances_signal_date_to_next_bar():
    df = _system3_frame()
    resolved = resolve_entry_bar(df, _system3_candidate(df.index[0]))
    assert resolved is not None
    position, entry_ts = resolved
    assert position == 1
    assert entry_ts == df.index[1]


def test_resolve_entry_bar_prefers_explicit_entry_date():
    df = _system3_frame()
    candidate = {"symbol": "AAA", "date": df.index[0], "entry_date": df.index[4]}
    assert resolve_entry_bar(df, candidate) == (4, df.index[4])


def test_resolve_entry_bar_returns_none_when_no_bar_left():
    df = _system3_frame()
    # Signal on the very last bar: nothing to enter on. Data condition, not a
    # schema error -> None, no exception.
    assert resolve_entry_bar(df, _system3_candidate(df.index[-1])) is None


def test_resolve_entry_bar_fails_loud_when_no_date_key():
    with pytest.raises(CandidateSchemaError):
        resolve_entry_bar(_system3_frame(), {"symbol": "AAA"})


# ---------------------------------------------------------------------------
# GAP 1 - System3 books trades in both engines
# ---------------------------------------------------------------------------


def test_gap1_system3_shaped_candidates_book_trades_single_engine():
    df = _system3_frame()
    signal_date = df.index[0]
    trades, _logs = simulate_trades_with_risk(
        {signal_date: [_system3_candidate(signal_date)]},
        {"AAA": df},
        CAPITAL,
        _system3_strategy(),
        side="long",
    )
    assert not trades.empty, "System3 full-scan candidates must book trades"
    assert pd.Timestamp(trades.iloc[0]["entry_date"]) == df.index[1]


def test_gap1_system3_shaped_candidates_book_trades_integrated_engine():
    df = _system3_frame()
    signal_date = df.index[0]
    state = SystemState(
        name="System3",
        side="long",
        strategy=_system3_strategy(),
        prepared={"AAA": df},
        candidates_by_date={signal_date: [_system3_candidate(signal_date)]},
    )
    trades, counts = run_integrated_backtest([state], CAPITAL)
    assert counts["System3"] == 1
    assert not trades.empty, "System3 must book trades in the integrated engine"
    assert trades.iloc[0]["system"] == "System3"


def test_gap1_single_engine_fails_loud_on_unmatched_schema():
    df = _system3_frame()
    with pytest.raises(CandidateSchemaError):
        simulate_trades_with_risk(
            {df.index[0]: [{"symbol": "AAA", "roc200": 1.0}]},
            {"AAA": df},
            CAPITAL,
            _system3_strategy(),
            side="long",
        )


def test_gap1_integrated_engine_fails_loud_on_unmatched_schema():
    df = _system3_frame()
    state = SystemState(
        name="System3",
        side="long",
        strategy=_system3_strategy(),
        prepared={"AAA": df},
        candidates_by_date={df.index[0]: [{"symbol": "AAA", "roc200": 1.0}]},
    )
    with pytest.raises(CandidateSchemaError):
        run_integrated_backtest([state], CAPITAL)


# ---------------------------------------------------------------------------
# GAP 2 - System6 is not collapsed to a single date inside a backtest
# ---------------------------------------------------------------------------


def _system6_frame(setup_positions: list[int], periods: int = 20) -> pd.DataFrame:
    idx = pd.bdate_range("2024-05-01", periods=periods)
    close = [100.0 + i for i in range(periods)]
    setup = [i in setup_positions for i in range(periods)]
    return pd.DataFrame(
        {
            "Open": [c - 1 for c in close],
            "High": [c + 8 for c in close],
            "Low": [c - 2 for c in close],
            "Close": close,
            "Volume": [5_000_000] * periods,
            "atr10": [2.0] * periods,
            "dollarvolume50": [60_000_000.0] * periods,
            "return_6d": [0.25] * periods,
            "UpTwoDays": [True] * periods,
            "hv50": [20.0] * periods,
            "filter": [True] * periods,
            "setup": setup,
        },
        index=idx,
    )


def _system6_env_forces_latest_only() -> bool:
    try:
        from config.environment import get_env_config

        env = get_env_config()
    except Exception:
        return False
    return bool(getattr(env, "system6_force_latest_only", False)) and not bool(
        getattr(env, "full_scan_today", False)
    )


class _NoPytestMarker:
    """Temporarily hide PYTEST_CURRENT_TEST.

    core/system6.py skips its forced-latest_only switch while pytest is running,
    so the collapse this gap is about is invisible from inside a test unless the
    marker is removed for the duration of the call.
    """

    def __enter__(self):
        self._saved = os.environ.pop("PYTEST_CURRENT_TEST", None)
        return self

    def __exit__(self, *exc):
        if self._saved is not None:
            os.environ["PYTEST_CURRENT_TEST"] = self._saved
        return False


def test_gap2_system6_collapses_outside_backtest_context():
    """The today-oriented fast path is still forced when not backtesting."""
    if not _system6_env_forces_latest_only():
        pytest.skip("SYSTEM6_FORCE_LATEST_ONLY disabled in this environment")
    from core.system6 import generate_candidates_system6

    prepared = {"AAA": _system6_frame([5, 9, 13])}
    with _NoPytestMarker():
        assert not in_backtest_context()
        by_date, _merged = generate_candidates_system6(
            prepared, top_n=10, latest_only=False
        )
    assert len(by_date) <= 1, "live/today path must keep collapsing to one date"


def test_gap2_system6_keeps_full_history_inside_backtest_context():
    from core.system6 import generate_candidates_system6

    setups = [5, 9, 13]
    prepared = {"AAA": _system6_frame(setups)}
    with _NoPytestMarker(), backtest_context():
        assert in_backtest_context()
        by_date, _merged = generate_candidates_system6(
            prepared, top_n=10, latest_only=False
        )
    assert len(by_date) == len(setups), (
        "backtest must see every setup date, got " f"{sorted(by_date)}"
    )


def test_gap2_system6_books_trades_on_more_than_one_date():
    from core.system6 import generate_candidates_system6
    from strategies.system6_strategy import System6Strategy

    prepared = {"AAA": _system6_frame([5, 9, 13])}
    with _NoPytestMarker(), backtest_context():
        by_date, _merged = generate_candidates_system6(
            prepared, top_n=10, latest_only=False
        )
    trades, _logs = simulate_trades_with_risk(
        by_date, prepared, CAPITAL, System6Strategy(), side="short"
    )
    assert not trades.empty, "System6 must book trades in a backtest"
    assert trades["entry_date"].nunique() > 1, (
        "System6 backtest history must span more than one entry date, got "
        f"{sorted(trades['entry_date'].unique())}"
    )


# ---------------------------------------------------------------------------
# GAP 3a - System1
# ---------------------------------------------------------------------------


def test_normalize_ohlc_frame_adds_index_and_upper_aliases():
    raw = pd.DataFrame(
        {
            "date": pd.bdate_range("2024-05-01", periods=3),
            "open": [1.0, 2.0, 3.0],
            "high": [2.0, 3.0, 4.0],
            "low": [0.5, 1.5, 2.5],
            "close": [1.5, 2.5, 3.5],
            "volume": [10, 20, 30],
        }
    )
    out = normalize_ohlc_frame(raw)
    assert isinstance(out.index, pd.DatetimeIndex)
    for canonical in ("Open", "High", "Low", "Close", "Volume"):
        assert canonical in out.columns
    # additive: the lowercase originals survive
    for lower in ("open", "high", "low", "close", "volume"):
        assert lower in out.columns
    assert list(out["Close"]) == [1.5, 2.5, 3.5]


def _system1_frame(periods: int = 15) -> pd.DataFrame:
    idx = pd.bdate_range("2024-05-01", periods=periods)
    close = [100.0 + i * 2 for i in range(periods)]
    return pd.DataFrame(
        {
            "Open": [c - 1 for c in close],
            "High": [c + 1 for c in close],
            "Low": [c - 2 for c in close],
            "Close": close,
            "Volume": [10_000_000] * periods,
            "atr20": [1.0] * periods,
            "sma25": [110.0] * periods,
            "sma50": [90.0] * periods,
            "sma200": [80.0] * periods,
            "roc200": [0.5] * periods,
            "dollarvolume20": [200_000_000.0] * periods,
        },
        index=idx,
    )


def test_gap3a_system1_full_scan_generates_candidates_without_setup_column():
    from core.system1 import generate_candidates_system1

    prepared = {"AAA": _system1_frame()}
    assert "setup" not in prepared["AAA"].columns
    by_date, _merged, _diag = generate_candidates_system1(
        prepared, top_n=5, latest_only=False, include_diagnostics=True
    )
    assert by_date, "System1 full scan must produce candidates via the predicate"
    first = by_date[sorted(by_date)[0]]
    assert first[0]["symbol"] == "AAA"


def test_gap3a_bulk_entry_dates_match_the_canonical_resolver():
    """The bulk NYSE lookup must agree with resolve_signal_entry_date exactly."""
    from common.utils_spy import resolve_signal_entry_date
    from core.system1 import _resolve_entry_dates_bulk

    dates = list(pd.bdate_range("2024-07-01", periods=40))
    bulk = _resolve_entry_dates_bulk(dates)
    for date in dates:
        assert pd.Timestamp(bulk[date]) == pd.Timestamp(
            resolve_signal_entry_date(date)
        ), f"bulk entry date diverged on {date}"


def test_gap3a_system1_books_trades():
    from core.system1 import generate_candidates_system1
    from strategies.system1_strategy import System1Strategy

    prepared = {"AAA": _system1_frame()}
    by_date, _merged, _diag = generate_candidates_system1(
        prepared, top_n=5, latest_only=False, include_diagnostics=True
    )
    trades, _logs = simulate_trades_with_risk(
        by_date, prepared, CAPITAL, System1Strategy(), side="long"
    )
    assert not trades.empty, "System1 must book trades in a backtest"


# ---------------------------------------------------------------------------
# GAP 3b - System7
# ---------------------------------------------------------------------------


def _spy_raw_base_cache_shape(periods: int = 150) -> pd.DataFrame:
    """A SPY frame exactly as load_base_cache hands it over.

    Integer index, all-lowercase columns, and *no* System7 indicators.
    """
    idx = pd.bdate_range("2023-05-01", periods=periods)
    close = [400.0 + (i % 7) for i in range(periods)]
    low = [c - 2 for c in close]
    high = [c + 2 for c in close]
    low[120] = 300.0  # new 50-day low -> setup
    return pd.DataFrame(
        {
            "date": idx,
            "open": [c - 1 for c in close],
            "high": high,
            "low": low,
            "close": close,
            "volume": [90_000_000] * periods,
        }
    )


def test_gap3b_system7_prepare_derives_indicators_in_backtest_context():
    from core.system7 import prepare_data_vectorized_system7

    raw = {"SPY": _spy_raw_base_cache_shape()}
    with backtest_context():
        prepared = prepare_data_vectorized_system7(raw, reuse_indicators=False)
    assert "SPY" in prepared, "System7 must prepare SPY from a base-cache frame"
    spy = prepared["SPY"]
    assert isinstance(spy.index, pd.DatetimeIndex)
    for column in ("ATR50", "atr50", "min_50", "max_70", "setup", "Low"):
        assert column in spy.columns
    assert bool(spy["setup"].any()), "the engineered 50-day low must set up"


def test_gap3b_system7_prepare_still_hard_stops_outside_backtest_context():
    """live guard intact: a SPY cache without atr50 must not be papered over."""
    from core.system7 import prepare_data_vectorized_system7

    raw = {"SPY": _spy_raw_base_cache_shape()}
    assert not in_backtest_context()
    prepared = prepare_data_vectorized_system7(raw, reuse_indicators=False)
    assert "SPY" not in prepared, (
        "outside a backtest the missing-indicator guard must still drop SPY "
        "(IMMEDIATE_STOP), not silently derive indicators"
    )


def test_gap3b_system7_books_trades():
    from core.system7 import (
        generate_candidates_system7,
        prepare_data_vectorized_system7,
    )
    from strategies.system7_strategy import System7Strategy

    raw = {"SPY": _spy_raw_base_cache_shape()}
    with backtest_context():
        prepared = prepare_data_vectorized_system7(raw, reuse_indicators=False)
        by_date, _merged = generate_candidates_system7(prepared, latest_only=False)
    assert by_date, "System7 full scan must produce candidates"
    trades, _logs = simulate_trades_with_risk(
        by_date, prepared, CAPITAL, System7Strategy(), side="short"
    )
    assert not trades.empty, "System7 must book trades in a backtest"
