import numpy as np
import pandas as pd
import pytest

from core.system5 import DEFAULT_ATR_PCT_THRESHOLD
from strategies.constants import FALLBACK_EXIT_DAYS_DEFAULT
from strategies.system5_strategy import System5Strategy

# Import needed functions from archived version
try:
    from tools.archive.system5_old import _compute_indicators_frame, _rename_ohlcv
except ImportError:
    # Fallback implementations if not available
    def _compute_indicators_frame(df):
        """Minimal fallback implementation"""
        from common.indicators_common import add_indicators

        return add_indicators(df)

    def _rename_ohlcv(df):
        """Minimal fallback implementation"""
        return df.rename(columns=str.upper)


@pytest.fixture
def dummy_data():
    dates = pd.date_range("2024-01-01", periods=150, freq="B")
    df = pd.DataFrame(
        {
            "Open": [100] * 150,
            "High": [101] * 150,
            "Low": [99] * 150,
            "Close": [100] * 150,
            "Volume": [1_000_000] * 150,
        },
        index=dates,
    )
    return {"DUMMY": df}


@pytest.fixture
def high_adx_data():
    """System5用の高ADX＋ミーンリバージョン条件を満たすデータ"""
    dates = pd.date_range("2024-01-01", periods=150, freq="B")

    # 強いトレンド後のプルバック（高ADX条件を満たすため）
    prices = []
    volumes = []

    for i in range(150):
        if i < 100:
            # 上昇トレンド期間（高ADXを作るため）
            price = 50 + i * 0.8 + np.sin(i * 0.2) * 3  # より大きな変動
        else:
            # プルバック期間（RSI3 < 50、but Close > SMA100 + ATR10）
            trend_price = 50 + 100 * 0.8
            pullback = (i - 99) * -0.3
            price = trend_price + pullback + np.random.normal(0, 1)

        prices.append(max(price, 10))
        volumes.append(600_000 + np.random.randint(0, 400_000))  # > 500,000

    df = pd.DataFrame(
        {
            "Open": prices,
            "High": [p * 1.025 for p in prices],
            "Low": [p * 0.975 for p in prices],
            "Close": prices,
            "Volume": volumes,
        },
        index=dates,
    )

    return {"HIGH_ADX": df}


def test_minimal_indicators(dummy_data):
    strategy = System5Strategy()
    processed = strategy.prepare_minimal_for_test(dummy_data)
    assert "SMA100" in processed["DUMMY"].columns


def test_core_indicators_computation():
    """System5コア指標計算のテスト"""
    dates = pd.date_range("2024-01-01", periods=150, freq="B")

    # トレンドのあるデータでテスト（ADXが機能するように）
    price_data = []
    for i in range(150):
        if i < 75:
            # 上昇トレンド
            price = 100 + i * 0.5 + np.random.normal(0, 0.5)
        else:
            # 下降トレンド
            price = 100 + 75 * 0.5 - (i - 74) * 0.3 + np.random.normal(0, 0.5)
        price_data.append(max(price, 20))  # 最低価格保証

    df = pd.DataFrame(
        {
            "Open": price_data,
            "High": [p * 1.02 for p in price_data],
            "Low": [p * 0.98 for p in price_data],
            "Close": price_data,
            "Volume": [800_000] * 150,
        },
        index=dates,
    )

    result = _compute_indicators_frame(df)

    # 必要な指標が計算されているかチェック
    expected_columns = [
        "SMA100",
        "ATR10",
        "ADX7",
        "RSI3",
        "AvgVolume50",
        "DollarVolume50",
        "ATR_Pct",
        "filter",
        "setup",
    ]
    for col in expected_columns:
        assert col in result.columns, f"{col} column missing"

    # 指標の妥当性チェック
    assert result["ATR10"].min() >= 0, "ATR should be positive"

    # ADX7の範囲チェック（NaN値を除外）
    adx_values = result["ADX7"].dropna()
    assert len(adx_values) > 0, "Should have some ADX values"
    assert (adx_values >= 0).all() and (
        adx_values <= 100
    ).all(), "ADX should be in [0,100]"

    # RSI3の範囲チェック（NaN値を除外）
    rsi_values = result["RSI3"].dropna()
    assert len(rsi_values) > 0, "Should have some RSI values"
    assert (rsi_values >= 0).all() and (
        rsi_values <= 100
    ).all(), "RSI should be in [0,100]"

    assert result["ATR_Pct"].min() >= 0, "ATR_Pct should be positive"


