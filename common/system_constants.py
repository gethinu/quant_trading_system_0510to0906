"""システム共通の定数定義。

各System*.pyで使用される定数を一元管理：
- 必須カラム名
- 最小行数要件
- 閾値パラメータ
- フィルタリング条件
"""

from __future__ import annotations

# === 基本データ要件 ===
REQUIRED_COLUMNS = ("Open", "High", "Low", "Close", "Volume")

# システム別最小行数要件
MIN_ROWS_SYSTEM1 = 200  # ROC200 + SMA200 必要
MIN_ROWS_SYSTEM2 = 150  # RSI3 + ADX7 ベース
MIN_ROWS_SYSTEM3 = 150  # 3日下落 + 各種指標
MIN_ROWS_SYSTEM4 = 200  # SMA200 + HV50 必要
MIN_ROWS_SYSTEM5 = 150  # ADX7 + ATR ベース
MIN_ROWS_SYSTEM6 = 100  # 比較的短期指標
MIN_ROWS_SYSTEM7 = 150  # SPY専用、基本指標

# === System1 (Long ROC200) 定数 ===
SYSTEM1_ROC_PERIOD = 200
SYSTEM1_SMA_PERIOD = 200
SYSTEM1_MIN_PRICE = 5.0
SYSTEM1_MIN_DOLLAR_VOLUME = 25_000_000  # 25M

# === System2 (Short RSI spike) 定数 ===
SYSTEM2_RSI_PERIOD = 3
SYSTEM2_RSI_THRESHOLD = 90  # RSI3 > 90
SYSTEM2_ADX_PERIOD = 7
SYSTEM2_MIN_PRICE = 5.0
SYSTEM2_MIN_DOLLAR_VOLUME = 25_000_000  # 25M
SYSTEM2_ATR_RATIO_THRESHOLD = 0.03  # 3%

# === System3 (Long mean-reversion) 定数 ===
SYSTEM3_MIN_PRICE = 5.0
SYSTEM3_MIN_DOLLAR_VOLUME = 25_000_000  # 25M
SYSTEM3_ATR_RATIO_THRESHOLD = 0.05  # 5%
SYSTEM3_DROP_3D_THRESHOLD = 0.125  # 12.5% 3日下落

# === System4 (Long trend low-vol pullback) 定数 ===
SYSTEM4_MIN_PRICE = 5.0
SYSTEM4_MIN_DOLLAR_VOLUME = 25_000_000  # 25M
SYSTEM4_RSI_THRESHOLD = 50  # RSI4 < 50
SYSTEM4_HV_PERIOD = 50
SYSTEM4_SMA_PERIOD = 200

# === System5 (Long mean-reversion with high ADX) 定数 ===
SYSTEM5_MIN_PRICE = 5.0
SYSTEM5_MIN_DOLLAR_VOLUME = 25_000_000  # 25M
SYSTEM5_ATR_PCT_THRESHOLD = 0.025  # 2.5%
SYSTEM5_ADX_THRESHOLD = 55  # ADX7 > 55
SYSTEM5_ADX_PERIOD = 7

# === System6 (Short mean-reversion momentum burst) 定数 ===
SYSTEM6_MIN_PRICE = 5.0
SYSTEM6_MIN_DOLLAR_VOLUME = 25_000_000  # 25M
SYSTEM6_RETURN_6D_THRESHOLD = 0.20  # 20% 6日リターン
SYSTEM6_ATR_PERIOD = 10
SYSTEM6_DOLLAR_VOLUME_PERIOD = 50

# === System7 (SPY short catastrophe hedge) 定数 ===
SYSTEM7_SYMBOL = "SPY"  # 固定シンボル
SYSTEM7_MIN_ROWS = 150

# === 共通フィルター閾値 ===
DEFAULT_MIN_PRICE = 5.0
DEFAULT_MIN_DOLLAR_VOLUME = 25_000_000  # 25M USD
DEFAULT_ATR_RATIO_THRESHOLD = 0.03  # 3%

# === 指標計算パラメータ ===
ATR_PERIOD_DEFAULT = 10
RSI_PERIOD_DEFAULT = 4
ADX_PERIOD_DEFAULT = 7
DOLLAR_VOLUME_PERIOD_DEFAULT = 20

