from __future__ import annotations

import pandas as pd

from core.system4 import generate_candidates_system4


def _make_prepared(
    symbol: str, dates: pd.DatetimeIndex, rsi_vals: list[float]
) -> pd.DataFrame:
    assert len(dates) == len(rsi_vals)
    return pd.DataFrame(
        {
            "Close": [200.0 + i for i in range(len(dates))],
            "rsi4": rsi_vals,  # System4 uses ascending ranking with rsi4 < 30 gate
            "setup": [True] * len(dates),
            "atr_ratio": [1.0] * len(dates),
            "sma200": [180.0] * len(dates),
        },
        index=dates,
    )


def test_system4_latest_only_parity_latest_day():
    dates = pd.date_range("2024-03-11", periods=3, freq="B")
    prepared = {
        # 最終日の rsi4: 5, 12, 18, 29 (いずれも <30)
        "AAA": _make_prepared("AAA", dates, [40, 35, 5]),
        "BBB": _make_prepared("BBB", dates, [50, 45, 12]),
        "CCC": _make_prepared("CCC", dates, [60, 55, 18]),
        "DDD": _make_prepared("DDD", dates, [55, 50, 29]),
        # 31 (>30) なのでゲートで除外される
        "EEE": _make_prepared("EEE", dates, [48, 47, 31]),
    }

    top_n = 3
    fast_by_date, fast_df = generate_candidates_system4(
        prepared, top_n=top_n, latest_only=True
    )
    assert fast_df is not None
    assert fast_by_date
    fast_latest_entry = max(fast_by_date.keys())
    full_by_date, full_df = generate_candidates_system4(
        prepared, top_n=top_n, latest_only=False
    )
    assert full_df is not None and fast_latest_entry in full_by_date

    # Ascending rsi4 => 5 < 12 < 18 < 29 (top_n=3 keeps first 3)
    fast_syms = list(fast_by_date[fast_latest_entry].keys())  # ascending
    full_syms = list(full_by_date[fast_latest_entry].keys())
    expected = ["AAA", "BBB", "CCC"]
    assert fast_syms == expected
    assert full_syms == expected

    fast_map = fast_by_date[fast_latest_entry]
    full_map = full_by_date[fast_latest_entry]
    for sym in expected:
        assert float(fast_map[sym]["rsi4"]) == float(full_map[sym]["rsi4"])  # type: ignore[index]
        assert float(fast_map[sym]["close"]) == float(full_map[sym]["close"])  # type: ignore[index]
