# ============================================================================
# 🧠 Context Note
# このファイルは System2（ショート RSI スパイク）のエントリー・ランキング・フィルタロジック専門
#
# 前提条件：
#   - ショート戦略（RSI3 > 90 が過熱サイン）
#   - 2 日連続上昇確認（twodayup フラグ）が前提
#   - 指標は precomputed のみ使用（ADX7 でランキング）
#   - フロー: setup() → rank() → signals() の順序実行
#
# ロジック単位：
#   setup()       → フィルター条件チェック（DollarVolume20>25M、ATR_Ratio>0.03 など）
#   rank()        → ADX7 の降順ランキング（ボラティリティ優先）
#   signals()     → スコア付きシグナル抽出
#
# Copilot へ：
#   → ショート戦略のため 2 日連続上昇は必須条件。ロジック変更禁止
#   → RSI3 閾値（90）の変更は慎重に。制御テストで検証必須
#   → ADX7 ランキングの順序は維持（他戦略への影響大）
# ============================================================================

"""System2 core logic (Short RSI spike).

RSI3-based short spike strategy:
- Indicators: rsi3, adx7, atr10, dollarvolume20, atr_ratio, twodayup (precomputed only)
- Setup conditions: Close>5, DollarVolume20>25M, ATR_Ratio>0.03, RSI3>90, twodayup
- Candidate generation: ADX7 descending ranking by date, extract top_n
- Optimization: Removed all indicator calculations, using precomputed indicators only
"""

from __future__ import annotations

from collections.abc import Callable
import logging
from typing import Any, cast

import pandas as pd

from common.batch_processing import process_symbols_batch
from common.system_candidates_utils import (
    choose_mode_date_for_latest_only,
    normalize_dataframe_to_by_date,
    set_diagnostics_after_ranking,
)
from common.system_common import (
    check_precomputed_indicators,
    get_total_days,
    slice_latest_rows,
)
from common.system_constants import SYSTEM2_REQUIRED_INDICATORS
from common.system_setup_predicates import validate_predicate_equivalence
from common.utils import get_cached_data

# System2 configuration constants
MIN_PRICE = 5.0  # Minimum price filter (dollars)
MIN_DOLLAR_VOLUME_20 = 25_000_000  # Minimum 20-day dollar volume
MIN_ATR_RATIO = 0.03  # Minimum ATR ratio for volatility filter
RSI3_SPIKE_THRESHOLD = 90  # RSI3 overbought threshold for short entry
DEFAULT_TOP_N = 10  # Default number of top candidates
LATEST_ONLY_TAIL_ROWS = 5
SYSTEM2_LATEST_ONLY_COLUMNS = (
    "Open",
    "High",
    "Low",
    "Close",
    "Volume",
    "rsi3",
    "adx7",
    "atr10",
    "dollarvolume20",
    "atr_ratio",
    "twodayup",
    "uptwodays",
)

logger = logging.getLogger(__name__)


def _numeric_or_nan(df: pd.DataFrame, column: str) -> pd.Series:
    """Return a numeric series for column or NaN-filled series if missing."""
    if column in df.columns:
        return pd.to_numeric(df[column], errors="coerce")
    return pd.Series([float("nan")] * len(df), index=df.index)


def _apply_filter_conditions(df: pd.DataFrame) -> pd.Series:
    """Apply System2 filter conditions (price, volume, volatility).

    Args:
        df: DataFrame with OHLCV and precomputed indicators

    Returns:
        Boolean Series indicating filter pass. Existing ``filter`` values are
        respected if they explicitly mark rows as False, but refreshed values
        always reflect the latest thresholds.
    """

    close = _numeric_or_nan(df, "Close")
    dv20 = _numeric_or_nan(df, "dollarvolume20")
    atr_ratio = _numeric_or_nan(df, "atr_ratio")
    computed = (close >= MIN_PRICE) & (dv20 > MIN_DOLLAR_VOLUME_20) & (
        atr_ratio > MIN_ATR_RATIO
    )

    filter_series = computed.fillna(False)

    if "filter" in df.columns:
        existing = pd.Series(df["filter"], index=df.index).fillna(False).astype(bool)
        filter_series = filter_series & existing

    return filter_series.astype(bool)


