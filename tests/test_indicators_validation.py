# test_indicators_validation.py
# 指標事前計算チェック機能のテスト

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from common.indicators_validation import (
    COMMON_REQUIRED_INDICATORS,
    SYSTEM_REQUIRED_INDICATORS,
    IndicatorValidationError,
    quick_indicator_check,
    validate_precomputed_indicators,
)


def create_test_data_with_indicators(
    symbols: list[str], include_indicators: bool = True
) -> dict[str, pd.DataFrame]:
    """テスト用データ作成（指標付き/なし選択可能）"""
    data_dict = {}

    for symbol in symbols:
        # 基本価格データ
        dates = pd.date_range("2020-01-01", periods=300, freq="D")
        np.random.seed(hash(symbol) % 2**32)  # 銘柄ごとに異なるseed
        prices = 100 + np.cumsum(np.random.randn(300) * 0.5)
        volumes = np.random.randint(100000, 1000000, 300)

        df = pd.DataFrame(
            {
                "Date": dates,
                "Open": prices + np.random.randn(300) * 0.2,
                "High": prices + np.abs(np.random.randn(300)) * 0.5,
                "Low": prices - np.abs(np.random.randn(300)) * 0.5,
                "Close": prices,
                "Volume": volumes,
            }
        )

        if include_indicators:
            # 必須指標を追加（COMMON_REQUIRED_INDICATORSの全て）
            df["ATR10"] = np.abs(np.random.randn(300) * 2)
            df["ATR20"] = np.abs(np.random.randn(300) * 2.5)
            df["ATR40"] = np.abs(np.random.randn(300) * 3)
            df["ATR50"] = np.abs(np.random.randn(300) * 3.5)
            df["SMA25"] = prices + np.random.randn(300) * 0.3
            df["SMA50"] = prices + np.random.randn(300) * 0.5
            df["SMA100"] = prices + np.random.randn(300) * 1.0
            df["SMA150"] = prices + np.random.randn(300) * 1.5
            df["SMA200"] = prices + np.random.randn(300) * 2.0
            df["RSI3"] = np.random.uniform(20, 80, 300)
            df["RSI4"] = np.random.uniform(20, 80, 300)
            df["ADX7"] = np.random.uniform(10, 50, 300)
            df["ROC200"] = np.random.uniform(-10, 10, 300)
            df["DollarVolume20"] = volumes * prices * np.random.uniform(0.8, 1.2, 300)
            df["DollarVolume50"] = volumes * prices * np.random.uniform(0.7, 1.3, 300)
            df["AvgVolume50"] = volumes * np.random.uniform(0.9, 1.1, 300)
            df["ATR_Ratio"] = np.random.uniform(0.01, 0.05, 300)
            df["Return_Pct"] = np.random.uniform(-0.03, 0.03, 300)
            df["Return_3D"] = np.random.uniform(-0.05, 0.05, 300)
            df["Return_6D"] = np.random.uniform(-0.08, 0.08, 300)
            df["UpTwoDays"] = np.random.choice([True, False], 300)
            df["Drop3D"] = np.random.uniform(-0.1, 0.1, 300)
            df["HV50"] = np.random.uniform(10, 50, 300)
            df["Min_50"] = prices - np.abs(np.random.randn(300) * 5)
            df["Max_70"] = prices + np.abs(np.random.randn(300) * 5)

        data_dict[symbol] = df

    return data_dict


def test_validate_precomputed_indicators_success():
    """指標が十分にある場合の成功テスト"""
    test_data = create_test_data_with_indicators(
        ["AAPL", "GOOGL", "MSFT"], include_indicators=True
    )

    # 全てのシステムで検証
    passed, missing_report = validate_precomputed_indicators(
        test_data, systems=[1, 2, 3, 4, 5, 6, 7], strict_mode=False
    )

    assert passed is True
    assert missing_report == {}


def test_validate_precomputed_indicators_missing():
    """指標が不足している場合のテスト"""
    test_data = create_test_data_with_indicators(
        ["AAPL", "GOOGL"], include_indicators=False
    )

    # strict_mode=False で警告のみ
    passed, missing_report = validate_precomputed_indicators(
        test_data, systems=[1, 2, 3], strict_mode=False
    )

    assert passed is False
    assert len(missing_report) > 0
    assert "AAPL" in missing_report or "GOOGL" in missing_report

    # 不足指標にはSystem1必須のATR10、SMA200等が含まれる
    if "AAPL" in missing_report:
        missing_indicators = missing_report["AAPL"]
        assert "ATR10" in missing_indicators
        assert "SMA200" in missing_indicators


def test_validate_precomputed_indicators_strict_mode():
    """strict_mode=Trueでの例外発生テスト"""
    test_data = create_test_data_with_indicators(["AAPL"], include_indicators=False)

    with pytest.raises(IndicatorValidationError) as exc_info:
        validate_precomputed_indicators(test_data, systems=[1], strict_mode=True)

    assert "指標事前計算が不足しています" in str(exc_info.value)
    assert "build_rolling_with_indicators.py" in str(exc_info.value)


