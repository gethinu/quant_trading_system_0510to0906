#!/usr/bin/env python3
"""
テスト用架空銘柄データ生成スクリプト

System1-7 の各段階（フィルター・セットアップ・シグナル）をテストするための
架空銘柄データを生成します。

使用方法:
    python tools/generate_test_symbols.py

生成されるファイル:
    data_cache/test_symbols/FAIL_ALL.feather
    data_cache/test_symbols/FILTER_ONLY_S1.feather
    ... (他の架空銘柄)
"""

from __future__ import annotations

from pathlib import Path
import sys
from typing import Any

import numpy as np
import pandas as pd
import pandas_market_calendars as mcal

# プロジェクトルートをパスに追加
sys.path.insert(0, str(Path(__file__).parent.parent))

from common.cache_manager import CacheManager
from common.indicators_common import add_indicators as compute_all_indicators
from config.settings import get_settings

DEFAULT_LOOKBACK_DAYS = 300
DEFAULT_VOLATILITY = 0.02
DEFAULT_RANDOM_SEED = 42

ALIAS_COLUMN_MAP: dict[str, str] = {
    "sma25": "SMA25",
    "sma50": "SMA50",
    "sma100": "SMA100",
    "sma150": "SMA150",
    "sma200": "SMA200",
    "atr10": "ATR10",
    "atr20": "ATR20",
    "atr40": "ATR40",
    "atr50": "ATR50",
    "atr_ratio": "ATR_Ratio",
    "atr_pct": "ATR_Pct",
    "dollarvolume20": "DollarVolume20",
    "dollarvolume50": "DollarVolume50",
    "avgvolume50": "AvgVolume50",
    "roc200": "ROC200",
    "rsi3": "RSI3",
    "rsi4": "RSI4",
    "hv50": "HV50",
    "adx7": "ADX7",
}


def create_base_dates(days: int = DEFAULT_LOOKBACK_DAYS) -> pd.DatetimeIndex:
    """営業日（NYSE）ベースの日付インデックスを作成"""

    nyse = mcal.get_calendar("NYSE")
    end_date = pd.Timestamp.utcnow().normalize()
    start_candidate = end_date - pd.Timedelta(days=int(days * 1.5))
    schedule = nyse.schedule(start_date=start_candidate, end_date=end_date)
    trading_days = pd.DatetimeIndex(schedule.index.tz_localize(None)).normalize()

    if len(trading_days) >= days:
        trading_days = trading_days[-days:]

    return pd.DatetimeIndex(trading_days, name="Date")


def create_base_ohlcv(
    dates: pd.DatetimeIndex,
    base_price: float,
    volatility: float = DEFAULT_VOLATILITY,
    seed: int | None = None,
) -> pd.DataFrame:
    """基本的な OHLCV データを生成"""

    rng = np.random.default_rng(seed if seed is not None else DEFAULT_RANDOM_SEED)
    n_days = len(dates)

    prices = np.empty(n_days)
    prices[0] = base_price
    returns = rng.normal(0, volatility, n_days)
    for idx in range(1, n_days):
        prices[idx] = prices[idx - 1] * (1 + returns[idx])

    high_noise = rng.uniform(0, 0.01, n_days)
    low_noise = rng.uniform(-0.01, 0, n_days)
    high_prices = prices * (1 + high_noise)
    low_prices = prices * (1 + low_noise)
    open_prices = np.roll(prices, 1)
    open_prices[0] = prices[0]

    base_volume = 1_000_000
    volume_noise = rng.uniform(0.5, 2.0, n_days)
    volumes = (base_volume * volume_noise).astype(int)

    return pd.DataFrame(
        {
            "Date": dates,
            "Open": open_prices,
            "High": high_prices,
            "Low": low_prices,
            "Close": prices,
            "Volume": volumes,
        }
    )


def ensure_custom_columns(df: pd.DataFrame) -> pd.DataFrame:
    """戦略が参照する追加カラムを確実に持たせる"""

    enriched = df.copy()
    if "Close" not in enriched:
        return enriched

    close = enriched["Close"]
    up_days = close.gt(close.shift(1))
    enriched["TwoDayUp"] = up_days & up_days.shift(1)
    enriched["UpTwoDays"] = enriched["TwoDayUp"]

    with np.errstate(divide="ignore", invalid="ignore"):
        enriched["3日下落率"] = (close.shift(3) - close) / close.shift(3) * 100
        enriched["6日上昇率"] = (close - close.shift(6)) / close.shift(6) * 100

    return enriched


