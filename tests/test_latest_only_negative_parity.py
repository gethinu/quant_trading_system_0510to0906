import pandas as pd

from core.system1 import generate_candidates_system1
from core.system6 import generate_candidates_system6

# シナリオ a: 最終日の setup 条件が偽で双方候補ゼロ


def _make_df_no_setup_latest():
    """System1 の filter は満たすが、setup 条件は満たさない DataFrame を返す。

    filter: (Close >= 5.0) & (dollarvolume20 > 25_000_000)
    setup (docs 準拠, D1 audit 2026-07-02 以降): filter & (sma25 > sma50) & (roc200 > 0)
    """
    dates = pd.date_range(end=pd.Timestamp("2024-12-31"), periods=5, freq="D")
    df = pd.DataFrame(
        {
            "Open": [10, 11, 12, 13, 14],
            "High": [10, 11, 12, 13, 14],
            "Low": [9, 10, 11, 12, 13],
            "Close": [10, 11, 12, 13, 14],  # >= 5.0 (filter OK)
            "Volume": [5_000_000] * 5,
            "dollarvolume20": [30_000_000] * 5,  # > 25_000_000 (filter OK)
            "sma200": [20, 21, 22, 23, 24],  # legacy: 参考値 (setup 判定には未使用)
            "roc200": [-0.1, -0.2, -0.3, -0.4, -0.5],  # roc200 < 0 (setup NG)
            "sma25": [9, 10, 11, 12, 13],
            "sma50": [9, 10, 11, 12, 13],  # sma25 == sma50 (setup NG)
            "atr20": [0.2] * 5,
        },
        index=dates,
    )
    # filter 列を明示的に追加
    df["filter"] = (df["Close"] >= 5.0) & (df["dollarvolume20"] > 25_000_000)
    # setup 列は docs 準拠: filter & (sma25 > sma50) & (roc200 > 0)
    df["setup"] = df["filter"] & (df["sma25"] > df["sma50"]) & (df["roc200"] > 0)
    return df


# シナリオ b: System6 用で return_6d が最終行 NaN -> fast でも full でも当日エントリなし


def _make_df_system6_nan():
    dates = pd.date_range(end=pd.Timestamp("2024-12-31"), periods=7, freq="D")
    returns6 = [0.01, 0.02, 0.03, 0.04, 0.05, 0.06, float("nan")]
    df = pd.DataFrame(
        {
            "date": dates,
            "open": [10, 11, 12, 13, 14, 15, 16],
            "high": [10, 11, 12, 13, 14, 15, 16],
            "low": [9, 10, 11, 12, 13, 14, 15],
            "close": [10, 11, 12, 13, 14, 15, 16],
            "volume": [1000] * 7,
            "return_6d": returns6,
            "adx7": [50] * 7,
        }
    )
    return df


def test_system1_negative_parity_empty_latest_day():
    """System1 で setup 不成立時、latest_only と full_scan が同等に空を返すことを検証。

    diagnostics 対応: include_diagnostics=True を追加し、3要素タプルを展開。
    """
    prepared = {"TEST1": _make_df_no_setup_latest()}
    result_fast = generate_candidates_system1(
        prepared, latest_only=True, include_diagnostics=True
    )
    result_full = generate_candidates_system1(
        prepared, latest_only=False, include_diagnostics=True
    )

    # タプル展開: diagnostics 対応
    if isinstance(result_fast, tuple) and len(result_fast) == 3:
        fast_by_date, _, diag_fast = result_fast
    else:
        fast_by_date, _ = result_fast  # type: ignore[misc]
        diag_fast = None

    if isinstance(result_full, tuple) and len(result_full) == 3:
        full_by_date, _, diag_full = result_full
    else:
        full_by_date, _ = result_full  # type: ignore[misc]
        diag_full = None

    # fast path dict empty
    assert fast_by_date == {} or all(len(v) == 0 for v in fast_by_date.values())
    # full path has no latest date key producing entries
    assert full_by_date == {} or all(len(v) == 0 for v in full_by_date.values())

    # diagnostics 検証 (オプショナル: 必須キーの存在確認)
    if diag_fast:
        assert "ranking_source" in diag_fast
        assert diag_fast["ranking_source"] == "latest_only"
    if diag_full:
        assert "ranking_source" in diag_full
        assert diag_full["ranking_source"] == "full_scan"


def test_system6_negative_parity_no_entry_due_to_nan():
    """System6 で return_6d が NaN の場合、双方が空を返すことを検証。

    diagnostics 対応: include_diagnostics=True を追加し、3要素タプル展開。
    """
    prepared = {"TEST6": _make_df_system6_nan()}
    result_fast = generate_candidates_system6(
        prepared, latest_only=True, include_diagnostics=True
    )
    result_full = generate_candidates_system6(
        prepared, latest_only=False, include_diagnostics=True
    )

    # タプル展開: diagnostics 対応
    if isinstance(result_fast, tuple) and len(result_fast) == 3:
        fast_by_date, _, diag_fast = result_fast
    else:
        fast_by_date, _ = result_fast  # type: ignore[misc]
        diag_fast = None

    if isinstance(result_full, tuple) and len(result_full) == 3:
        full_by_date, _, diag_full = result_full
    else:
        full_by_date, _ = result_full  # type: ignore[misc]
        diag_full = None

    assert fast_by_date == {} or all(len(v) == 0 for v in fast_by_date.values())
    assert full_by_date == {} or all(len(v) == 0 for v in full_by_date.values())

    # diagnostics 検証 (オプショナル)
    if diag_fast:
        assert "ranking_source" in diag_fast
    if diag_full:
        assert "ranking_source" in diag_full