# === システム別必須指標リスト ===
SYSTEM1_REQUIRED_INDICATORS = ["roc200", "sma200", "dollarvolume20"]

SYSTEM2_REQUIRED_INDICATORS = [
    "rsi3",
    "adx7",
    "atr10",
    "dollarvolume20",
    "atr_ratio",
    "twodayup",
]

SYSTEM3_REQUIRED_INDICATORS = [
    "atr10",
    "dollarvolume20",
    "atr_ratio",
    "drop3d",
]

SYSTEM4_REQUIRED_INDICATORS = [
    "rsi4",
    "sma200",
    "atr40",  # Required for ATR ratio calculation
    "hv50",
    "dollarvolume50",
]

SYSTEM5_REQUIRED_INDICATORS = [
    "adx7",
    "atr10",
    "dollarvolume20",
    "atr_pct",
]

SYSTEM6_REQUIRED_INDICATORS = [
    "atr10",
    "dollarvolume50",
    "return_6d",
    "uptwodays",
]

SYSTEM7_REQUIRED_INDICATORS = [
    "atr50",  # ATR50 (lowercase, as used in system7)
    "min_50",  # Min_50 - 50日の最低価格
    "max_70",  # Max_70 - 70日の最高価格
]

# === システム別設定マッピング ===
SYSTEM_CONFIGS = {
    "system1": {
        "min_rows": MIN_ROWS_SYSTEM1,
        "required_indicators": SYSTEM1_REQUIRED_INDICATORS,
        "min_price": SYSTEM1_MIN_PRICE,
        "min_dollar_volume": SYSTEM1_MIN_DOLLAR_VOLUME,
    },
    "system2": {
        "min_rows": MIN_ROWS_SYSTEM2,
        "required_indicators": SYSTEM2_REQUIRED_INDICATORS,
        "min_price": SYSTEM2_MIN_PRICE,
        "min_dollar_volume": SYSTEM2_MIN_DOLLAR_VOLUME,
        "atr_ratio_threshold": SYSTEM2_ATR_RATIO_THRESHOLD,
        "rsi_threshold": SYSTEM2_RSI_THRESHOLD,
    },
    "system3": {
        "min_rows": MIN_ROWS_SYSTEM3,
        "required_indicators": SYSTEM3_REQUIRED_INDICATORS,
        "min_price": SYSTEM3_MIN_PRICE,
        "min_dollar_volume": SYSTEM3_MIN_DOLLAR_VOLUME,
        "atr_ratio_threshold": SYSTEM3_ATR_RATIO_THRESHOLD,
        "drop_3d_threshold": SYSTEM3_DROP_3D_THRESHOLD,
    },
    "system4": {
        "min_rows": MIN_ROWS_SYSTEM4,
        "required_indicators": SYSTEM4_REQUIRED_INDICATORS,
        "min_price": SYSTEM4_MIN_PRICE,
        "min_dollar_volume": SYSTEM4_MIN_DOLLAR_VOLUME,
        "rsi_threshold": SYSTEM4_RSI_THRESHOLD,
    },
    "system5": {
        "min_rows": MIN_ROWS_SYSTEM5,
        "required_indicators": SYSTEM5_REQUIRED_INDICATORS,
        "min_price": SYSTEM5_MIN_PRICE,
        "min_dollar_volume": SYSTEM5_MIN_DOLLAR_VOLUME,
        "atr_pct_threshold": SYSTEM5_ATR_PCT_THRESHOLD,
        "adx_threshold": SYSTEM5_ADX_THRESHOLD,
    },
    "system6": {
        "min_rows": MIN_ROWS_SYSTEM6,
        "required_indicators": SYSTEM6_REQUIRED_INDICATORS,
        "min_price": SYSTEM6_MIN_PRICE,
        "min_dollar_volume": SYSTEM6_MIN_DOLLAR_VOLUME,
        "return_6d_threshold": SYSTEM6_RETURN_6D_THRESHOLD,
    },
    "system7": {
        "min_rows": MIN_ROWS_SYSTEM7,
        "required_indicators": SYSTEM7_REQUIRED_INDICATORS,
        "symbol": SYSTEM7_SYMBOL,
    },
}

