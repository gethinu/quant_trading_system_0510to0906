# ============================================================================
# 🧠 Context Note
# このファイルは System5（ロング ミーン・リバージョン 高 ADX）のロジック専門
#
# 前提条件（audit-remediation 2026-07-03 D3 Case A で docs 完全準拠に是正）：
#   - 高 ADX 環境（ADX7 > 55）でのミーン・リバージョン狙い
#   - 流動性 filter: AvgVolume50 > 500k 株, DollarVolume50 > 2.5M USD
#   - ATR_Pct による変動性フィルター（> 4%, spec 準拠、旧 2.5% から是正）
#   - Close > SMA100 + ATR10（100SMA+ATR バンドの上）
#   - RSI3 < 50 で一時的な押し目（リバージョン環境）確認
#   - 指標は precomputed のみ使用（ADX7 でランキング）
#   - フロー: setup() → rank() → signals() の順序実行
#
# ロジック単位：
#   filter()      → Close>=5, ADX7>55, ATR_Pct>4%, AvgVol50>500k, DV50>2.5M
#   setup()       → filter & Close>SMA100+ATR10 & RSI3<50
#   rank()        → ADX7 の降順ランキング（強いトレンド環境優先）
#   signals()     → スコア付きシグナル抽出
#
# Copilot へ：
#   → ADX 閾値（55, spec 準拠）の変更は慎重に。他システムとの競合検証必須
#   → RSI3 条件（< 50）の役割は「リバージョン環境確認」。ロジック変更禁止
#   → ATR_Pct > 4% は変動性フィルター (spec)。下限変更は制御テストで確認
#   → 流動性 filter (AvgVol50/DV50) は subscriber 実運用のスリッページ抑制。緩和禁止
# ============================================================================

