# ============================================================================
# 🧠 Context Note
# このファイルは System4（ロング トレンド ロー・ボラティリティ）のロジック専門
#
# 前提条件：
#   - 低ボラティリティ収縮期を検出（HV50: 10-40%）
#   - トレンド確認（Close > SMA200）が必須
#   - 指標は precomputed のみ使用（RSI4 でランキング）
#   - フロー: setup() → rank() → signals() の順序実行
#
# ロジック単位：
#   setup()       → フィルター条件チェック（DollarVolume50>100M など）
#   rank()        → RSI4 の昇順ランキング（低 RSI = 過売り優先）
#   signals()     → スコア付きシグナル抽出
#
# Copilot へ：
#   → ボラティリティ収縮の判定ロジック（HV50 %ile）は厳格に守る
#   → RSI4 ランキングは昇順（低い値優先）。間違えるな
#   → DollarVolume50 の高閾値（100M）を変更する場合は制御テストで確認
# ============================================================================

"""System4 core logic (Long trend low-vol pullback).

Trend low-volatility pullback strategy:
- Indicators: rsi4, sma200, atr40, hv50, dollarvolume50 (precomputed only)
- Filter conditions: DollarVolume50>100M, HV50 10-40% (volatility contraction)
- Setup conditions: Filter + Close>SMA200 (trend confirmation)
- Candidate generation: RSI4 ascending ranking by date, extract top_n
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
from common.system_common import (
    check_precomputed_indicators,
    get_total_days,
    slice_latest_rows,
)
from common.system_constants import SYSTEM4_REQUIRED_INDICATORS
from common.system_setup_predicates import validate_predicate_equivalence
from common.utils import get_cached_data

# === System4 Business Rule Constants ===
MIN_DOLLAR_VOLUME = 100_000_000  # DollarVolume50 minimum threshold
HV50_MIN = 10  # Historical Volatility 50-day minimum %
HV50_MAX = 40  # Historical Volatility 50-day maximum %
MAX_RSI4_THRESHOLD = 30.0  # RSI4 oversold threshold
DEFAULT_TOP_N = 20  # Default number of top candidates
LATEST_ONLY_TAIL_ROWS = 5
SYSTEM4_LATEST_ONLY_COLUMNS = (
    "Open",
    "High",
    "Low",
    "Close",
    "Volume",
    "rsi4",
    "sma200",
    "atr40",
    "hv50",
    "dollarvolume50",
)


def _apply_filter_conditions(df: pd.DataFrame) -> pd.DataFrame:
    """Apply System4 filter conditions to DataFrame.

    Filter conditions: DollarVolume50>100M, HV50 10-40% (volatility contraction)

    Args:
        df: Input DataFrame with required indicators

    Returns:
        DataFrame with 'filter' column added
    """
    df["filter"] = (df["dollarvolume50"] > MIN_DOLLAR_VOLUME) & df["hv50"].between(
        HV50_MIN, HV50_MAX
    )
    return df


def _apply_setup_conditions(df: pd.DataFrame) -> pd.DataFrame:
    """Apply System4 setup conditions to DataFrame.

    Setup conditions: Filter + Close>SMA200 (trend confirmation)

    Args:
        df: Input DataFrame with 'filter' column and required indicators

    Returns:
        DataFrame with 'setup' column added
    """
    df["setup"] = df["filter"] & (df["Close"] > df["sma200"])
    return df


def _validate_and_apply_filters(symbol: str) -> tuple[str, pd.DataFrame | None]:
    """Validate precomputed indicators and apply System4-specific filters.

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
            col for col in SYSTEM4_REQUIRED_INDICATORS if col not in df.columns
        ]
        if missing_indicators:
            return symbol, None

        # Apply System4-specific filters and setup using helper functions
        x = df.copy()
        x = _apply_filter_conditions(x)
        x = _apply_setup_conditions(x)

        return symbol, x

    except Exception as e:
        # Log the error for debugging but return None to continue processing
        import logging

        logging.getLogger(__name__).debug(f"Failed to process {symbol}: {e}")
        return symbol, None