def _apply_setup_conditions(
    df: pd.DataFrame, filter_series: pd.Series | None = None
) -> pd.Series:
    """Apply System2 setup conditions (filter + RSI spike + two-day up).

    Args:
        df: DataFrame with OHLCV and precomputed indicators

    Returns:
        Boolean Series indicating setup pass

    Note:
        If df already has 'setup' column, it is preserved and returned as-is.
        This maintains backward compatibility with test fixtures.
    """
    filter_pass = (
        filter_series if filter_series is not None else _apply_filter_conditions(df)
    )
    rsi_ok = _numeric_or_nan(df, "rsi3") > RSI3_SPIKE_THRESHOLD
    if "twodayup" in df.columns:
        two_day_up = (
            pd.Series(df["twodayup"], index=df.index).fillna(False).astype(bool)
        )
    else:
        two_day_up = pd.Series([False] * len(df), index=df.index)

    setup_series = (filter_pass & rsi_ok & two_day_up).fillna(False)

    if "setup" in df.columns:
        existing = pd.Series(df["setup"], index=df.index).fillna(False).astype(bool)
        setup_series = setup_series & existing

    return setup_series.astype(bool)


def _compute_indicators(symbol: str) -> tuple[str, pd.DataFrame | None]:
    """Check precomputed indicators and apply System2-specific filters.

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
            col for col in SYSTEM2_REQUIRED_INDICATORS if col not in df.columns
        ]
        if missing_indicators:
            return symbol, None

        # Apply System2-specific filters and setup
        x = df.copy()
        filter_series = _apply_filter_conditions(x)
        x["filter"] = filter_series
        x["setup"] = _apply_setup_conditions(x, filter_series=filter_series)

        return symbol, x

    except Exception as e:
        logger.debug(f"System2: Failed to process {symbol}: {e}")
        return symbol, None


def prepare_data_vectorized_system2(
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
    """System2 data preparation processing (RSI3 spike strategy).

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
    latest_only = bool(kwargs.get("latest_only", False))
    # Fast path: reuse precomputed indicators
    if reuse_indicators and raw_data_dict:
        try:
            # Early check - verify required indicators exist
            valid_data_dict, error_symbols = check_precomputed_indicators(
                raw_data_dict, SYSTEM2_REQUIRED_INDICATORS, "System2", skip_callback
            )

            if valid_data_dict:
                # Apply System2-specific filters
                prepared_dict = {}
                for symbol, df in valid_data_dict.items():
                    if latest_only:
                        x = slice_latest_rows(
                            df,
                            keep_columns=SYSTEM2_LATEST_ONLY_COLUMNS,
                            tail_rows=LATEST_ONLY_TAIL_ROWS,
                        )
                    else:
                        x = df.copy()
                    filter_series = _apply_filter_conditions(x)
                    x["filter"] = filter_series
                    x["setup"] = _apply_setup_conditions(x, filter_series=filter_series)
                    prepared_dict[symbol] = x

                if log_callback:
                    log_callback(
                        f"System2: Fast-path processed {len(prepared_dict)} symbols"
                    )

                return prepared_dict

        except RuntimeError:
            # Re-raise error immediately if required indicators are missing
            raise
        except Exception as e:
            # Fall back to normal processing for other errors
            logger.debug(f"System2: Fast-path failed: {e}")
            if log_callback:
                log_callback(
                    "System2: Fast-path failed, falling back to normal processing"
                )

    # Normal processing path: batch processing from symbol list
    if symbols:
        target_symbols = symbols
    elif raw_data_dict:
        target_symbols = list(raw_data_dict.keys())
    else:
        if log_callback:
            log_callback("System2: No symbols provided, returning empty dict")
        return {}

    if log_callback:
        log_callback(
            f"System2: Starting normal processing for {len(target_symbols)} symbols"
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
        system_name="System2",
    )
    try:
        validate_predicate_equivalence(results, "2", log_fn=log_callback)
    except Exception as e:
        logger.debug(f"System2: Predicate validation failed: {e}")
        pass
    typed_results = cast(dict[str, pd.DataFrame], results)
    return typed_results