"""System5 core logic (Long mean-reversion with high ADX).

High ADX mean-reversion strategy:
- Indicators: adx7, atr10, dollarvolume20, atr_pct, sma100, rsi3,
              avgvolume50, dollarvolume50 (precomputed only)
- Filter conditions: Close>=5, ADX7>55, ATR_Pct>4%,
                     AvgVolume50>500k, DollarVolume50>2.5M
    (audit-remediation 2026-07-03 D3 Case A: docs/systems/システム5.txt 準拠。
     旧: Close>=5 & ADX7>55 & ATR_Pct>2.5% のみで流動性 filter 完全欠如。
     Case A では spec の AvgVol50/DV50 を追加し ATR を 2.5%→4% に是正。)
- Setup conditions: filter & Close>SMA100+ATR10 & RSI3<50
    (audit-remediation 2026-07-02: spec 準拠に是正。旧実装は setup==filter で
     100SMA+ATR バンド / RSI3<50 が未 enforce、ADX 閾値も 35 と緩かった)
- Candidate generation: ADX7 descending ranking by date, extract top_n
- Optimization: Removed all indicator calculations, using precomputed indicators only
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

import pandas as pd

from common.batch_processing import process_symbols_batch
from common.system_candidates_utils import (
    choose_mode_date_for_latest_only,
    normalize_candidates_by_date,
    normalize_dataframe_to_by_date,
    set_diagnostics_after_ranking,
)
from common.system_common import check_precomputed_indicators, get_total_days
from common.system_constants import SYSTEM5_REQUIRED_INDICATORS
from common.system_setup_predicates import validate_predicate_equivalence
from common.utils import get_cached_data

# ============================================================================
# System5 Strategy Constants
# ============================================================================
# Price & Volume filters
MIN_PRICE = 5.0  # Minimum closing price for candidates

# ADX-based filters
# audit-remediation 2026-07-02 (P0): spec (システム5.txt) は ADX7>55。
# 旧実装は 35 で緩すぎたため 55 に是正 (SYSTEM5_ADX_THRESHOLD と一致)。
MIN_ADX = 55.0  # Minimum ADX7 for high trend strength environment (spec: >55)
MIN_ADX_FULL_SCAN = 55.0  # ADX7 threshold for full-scan filtering (spec: >55)

# Mean-reversion setup thresholds (audit-remediation 2026-07-02, P0)
MAX_RSI3 = 50.0  # spec: 3日RSI < 50 (一時的な押し目確認)

# Volatility filters
# audit-remediation 2026-07-03 (D3 Case A: docs 完全準拠に是正):
#   docs/systems/システム5.txt:9 「ATRが4%を上回る」→ 0.04。
#   旧 0.025 は 2026-07-02 audit で意図的に緩めていたが、Case A ユーザ判断
#   (ペンス・ドープ methodology 原著者の意思通り docs 準拠) で spec に revert。
DEFAULT_ATR_PCT_THRESHOLD = 0.04  # 4% minimum ATR percentage (spec)

# Liquidity filters (audit-remediation 2026-07-03 D3 Case A: spec 準拠に追加)
#   docs/systems/システム5.txt:7-8 の 2 条件:
#     - 過去50日の平均出来高 > 500,000 株
#     - 過去50日の平均売買代金 > 2,500,000 $
#   旧実装ではこれらが filter に存在せず、common/today_signals.py の診断
#   カウンタとして数えるだけで実 gate になっていなかった (D3 audit で判明)。
MIN_AVG_VOLUME_50 = 500_000  # spec: AvgVolume50 > 500k 株
MIN_DOLLAR_VOLUME_50 = 2_500_000  # spec: DollarVolume50 > 2.5M $

# Ranking parameters
DEFAULT_TOP_N = 20  # Default number of top candidates to extract


def format_atr_pct_threshold_label(threshold: float | None = None) -> str:
    """UI/ログ用のATR閾値ラベルを一元化。scripts/today や today_signals で利用。"""
    actual_threshold = threshold if threshold is not None else DEFAULT_ATR_PCT_THRESHOLD
    return f"> {actual_threshold:.2%}"


# ============================================================================
# System5 Helper Functions
# ============================================================================


def _apply_filter_conditions(df: pd.DataFrame) -> pd.DataFrame:
    """Apply System5 filter conditions, preserving existing 'filter' column if present.

    audit-remediation 2026-07-03 (D3 Case A: docs 完全準拠に是正):
        spec (docs/systems/システム5.txt:6-9) の 3 条件を全て enforce する:
          - 過去50日の平均出来高 > 500,000 株      (avgvolume50 > MIN_AVG_VOLUME_50)
          - 過去50日の平均売買代金 > 2,500,000 $   (dollarvolume50 > MIN_DOLLAR_VOLUME_50)
          - ATR > 4%                              (atr_pct > DEFAULT_ATR_PCT_THRESHOLD)
        加えて Close>=5 (penny stock 除外, operational safety) と ADX7>55
        (spec では setup 条件だが filter に前倒し) を維持する。

    Args:
        df: DataFrame with required indicators (Close, adx7, atr_pct,
            avgvolume50, dollarvolume50)

    Returns:
        DataFrame with 'filter' column added/updated
    """
    result = df.copy()

    close = pd.to_numeric(result["Close"], errors="coerce")
    adx7 = pd.to_numeric(result["adx7"], errors="coerce")
    atr_pct = pd.to_numeric(result["atr_pct"], errors="coerce")
    # audit-remediation 2026-07-03 (D3 Case A): 流動性 filter 追加。欠損列は
    # NaN Series にフォールバックし fillna(False) で自動的に False 判定にする。
    avgvol50 = (
        pd.to_numeric(result["avgvolume50"], errors="coerce")
        if "avgvolume50" in result.columns
        else pd.Series(float("nan"), index=result.index)
    )
    dv50 = (
        pd.to_numeric(result["dollarvolume50"], errors="coerce")
        if "dollarvolume50" in result.columns
        else pd.Series(float("nan"), index=result.index)
    )

    computed_filter = (
        (close >= MIN_PRICE)
        & (adx7 > MIN_ADX)
        & (atr_pct > DEFAULT_ATR_PCT_THRESHOLD)
        & (avgvol50 > MIN_AVG_VOLUME_50)
        & (dv50 > MIN_DOLLAR_VOLUME_50)
    ).fillna(False)

    if "filter" in result.columns:
        existing = (
            pd.Series(result["filter"], index=result.index).fillna(False).astype(bool)
        )
        computed_filter = computed_filter & existing

    result["filter"] = computed_filter.astype(bool)

    return result


def _col_numeric_ci(df: pd.DataFrame, name: str) -> pd.Series:
    """Case-insensitive numeric column access (returns NaN Series if absent)."""
    if name in df.columns:
        return pd.to_numeric(df[name], errors="coerce")
    low = name.lower()
    for c in df.columns:
        if isinstance(c, str) and c.lower() == low:
            return pd.to_numeric(df[c], errors="coerce")
    return pd.Series(float("nan"), index=df.index)


def _apply_setup_conditions(df: pd.DataFrame) -> pd.DataFrame:
    """Apply System5 setup conditions on top of the filter.

    audit-remediation 2026-07-02 (P0 System5 setup 乖離):
    仕様 (docs/systems/システム5.txt) の setup 条件を enforce する。
        setup = filter (Close>=5, ADX7>55, ATR_Pct>2.5%)
                & Close > SMA100 + ATR10   (100SMA+ATR バンド上)
                & RSI3 < 50                (一時的な押し目 = リバージョン環境)
    旧実装は setup == filter で 100SMA+ATR バンドと RSI3<50 が未 enforce だった。

    Args:
        df: DataFrame with 'filter' column and Close/sma100/atr10/rsi3

    Returns:
        DataFrame with 'setup' column added/updated
    """
    result = df.copy()

    filter_ok = (
        pd.Series(result["filter"], index=result.index).fillna(False).astype(bool)
    )

    close = _col_numeric_ci(result, "Close")
    sma100 = _col_numeric_ci(result, "sma100")
    atr10 = _col_numeric_ci(result, "atr10")
    rsi3 = _col_numeric_ci(result, "rsi3")

    price_band_ok = (close > (sma100 + atr10)).fillna(False)
    rsi_ok = (rsi3 < MAX_RSI3).fillna(False)

    computed_setup = filter_ok & price_band_ok & rsi_ok

    if "setup" in result.columns:
        existing = (
            pd.Series(result["setup"], index=result.index).fillna(False).astype(bool)
        )
        computed_setup = computed_setup & existing

    result["setup"] = computed_setup.astype(bool)

    return result


def _compute_indicators(symbol: str) -> tuple[str, pd.DataFrame | None]:
    """Check precomputed indicators and apply System5-specific filters.

    Args:
        symbol: Target symbol to process

    Returns:
        (symbol, processed DataFrame | None)
    """
    try:
        df = get_cached_data(symbol)
        if df is None or df.empty:
            return symbol, None

        # Check for required indicators
        missing_indicators = [
            col for col in SYSTEM5_REQUIRED_INDICATORS if col not in df.columns
        ]
        if missing_indicators:
            return symbol, None

        # Apply System5-specific filters and setup using helpers
        x = df.copy()
        x = _apply_filter_conditions(x)
        x = _apply_setup_conditions(x)

        return symbol, x

    except Exception:
        return symbol, None


def prepare_data_vectorized_system5(
    raw_data_dict: dict[str, pd.DataFrame] | None,
    *,
    progress_callback: Callable[[str], None] | None = None,
    log_callback: Callable[[str], None] | None = None,
    skip_callback: Callable[[str, str], None] | None = None,
    batch_size: int | None = None,
    reuse_indicators: bool = True,
    symbols: list[str] | None = None,
    use_process_pool: bool = False,
    max_workers: int | None = None,
    **kwargs: Any,
) -> dict[str, pd.DataFrame]:
    """System5 data preparation processing (high ADX mean-reversion strategy).

    Execute high-speed processing using precomputed indicators.

    Args:
        raw_data_dict: Raw data dictionary (None to fetch from cache)
        progress_callback: Progress reporting callback
        log_callback: Log output callback
        skip_callback: Error skip callback
        batch_size: Batch size
        reuse_indicators: Reuse existing indicators (for speed)
        symbols: Target symbol list
        use_process_pool: Process pool usage flag
        max_workers: Maximum worker count

    Returns:
        Processed data dictionary
    """
    # Fast path: reuse precomputed indicators
    if reuse_indicators and raw_data_dict:
        try:
            # Early check - verify required indicators exist
            valid_data_dict, error_symbols = check_precomputed_indicators(
                raw_data_dict, SYSTEM5_REQUIRED_INDICATORS, "System5", skip_callback
            )

            if valid_data_dict:
                # Apply System5-specific filters using helpers
                prepared_dict = {}
                for symbol, df in valid_data_dict.items():
                    x = df.copy()
                    x = _apply_filter_conditions(x)
                    x = _apply_setup_conditions(x)
                    prepared_dict[symbol] = x

                if log_callback:
                    log_callback(
                        f"System5: Fast-path processed {len(prepared_dict)} symbols"
                    )

                return prepared_dict

        except RuntimeError:
            # Re-raise error immediately if required indicators are missing
            raise
        except Exception:
            # Fall back to normal processing for other errors
            if log_callback:
                log_callback(
                    "System5: Fast-path failed, falling back to normal processing"
                )

    # Normal processing path: batch processing from symbol list
    if symbols:
        target_symbols = symbols
    elif raw_data_dict:
        target_symbols = list(raw_data_dict.keys())
    else:
        if log_callback:
            log_callback("System5: No symbols provided, returning empty dict")
        return {}

    if log_callback:
        log_callback(
            f"System5: Starting normal processing for {len(target_symbols)} symbols"
        )

    # Execute batch processing
    results, error_symbols = process_symbols_batch(
        target_symbols,
        _compute_indicators,
        batch_size=batch_size,
        use_process_pool=use_process_pool,
        max_workers=max_workers,
        progress_callback=progress_callback,
        log_callback=log_callback,
        skip_callback=skip_callback,
        system_name="System5",
    )
    try:
        validate_predicate_equivalence(results, "5", log_fn=log_callback)
    except Exception:
        pass
    return cast(dict[str, pd.DataFrame], results)


def generate_candidates_system5(
    prepared_dict: dict[str, pd.DataFrame],
    *,
    top_n: int | None = None,
    progress_callback: Callable[[str], None] | None = None,
    log_callback: Callable[[str], None] | None = None,
    batch_size: int | None = None,
    latest_only: bool = False,
    include_diagnostics: bool = False,
    diagnostics: dict[str, Any] | None = None,
    **kwargs: Any,
) -> (
    tuple[dict[pd.Timestamp, dict[str, dict[str, Any]]], pd.DataFrame | None]
    | tuple[
        dict[pd.Timestamp, dict[str, dict[str, Any]]],
        pd.DataFrame | None,
        dict[str, Any],
    ]
):
    """System5 candidate generation (ADX7 descending ranking).

    Args:
        prepared_dict: Prepared data dictionary
        top_n: Number of top entries to extract
        progress_callback: Progress reporting callback
        log_callback: Log output callback

    Returns:
        (Daily candidate dictionary, Integrated candidate DataFrame)
    """
    if diagnostics is None:
        diagnostics = {
            "ranking_source": None,
            "setup_predicate_count": 0,
            "ranked_top_n_count": 0,
            "predicate_only_pass_count": 0,
            "mismatch_flag": 0,
        }

    # Reset counters every invocation to avoid carrying stale values when dict is reused
    diagnostics["setup_predicate_count"] = 0
    diagnostics["ranked_top_n_count"] = 0
    diagnostics["predicate_only_pass_count"] = 0
    diagnostics["mismatch_flag"] = 0

    if not prepared_dict:
        if log_callback:
            log_callback("System5: No data provided for candidate generation")
        # データが空でも latest_only / full_scan に応じて ranking_source を設定
        try:
            set_diagnostics_after_ranking(
                diagnostics,
                final_df=None,
                ranking_source=("latest_only" if latest_only else "full_scan"),
            )
        except Exception:
            diagnostics["ranking_source"] = (
                "latest_only" if latest_only else "full_scan"
            )
        return ({}, None, diagnostics) if include_diagnostics else ({}, None)

    if top_n is None:
        top_n = DEFAULT_TOP_N

    if latest_only:
        try:
            rows: list[dict] = []
            date_counter: dict[pd.Timestamp, int] = {}
            try:
                from common.system_setup_predicates import (
                    system5_setup_predicate as _s5_pred,
                )
            except Exception:
                _s5_pred = None
            for sym, df in prepared_dict.items():
                if df is None or df.empty:
                    continue
                last_row = df.iloc[-1]

                # Prefer precomputed setup column; fall back to predicate evaluation
                # or manual recomputation when needed
                setup_from_column = False
                setup_value_available = False
                try:
                    raw_setup = last_row.get("setup", None)
                    if raw_setup is not None and not pd.isna(raw_setup):
                        setup_value_available = True
                        if bool(raw_setup):
                            setup_from_column = True
                except Exception:
                    setup_from_column = False
                    setup_value_available = False

                predicate_pass = False
                predicate_evaluated = False
                if _s5_pred is not None:
                    try:
                        predicate_pass = bool(_s5_pred(last_row))
                        predicate_evaluated = True
                    except Exception:
                        predicate_pass = False
                        predicate_evaluated = False

                manual_pass = False
                if (
                    not setup_from_column
                    and not predicate_evaluated
                    and not setup_value_available
                ):
                    try:
                        close_val = last_row.get("Close", float("nan"))
                        adx_val = last_row.get("adx7", float("nan"))
                        atr_pct_val = last_row.get("atr_pct", float("nan"))
                        # audit-remediation 2026-07-02 (P0): spec setup 条件を
                        # deep-fallback にも反映 (100SMA+ATR バンド, RSI3<50)。
                        sma100_val = last_row.get("sma100", last_row.get("SMA100"))
                        atr10_val_m = last_row.get("atr10", last_row.get("ATR10"))
                        rsi3_val = last_row.get("rsi3", last_row.get("RSI3"))
                        if (
                            pd.notna(close_val)
                            and pd.notna(adx_val)
                            and pd.notna(atr_pct_val)
                            and pd.notna(sma100_val)
                            and pd.notna(atr10_val_m)
                            and pd.notna(rsi3_val)
                        ):
                            manual_pass = bool(
                                float(close_val) >= MIN_PRICE
                                and float(adx_val) > MIN_ADX
                                and float(atr_pct_val) > DEFAULT_ATR_PCT_THRESHOLD
                                and float(close_val)
                                > (float(sma100_val) + float(atr10_val_m))
                                and float(rsi3_val) < MAX_RSI3
                            )
                    except Exception:
                        manual_pass = False

                setup_ok = False
                setup_source = ""
                if setup_from_column:
                    setup_ok = True
                    setup_source = "column"
                    if predicate_evaluated and not predicate_pass:
                        diagnostics["mismatch_flag"] = 1
                elif predicate_pass:
                    setup_ok = True
                    setup_source = "predicate"
                    diagnostics["mismatch_flag"] = 1
                elif manual_pass:
                    setup_ok = True
                    setup_source = "manual"
                    diagnostics["mismatch_flag"] = 1

                if not setup_ok:
                    continue

                adx7_val = last_row.get("adx7", None)
                try:
                    if adx7_val is None or pd.isna(adx7_val):
                        continue
                except Exception:
                    continue
                dt = pd.Timestamp(str(df.index[-1]))
                date_counter[dt] = date_counter.get(dt, 0) + 1

                # ATR10を配分計算用に保持
                atr10_val = 0.0
                try:
                    atr10_raw = last_row.get("atr10")
                    if atr10_raw is not None and not pd.isna(atr10_raw):
                        atr10_val = float(atr10_raw)
                except Exception:
                    pass

                rows.append(
                    {
                        "symbol": sym,
                        "date": dt,
                        "adx7": adx7_val,
                        "atr_pct": last_row.get("atr_pct", 0),
                        "close": last_row.get("Close", 0),
                        "atr10": atr10_val,
                        "_setup_via": setup_source,
                        "_predicate_pass": bool(predicate_pass),
                        "_manual_pass": bool(manual_pass),
                    }
                )

            diagnostics["setup_unique_symbols"] = len(
                set(row["symbol"] for row in rows)
            )
            if not rows:
                if log_callback:
                    try:
                        samples: list[str] = []
                        taken = 0
                        for s_sym, s_df in prepared_dict.items():
                            if s_df is None or getattr(s_df, "empty", True):
                                continue
                            try:
                                s_last = s_df.iloc[-1]
                                s_dt = pd.to_datetime(str(s_df.index[-1])).normalize()
                                s_setup = bool(s_last.get("setup", False))
                                s_adx = s_last.get("adx7", float("nan"))
                                try:
                                    s_adx_f = float(s_adx)
                                except Exception:
                                    s_adx_f = float("nan")
                                samples.append(
                                    f"{s_sym}: date={s_dt.date()} "
                                    f"setup={s_setup} adx7={s_adx_f:.4f}"
                                )
                                taken += 1
                                if taken >= 2:
                                    break
                            except Exception:
                                continue
                        if samples:
                            log_callback(
                                (
                                    "System5: DEBUG latest_only 0 candidates. "
                                    + " | ".join(samples)
                                )
                            )
                    except Exception:
                        pass
                    log_callback("System5: latest_only fast-path produced 0 rows")
                # 診断の一貫性: 0件でも ranking_source を latest_only に設定（log_callback 有無に関わらず）
                try:
                    set_diagnostics_after_ranking(
                        diagnostics, final_df=None, ranking_source="latest_only"
                    )
                except Exception:
                    diagnostics["ranking_source"] = "latest_only"
                return ({}, None, diagnostics) if include_diagnostics else ({}, None)
            df_all = pd.DataFrame(rows)
            mode_date = choose_mode_date_for_latest_only(date_counter)
            if mode_date is not None:
                df_all = df_all[df_all["date"] == mode_date]
            df_all = df_all.sort_values("adx7", ascending=False, kind="stable").head(
                top_n
            )

            if "_setup_via" in df_all.columns:
                via_series = df_all["_setup_via"].fillna("").astype(str)
                diagnostics["setup_predicate_count"] = int((via_series != "").sum())

                if "_predicate_pass" in df_all.columns:
                    predicate_series = (
                        df_all["_predicate_pass"].fillna(False).astype(bool)
                    )
                else:
                    predicate_series = pd.Series(False, index=df_all.index)

                if "_manual_pass" in df_all.columns:
                    manual_series = df_all["_manual_pass"].fillna(False).astype(bool)
                else:
                    manual_series = pd.Series(False, index=df_all.index)

                predicate_only_mask = (via_series != "column") & (
                    predicate_series | manual_series
                )
                diagnostics["predicate_only_pass_count"] = int(
                    predicate_only_mask.sum()
                )
            else:
                diagnostics["setup_predicate_count"] = len(df_all)
                diagnostics["predicate_only_pass_count"] = 0

            diagnostics["setup_unique_symbols"] = int(df_all["symbol"].nunique())

            meta_cols = ["_setup_via", "_predicate_pass", "_manual_pass"]
            df_public = df_all.drop(
                columns=[c for c in meta_cols if c in df_all.columns]
            )

            # Feature flag: allow using Option-B finalize helper (non-breaking)
            use_option_b_utils = False
            try:
                if bool(kwargs.get("use_option_b_utils", False)):
                    use_option_b_utils = True
                else:
                    try:
                        from config.environment import get_env_config as _get_env

                        _env = _get_env()
                        v = getattr(_env, "enable_option_b_system5", None)
                        if v is not None and bool(v):
                            use_option_b_utils = True
                    except Exception:
                        use_option_b_utils = False
            except Exception:
                use_option_b_utils = False

            if use_option_b_utils:
                try:
                    from common.system_candidates_utils import (
                        finalize_ranking_and_diagnostics as _finalize_diag,
                    )

                    _finalize_diag(
                        diagnostics,
                        df_public,
                        ranking_source="latest_only",
                        extras=None,
                    )
                except Exception:
                    set_diagnostics_after_ranking(
                        diagnostics, final_df=df_public, ranking_source="latest_only"
                    )
            else:
                set_diagnostics_after_ranking(
                    diagnostics, final_df=df_public, ranking_source="latest_only"
                )
            diagnostics["top_n_requested"] = top_n

            # ✅ 診断整合性チェック: ranked > setup は論理エラー
            if diagnostics["ranked_top_n_count"] > diagnostics["setup_predicate_count"]:
                if log_callback:
                    ranked = diagnostics["ranked_top_n_count"]
                    setup = diagnostics["setup_predicate_count"]
                    log_callback(
                        f"System5: WARNING - ranked_top_n ({ranked}) > "
                        f"setup_predicate_count ({setup}). "
                        "Possible duplicate or logic error."
                    )
            by_date = normalize_dataframe_to_by_date(df_public)
            if log_callback:
                msg = (
                    f"System5: latest_only fast-path -> {len(df_public)} "
                    f"candidates (symbols={len(rows)})"
                )
                log_callback(msg)
            return (
                (by_date, df_public.copy(), diagnostics)
                if include_diagnostics
                else (by_date, df_public.copy())
            )
        except Exception as e:
            if log_callback:
                log_callback(f"System5: fast-path failed -> fallback ({e})")
            pass

    # Aggregate all dates
    all_dates_set: set[pd.Timestamp] = set()
    for df in prepared_dict.values():
        if df is not None and not df.empty:
            all_dates_set.update(df.index)

    if not all_dates_set:
        if log_callback:
            log_callback("System5: No valid dates found in data")
        return ({}, None, diagnostics) if include_diagnostics else ({}, None)
    all_dates = sorted(all_dates_set)

    candidates_by_date: dict[pd.Timestamp, list[dict[str, Any]]] = {}
    all_candidates: list[dict[str, Any]] = []

    if log_callback:
        log_callback(f"System5: Generating candidates for {len(all_dates)} dates")

    # Execute ADX7 ranking by date (descending - highest ADX7 first)
    for i, date in enumerate(all_dates):
        date_candidates = []

        for symbol, df in prepared_dict.items():
            try:
                if df is None or date not in df.index:
                    continue
                row = cast(pd.Series, df.loc[date])
                setup_val = bool(row.get("setup", False))
                from common.system_setup_predicates import (
                    system5_setup_predicate as _s5_pred,
                )

                pred_val = _s5_pred(row)
                # setup 通過は最終候補確定後に一括計上（ここでは加算しない）
                if pred_val and not setup_val:
                    diagnostics["predicate_only_pass_count"] += 1
                    diagnostics["mismatch_flag"] = 1
                if not bool(setup_val):
                    continue
                adx7_val = cast(Any, row.get("adx7", 0))
                try:
                    if pd.isna(adx7_val) or float(adx7_val) <= MIN_ADX_FULL_SCAN:
                        continue
                except Exception:
                    continue

                date_candidates.append(
                    {
                        "symbol": symbol,
                        "date": date,
                        "adx7": adx7_val,
                        "atr_pct": row.get("atr_pct", 0),
                        "close": row.get("Close", 0),
                    }
                )

            except Exception:
                continue

        # Sort by ADX7 descending (highest first) and extract top_n
        if date_candidates:
            date_candidates.sort(key=lambda x: x["adx7"], reverse=True)
            top_candidates = date_candidates[:top_n]

            candidates_by_date[date] = top_candidates
            all_candidates.extend(top_candidates)

        # Progress reporting
        if progress_callback and (i + 1) % max(1, len(all_dates) // 10) == 0:
            progress_callback(f"Processed {i + 1}/{len(all_dates)} dates")

    # Create integrated DataFrame
    if all_candidates:
        candidates_df = pd.DataFrame(all_candidates)
        candidates_df["date"] = pd.to_datetime(candidates_df["date"])
        candidates_df = candidates_df.sort_values(
            ["date", "adx7"], ascending=[True, False]
        )
        diagnostics["ranking_source"] = "full_scan"
        # Feature flag: Option-B finalize helper
        use_option_b_utils = False
        try:
            if bool(kwargs.get("use_option_b_utils", False)):
                use_option_b_utils = True
            else:
                try:
                    from config.environment import get_env_config as _get_env

                    _env = _get_env()
                    v = getattr(_env, "enable_option_b_system5", None)
                    if v is not None and bool(v):
                        use_option_b_utils = True
                except Exception:
                    use_option_b_utils = False
        except Exception:
            use_option_b_utils = False

        if use_option_b_utils:
            try:
                from common.system_candidates_utils import (
                    finalize_ranking_and_diagnostics as _finalize_diag,
                )

                _finalize_diag(
                    diagnostics, candidates_df, ranking_source="full_scan", extras=None
                )
            except Exception:
                set_diagnostics_after_ranking(
                    diagnostics, final_df=candidates_df, ranking_source="full_scan"
                )
        else:
            set_diagnostics_after_ranking(
                diagnostics, final_df=candidates_df, ranking_source="full_scan"
            )
    else:
        candidates_df = None
        # Feature flag: Option-B finalize helper for empty case
        use_option_b_utils = False
        try:
            if bool(kwargs.get("use_option_b_utils", False)):
                use_option_b_utils = True
            else:
                try:
                    from config.environment import get_env_config as _get_env

                    _env = _get_env()
                    v = getattr(_env, "enable_option_b_system5", None)
                    if v is not None and bool(v):
                        use_option_b_utils = True
                except Exception:
                    use_option_b_utils = False
        except Exception:
            use_option_b_utils = False

        if use_option_b_utils:
            try:
                from common.system_candidates_utils import (
                    finalize_ranking_and_diagnostics as _finalize_diag,
                )

                _finalize_diag(
                    diagnostics, None, ranking_source="full_scan", extras=None
                )
            except Exception:
                set_diagnostics_after_ranking(
                    diagnostics, final_df=None, ranking_source="full_scan"
                )
        else:
            set_diagnostics_after_ranking(
                diagnostics, final_df=None, ranking_source="full_scan"
            )

    if log_callback:
        total_candidates = len(all_candidates)
        unique_dates = len(candidates_by_date)
        msg = (
            f"System5: Generated {total_candidates} candidates "
            f"across {unique_dates} dates"
        )
        log_callback(msg)

    normalized = normalize_candidates_by_date(candidates_by_date)
    return (
        (normalized, candidates_df, diagnostics)
        if include_diagnostics
        else (normalized, candidates_df)
    )


def get_total_days_system5(data_dict: dict[str, pd.DataFrame]) -> int:
    """Get total days count for System5 data.

    Args:
        data_dict: Data dictionary

    Returns:
        Maximum day count
    """
    # follow-imports 設定により戻り値が Any 扱いになる環境向けに明示変換
    return int(get_total_days(data_dict))


__all__ = [
    "prepare_data_vectorized_system5",
    "generate_candidates_system5",
    "get_total_days_system5",
]