def add_alias_columns(df: pd.DataFrame) -> pd.DataFrame:
    """主要列に従来表記（大文字）を付与する"""

    enriched = df.copy()
    for source, alias in ALIAS_COLUMN_MAP.items():
        if source in enriched.columns and alias not in enriched.columns:
            enriched[alias] = enriched[source]
    return enriched


def apply_symbol_config(df: pd.DataFrame, config: dict[str, Any]) -> pd.DataFrame:
    """共通指標を計算しつつ設定に基づいて最終行を上書き

    全カラムを小文字に統一して CacheManager と互換性を確保
    """

    enriched = compute_all_indicators(df)
    enriched = ensure_custom_columns(enriched)
    enriched = add_alias_columns(enriched)

    # 全カラムを小文字に統一（CacheManager 互換）
    enriched.columns = [c.lower() for c in enriched.columns]

    col_map = {
        col.lower(): col for col in enriched.columns if col.lower() not in {"date"}
    }

    skip_keys = {"base_price", "volatility"}

    for key, value in config.items():
        if key.lower() in skip_keys:
            continue
        if not isinstance(value, (int, float, bool)):
            continue

        lookup_key = key.lower()
        actual_col = col_map.get(lookup_key)
        if actual_col is None:
            actual_col = key.lower()
            enriched[actual_col] = np.nan
            col_map[lookup_key] = actual_col

        enriched.loc[enriched.index[-1], actual_col] = value

    return enriched


