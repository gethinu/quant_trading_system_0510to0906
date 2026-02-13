from __future__ import annotations

import pandas as pd

from core.system2 import generate_candidates_system2


def _make_prepared(
    symbol: str, dates: pd.DatetimeIndex, adx_vals: list[float]
) -> pd.DataFrame:
    assert len(dates) == len(adx_vals)
    return pd.DataFrame(
        {
            "Close": [50.0 + i for i in range(len(dates))],
            "adx7": adx_vals,
            "setup": [True] * len(dates),
            "rsi3": [10.0 + i for i in range(len(dates))],
        },
        index=dates,
    )


def test_system2_latest_only_parity_latest_day():
    dates = pd.date_range("2024-02-05", periods=4, freq="B")
    prepared = {
        "AAA": _make_prepared("AAA", dates, [5, 10, 15, 40]),
        "BBB": _make_prepared("BBB", dates, [3, 8, 12, 30]),
        "CCC": _make_prepared("CCC", dates, [2, 7, 11, 25]),
        "DDD": _make_prepared("DDD", dates, [1, 6, 9, 5]),
        # 0 or negative final -> 除外される
        "EEE": _make_prepared("EEE", dates, [4, 5, 6, 0]),
    }

    top_n = 3
    fast_by_date, fast_df = generate_candidates_system2(
        prepared, top_n=top_n, latest_only=True
    )
    assert fast_df is not None
    assert fast_by_date
    fast_latest_entry = max(fast_by_date.keys())
    full_by_date, full_df = generate_candidates_system2(
        prepared, top_n=top_n, latest_only=False
    )
    assert full_df is not None and fast_latest_entry in full_by_date

    fast_syms = list(fast_by_date[fast_latest_entry].keys())  # ranking desc by adx7
    full_syms = list(full_by_date[fast_latest_entry].keys())
    expected = ["AAA", "BBB", "CCC"]  # 40 > 30 > 25
    assert fast_syms == expected
    assert full_syms == expected

    fast_map = fast_by_date[fast_latest_entry]
    full_map = full_by_date[fast_latest_entry]
    for sym in expected:
        assert float(fast_map[sym]["adx7"]) == float(full_map[sym]["adx7"])  # type: ignore[index]
        assert float(fast_map[sym]["close"]) == float(full_map[sym]["close"])  # type: ignore[index]
