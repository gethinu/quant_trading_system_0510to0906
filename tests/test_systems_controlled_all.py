"""
tests/test_systems_controlled_all.py

統合制御テスト（systems 1-6）

目的:
 - systems 1〜6 の generate_candidates 系パスを合成データで回し、
     ランキング段階で top-10 が選ばれること（diagnostics['ranked_top_n_count']==10）と
     最終出力のエントリ数が 10 であることを検証する。

テストの契約（簡潔）:
 - 入力: symbol -> pandas.DataFrame の辞書（各 DataFrame は最終行に検査値を含む）
 - 出力: core.generate_candidates_systemX の diagnostics dict に
     'ranked_top_n_count' が含まれ、かつ最終 DataFrame の行数が 10 であること

実行方法（いつでも）:
 - 単体実行:
         python -m pytest -q tests/test_systems_controlled_all.py
 - 補助スクリプト:
         python scripts/run_controlled_tests.py

備考:
 - このファイルは CI やデバッグ時に頻繁に使うため、`docs/README.md` の
     "検証テスト" セクションに必ず追記してある（プロジェクト運用者はここを参照すること）。
 - core 側の戻り値の形式が変わるとテスト側のアンパックを調整する必要があります。
"""

import pandas as pd
import pytest

# Import core generator functions for systems 1-6
from core.system1 import generate_candidates_system1
from core.system2 import generate_candidates_system2
from core.system3 import generate_candidates_system3
from core.system4 import generate_candidates_system4
from core.system5 import generate_candidates_system5
from core.system6 import generate_candidates_system6


def _make_series_frame(
    last_row_values: dict, days: int = 3, end_date: str = "2023-01-10"
):
    """Create a small DataFrame with a few days of data and ensure the last row
    contains the values from last_row_values. Previous rows are filled with
    safe defaults so prepare/generate steps do not fail.
    """
    idx = pd.date_range(
        end=pd.to_datetime(end_date).normalize(), periods=days, freq="D"
    )
    # Build a dict of columns where earlier rows have safe defaults and last row uses
    # provided values
    data: dict = {}
    # collect all column names
    cols = set(last_row_values.keys())
    for col in cols:
        vals = [None] * days
        # fill earlier rows with safe numeric defaults
        for i in range(days - 1):
            vals[i] = 1.0
        vals[-1] = last_row_values.get(col)
        data[col] = vals
    df = pd.DataFrame(data, index=idx)
    return df


def _make_many_rows_frame(
    last_row_values: dict, days: int = 60, end_date: str = "2023-01-10"
):
    """Like _make_series_frame but produce many rows (used for System6 which
    expects >=50 rows in its prepare path).
    """
    idx = pd.date_range(
        end=pd.to_datetime(end_date).normalize(), periods=days, freq="D"
    )
    data: dict = {}
    cols = set(last_row_values.keys())
    for col in cols:
        vals = [1.0] * days
        vals[-1] = last_row_values.get(col)
        data[col] = vals
    df = pd.DataFrame(data, index=idx)
    return df