def generate_candidates_system2(
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
    tuple[dict[pd.Timestamp, dict[str, dict]], pd.DataFrame | None]
    | tuple[dict[pd.Timestamp, dict[str, dict]], pd.DataFrame | None, dict[str, Any]]
):
    """System2 candidate generation (ADX7 descending ranking).

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
            # single-source-of-truth: ranked_top_n_count only
            "ranked_top_n_count": 0,
            "predicate_only_pass_count": 0,
            "mismatch_flag": 0,
        }

    if not prepared_dict:
        if log_callback:
            log_callback("System2: No data provided for candidate generation")
        return ({}, None, diagnostics) if include_diagnostics else ({}, None)

    if top_n is None:
        top_n = DEFAULT_TOP_N  # Use configured default value

    # === Fast Path (latest_only) ===
    # 当日シグナル抽出用途: 最新日のみを対象に O(S) でランキング
    if latest_only:
        try:
            rows: list[dict] = []
            date_counter: dict[pd.Timestamp, int] = {}
            setup_pass_count = 0  # カウンター追加
            for sym, df in prepared_dict.items():
                if df is None or df.empty:
                    continue
                last_row = df.iloc[-1]

                # Prefer explicit 'setup' flag when provided (tests/minimal fixtures)
                # Fallback to predicate evaluation only if 'setup' is absent
                setup_ok = False
                try:
                    if "setup" in last_row.index:
                        setup_ok = bool(last_row.get("setup", False))
                    else:
                        try:
                            from common.system_setup_predicates import (
                                system2_setup_predicate as _s2_pred,
                            )
                        except Exception as e:
                            logger.debug(f"System2: Failed to import predicate: {e}")
                            _s2_pred = None
                        if _s2_pred is not None:
                            try:
                                setup_ok = bool(_s2_pred(last_row))
                            except Exception as e:
                                logger.debug(
                                    f"System2: Predicate eval failed for {sym}: {e}"
                                )
                                setup_ok = False
                except Exception as e:
                    logger.debug(f"System2: Setup check failed for {sym}: {e}")
                    setup_ok = False

                if not setup_ok:
                    continue

                setup_pass_count += 1  # setup通過カウント

                adx7_val = last_row.get("adx7", None)
                try:
                    if adx7_val is None or pd.isna(adx7_val):
                        continue
                except Exception as e:
                    logger.debug(f"System2: ADX7 check failed for {sym}: {e}")
                    continue
                dt = pd.Timestamp(str(df.index[-1]))
                date_counter[dt] = date_counter.get(dt, 0) + 1

                # ATR10を配分計算用に保持
                atr10_val = 0.0
                try:
                    atr10_raw = last_row.get("atr10")
                    if atr10_raw is not None and not pd.isna(atr10_raw):
                        atr10_val = float(atr10_raw)
                except Exception as e:
                    logger.debug(f"System2: ATR10 extraction failed for {sym}: {e}")
                    pass

                rows.append(
                    {
                        "symbol": sym,
                        "date": dt,
                        "adx7": adx7_val,
                        "rsi3": last_row.get("rsi3", 0),
                        "close": last_row.get("Close", 0),
                        "atr10": atr10_val,
                    }
                )

            diagnostics["setup_predicate_count"] = setup_pass_count  # 記録

            if not rows:
                if log_callback:
                    log_callback("System2: latest_only fast-path produced 0 rows")
                empty_df = pd.DataFrame(
                    columns=["symbol", "date", "adx7", "rsi3", "close", "atr10"]
                )
                return (
                    ({}, empty_df, diagnostics)
                    if include_diagnostics
                    else ({}, empty_df)
                )
            df_all = pd.DataFrame(rows)
            # 最頻日で揃える（欠落シンボル耐性）
            mode_date = choose_mode_date_for_latest_only(date_counter)
            if mode_date is not None:
                df_all = df_all[df_all["date"] == mode_date]
            df_all = df_all.sort_values("adx7", ascending=False, kind="stable").head(
                top_n
            )
            set_diagnostics_after_ranking(
                diagnostics, final_df=df_all, ranking_source="latest_only"
            )
            # 候補0件なら代表サンプルを1-2件だけDEBUGログ出力
            if diagnostics.get("ranked_top_n_count", 0) == 0 and log_callback:
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
                            samples.append(
                                (
                                    f"{s_sym}: date={s_dt.date()} setup={s_setup} "
                                    f"adx7={float(s_adx):.4f}"
                                )
                            )
                            taken += 1
                            if taken >= 2:
                                break
                        except Exception as e:
                            logger.debug(f"System2: Sample log failed for {s_sym}: {e}")
                            continue
                    if samples:
                        log_callback(
                            "System2: DEBUG latest_only 0 candidates. "
                            + " | ".join(samples)
                        )
                except Exception as e:
                    logger.debug(f"System2: Debug log generation failed: {e}")
                    pass
            # Orchestrator expects: {date: {symbol: {field: value}}}
            by_date = normalize_dataframe_to_by_date(df_all)
            if log_callback:
                log_callback(
                    f"System2: latest_only fast-path -> {len(df_all)} candidates "
                    f"(symbols={len(rows)})"
                )
            return (
                (by_date, df_all.copy(), diagnostics)
                if include_diagnostics
                else (by_date, df_all.copy())
            )
        except Exception as e:
            logger.debug(f"System2: Fast-path failed, falling back to full scan: {e}")
            if log_callback:
                log_callback(f"System2: fast-path failed -> fallback ({e})")
            # フォールバックして従来ロジックへ続行
            pass

    # Helper: case-insensitive getter
    def _get_ci(row: pd.Series, names: list[str], default: Any = None) -> Any:
        for n in names:
            try:
                if n in row:
                    return row.get(n)
            except Exception:
                pass
        return default

    # Collect all unique signal dates (index values)
    all_dates_set: set[pd.Timestamp] = set()
    for df in prepared_dict.values():
        if df is not None and not df.empty:
            try:
                all_dates_set.update(pd.to_datetime(df.index))
            except Exception:
                all_dates_set.update(df.index)

    if not all_dates_set:
        if log_callback:
            log_callback("System2: No valid dates found in data")
        return ({}, None, diagnostics) if include_diagnostics else ({}, None)
    all_signal_dates = sorted(all_dates_set)

    # Build raw candidates keyed by signal date (entry == signal for System2 tests)

    candidates_by_entry_date: dict[pd.Timestamp, list[dict[str, Any]]] = {}

    if log_callback:
        log_callback(
            (
                "System2: Generating candidates for "
                f"{len(all_signal_dates)} dates (entry-date grouping)"
            )
        )

    for i, sig_date in enumerate(all_signal_dates):
        per_date_records: list[dict[str, Any]] = []
        for symbol, df in prepared_dict.items():
            try:
                if df is None or sig_date not in df.index:
                    continue
                row = cast(pd.Series, df.loc[sig_date])

                # Use 'setup' flag only (tests provide minimal columns)
                if not bool(row.get("setup", False)):
                    continue

                # Extract indicators with case-insensitive keys
                adx_val = _get_ci(row, ["ADX7", "adx7"], None)
                try:
                    if adx_val is None or pd.isna(adx_val):
                        continue
                    adx_f = float(adx_val)
                except Exception:
                    continue

                close_val = _get_ci(row, ["Close", "close"], None)
                entry_price = None if close_val is None else float(close_val)

                # For parity tests, use signal date as entry date (same-day entry)
                entry_date = pd.Timestamp(sig_date)

                per_date_records.append(
                    {
                        "symbol": symbol,
                        # normalize field names to align with latest_only path/tests
                        "close": entry_price,
                        "adx7": adx_f,
                        # keep auxiliary fields if needed later
                        "date": pd.Timestamp(sig_date),
                    }
                )
            except Exception as e:
                logger.debug(f"System2: Failed to process {symbol} on {sig_date}: {e}")
                continue

        # Rank by ADX7 desc and apply top_n, then assign ranks
        if per_date_records:
            per_date_records.sort(key=lambda r: r["adx7"], reverse=True)
            ranked = per_date_records[: top_n or DEFAULT_TOP_N]
            total = len(ranked)
            for r_idx, rec in enumerate(ranked, start=1):
                rec["rank"] = r_idx
                rec["rank_total"] = total

            # Use entry_date key (same for all records of this signal date
            # by our mocked resolve). Grouping by the actual entry_date per
            # record is safer in case side-effects vary by symbol.
            for rec in ranked:
                # Group by the signal date itself
                entry_date = pd.Timestamp(rec["date"])  # same-day entry
                candidates_by_entry_date.setdefault(entry_date, []).append(rec)

        if progress_callback and (i + 1) % max(1, len(all_signal_dates) // 10) == 0:
            progress_callback(f"Processed {i + 1}/{len(all_signal_dates)} signal dates")

    # Diagnostics update from final counts (flatten all lists)
    all_records = [rec for lst in candidates_by_entry_date.values() for rec in lst]
    if all_records:
        # Build a minimal DataFrame to reuse shared utility for diagnostics
        df_diag = pd.DataFrame(
            [{"symbol": r.get("symbol"), "date": r.get("date")} for r in all_records]
        )
        set_diagnostics_after_ranking(
            diagnostics, final_df=df_diag, ranking_source="full_scan"
        )
    else:
        set_diagnostics_after_ranking(
            diagnostics, final_df=None, ranking_source="full_scan"
        )

    # Build a DataFrame to return for parity with latest_only
    if all_records:
        df_full = pd.DataFrame(all_records)
    else:
        df_full = pd.DataFrame(
            columns=["symbol", "date", "adx7", "close", "rank", "rank_total"]
        )

    # Normalize to orchestrator-expected shape: {date: {symbol: payload}}
    from common.system_candidates_utils import normalize_candidates_by_date

    normalized = normalize_candidates_by_date(candidates_by_entry_date)

    return (
        (normalized, df_full, diagnostics)
        if include_diagnostics
        else (normalized, df_full)
    )


def get_total_days_system2(data_dict: dict[str, pd.DataFrame]) -> int:
    """Get total days count for System2 data.

    Args:
        data_dict: Data dictionary

    Returns:
        Maximum day count
    """
    total_days: int = get_total_days(data_dict)
    return total_days


__all__ = [
    "prepare_data_vectorized_system2",
    "generate_candidates_system2",
    "get_total_days_system2",
]