# === Signal pipeline phases (絞込フロー / narrowing flow) ===
# 各 system の signal 生成が universe → setup/filter → ranking → final signals へ
# 絞り込まれる段階を宣言的に列挙する「参考メタデータ」。
#
# 目的: dashboard で「単一 survival rate」ではなく phase 別の絞込透明性
#       (universe に対して各段でどれだけ残るか) を見せるための表示メタ。
#
# 重要:
#   - これは **評価軸ではなく参考数値** のためのラベル定義。通過率が低いこと自体は
#     要件でも異常でもない (厳しい gate ほど final は少数になる設計)。
#   - 各 phase の ``name`` は monitor / dashboard で JSON key として使う安定 ID。
#   - ``measurable_from_grouped_daily=True`` の phase のみ daily_polygon_monitor.py が
#     grouped-daily (全 US ユニバース) から実測できる。setup/ranking など指標依存の
#     phase は full today-pipeline 実行が必要で、monitor では count=None (未計測) になる。
#   - pass 条件は core/system{1..7}.py の実装に対応 (2026-07 時点)。DV 閾値は
#     daily_polygon_monitor.SYSTEM_GATES (= 実際に集計に使う値) に揃える。
SYSTEM_PIPELINE_PHASES: dict[str, list[dict[str, object]]] = {
    "sys1": [
        {"name": "universe", "label": "Universe", "condition": "当日価格のある全 US 銘柄", "measurable_from_grouped_daily": True},
        {"name": "price_filter", "label": "Price ≥ $5", "condition": "Close >= 5", "measurable_from_grouped_daily": True},
        {"name": "dv20_filter", "label": "DollarVolume20 > $50M", "condition": "DollarVolume20 > 50M", "measurable_from_grouped_daily": True},
        {"name": "setup", "label": "Trend setup", "condition": "SMA25 > SMA50 かつ ROC200 > 0", "measurable_from_grouped_daily": False},
        {"name": "ranking", "label": "Rank by ROC200", "condition": "ROC200 降順 top-N", "measurable_from_grouped_daily": False},
        {"name": "final", "label": "Final signals", "condition": "採用シグナル", "measurable_from_grouped_daily": False},
    ],
    "sys2": [
        {"name": "universe", "label": "Universe", "condition": "当日価格のある全 US 銘柄", "measurable_from_grouped_daily": True},
        {"name": "price_filter", "label": "Price ≥ $5", "condition": "Close >= 5", "measurable_from_grouped_daily": True},
        {"name": "dv20_filter", "label": "DollarVolume20 > $25M", "condition": "DollarVolume20 > 25M", "measurable_from_grouped_daily": True},
        {"name": "setup", "label": "RSI spike setup", "condition": "ATR_Ratio > 0.03 かつ RSI3 > 90 かつ twodayup", "measurable_from_grouped_daily": False},
        {"name": "ranking", "label": "Rank by ADX7", "condition": "ADX7 降順 top-N", "measurable_from_grouped_daily": False},
        {"name": "final", "label": "Final signals", "condition": "採用シグナル", "measurable_from_grouped_daily": False},
    ],
    "sys3": [
        {"name": "universe", "label": "Universe", "condition": "当日価格のある全 US 銘柄", "measurable_from_grouped_daily": True},
        {"name": "price_filter", "label": "Price ≥ $5", "condition": "Close >= 5", "measurable_from_grouped_daily": True},
        {"name": "dv20_filter", "label": "DollarVolume20 > $25M", "condition": "DollarVolume20 > 25M", "measurable_from_grouped_daily": True},
        {"name": "setup", "label": "3-day drop setup", "condition": "ATR_Ratio >= 0.05 かつ drop3d >= 0.125", "measurable_from_grouped_daily": False},
        {"name": "ranking", "label": "Rank by drop3d", "condition": "drop3d 降順 top-N", "measurable_from_grouped_daily": False},
        {"name": "final", "label": "Final signals", "condition": "採用シグナル", "measurable_from_grouped_daily": False},
    ],
    "sys4": [
        {"name": "universe", "label": "Universe", "condition": "当日価格のある全 US 銘柄", "measurable_from_grouped_daily": True},
        {"name": "dv50_filter", "label": "DollarVolume50 > $100M", "condition": "DollarVolume50 > 100M", "measurable_from_grouped_daily": True},
        {"name": "setup", "label": "Low-vol trend setup", "condition": "HV50 in [10,40] かつ Close > SMA200", "measurable_from_grouped_daily": False},
        {"name": "ranking", "label": "Rank by RSI4", "condition": "RSI4 昇順 top-N (最も oversold)", "measurable_from_grouped_daily": False},
        {"name": "final", "label": "Final signals", "condition": "採用シグナル", "measurable_from_grouped_daily": False},
    ],
    "sys5": [
        {"name": "universe", "label": "Universe", "condition": "当日価格のある全 US 銘柄", "measurable_from_grouped_daily": True},
        {"name": "price_filter", "label": "Price ≥ $5", "condition": "Close >= 5", "measurable_from_grouped_daily": True},
        {"name": "setup", "label": "High-ADX reversion setup", "condition": "ADX7 > 55 かつ ATR_Pct > 0.025 かつ Close > SMA100+ATR10 かつ RSI3 < 50", "measurable_from_grouped_daily": False},
        {"name": "ranking", "label": "Rank by ADX7", "condition": "ADX7 降順 top-N", "measurable_from_grouped_daily": False},
        {"name": "final", "label": "Final signals", "condition": "採用シグナル", "measurable_from_grouped_daily": False},
    ],
    "sys6": [
        {"name": "universe", "label": "Universe", "condition": "当日価格のある全 US 銘柄", "measurable_from_grouped_daily": True},
        {"name": "price_filter", "label": "Low ≥ $5", "condition": "Low >= 5", "measurable_from_grouped_daily": True},
        {"name": "dv50_filter", "label": "DollarVolume50 > $10M", "condition": "DollarVolume50 > 10M", "measurable_from_grouped_daily": True},
        {"name": "setup", "label": "Momentum burst setup", "condition": "return_6d > 0.20 かつ UpTwoDays", "measurable_from_grouped_daily": False},
        {"name": "ranking", "label": "Rank by return_6d", "condition": "return_6d 降順 top-N", "measurable_from_grouped_daily": False},
        {"name": "final", "label": "Final signals", "condition": "採用シグナル", "measurable_from_grouped_daily": False},
    ],
    "sys7": [
        {"name": "universe", "label": "Universe (SPY)", "condition": "SPY 固定", "measurable_from_grouped_daily": True},
        {"name": "setup", "label": "52w low setup", "condition": "Low <= Min_50", "measurable_from_grouped_daily": False},
        {"name": "ranking", "label": "Score by ATR50", "condition": "ATR50 (position sizing)", "measurable_from_grouped_daily": False},
        {"name": "final", "label": "Hedge signal", "condition": "採用ヘッジシグナル", "measurable_from_grouped_daily": False},
    ],
}