@pytest.mark.parametrize(
    "system_name",
    [
        "system1",
        "system2",
        "system3",
        "system4",
        "system5",
        "system6",
    ],
)
def test_systems_controlled_top10_latest_only(system_name):
    """Controlled end-to-end smoke test for systems 1-6.

    For each system we create three groups of synthetic symbols:
      - A: Phase2 filter passes but setup fails
      - B: Phase2 filter + setup pass (these are the ranking-eligible group)
      - C: Phase2 filter fails

    We create >=11 B symbols so the ranking will select top-10. We then run
    the strategy through the full ``get_today_signals_for_strategy`` path and
    assert that the diagnostics report ranked_top_n_count == 10 and the
    final signals DataFrame contains 10 rows (entry selection survived).
    """
    # Build prepared dict directly for each core generator to avoid full prepare
    prepared: dict[str, pd.DataFrame] = {}

    def add(sym: str, df: pd.DataFrame):
        prepared[sym] = df

    # Create A group: Phase2 filter pass but setup False
    for i in range(2):
        sym = f"A{i+1}"
        if system_name == "system1":
            vals = {
                "Close": 10.0,
                "dollarvolume20": 100_000_000,
                "sma25": 1.0,
                "sma50": 2.0,
                "roc200": 0.1,
                "atr20": 0.5,
            }
            add(sym, _make_series_frame(vals))
        elif system_name == "system2":
            vals = {
                "Close": 10.0,
                "dollarvolume20": 50_000_000,
                "atr_ratio": 0.05,
                "rsi3": 50.0,
                "twodayup": False,
                "adx7": 10.0,
                "atr10": 0.5,
            }
            add(sym, _make_series_frame(vals))
        elif system_name == "system3":
            vals = {
                "Low": 2.0,
                "AvgVolume50": 1_500_000,
                "atr_ratio": 0.06,
                "Close": 10.0,
                "sma150": 11.0,
                "drop3d": 0.05,
                "dollarvolume20": 30_000_000,
            }
            add(sym, _make_series_frame(vals))
        elif system_name == "system4":
            vals = {
                "Close": 120.0,
                "dollarvolume50": 200_000_000,
                "hv50": 20.0,
                "sma200": 130.0,
                "rsi4": 40.0,
                "atr40": 1.0,
            }
            add(sym, _make_series_frame(vals))
        elif system_name == "system5":
            vals = {
                "Close": 50.0,
                "adx7": 40.0,
                "atr_pct": 0.03,
                "atr10": 0.5,
            }
            add(sym, _make_series_frame(vals))
        elif system_name == "system6":
            vals = {
                "Open": 19.0,
                "High": 21.0,
                "Low": 18.0,
                "Close": 20.0,
                "Volume": 1_000_000,
                "return_6d": 0.1,
                "atr10": 0.5,
                "UpTwoDays": False,
                "dollarvolume50": 20_000_000,
                "hv50": 12.0,
            }
            add(sym, _make_many_rows_frame(vals))

    # B group: ranking-eligible with >=11 symbols
    for j in range(11):
        sym = f"B{j+1}"
        rank_val = float(100 - j)
        if system_name == "system1":
            vals = {
                "Close": 20.0 + j,
                "dollarvolume20": 200_000_000,
                "sma25": 5.0,
                "sma50": 1.0,
                "roc200": rank_val,
                "atr20": 0.6,
            }
            add(sym, _make_series_frame(vals))
        elif system_name == "system2":
            vals = {
                "Close": 20.0,
                "dollarvolume20": 80_000_000,
                "atr_ratio": 0.05,
                "rsi3": 95.0,
                "twodayup": True,
                "adx7": rank_val,
                "atr10": 0.4,
            }
            add(sym, _make_series_frame(vals))
        elif system_name == "system3":
            vals = {
                "Low": 2.0,
                "AvgVolume50": 2_000_000,
                "atr_ratio": 0.08,
                "Close": 20.0,
                "sma150": 10.0,
                "drop3d": float(0.30 - j * 0.01),
                "dollarvolume20": 50_000_000,
            }
            add(sym, _make_series_frame(vals))
        elif system_name == "system4":
            vals = {
                "Close": 200.0,
                "dollarvolume50": 200_000_000,
                "hv50": 15.0,
                "sma200": 150.0,
                "rsi4": float(5 + j),
                "atr40": 1.2,
            }
            add(sym, _make_series_frame(vals))
        elif system_name == "system5":
            # audit-remediation 2026-07-02: spec setup now enforces
            # Close>SMA100+ATR10 (60 > 50+0.6) and RSI3<50 in addition to
            # ADX7>55 and ATR_Pct>2.5%.
            vals = {
                "Close": 60.0,
                "adx7": rank_val + 50.0,
                "atr_pct": 0.03 + 0.01,
                "atr10": 0.6,
                "sma100": 50.0,
                "rsi3": 30.0,
            }
            add(sym, _make_series_frame(vals))
        elif system_name == "system6":
            vals = {
                "Open": 39.0,
                "High": 41.0,
                "Low": 38.0,
                "Close": 40.0,
                "Volume": 1_000_000,
                "return_6d": 1.0 + j * 0.1,
                "atr10": 0.5,
                "UpTwoDays": True,
                "dollarvolume50": 50_000_000,
                "hv50": 15.0,
            }
            add(sym, _make_many_rows_frame(vals))

    # C group: filter fail
    for k in range(2):
        sym = f"C{k+1}"
        if system_name == "system1":
            vals = {"Close": 1.0, "dollarvolume20": 1_000_000, "roc200": -1.0}
            add(sym, _make_series_frame(vals))
        elif system_name == "system2":
            vals = {
                "Close": 2.0,
                "dollarvolume20": 1_000_000,
                "atr_ratio": 0.01,
                "rsi3": 30.0,
                "twodayup": False,
                "adx7": 1.0,
            }
            add(sym, _make_series_frame(vals))
        elif system_name == "system3":
            vals = {
                "Low": 0.5,
                "AvgVolume50": 500_000,
                "atr_ratio": 0.01,
                "Close": 3.0,
                "sma150": 5.0,
                "drop3d": 0.01,
                "dollarvolume20": 1_000_000,
            }
            add(sym, _make_series_frame(vals))
        elif system_name == "system4":
            vals = {
                "Close": 10.0,
                "dollarvolume50": 10_000_000,
                "hv50": 5.0,
                "sma200": 100.0,
                "rsi4": 50.0,
                "atr40": 0.2,
            }
            add(sym, _make_series_frame(vals))
        elif system_name == "system5":
            vals = {
                "Close": 4.0,
                "adx7": 10.0,
                "atr_pct": 0.01,
                "atr10": 0.1,
            }
            add(sym, _make_series_frame(vals))
        elif system_name == "system6":
            vals = {
                "Open": 3.0,
                "High": 4.0,
                "Low": 2.0,
                "Close": 4.0,
                "Volume": 1000,
                "return_6d": 0.01,
                "atr10": 0.1,
                "UpTwoDays": False,
            }
            add(sym, _make_many_rows_frame(vals))

    # Map system name to generator function
    gen_map = {
        "system1": generate_candidates_system1,
        "system2": generate_candidates_system2,
        "system3": generate_candidates_system3,
        "system4": generate_candidates_system4,
        "system5": generate_candidates_system5,
        "system6": generate_candidates_system6,
    }

    gen_fn = gen_map.get(system_name)
    assert gen_fn is not None, f"No generator for {system_name}"

    # Sanity: ensure we created >=11 B symbols to trigger top-10 ranking
    b_count = sum(1 for s in prepared.keys() if str(s).startswith("B"))
    assert (
        b_count >= 11
    ), f"Test setup error: expected >=11 B-group symbols, got {b_count}"

    # Call generator in latest_only mode and request diagnostics
    result = gen_fn(
        prepared,
        top_n=10,
        latest_only=True,
        include_diagnostics=True,
        latest_mode_date=pd.Timestamp("2023-01-10"),
    )

    # Unpack result (generators may return (by_date, df, diag) or (df, diag))
    df_all = None
    diagnostics = {}
    if isinstance(result, tuple):
        if len(result) == 3:
            _, df_all, diagnostics = result
        elif len(result) == 2:
            df_all, diagnostics = result
        else:
            # unexpected tuple shape
            df_all = None
            diagnostics = {}
    elif hasattr(result, "get") or isinstance(result, dict):
        # Some implementations might return a dict-like; be defensive
        diagnostics = dict(result)
    else:
        # Unknown return type — leave diagnostics empty to trigger clear failure
        diagnostics = {}

    # Validate diagnostics shape
    assert isinstance(
        diagnostics, dict
    ), f"{system_name}: diagnostics must be a dict, got {type(diagnostics)}"

    # Require ranked_top_n_count key to exist; fail with available keys for clarity
    if "ranked_top_n_count" not in diagnostics:
        available = list(diagnostics.keys())
        raise AssertionError(
            f"{system_name}: diagnostics missing 'ranked_top_n_count'. Available keys: {available}"
        )

    ranked = int(diagnostics.get("ranked_top_n_count", 0))
    assert ranked == 10, (
        f"{system_name}: expected diagnostics['ranked_top_n_count']==10, got {ranked}. "
        f"Available diag keys: {list(diagnostics.keys())}"
    )

    # For generators that return a DataFrame of selected candidates, ensure length==10
    final_count = 0
    if df_all is not None:
        try:
            final_count = len(df_all)
        except Exception:
            final_count = 0

    assert final_count == 10, (
        f"{system_name}: expected final entry count 10, got {final_count}. "
        f"Diagnostics keys: {list(diagnostics.keys())}"
    )
