from __future__ import annotations

from pathlib import Path

import pandas as pd

from common.exit_signals import build_exit_signals_from_tracker
from common.position_tracker import load_tracker, update_positions_from_signals


def _make_price_df(
    dates: list[str], close: list[float], high: list[float], low: list[float]
) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": pd.to_datetime(dates),
            "open": close,
            "high": high,
            "low": low,
            "close": close,
        }
    )


def test_build_exit_signals_from_tracker() -> None:
    df_aaa = _make_price_df(
        ["2025-01-01", "2025-01-02", "2025-01-03"],
        [100, 98, 92],
        [101, 100, 96],
        [99, 95, 90],
    )
    df_bbb = _make_price_df(
        ["2025-01-01", "2025-01-02", "2025-01-03"],
        [100, 99, 97],
        [101, 101, 100],
        [99, 96, 94],
    )
    df_ccc = _make_price_df(
        ["2025-01-01", "2025-01-02", "2025-01-03"],
        [100, 100, 100],
        [101, 101, 101],
        [99, 99, 99],
    )

    spy_dates = pd.date_range("2025-01-01", periods=70, freq="D")
    spy_high = list(range(100, 170))
    spy_close = [val - 1 for val in spy_high]
    spy_low = [val - 2 for val in spy_high]
    df_spy = _make_price_df(
        [d.strftime("%Y-%m-%d") for d in spy_dates],
        spy_close,
        spy_high,
        spy_low,
    )

    def loader(symbol: str, _profile: str) -> pd.DataFrame:
        if symbol == "AAA":
            return df_aaa
        if symbol == "BBB":
            return df_bbb
        if symbol == "CCC":
            return df_ccc
        if symbol == "SPY":
            return df_spy
        return pd.DataFrame()

    tracker = {
        "AAA": {
            "system": "system1",
            "side": "long",
            "entry_date": "2025-01-01",
            "entry_price": 100,
            "stop_price": 90,
        },
        "BBB": {
            "system": "system2",
            "side": "short",
            "entry_date": "2025-01-01",
            "entry_price": 100,
            "profit_target_price": 95,
        },
        "CCC": {
            "system": "system3",
            "side": "long",
            "entry_date": "2025-01-01",
            "entry_price": 100,
            "max_exit_date": "2025-01-02",
        },
        "SPY": {
            "system": "system7",
            "side": "short",
        },
    }

    today = pd.Timestamp(spy_dates[-1]).normalize()
    exit_df = build_exit_signals_from_tracker(
        tracker, today=today, price_loader=loader
    )

    assert not exit_df.empty
    reasons = {row["symbol"]: row["exit_reason"] for _, row in exit_df.iterrows()}
    assert reasons["AAA"] == "stop_loss"
    assert reasons["BBB"] == "profit_target"
    assert reasons["CCC"] == "time_based"
    assert reasons["SPY"] == "new_70day_high"


def test_update_positions_from_signals_stores_meta(tmp_path: Path) -> None:
    signals = pd.DataFrame(
        [
            {
                "symbol": "XYZ",
                "system": "system5",
                "side": "long",
                "entry_date": "2025-01-02",
                "entry_price": 50.0,
                "stop_price": 45.0,
                "profit_target_price": 60.0,
                "trailing_stop_pct": 0.25,
                "use_trailing_stop": True,
                "max_holding_days": 6,
            }
        ]
    )
    tracker_path = tmp_path / "tracker.json"
    update_positions_from_signals(signals, path=tracker_path)
    tracker = load_tracker(path=tracker_path)

    assert "XYZ" in tracker
    info = tracker["XYZ"]
    assert info["system"] == "system5"
    assert info["side"] == "long"
    assert info["stop_price"] == 45.0
    assert info["profit_target_price"] == 60.0
    assert info["trailing_stop_pct"] == 0.25