def test_filter_conditions():
    """System5フィルター条件のテスト"""
    dates = pd.date_range("2024-01-01", periods=150, freq="B")

    # フィルター条件を満たすデータ
    df = pd.DataFrame(
        {
            "Open": [20.0] * 150,
            "High": [21.0] * 150,
            "Low": [19.0] * 150,
            "Close": [20.0] * 150,
            "Volume": [600_000] * 150,  # > 500,000
        },
        index=dates,
    )

    result = _compute_indicators_frame(df)

    # フィルター条件の確認
    valid_rows = result.iloc[100:]  # 十分なデータがある行のみ

    # 出来高条件
    assert (
        valid_rows["AvgVolume50"] > 500_000
    ).all(), "Volume filter should be satisfied"

    # ドルボリューム条件
    dollar_vol_msg = "Dollar volume filter should be satisfied"
    assert (valid_rows["DollarVolume50"] > 2_500_000).all(), dollar_vol_msg

    # ATR_Pct条件
    atr_pct_valid = valid_rows["ATR_Pct"] > DEFAULT_ATR_PCT_THRESHOLD
    assert atr_pct_valid.any(), "At least some rows should satisfy ATR_Pct condition"


def test_high_adx_setup_detection():
    """System5高ADX+ミーンリバージョンセットアップ検出のテスト"""
    dates = pd.date_range("2024-01-01", periods=150, freq="B")

    # 強いトレンド後のプルバック条件を作成
    prices = []

    # より極端な変動でADXを高くする
    for i in range(150):
        if i < 100:
            # 強い上昇トレンド期間
            base = 30 + i * 1.0  # 強い上昇
            volatility = np.sin(i * 0.1) * 10  # 大きな変動
            price = base + volatility
        else:
            # プルバック期間
            trend_high = 30 + 100 * 1.0
            pullback = -(i - 99) * 2.0  # 大きなプルバック
            price = trend_high + pullback

        prices.append(max(price, 10))

    df = pd.DataFrame(
        {
            "Open": prices,
            "High": [p * 1.05 for p in prices],  # より大きなレンジ
            "Low": [p * 0.95 for p in prices],
            "Close": prices,
            "Volume": [800_000] * 150,
        },
        index=dates,
    )

    result = _compute_indicators_frame(df)

    # ADXが高い値を示すことを確認
    adx_values = result["ADX7"].dropna()
    max_adx = adx_values.max()
    assert max_adx > 25, f"ADX should reach high values, got max: {max_adx:.1f}"

    # フィルター条件を満たす行があることを確認
    filter_rows = result[result["filter"]]
    assert len(filter_rows) > 0, "Should have rows that pass filter conditions"


def test_atr_pct_calculation():
    """ATR_Pct計算の正確性テスト"""
    dates = pd.date_range("2024-01-01", periods=50, freq="B")

    # 異なる価格レベルでのATR_Pctテスト
    high_price_df = pd.DataFrame(
        {
            "Open": [200] * 50,
            "High": [210] * 50,  # 5%レンジ
            "Low": [190] * 50,
            "Close": [200] * 50,
            "Volume": [600_000] * 50,
        },
        index=dates,
    )

    low_price_df = pd.DataFrame(
        {
            "Open": [20] * 50,
            "High": [21] * 50,  # 5%レンジ（同じ割合）
            "Low": [19] * 50,
            "Close": [20] * 50,
            "Volume": [600_000] * 50,
        },
        index=dates,
    )

    high_result = _compute_indicators_frame(high_price_df)
    low_result = _compute_indicators_frame(low_price_df)

    # ATR_Pctは価格に関係なく同程度の値になるはず
    high_atr_pct = high_result["ATR_Pct"].dropna().mean()
    low_atr_pct = low_result["ATR_Pct"].dropna().mean()

    # 同じ相対的なボラティリティなら、ATR_Pctは近い値になるはず
    ratio = high_atr_pct / low_atr_pct if low_atr_pct > 0 else 0
    assert (
        0.8 <= ratio <= 1.2
    ), f"ATR_Pct should be similar regardless of price level: {high_atr_pct:.4f} vs {low_atr_pct:.4f}"


def test_ohlcv_column_normalization():
    """OHLCV列名の正規化テスト"""
    # 小文字の列名データ
    df_lower = pd.DataFrame(
        {
            "open": [100, 101, 102],
            "high": [101, 102, 103],
            "low": [99, 100, 101],
            "close": [100, 101, 102],
            "volume": [1000, 1100, 1200],
        }
    )

    result = _rename_ohlcv(df_lower)

    # 大文字に正規化されているかチェック
    expected_cols = ["Open", "High", "Low", "Close", "Volume"]
    for col in expected_cols:
        assert col in result.columns, f"{col} should be present after normalization"