def test_quick_indicator_check_success():
    """高速指標チェックの成功テスト"""
    test_data = create_test_data_with_indicators(
        ["AAPL", "GOOGL"], include_indicators=True
    )

    result = quick_indicator_check(test_data)
    assert result is True


def test_quick_indicator_check_failure():
    """高速指標チェックの失敗テスト"""
    test_data = create_test_data_with_indicators(["AAPL"], include_indicators=False)

    result = quick_indicator_check(test_data)
    assert result is False


def test_system_required_indicators_definition():
    """System必須指標定義の妥当性テスト"""
    # 各Systemに必須指標が定義されていることを確認
    for system_num in [1, 2, 3, 4, 5, 6, 7]:
        assert system_num in SYSTEM_REQUIRED_INDICATORS
        indicators = SYSTEM_REQUIRED_INDICATORS[system_num]
        assert len(indicators) > 0

        # ATR10は全Systemで必要
        assert "ATR10" in indicators


def test_common_required_indicators_coverage():
    """共通必須指標の網羅性テスト"""
    # 基本的な指標カテゴリが含まれていることを確認
    common_indicators = COMMON_REQUIRED_INDICATORS

    # ATR系
    atr_indicators = [ind for ind in common_indicators if ind.startswith("ATR")]
    assert len(atr_indicators) >= 4  # ATR10, ATR20, ATR40, ATR50

    # SMA系
    sma_indicators = [ind for ind in common_indicators if ind.startswith("SMA")]
    assert len(sma_indicators) >= 5  # SMA25, SMA50, SMA100, SMA150, SMA200

    # RSI系
    rsi_indicators = [ind for ind in common_indicators if ind.startswith("RSI")]
    assert len(rsi_indicators) >= 2  # RSI3, RSI4


def test_validate_empty_data():
    """空データでの動作テスト"""
    empty_data = {}

    passed, missing_report = validate_precomputed_indicators(empty_data)
    assert passed is True
    assert missing_report == {}

    result = quick_indicator_check(empty_data)
    assert result is True


def test_validate_tolerates_junk_tickers_at_head():
    """回帰: dict 先頭に短命ジャンク銘柄が並んでも、大多数が流動的なら abort しない。

    旧実装は「先頭 10 銘柄 strict」でアルファベット順キーだと AAC.U (2行) 等の
    ジャンクが先頭に集中し、build_rolling が正常でも guard が全体 abort していた。
    履歴長優先サンプリング + 欠損許容率でこれを解消したことを固定する。
    """
    # 大多数 (25銘柄) は指標付き＝長い履歴、少数 (3銘柄) はジャンク＝指標なし
    liquid = create_test_data_with_indicators(
        [f"LIQ{i}" for i in range(25)], include_indicators=True
    )
    junk = create_test_data_with_indicators(
        ["AAC.U", "AADX", "AAXJ"], include_indicators=False
    )
    # ジャンクの履歴を短くして「短命銘柄」を再現
    for sym, df in junk.items():
        junk[sym] = df.head(2).copy()

    # dict 先頭にジャンクを配置（アルファベット順キーの最悪ケースを模倣）
    data_dict = {**junk, **liquid}

    # strict_mode=True でも abort しない（履歴長優先サンプル→流動銘柄が選ばれる）
    passed, missing_report = validate_precomputed_indicators(
        data_dict, systems=[1, 2, 3, 4, 5, 6, 7], strict_mode=True
    )
    assert passed is True


def test_validate_fails_when_majority_missing():
    """欠損が許容率を超える（＝rebuild が本当に走っていない）場合は従来通り fail。"""
    data_dict = create_test_data_with_indicators(
        [f"SYM{i}" for i in range(20)], include_indicators=False
    )
    passed, missing_report = validate_precomputed_indicators(
        data_dict, systems=[1], strict_mode=False
    )
    assert passed is False
    assert len(missing_report) > 0


def test_validate_partial_indicators():
    """部分的に指標がある場合のテスト"""
    test_data = create_test_data_with_indicators(["AAPL"], include_indicators=False)

    # 一部の指標のみ追加
    test_data["AAPL"]["ATR10"] = 1.5
    test_data["AAPL"]["SMA50"] = 100.0
    test_data["AAPL"]["DollarVolume20"] = 1000000.0

    # 高速チェックは通るが、厳密チェックでは不足が検出される
    quick_result = quick_indicator_check(test_data)
    assert quick_result is True  # 最低限の指標はある

    passed, missing_report = validate_precomputed_indicators(
        test_data, systems=[1], strict_mode=False
    )
    assert passed is False  # System1にはROC200等も必要
    assert len(missing_report) > 0