def get_system_config(system_name: str) -> dict:
    """指定されたシステムの設定を取得する。

    Args:
        system_name: システム名（例: "system1", "system2"）

    Returns:
        システム設定辞書

    Raises:
        KeyError: 未知のシステム名の場合
    """
    return SYSTEM_CONFIGS[system_name.lower()]


__all__ = [
    # 基本定数
    "REQUIRED_COLUMNS",
    "DEFAULT_MIN_PRICE",
    "DEFAULT_MIN_DOLLAR_VOLUME",
    "DEFAULT_ATR_RATIO_THRESHOLD",
    # システム別最小行数
    "MIN_ROWS_SYSTEM1",
    "MIN_ROWS_SYSTEM2",
    "MIN_ROWS_SYSTEM3",
    "MIN_ROWS_SYSTEM4",
    "MIN_ROWS_SYSTEM5",
    "MIN_ROWS_SYSTEM6",
    "MIN_ROWS_SYSTEM7",
    # システム別必須指標
    "SYSTEM1_REQUIRED_INDICATORS",
    "SYSTEM2_REQUIRED_INDICATORS",
    "SYSTEM3_REQUIRED_INDICATORS",
    "SYSTEM4_REQUIRED_INDICATORS",
    "SYSTEM5_REQUIRED_INDICATORS",
    "SYSTEM6_REQUIRED_INDICATORS",
    "SYSTEM7_REQUIRED_INDICATORS",
    # 設定管理
    "SYSTEM_CONFIGS",
    "get_system_config",
    # signal pipeline 絞込フロー (参考メタ)
    "SYSTEM_PIPELINE_PHASES",
]
