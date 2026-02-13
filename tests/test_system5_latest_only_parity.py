from __future__ import annotations

import pandas as pd

from core.system5 import generate_candidates_system5


def _make_prepared(
    symbol: str, dates: pd.DatetimeIndex, adx_vals: list[float]
) -> pd.DataFrame:
    assert len(dates) == len(adx_vals)
    close_vals = [120.0 + i for i in range(len(dates))]
    atr10_vals = [2.0] * len(dates)
    return pd.DataFrame(
        {
            "Close": close_vals,
            "adx7": adx_vals,
            "atr10": atr10_vals,
            "sma100": [100.0] * len(dates),
            "rsi3": [40.0] * len(dates),
            "avgvolume50": [800_000.0] * len(dates),
            "dollarvolume50": [15_000_000.0] * len(dates),
            "setup": [True] * len(dates),
            "atr_pct": [0.06] * len(dates),
        },
        index=dates,
    )


def test_system5_latest_only_parity_latest_day():
    dates = pd.date_range("2024-05-13", periods=4, freq="B")
    prepared = {
        # 最終日の adx7: 70, 65, 60, 54, 49 (ADX>55 の 3銘柄のみ残る)
        "AAA": _make_prepared("AAA", dates, [20, 30, 40, 70]),
        "BBB": _make_prepared("BBB", dates, [19, 28, 38, 65]),
        "CCC": _make_prepared("CCC", dates, [18, 27, 37, 60]),
        "DDD": _make_prepared("DDD", dates, [17, 26, 36, 54]),
        "EEE": _make_prepared("EEE", dates, [16, 25, 34, 49]),  # 閾値以下で除外
    }

    top_n = 3
    fast_by_date, fast_df = generate_candidates_system5(
        prepared, top_n=top_n, latest_only=True
    )
    assert fast_df is not None
    assert fast_by_date
    fast_latest_entry = max(fast_by_date.keys())
    full_by_date, full_df = generate_candidates_system5(
        prepared, top_n=top_n, latest_only=False
    )
    assert full_df is not None and fast_latest_entry in full_by_date

    fast_syms = list(fast_by_date[fast_latest_entry].keys())  # adx7 desc
    full_syms = list(full_by_date[fast_latest_entry].keys())
    expected = ["AAA", "BBB", "CCC"]  # 70 > 65 > 60
    assert fast_syms == expected
    assert full_syms == expected

    fast_map = fast_by_date[fast_latest_entry]
    full_map = full_by_date[fast_latest_entry]
    for sym in expected:
        assert float(fast_map[sym]["adx7"]) == float(full_map[sym]["adx7"])  # type: ignore[index]
        assert float(fast_map[sym]["close"]) == float(full_map[sym]["close"])  # type: ignore[index]