def create_test_symbol_configs() -> dict[str, dict[str, Any]]:
    """各架空銘柄の設定を定義: 113銘柄+SPY rolling パターン

    - FAIL_ALL_00..04 (5個): フィルタ不合格
    - FILTER_ONLY_S{1..6}_00..02 (18個): フィルタは合格、セットアップ不合格
    - SETUP_PASS_S{1..6}_00..14 (90個): フィルタ・セットアップ合格、ランキング分散
    合計: 113銘柄
    """
    configs = {}

    # === FAIL_ALL_00..04: フィルタ不合格 ===
    for idx in range(5):
        configs[f"FAIL_ALL_{idx:02d}"] = {
            "base_price": 2.0,
            "Close": 2.0,
            "Volume": 50000 + idx * 10000,
            "SMA25": 2.1,
            "SMA50": 2.0,
            "RSI3": 50,
            "ATR_Ratio": 0.01,
            "DollarVolume20": 100000 + idx * 20000,
            "DollarVolume50": 100000 + idx * 20000,
            "HV50": 3 + idx,
        }

    # === FILTER_ONLY_S{1..6}_00..02: フィルタ合格、セットアップ不合格 ===
    # S1: ROC200, SMA条件 OK だが Signal 不合格
    for idx in range(3):
        configs[f"FILTER_ONLY_S1_{idx:02d}"] = {
            "base_price": 50.0 + idx * 2,
            "Close": 50.0 + idx * 2,
            "Volume": 1500000 + idx * 100000,
            "SMA25": 51.0 + idx,
            "SMA50": 49.0 + idx,
            "DollarVolume20": 75000000 + idx * 5000000,
            "ATR_Ratio": 0.015,
            "ROC200": 0.08 + idx * 0.01,
            "RSI3": 45 + idx * 5,  # Signal では 条件外
        }

    # S2: RSI3 が高いがセットアップ条件外
    for idx in range(3):
        configs[f"FILTER_ONLY_S2_{idx:02d}"] = {
            "base_price": 25.0 + idx * 1.5,
            "Close": 25.0 + idx * 1.5,
            "Volume": 1200000 + idx * 80000,
            "DollarVolume20": 30000000 + idx * 2000000,
            "ATR_Ratio": 0.04 + idx * 0.005,
            "RSI3": 80 + idx * 3,
            "ADX7": 28 + idx * 2,
            "TwoDayUp": False,  # Signal では True 必須
        }

    # S3: Low が低い、下落率がある程度あるがセットアップ条件外
    for idx in range(3):
        configs[f"FILTER_ONLY_S3_{idx:02d}"] = {
            "base_price": 22.0 + idx * 1,
            "Close": 22.0 + idx * 1,
            "Low": 19.5 + idx * 0.5,
            "Volume": 1200000 + idx * 80000,
            "AvgVolume50": 1200000 + idx * 80000,
            "ATR_Ratio": 0.065 + idx * 0.01,
            "SMA150": 23.5 + idx,
            "3日下落率": 8.0 + idx * 2,  # Signal では 15% 以上必須
        }

    # S4: HV/SMA は合格だがセットアップ条件外
    for idx in range(3):
        configs[f"FILTER_ONLY_S4_{idx:02d}"] = {
            "base_price": 100.0 + idx * 5,
            "Close": 100.0 + idx * 5,
            "Volume": 1000000 + idx * 80000,
            "DollarVolume50": 100000000 + idx * 5000000,
            "HV50": 24 + idx * 2,
            "SMA200": 106.0 + idx * 2,  # Signal では 95 以下必須
            "RSI4": 35 + idx * 5,
        }

    # S5: 低価格・高ボラティリティ、ただしセットアップ条件外
    for idx in range(3):
        configs[f"FILTER_ONLY_S5_{idx:02d}"] = {
            "base_price": 15.0 + idx * 0.5,
            "Close": 15.0 + idx * 0.5,
            "Volume": 500000 + idx * 50000,
            "AvgVolume50": 500000 + idx * 50000,
            "DollarVolume50": 7500000 + idx * 500000,
            "ATR_Pct": 0.035 + idx * 0.005,
            "SMA100": 14.5 + idx * 0.2,
            "ATR10": 0.9 + idx * 0.05,
            "ADX7": 55 + idx * 5,
            "RSI3": 50 + idx * 3,  # Signal では 40 未満必須
        }

    # S6: return_6d が中程度だがセットアップ条件外
    for idx in range(3):
        configs[f"FILTER_ONLY_S6_{idx:02d}"] = {
            "base_price": 20.0 + idx * 1,
            "Close": 20.0 + idx * 1,
            "Low": 18.0 + idx * 0.5,
            "Volume": 700000 + idx * 50000,
            "DollarVolume50": 14000000 + idx * 1000000,
            "return_6d": 12.0 + idx * 2,
            "UpTwoDays": False,  # Signal では True 必須
            "6日上昇率": 20.0 + idx * 2,
        }

    # === SETUP_PASS_S{1..6}_00..14: フィルタ・セットアップ合格 (ランキング分散) ===
    # S1: SMA25 > SMA50, ROC200 > 0
    for idx in range(15):
        configs[f"SETUP_PASS_S1_{idx:02d}"] = {
            "base_price": 50.0 + idx * 2,
            "Close": 50.0 + idx * 2,
            "Volume": 1500000 + idx * 50000,
            "SMA25": 51.5 + idx * 1.2,
            "SMA50": 49.0 + idx * 0.8,
            "DollarVolume20": 75000000 + idx * 3000000,
            "ATR_Ratio": 0.012 + idx * 0.001,
            "ROC200": 0.08 + idx * 0.02,
            "RSI3": 55 + idx * 2,
        }

    # S2: RSI3 > 80, TwoDayUp=True, ADX > 25
    for idx in range(15):
        configs[f"SETUP_PASS_S2_{idx:02d}"] = {
            "base_price": 25.0 + idx * 1.5,
            "Close": 25.0 + idx * 1.5,
            "Volume": 1200000 + idx * 60000,
            "DollarVolume20": 30000000 + idx * 2000000,
            "ATR_Ratio": 0.04 + idx * 0.003,
            "RSI3": 85 + idx * 1,
            "TwoDayUp": True,
            "ADX7": 30 + idx * 2,
        }

    # S3: Close > SMA150, 3日下落率 > 15%, ATR_Ratio
    for idx in range(15):
        configs[f"SETUP_PASS_S3_{idx:02d}"] = {
            "base_price": 22.0 + idx * 1.2,
            "Close": 22.0 + idx * 1.2,
            "Low": 20.0 + idx * 0.8,
            "Volume": 1200000 + idx * 60000,
            "AvgVolume50": 1200000 + idx * 60000,
            "ATR_Ratio": 0.062 + idx * 0.003,
            "SMA150": 20.5 + idx * 0.5,
            "3日下落率": 18.0 + idx * 1.5,
        }

    # S4: Close > SMA200, HV > 20
    for idx in range(15):
        configs[f"SETUP_PASS_S4_{idx:02d}"] = {
            "base_price": 100.0 + idx * 5,
            "Close": 100.0 + idx * 5,
            "Volume": 1000000 + idx * 50000,
            "DollarVolume50": 100000000 + idx * 5000000,
            "HV50": 22 + idx * 1.5,
            "SMA200": 92.0 + idx * 2,
            "RSI4": 32 + idx * 1.5,
        }

    # S5: AvgVolume > threshold, ATR_Pct > threshold, ADX, RSI3 < 40
    for idx in range(15):
        configs[f"SETUP_PASS_S5_{idx:02d}"] = {
            "base_price": 15.0 + idx * 0.8,
            "Close": 15.0 + idx * 0.8,
            "Volume": 500000 + idx * 40000,
            "AvgVolume50": 500000 + idx * 40000,
            "DollarVolume50": 7500000 + idx * 600000,
            "ATR_Pct": 0.032 + idx * 0.002,
            "SMA100": 13.5 + idx * 0.3,
            "ATR10": 0.6 + idx * 0.05,
            "ADX7": 58 + idx * 1.5,
            "RSI3": 35 + idx * 1,
        }

    # S6: return_6d > 20%, UpTwoDays=True, 6日上昇率 > 25%
    for idx in range(15):
        configs[f"SETUP_PASS_S6_{idx:02d}"] = {
            "base_price": 20.0 + idx * 1.2,
            "Close": 20.0 + idx * 1.2,
            "Low": 18.0 + idx * 0.5,
            "Volume": 700000 + idx * 50000,
            "DollarVolume50": 14000000 + idx * 1200000,
            "return_6d": 22.0 + idx * 1.5,
            "UpTwoDays": True,
            "6日上昇率": 28.0 + idx * 2,
        }

    # === 旧13銘柄互換エイリアス（後で upsert 時に追加） ===
    # これらは別途ハンドルし、生成後に CacheManager で copy として保存

    return configs


