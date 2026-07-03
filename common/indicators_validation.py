# indicators_validation.py
# 当日シグナル実行時の指標事前計算チェック機能

from __future__ import annotations

import pandas as pd

from common.cache_manager import get_indicator_column_flexible


class IndicatorValidationError(Exception):
    """指標不足による実行停止エラー"""

    pass


# System別必須指標定義
SYSTEM_REQUIRED_INDICATORS = {
    1: {"ATR10", "SMA200", "ROC200", "DollarVolume20"},
    2: {"ATR10", "ADX7", "DollarVolume20"},
    3: {"ATR10", "Drop3D", "DollarVolume20"},
    4: {"ATR10", "RSI4", "DollarVolume20", "UpTwoDays"},
    5: {"ATR10", "ADX7", "DollarVolume20"},
    6: {"ATR10", "Return_6D", "DollarVolume20"},
    7: {"ATR10", "SMA25", "SMA50"},  # SPY固定
}

# 共通必須指標（全Systemで必要）
COMMON_REQUIRED_INDICATORS = {
    "ATR10",
    "ATR20",
    "ATR40",
    "ATR50",
    "SMA25",
    "SMA50",
    "SMA100",
    "SMA150",
    "SMA200",
    "RSI3",
    "RSI4",
    "ADX7",
    "ROC200",
    "DollarVolume20",
    "DollarVolume50",
    "AvgVolume50",
    "ATR_Ratio",
    "Return_Pct",
    "Return_3D",
    "Return_6D",
    "UpTwoDays",
    "Drop3D",
    "HV50",
    "Min_50",
    "Max_70",
}


def validate_precomputed_indicators(
    data_dict: dict[str, pd.DataFrame],
    systems: list[int] | None = None,
    strict_mode: bool = True,
    log_callback=None,
    sample_size: int = 20,
    missing_tolerance: float = 0.2,
) -> tuple[bool, dict[str, list[str]]]:
    """
    指標事前計算状況を検証し、不足があればエラーレポートを返す

    Args:
        data_dict: 銘柄別データ辞書
        systems: チェック対象システム番号リスト（Noneなら全System）
        strict_mode: True=不足時エラー、False=警告のみ
        log_callback: ログ出力関数
        sample_size: 検証にサンプリングする銘柄数（履歴の長い＝流動的な銘柄を優先）
        missing_tolerance: 許容する欠損銘柄割合（この割合を超えたら不足と判定）

    Returns:
        (validation_passed, missing_indicators_report)

    Note:
        以前は「dict 先頭 10 銘柄を strict」で、キーがアルファベット順の場合
        先頭に短命ジャンクティッカー (例: AAC.U=2行) が集中し、build_rolling が
        正常でも guard が全体 abort する flakiness があった。対策として
        (1) サンプルを履歴の長い＝確立した流動銘柄優先に変更、
        (2) 少数の変則銘柄を許容する欠損割合 (missing_tolerance) を導入した。
        「build_rolling が本当に走ったか」という guard 本来の意図は維持する。
    """
    if not data_dict:
        return True, {}

    if systems is None:
        systems = list(SYSTEM_REQUIRED_INDICATORS.keys())

    if log_callback is None:

        def log_callback(x: str) -> None:
            pass

    # 全システムで必要な指標を収集
    all_required = set(COMMON_REQUIRED_INDICATORS)
    for system_num in systems:
        if system_num in SYSTEM_REQUIRED_INDICATORS:
            all_required.update(SYSTEM_REQUIRED_INDICATORS[system_num])

    missing_report = {}
    validation_errors = []

    # サンプル銘柄でのチェック
    # 履歴が長い銘柄ほど確立した流動銘柄で、rebuild が走っていれば必ず指標を持つ。
    # 先頭固定 (アルファベット順→ジャンク偏重) を避け、行数降順で上位をサンプルする。
    def _hist_len(sym: str) -> int:
        df = data_dict.get(sym)
        return 0 if df is None else len(df)

    ranked_symbols = sorted(data_dict.keys(), key=_hist_len, reverse=True)
    sample_symbols = ranked_symbols[: min(sample_size, len(ranked_symbols))]

    evaluated = 0
    for symbol in sample_symbols:
        df = data_dict[symbol]
        if df is None or df.empty:
            continue
        evaluated += 1

        missing_for_symbol = []

        for indicator in all_required:
            # 大文字・小文字柔軟チェック
            found_col = get_indicator_column_flexible(df, indicator)
            if found_col is None:
                missing_for_symbol.append(indicator)

        if missing_for_symbol:
            missing_report[symbol] = missing_for_symbol
            if len(missing_for_symbol) > 5:  # 多数不足の場合は簡潔にする
                validation_errors.append(
                    f"{symbol}: {len(missing_for_symbol)}個の指標が不足 (例: {', '.join(missing_for_symbol[:3])}...)"
                )
            else:
                validation_errors.append(f"{symbol}: {', '.join(missing_for_symbol)}")

    # 検証結果の判定
    # 欠損銘柄が許容割合以下なら pass（少数のジャンク銘柄で全体を止めない）。
    # 評価銘柄が 0 の場合は全て空データ → 判定できないので pass 扱い（従来踏襲）。
    missing_fraction = (len(missing_report) / evaluated) if evaluated else 0.0
    validation_passed = missing_fraction <= missing_tolerance

    if not validation_passed:
        error_summary = (
            f"指標事前計算チェックで不足を検出: {len(missing_report)}/{evaluated}銘柄で問題あり "
            f"(欠損率 {missing_fraction:.0%} > 許容 {missing_tolerance:.0%})"
        )
        log_callback(f"❌ {error_summary}")

        if len(validation_errors) <= 5:
            for error in validation_errors:
                log_callback(f"   • {error}")
        else:
            for error in validation_errors[:3]:
                log_callback(f"   • {error}")
            log_callback(f"   ... 他{len(validation_errors) - 3}件の問題")

        if strict_mode:
            detailed_msg = "\\n".join(
                [
                    "🚨 指標事前計算が不足しています",
                    f"対象システム: {systems}",
                    f"不足銘柄: {len(missing_report)}/{evaluated} (欠損率 {missing_fraction:.0%})",
                    "解決方法: scripts/build_rolling_with_indicators.py を実行してください",
                ]
            )
            raise IndicatorValidationError(detailed_msg)
    else:
        log_callback("✅ 指標事前計算チェック: 全て正常")

    return validation_passed, missing_report


def quick_indicator_check(
    data_dict: dict[str, pd.DataFrame], log_callback=None
) -> bool:
    """
    高速な指標存在チェック（サンプル銘柄のみ）

    Returns:
        True=十分な指標が存在, False=指標不足
    """
    if not data_dict:
        return True

    if log_callback is None:

        def log_callback(x: str) -> None:
            pass

    # 最初の3銘柄をサンプリング
    sample_symbols = list(data_dict.keys())[:3]

    # 最低限必要な指標
    key_indicators = ["ATR10", "SMA50", "RSI4", "DollarVolume20"]

    for symbol in sample_symbols:
        df = data_dict[symbol]
        if df is None or df.empty:
            continue

        found_count = 0
        for indicator in key_indicators:
            if get_indicator_column_flexible(df, indicator) is not None:
                found_count += 1

        # 4つ中3つ以上見つかれば良しとする
        if found_count < 3:
            log_callback(f"⚠️  高速チェック: {symbol}で指標不足 ({found_count}/4)")
            return False

    log_callback("✅ 高速指標チェック: OK")
    return True