def prepare_data_vectorized_system4(
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
    **_unused_kwargs: Any,
) -> dict[str, pd.DataFrame]:
    """System4 data preparation processing (trend low-volatility pullback strategy).

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
    latest_only = bool(_unused_kwargs.get("latest_only", False))
    # Fast path: reuse precomputed indicators
    if reuse_indicators and raw_data_dict:
        try:
            # Early check - verify required indicators exist
            valid_data_dict, _error_symbols = check_precomputed_indicators(
                raw_data_dict, SYSTEM4_REQUIRED_INDICATORS, "System4", skip_callback
            )

            if valid_data_dict:
                # Apply System4-specific filters using helper functions
                prepared_dict = {}
                for symbol, df in valid_data_dict.items():
                    if latest_only:
                        x = slice_latest_rows(
                            df,
                            keep_columns=SYSTEM4_LATEST_ONLY_COLUMNS,
                            tail_rows=LATEST_ONLY_TAIL_ROWS,
                        )
                    else:
                        x = df.copy()
                    x = _apply_filter_conditions(x)
                    x = _apply_setup_conditions(x)
                    prepared_dict[symbol] = x

                if log_callback:
                    log_callback(
                        f"System4: Fast-path processed {len(prepared_dict)} symbols"
                    )

                return prepared_dict

        except RuntimeError:
            # Re-raise error immediately if required indicators are missing
            raise
        except Exception:
            # Fall back to normal processing for other errors
            if log_callback:
                log_callback(
                    "System4: Fast-path failed, falling back to normal processing"
                )

    # Normal processing path: batch processing from symbol list
    if symbols:
        target_symbols = symbols
    elif raw_data_dict:
        target_symbols = list(raw_data_dict.keys())
    else:
        if log_callback:
            log_callback("System4: No symbols provided, returning empty dict")
        return {}

    if log_callback:
        log_callback(
            f"System4: Starting normal processing for {len(target_symbols)} symbols"
        )

    # Execute batch processing
    results, error_symbols = process_symbols_batch(
        target_symbols,
        _validate_and_apply_filters,
        batch_size=batch_size,
        use_process_pool=use_process_pool,
        max_workers=max_workers,
        progress_callback=progress_callback,
        log_callback=log_callback,
        skip_callback=skip_callback,
        system_name="System4",
    )
    try:
        validate_predicate_equivalence(results, "4", log_fn=log_callback)
    except Exception:
        pass
    typed_results = cast(dict[str, pd.DataFrame], results)
    return typed_results


def generate_candidates_system4(
    prepared_dict: dict[str, pd.DataFrame],
    *,
    top_n: int | None = None,
    progress_callback: Callable[[str], None] | None = None,
    log_callback: Callable[[str], None] | None = None,
    latest_only: bool = False,
    include_diagnostics: bool = False,
    **_unused_kwargs: Any,
) -> (
    tuple[dict[pd.Timestamp, dict[str, dict[str, Any]]], pd.DataFrame | None]
    | tuple[
        dict[pd.Timestamp, dict[str, dict[str, Any]]],
        pd.DataFrame | None,
        dict[str, Any],
    ]
):
    """System4 candidate generation (RSI4 ascending ranking).

    Args:
        prepared_dict: Prepared data dictionary
        top_n: Number of top entries to extract
        progress_callback: Progress reporting callback
        log_callback: Log output callback

    Returns:
        (Daily candidate dictionary, Integrated candidate DataFrame)
    """
    # Initialize diagnostics dict
    diagnostics = {
        "ranking_source": None,
        "setup_predicate_count": 0,
        "ranked_top_n_count": 0,
        "predicate_only_pass_count": 0,
        "mismatch_flag": 0,
    }

    if not prepared_dict:
        if log_callback:
            log_callback("System4: No data provided for candidate generation")
        return ({}, None, diagnostics) if include_diagnostics else ({}, None)

    if top_n is None:
        top_n = DEFAULT_TOP_N

    if latest_only:
        try:
            rows: list[dict] = []
            date_counter: dict[pd.Timestamp, int] = {}
            try:
                from common.system_setup_predicates import (
                    system4_setup_predicate as _s4_pred,
                )
            except Exception:
                _s4_pred = None

            for sym, df in prepared_dict.items():
                if df is None or df.empty:
                    continue
                last_row = df.iloc[-1]

                setup_from_column = False
                setup_value_available = False
                try:
                    raw_setup = last_row.get("setup", None)
                    if raw_setup is not None and not pd.isna(raw_setup):
                        setup_value_available = True
                        if bool(raw_setup):
                            setup_from_column = True
                except Exception:
                    setup_value_available = False

                predicate_pass = False
                predicate_evaluated = False
                if _s4_pred is not None:
                    try:
                        predicate_pass = bool(_s4_pred(last_row))
                        predicate_evaluated = True
                    except Exception:
                        predicate_pass = False

                if not predicate_evaluated and not setup_value_available:
                    try:
                        filt = bool(
                            (last_row.get("dollarvolume50", 0) > MIN_DOLLAR_VOLUME)
                            and pd.notna(last_row.get("hv50", None))
                            and HV50_MIN <= float(last_row.get("hv50", 0)) <= HV50_MAX
                        )
                        close_val = float(last_row.get("Close", float("nan")))
                        sma200_val = float(last_row.get("sma200", float("nan")))
                        predicate_pass = filt and (close_val > sma200_val)
                        predicate_evaluated = True
                    except Exception:
                        predicate_pass = False
                        predicate_evaluated = False

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

                if not setup_ok:
                    continue

                rsi4_val = last_row.get("rsi4", None)
                try:
                    if rsi4_val is None or pd.isna(rsi4_val):
                        continue
                except Exception:
                    continue

                # Entry/Stop価格計算用の値取得
                close_val = last_row.get("Close", 0)
                open_val = last_row.get("Open", 0)
                high_val = last_row.get("High", 0)
                entry_price = close_val if close_val > 0 else open_val
                stop_price = high_val if high_val > 0 else close_val * 1.015

                # System4はatr40を使用（SYSTEM4_REQUIRED_INDICATORSで定義）
                atr40_val = 0.0
                try:
                    atr40_raw = last_row.get("atr40")
                    if atr40_raw is not None and not pd.isna(atr40_raw):
                        atr40_val = float(atr40_raw)
                except Exception:
                    pass

                dt = pd.Timestamp(str(df.index[-1]))
                date_counter[dt] = date_counter.get(dt, 0) + 1
                rows.append(
                    {
                        "symbol": sym,
                        "date": dt,
                        "rsi4": rsi4_val,
                        "atr_ratio": last_row.get("atr_ratio", 0),
                        "close": close_val,
                        "sma200": last_row.get("sma200", 0),
                        "entry_price": entry_price,
                        "stop_price": stop_price,
                        "atr40": atr40_val,
                        "_setup_via": setup_source,
                        "_predicate_pass": bool(predicate_pass),
                    }
                )

            if not rows:
                # 0件時も診断を明示セット（一貫性のため）
                try:
                    diagnostics["setup_unique_symbols"] = 0
                    set_diagnostics_after_ranking(
                        diagnostics, final_df=None, ranking_source="latest_only"
                    )
                except Exception:
                    diagnostics["ranking_source"] = "latest_only"
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
                                s_rsi = s_last.get("rsi4", float("nan"))
                                try:
                                    s_rsi_f = float(s_rsi)
                                except Exception:
                                    s_rsi_f = float("nan")
                                samples.append(
                                    (
                                        f"{s_sym}: date={s_dt.date()} setup={s_setup} "
                                        f"rsi4={s_rsi_f:.4f}"
                                    )
                                )
                                taken += 1
                                if taken >= 2:
                                    break
                            except Exception:
                                continue
                        if samples:
                            log_callback(
                                (
                                    "System4: DEBUG latest_only 0 candidates. "
                                    + " | ".join(samples)
                                )
                            )
                    except Exception:
                        pass
                    log_callback("System4: latest_only fast-path produced 0 rows")
                return ({}, None, diagnostics) if include_diagnostics else ({}, None)
            df_all = pd.DataFrame(rows)
            mode_date = choose_mode_date_for_latest_only(date_counter)
            if mode_date is not None:
                df_all = df_all[df_all["date"] == mode_date]
            df_all = df_all.sort_values("rsi4", ascending=True, kind="stable").head(
                top_n
            )

            # Recalculate diagnostics from metadata
            if "_setup_via" in df_all.columns:
                via_series = df_all["_setup_via"].fillna("").astype(str)
                diagnostics["setup_predicate_count"] = int((via_series != "").sum())

                predicate_series = (
                    df_all["_predicate_pass"].fillna(False).astype(bool)
                    if "_predicate_pass" in df_all.columns
                    else pd.Series(False, index=df_all.index)
                )

                predicate_only_mask = (via_series != "column") & predicate_series
                diagnostics["predicate_only_pass_count"] = int(
                    predicate_only_mask.sum()
                )
            else:
                diagnostics["setup_predicate_count"] = len(df_all)
                diagnostics["predicate_only_pass_count"] = 0

            diagnostics["setup_unique_symbols"] = int(df_all["symbol"].nunique())

            # Strip metadata before public return
            meta_cols = ["_setup_via", "_predicate_pass"]
            df_public = df_all.drop(
                columns=[c for c in meta_cols if c in df_all.columns]
            )

            set_diagnostics_after_ranking(
                diagnostics, final_df=df_public, ranking_source="latest_only"
            )
            by_date = normalize_dataframe_to_by_date(df_public)
            if log_callback:
                log_callback(
                    f"System4: latest_only fast-path -> {len(df_public)} candidates "
                    f"(symbols={len(rows)})"
                )
            return (
                (by_date, df_public.copy(), diagnostics)
                if include_diagnostics
                else (by_date, df_public.copy())
            )
        except Exception as e:
            if log_callback:
                log_callback(f"System4: fast-path failed -> fallback ({e})")
            # fall back to normal path below

    # Aggregate all dates
    all_dates_set: set[pd.Timestamp] = set()
    for df in prepared_dict.values():
        if df is not None and not df.empty:
            all_dates_set.update(df.index)

    if not all_dates_set:
        if log_callback:
            log_callback("System4: No valid dates found in data")
        return ({}, None, diagnostics) if include_diagnostics else ({}, None)
    all_dates = sorted(all_dates_set)

    candidates_by_date: dict[pd.Timestamp, list[dict[str, Any]]] = {}
    all_candidates: list[dict[str, Any]] = []

    if log_callback:
        log_callback(f"System4: Generating candidates for {len(all_dates)} dates")

    # Execute RSI4 ranking by date (ascending - lowest RSI4 first for oversold)
    for i, date in enumerate(all_dates):
        date_candidates = []

        for symbol, df in prepared_dict.items():
            try:
                if df is None or date not in df.index:
                    continue
                row = cast(pd.Series, df.loc[date])
                setup_val = bool(row.get("setup", False))
                from common.system_setup_predicates import (
                    system4_setup_predicate as _s4_pred,
                )

                pred_val = _s4_pred(row)
                if pred_val:
                    diagnostics["setup_predicate_count"] = (
                        int(diagnostics.get("setup_predicate_count") or 0) + 1
                    )
                if pred_val and not setup_val:
                    diagnostics["predicate_only_pass_count"] = (
                        int(diagnostics.get("predicate_only_pass_count") or 0) + 1
                    )
                    diagnostics["mismatch_flag"] = 1
                if not bool(setup_val):
                    continue
                rsi4_val = cast(Any, row.get("rsi4", 100))
                try:
                    if pd.isna(rsi4_val) or float(rsi4_val) >= MAX_RSI4_THRESHOLD:
                        continue
                except Exception:
                    continue

                date_candidates.append(
                    {
                        "symbol": symbol,
                        "date": date,
                        "rsi4": rsi4_val,
                        "atr_ratio": row.get("atr_ratio", 0),
                        "close": row.get("Close", 0),
                        "sma200": row.get("sma200", 0),
                    }
                )

            except Exception:
                continue

        # Sort by RSI4 ascending (lowest first) and extract top_n
        if date_candidates:
            date_candidates.sort(key=lambda x: x["rsi4"])
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
            ["date", "rsi4"], ascending=[True, True]
        )
        set_diagnostics_after_ranking(
            diagnostics, final_df=candidates_df, ranking_source="full_scan"
        )
    else:
        candidates_df = None
        set_diagnostics_after_ranking(
            diagnostics, final_df=None, ranking_source="full_scan"
        )

    if log_callback:
        total_candidates = len(all_candidates)
        unique_dates = len(candidates_by_date)
        log_callback(
            f"System4: Generated {total_candidates} candidates across "
            f"{unique_dates} dates"
        )

    normalized = normalize_candidates_by_date(candidates_by_date)
    return (
        (normalized, candidates_df, diagnostics)
        if include_diagnostics
        else (normalized, candidates_df)
    )


def get_total_days_system4(data_dict: dict[str, pd.DataFrame]) -> int:
    """Get total days count for System4 data.

    Args:
        data_dict: Data dictionary

    Returns:
        Maximum day count
    """
    total_days: int = get_total_days(data_dict)
    return total_days


__all__ = [
    "prepare_data_vectorized_system4",
    "generate_candidates_system4",
    "get_total_days_system4",
]
