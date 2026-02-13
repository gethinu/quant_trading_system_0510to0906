from __future__ import annotations

import pandas as pd

from core.system1 import generate_candidates_system1


def _make_prepared(
    symbol: str, dates: pd.DatetimeIndex, roc_vals: list[float]
) -> pd.DataFrame:
    """Helper to build a minimal prepared DataFrame for System1.

    We only include columns referenced in fast/full candidate paths:
      - Close, roc200, setup, filter, sma25, sma50, sma200, dollarvolume20
    """
    assert len(dates) == len(roc_vals)
    df = pd.DataFrame(
        {
            "Close": [100.0 + i for i in range(len(dates))],
            "roc200": roc_vals,
            # Ensure setup passes (True) so both paths include symbol directly
            "setup": [True] * len(dates),
            # Additional columns used only when setup False (we keep simple True case)
            "filter": [True] * len(dates),
            "sma25": [50.0] * len(dates),
            "sma50": [40.0] * len(dates),
            # System1 predicate needs sma200 and dollarvolume20 for full scan
            "sma200": [90.0] * len(dates),
            "dollarvolume20": [60_000_000.0] * len(dates),
        },
        index=dates,
    )
    return df


def test_system1_latest_only_parity_latest_day():
    """latest_only=True の高速パスと従来パスの『最新日』候補が同一であることを検証。

    条件:
      - 5銘柄 / 3 営業日
      - top_n=3
      - setup は全て True / roc200>0 の上位3件が一致するはず
    確認:
      1. 最新日のシンボル順序 (roc200 降順) が一致
      2. 各シンボルの roc200 / close 値が一致
    """

    dates = pd.date_range("2024-01-02", periods=3, freq="B")  # 例: 2024-01-02,03,04
    prepared = {
        # symbol: roc200 時系列 (最後の値でランキング)
        "AAA": _make_prepared("AAA", dates, [1.0, 2.0, 10.0]),
        "BBB": _make_prepared("BBB", dates, [0.5, 4.0, 8.0]),
        "CCC": _make_prepared("CCC", dates, [2.0, 1.0, 5.0]),
        "DDD": _make_prepared("DDD", dates, [3.0, 3.5, 3.0]),
        "EEE": _make_prepared("EEE", dates, [0.1, 0.2, -1.0]),  # 負値で除外対象
    }

    top_n = 3

    # Fast path (latest_only)
    fast_by_date, fast_df, _ = generate_candidates_system1(
        prepared, top_n=top_n, latest_only=True
    )
    assert fast_df is not None, "Fast path returned None unexpectedly"
    assert fast_by_date, "Fast path returned empty candidates unexpectedly"

    # Candidates are keyed by entry_date (next NYSE trading day after signal date)
    fast_latest_entry = max(fast_by_date.keys())

    # Full path
    full_by_date, full_df, _ = generate_candidates_system1(
        prepared, top_n=top_n, latest_only=False
    )
    assert full_df is not None, "Full path returned None unexpectedly"
    assert (
        fast_latest_entry in full_by_date
    ), "Full path missing latest entry day candidates"

    # Extract symbol order
    fast_symbols = list(fast_by_date[fast_latest_entry].keys())
    full_symbols = [c["symbol"] for c in full_by_date[fast_latest_entry]]

    # Expected ranking manually (descending roc200 positive values only)
    expected_rank = ["AAA", "BBB", "CCC"]  # 10 > 8 > 5
    assert fast_symbols == expected_rank
    assert full_symbols == expected_rank

    # Cross-validate metric equality for each symbol
    fast_map = fast_by_date[fast_latest_entry]
    full_map = {c["symbol"]: c for c in full_by_date[fast_latest_entry]}
    for sym in expected_rank:
        assert sym in fast_map
        assert sym in full_map
        assert float(fast_map[sym]["roc200"]) == float(full_map[sym]["roc200"])  # type: ignore[index]
        assert float(fast_map[sym]["close"]) == float(full_map[sym]["close"])  # type: ignore[index]