def generate_test_symbols() -> None:
    """113銘柄+SPY rolling を生成・CacheManager経由で保存

    パターン:
      - FAIL_ALL_00..04 (5個)
      - FILTER_ONLY_S{1..6}_00..02 (18個)
      - SETUP_PASS_S{1..6}_00..14 (90個)
      = 113銘柄 + 旧13銘柄エイリアス + SPY rolling-only
    """

    settings = get_settings()
    cache = CacheManager(settings)
    dates = create_base_dates()
    configs = create_test_symbol_configs()

    # test_symbols ディレクトリを先に準備
    test_symbols_dir = settings.DATA_CACHE_DIR / "test_symbols"
    test_symbols_dir.mkdir(exist_ok=True)

    print("架空銘柄データを生成中... (113+SPY銘柄)")

    # === 113銘柄を生成・保存 ===
    for idx, (symbol_name, config) in enumerate(configs.items()):
        print(f"  {symbol_name}を生成中...")

        df = create_base_ohlcv(
            dates=dates,
            base_price=float(config["base_price"]),
            volatility=float(config.get("volatility", DEFAULT_VOLATILITY)),
            seed=DEFAULT_RANDOM_SEED + idx,
        )

        df = apply_symbol_config(df, config)

        # upsert_both に備えて Date インデックスを date カラムに戻す
        # （apply_symbol_config で小文字に統一しているため date カラムあり）
        is_indexed = (
            df.index.name and isinstance(df.index.name, str) and "Date" in df.index.name
        ) or isinstance(df.index, pd.DatetimeIndex)
        if is_indexed:
            # インデックスが Date の場合、date カラムに変換
            df = df.reset_index()
            if "index" in df.columns:
                df = df.drop(columns=["index"])

        # date カラムが無い場合は作成
        if "date" not in df.columns:
            if "Date" in df.columns:
                df = df.rename(columns={"Date": "date"})
            else:
                df["date"] = dates

        # CacheManager経由で rolling+full を自動計算・保存
        cache.upsert_both(symbol_name, df)

        # test_symbols に直接保存（パイプラインで読める形で）
        try:
            test_file = test_symbols_dir / f"{symbol_name}.feather"
            df.to_feather(str(test_file))
        except Exception:
            pass  # 失敗しても続行

        last_row = df.iloc[-1]
        close_val = last_row.get("close", last_row.get("Close", "N/A"))
        volume_val = last_row.get("volume", last_row.get("Volume", "N/A"))
        if close_val != "N/A" and volume_val != "N/A":
            print(
                f"    保存完了 (upsert_both)\n"
                f"    close={close_val:.2f},"
                f" volume={volume_val:,}"
            )
        else:
            print("    保存完了 (upsert_both)")

    # === 旧13銘柄互換エイリアス: rolling/full をコピー ===
    legacy_aliases = {
        "FAIL_ALL": "FAIL_ALL_00",
        "FILTER_ONLY_S1": "FILTER_ONLY_S1_00",
        "FILTER_ONLY_S2": "FILTER_ONLY_S2_00",
        "FILTER_ONLY_S3": "FILTER_ONLY_S3_00",
        "FILTER_ONLY_S4": "FILTER_ONLY_S4_00",
        "FILTER_ONLY_S5": "FILTER_ONLY_S5_00",
        "FILTER_ONLY_S6": "FILTER_ONLY_S6_00",
        "SETUP_PASS_S1": "SETUP_PASS_S1_00",
        "SETUP_PASS_S2": "SETUP_PASS_S2_00",
        "SETUP_PASS_S3": "SETUP_PASS_S3_00",
        "SETUP_PASS_S4": "SETUP_PASS_S4_00",
        "SETUP_PASS_S5": "SETUP_PASS_S5_00",
        "SETUP_PASS_S6": "SETUP_PASS_S6_00",
    }

    print("\n旧13銘柄エイリアスを生成中...")
    for alias, source_symbol in legacy_aliases.items():
        print(f"  {alias} -> {source_symbol}")
        # full を読み込んでコピー保存（upsert_both で rolling も再計算）
        df_full = cache.read(source_symbol, "full")
        if df_full is not None and not df_full.empty:
            cache.upsert_both(alias, df_full)

    # === SPY rolling-only を生成 ===
    # SPY は upsert_both で rolling も保存
    print("\nSPY rolling-only を生成中...")
    spy_df = create_base_ohlcv(
        dates=dates,
        base_price=450.0,
        volatility=0.015,
        seed=DEFAULT_RANDOM_SEED + len(configs),
    )
    spy_df = compute_all_indicators(spy_df)

    # date カラムへの統一
    is_indexed = (
        isinstance(spy_df.index.name, str) and "Date" in spy_df.index.name
    ) or isinstance(spy_df.index, pd.DatetimeIndex)
    if is_indexed:
        spy_df = spy_df.reset_index()
        if "index" in spy_df.columns:
            spy_df = spy_df.drop(columns=["index"])
    if "date" not in spy_df.columns:
        if "Date" in spy_df.columns:
            spy_df = spy_df.rename(columns={"Date": "date"})
        else:
            spy_df["date"] = dates

    # 指標を大文字から小文字に統一
    rename_map = {}
    for col_upper, col_lower in zip(
        ["Open", "High", "Low", "Close", "Volume"],
        ["open", "high", "low", "close", "volume"],
    ):
        if col_upper in spy_df.columns and col_lower not in spy_df.columns:
            rename_map[col_upper] = col_lower
    if rename_map:
        spy_df = spy_df.rename(columns=rename_map)

    cache.upsert_both("SPY", spy_df)
    print("  SPY rolling 保存完了")

    # test_symbols ディレクトリへの保存はループ内で実施済み

    total_symbols = len(configs) + len(legacy_aliases) + 1
    print(f"\n✅ 架空銘柄データ生成完了: {total_symbols}銘柄")
    print(f"  - 新パターン: {len(configs)}銘柄")
    print(f"  - 旧エイリアス: {len(legacy_aliases)}銘柄")
    print("  - SPY rolling: 1銘柄")
    print("\n📖 使用方法:")
    print(
        "  python scripts/run_all_systems_today.py"
        " --test-mode test_symbols --skip-external"
    )


if __name__ == "__main__":
    generate_test_symbols()