def test_placeholder_run(dummy_data):
    strategy = System5Strategy()
    # 戦略オブジェクトが正常に作成できることをテスト
    assert strategy is not None, "Strategy should be created successfully"

    # prepare_minimal_for_testが動作することを確認
    processed = strategy.prepare_minimal_for_test(dummy_data)
    assert isinstance(
        processed, dict
    ), "prepare_minimal_for_test should return a dictionary"
    assert len(processed) > 0, "Processed data should not be empty"
    strategy = System5Strategy()
    dates = pd.date_range("2024-01-01", periods=4, freq="D")
    df = pd.DataFrame(
        {
            "Open": [100, 100, 100, 99],
            "High": [100, 100, 100, 99],
            "Low": [100, 90, 90, 99],
            "Close": [100, 97, 99, 99],
            "Volume": [1_000_000] * 4,
            "ATR10": [1, 1, 1, 1],
        },
        index=dates,
    )
    prepared = {"DUMMY": df}
    entry_date = dates[1]
    candidates = {entry_date: [{"symbol": "DUMMY", "entry_date": entry_date}]}
    trades = strategy.run_backtest(prepared, candidates, capital=10_000)
    assert not trades.empty
    assert "pnl" in trades.columns


def test_entry_rule_limit_buy():
    strategy = System5Strategy()
    dates = pd.date_range("2024-01-01", periods=2, freq="B")
    df = pd.DataFrame(
        {
            "Open": [100, 100],
            "High": [101, 101],
            # Limit buy at 0.97 * prev_close (=97.0) should fill only if Low touches it
            "Low": [99, 97],
            "Close": [100, 100],
            "ATR10": [1, 1],
        },
        index=dates,
    )
    candidate = {"symbol": "DUMMY", "entry_date": dates[1]}
    entry = strategy.compute_entry(df, candidate, 10_000)
    assert entry == (97.0, pytest.approx(94.0))


def test_system5_profit_target_exits_next_open():
    strategy = System5Strategy()
    dates = pd.date_range("2024-01-01", periods=5, freq="B")
    df = pd.DataFrame(
        {
            "Open": [100, 100, 110, 120, 120],
            "High": [100, 101, 100, 121, 121],
            # Ensure entry-day Low touches the limit (97.0)
            "Low": [99, 97, 95, 119, 119],
            "Close": [100, 100, 99, 120, 120],
            "ATR10": [1, 1, 1, 1, 1],
        },
        index=dates,
    )
    candidate = {"symbol": "DUMMY", "entry_date": dates[1]}
    entry_price, stop_price = strategy.compute_entry(df, candidate, 10_000)
    entry_idx = df.index.get_loc(dates[1])

    exit_price, exit_date = strategy.compute_exit(
        df, entry_idx, entry_price, stop_price
    )

    assert exit_date == dates[3]
    assert exit_price == pytest.approx(float(df.iloc[3]["Open"]))


def test_system5_stop_exit_uses_stop_price_same_day():
    strategy = System5Strategy()
    dates = pd.date_range("2024-01-01", periods=4, freq="B")
    df = pd.DataFrame(
        {
            "Open": [100, 100, 100, 100],
            "High": [100, 101, 98, 100],
            # Ensure entry-day Low touches the limit (97.0)
            "Low": [99, 97, 90, 100],
            "Close": [100, 100, 95, 100],
            "ATR10": [1, 1, 1, 1],
        },
        index=dates,
    )
    candidate = {"symbol": "DUMMY", "entry_date": dates[1]}
    entry_price, stop_price = strategy.compute_entry(df, candidate, 10_000)
    entry_idx = df.index.get_loc(dates[1])

    exit_price, exit_date = strategy.compute_exit(
        df, entry_idx, entry_price, stop_price
    )

    assert exit_date == dates[2]
    assert exit_price == pytest.approx(stop_price)


def test_system5_fallback_exit_next_open_after_six_days():
    strategy = System5Strategy()
    fallback_days = strategy.config.get(
        "fallback_exit_after_days", FALLBACK_EXIT_DAYS_DEFAULT
    )
    periods = fallback_days + 3  # entry day + fallback window + next day
    dates = pd.date_range("2024-01-01", periods=periods, freq="B")
    highs = [97] * periods
    lows = [95] * periods
    df = pd.DataFrame(
        {
            "Open": [100 + i for i in range(periods)],
            "High": highs,
            "Low": lows,
            "Close": [100] * periods,
            "ATR10": [1] * periods,
        },
        index=dates,
    )
    candidate = {"symbol": "DUMMY", "entry_date": dates[1]}
    entry_price, stop_price = strategy.compute_entry(df, candidate, 10_000)
    entry_idx = df.index.get_loc(dates[1])

    exit_price, exit_date = strategy.compute_exit(
        df, entry_idx, entry_price, stop_price
    )

    expected_idx = entry_idx + fallback_days + 1
    assert exit_date == dates[expected_idx]
    assert exit_price == pytest.approx(float(df.iloc[expected_idx]["Open"]))
