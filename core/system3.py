# ============================================================================
# 🧠 Context Note
# このファイルは System3（ロング ミーン・リバージョン 3 日ドロップ）のロジック専門
#
# 前提条件：
#   - 3 日連続下落を売却シグナル検出（drop3d >= 0.125）
#   - ロング戦略（売却後の回復狙い）
#   - 指標は precomputed のみ使用（indicator_access.py 経由）
#   - フロー: setup() → rank() → signals() の順序実行
#
# ロジック単位：
#   setup()       → フィルター条件チェック（DollarVolume20>25M、atr_ratio>=0.05）
#   rank()        → drop3d の降順ランキング（下落度合い大きい順）
#   signals()     → スコア付きシグナル抽出
#
# Copilot へ：
#   → 正確性を最優先。ドロップ判定の敏感性は慎重に調整
#   → candidates が 0 の場合は正常。エラーと混同するな
#   → setup 条件の厳格化は必ず制御テストで確認
# ============================================================================

"""System3 core logic (Long mean-reversion).

3-day drop mean-reversion strategy:
- Indicators: atr10, dollarvolume20, atr_ratio, drop3d (precomputed only)
- Setup conditions: Close>5, DollarVolume20>25M, atr_ratio>=0.05, drop3d>=0.125
- Candidate generation: drop3d descending ranking by date, extract top_n
- Optimization: Removed all indicator calculations, using precomputed indicators only

"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

import pandas as pd

from common.batch_processing import process_symbols_batch
from common.system_candidates_utils import (
    apply_thresholds,
    choose_mode_date_for_latest_only,
    finalize_ranking_and_diagnostics,
    prepare_ranking_input,
    set_diagnostics_after_ranking,
)
from common.system_common import (
    check_precomputed_indicators,
    get_total_days,
    slice_latest_rows,
)
from common.system_constants import SYSTEM3_REQUIRED_INDICATORS
from common.system_setup_predicates import validate_predicate_equivalence
from common.utils import get_cached_data

# 型安全な環境変数アクセス（可能なら）
try:
    from config.environment import get_env_config as _get_env
except Exception:  # フォールバック
    _get_env = None

# ============================================================================
# System3 Strategy Constants
# ============================================================================
MIN_PRICE = 5.0  # Minimum closing price for filter
MIN_DOLLAR_VOLUME_20 = 25_000_000  # Minimum 20-day dollar volume
DEFAULT_ATR_RATIO_THRESHOLD = 0.05  # Default ATR ratio threshold (can be overridden)
DROP_3D_THRESHOLD = 0.125  # 3-day drop threshold (12.5%)
LATEST_ONLY_TAIL_ROWS = 5
SYSTEM3_LATEST_ONLY_COLUMNS = (
    "Open",
    "High",
    "Low",
    "Close",
    "Volume",
    "atr10",
    "dollarvolume20",
    "atr_ratio",
    "drop3d",
    "sma150",
)


# ============================================================================
# Helper Functions
# ============================================================================
def _apply_filter_conditions(
    df: pd.DataFrame, atr_threshold: float = DEFAULT_ATR_RATIO_THRESHOLD
) -> pd.DataFrame:
    """Apply System3 filter: Close>=MIN_PRICE, DollarVolume20>MIN, ATR>=threshold.

    Preserves existing 'filter' column if present for test compatibility.

    Args:
        df: DataFrame with required columns (Close, dollarvolume20, atr_ratio)
        atr_threshold: ATR ratio threshold (default: DEFAULT_ATR_RATIO_THRESHOLD)

    Returns:
        DataFrame with 'filter' boolean column added/updated
    """
    x = df.copy()

    # Coerce to numeric to avoid runtime/type issues
    try:
        _val_close = x.get("Close")
        if _val_close is None:
            _close = pd.Series(0.0, index=x.index)
        else:
            _close = pd.to_numeric(_val_close, errors="coerce").fillna(0.0)
    except Exception:
        _close = pd.Series(0.0, index=x.index)

    try:
        _val_dvol = x.get("dollarvolume20")
        if _val_dvol is None:
            _dvol = pd.Series(0.0, index=x.index)
        else:
            _dvol = pd.to_numeric(_val_dvol, errors="coerce").fillna(0.0)
    except Exception:
        _dvol = pd.Series(0.0, index=x.index)

    try:
        _val_atr = x.get("atr_ratio")
        if _val_atr is None:
            _atr_ratio = pd.Series(0.0, index=x.index)
        else:
            _atr_ratio = pd.to_numeric(_val_atr, errors="coerce").fillna(0.0)
    except Exception:
        _atr_ratio = pd.Series(0.0, index=x.index)

    computed_filter = (
        (_close >= MIN_PRICE)
        & (_dvol > MIN_DOLLAR_VOLUME_20)
        & (_atr_ratio >= atr_threshold)
    ).fillna(False)

    if "filter" in x.columns:
        existing = pd.Series(x["filter"], index=x.index).fillna(False).astype(bool)
        computed_filter = computed_filter & existing

    x["filter"] = computed_filter.astype(bool)
    return x


def _apply_setup_conditions(df: pd.DataFrame) -> pd.DataFrame:
    """Apply System3 setup conditions: filter & drop3d>=DROP_3D_THRESHOLD.

    Preserves existing 'setup' column if present for test compatibility.

    Args:
        df: DataFrame with 'filter' and 'drop3d' columns

    Returns:
        DataFrame with 'setup' boolean column added/updated
    """
    x = df.copy()

    try:
        _val_drop = x.get("drop3d")
        if _val_drop is None:
            _drop3d = pd.Series(dtype=float, index=x.index)
        else:
            _drop3d = pd.to_numeric(_val_drop, errors="coerce")
    except Exception:
        _drop3d = pd.Series(dtype=float, index=x.index)

    computed_setup = (
        x["filter"].astype(bool) & (~_drop3d.isna()) & (_drop3d >= DROP_3D_THRESHOLD)
    ).fillna(False)

    if "setup" in x.columns:
        existing = pd.Series(x["setup"], index=x.index).fillna(False).astype(bool)
        computed_setup = computed_setup & existing

    x["setup"] = computed_setup.astype(bool)
    return x


def _compute_indicators(symbol: str) -> tuple[str, pd.DataFrame | None]:
    """Check precomputed indicators and apply System3-specific filters.

    Args:
        symbol: Target symbol to process

    Returns:
        (symbol, processed DataFrame | None)
    """
    try:
        df = get_cached_data(symbol)
        if df is None or df.empty:
            return symbol, None

        # Normalize/rename potential variant column names so downstream
        # logic can rely on the canonical indicator names.
        try:
            cols = [c for c in df.columns if isinstance(c, str)]
            rename_map: dict[str, str] = {}

            def _norm_col(name: str) -> str:
                return name.lower().replace("_", "").replace(" ", "")

            for req in SYSTEM3_REQUIRED_INDICATORS:
                req_key = str(req)
                if req_key in df.columns:
                    continue
                # case-insensitive direct match
                found = next((c for c in cols if c.lower() == req_key.lower()), None)
                if found and found != req_key:
                    rename_map[found] = req_key
                    continue
                # fuzzy normalized match
                req_norm = req_key.lower().replace("_", "")
                found2 = next((c for c in cols if _norm_col(str(c)) == req_norm), None)
                if found2 and found2 != req_key:
                    rename_map[found2] = req_key
                    continue
            if rename_map:
                try:
                    df = df.rename(columns=rename_map)
                except Exception:
                    pass
        except Exception:
            pass

        # Ensure required indicators exist (after potential renames)
        missing_indicators = []
        for c in SYSTEM3_REQUIRED_INDICATORS:
            if c not in df.columns:
                missing_indicators.append(c)
        if missing_indicators:
            return symbol, None

        x = df.copy()

        # ATR ratio threshold (allow test override)
        _atr_thr = DEFAULT_ATR_RATIO_THRESHOLD
        try:
            if _get_env is not None:
                _env = _get_env()
                v = getattr(_env, "min_atr_ratio_for_test", None)
                if v is not None:
                    _atr_thr = float(v)
        except Exception:
            _atr_thr = DEFAULT_ATR_RATIO_THRESHOLD

        # Apply filter and setup conditions using helper functions
        x = _apply_filter_conditions(x, atr_threshold=_atr_thr)
        x = _apply_setup_conditions(x)

        return symbol, x
    except Exception:
        return symbol, None


def prepare_data_vectorized_system3(
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
    """System3 data preparation processing (3-day drop mean-reversion strategy).

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
                raw_data_dict, SYSTEM3_REQUIRED_INDICATORS, "System3", skip_callback
            )

            if valid_data_dict:
                # Apply System3-specific filters
                prepared_dict = {}
                for symbol, df in valid_data_dict.items():
                    if latest_only:
                        x = slice_latest_rows(
                            df,
                            keep_columns=SYSTEM3_LATEST_ONLY_COLUMNS,
                            tail_rows=LATEST_ONLY_TAIL_ROWS,
                        )
                    else:
                        x = df.copy()

                    # Filter: Close>=5, DollarVolume20>25M,
                    # ATR_Ratio>=0.05 (test override allowed)
                    _atr_thr = 0.05
                    try:
                        if _get_env is not None:
                            _env = _get_env()
                            if _env.min_atr_ratio_for_test is not None:
                                _atr_thr = float(_env.min_atr_ratio_for_test)
                    except Exception:
                        _atr_thr = 0.05
                    # Build filter mask in steps to avoid too-long expressions
                    close_ok = x["Close"] >= 5.0
                    vol_ok = x["dollarvolume20"] > 25_000_000
                    atr_ok = x["atr_ratio"] >= _atr_thr
                    x["filter"] = close_ok & vol_ok & atr_ok

                    # Setup: Filter + drop3d>=0.125 (12.5% 3-day drop)
                    x["setup"] = x["filter"] & (x["drop3d"] >= 0.125)

                    prepared_dict[symbol] = x

                if log_callback:
                    log_callback(
                        f"System3: Fast-path processed {len(prepared_dict)} symbols"
                    )

                return prepared_dict

        except RuntimeError:
            # Re-raise error immediately if required indicators are missing
            raise
        except Exception:
            # Fall back to normal processing for other errors
            if log_callback:
                log_callback(
                    "System3: Fast-path failed, falling back to normal processing"
                )

    # Normal processing path: batch processing from symbol list
    if symbols:
        target_symbols = symbols
    elif raw_data_dict:
        target_symbols = list(raw_data_dict.keys())
    else:
        if log_callback:
            log_callback("System3: No symbols provided, returning empty dict")
        return {}

    if log_callback:
        log_callback(
            f"System3: Starting normal processing for {len(target_symbols)} symbols"
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
        system_name="System3",
    )
    # Optional predicate equivalence validation (env gated)
    try:
        validate_predicate_equivalence(results, "3", log_fn=log_callback)
    except Exception:
        pass
    from typing import cast as _cast

    return _cast(dict[str, pd.DataFrame], results) if isinstance(results, dict) else {}


def generate_candidates_system3(
    prepared_dict: dict[str, pd.DataFrame],
    *,
    top_n: int | None = None,
    progress_callback: Callable[[str], None] | None = None,
    log_callback: Callable[[str], None] | None = None,
    batch_size: int | None = None,
    latest_only: bool = False,
    include_diagnostics: bool = False,
    **kwargs: Any,
) -> (
    tuple[dict[pd.Timestamp, list[dict[str, Any]]], pd.DataFrame | None]
    | tuple[
        dict[pd.Timestamp, list[dict[str, Any]]],
        pd.DataFrame | None,
        dict[str, Any],
    ]
):
    """System3 candidate generation (drop3d descending ranking).

    Args:
        prepared_dict: Prepared data dictionary
        top_n: Number of top entries to extract
        progress_callback: Progress reporting callback
        log_callback: Log output callback

    Returns:
        (Daily candidate dictionary, Integrated candidate DataFrame)
    """

    def _make_empty_candidates_df() -> pd.DataFrame:
        return pd.DataFrame(
            columns=[
                "symbol",
                "date",
                "entry_date",
                "drop3d",
                "atr_ratio",
                "close",
                "atr10",
            ]
        )

    # Initialize diagnostics dict
    diagnostics: dict[str, Any] = {
        "ranking_source": None,
        "setup_predicate_count": 0,
        "predicate_only_pass_count": 0,
        "ranked_top_n_count": 0,
        "exclude_reasons": {},
        "mismatch_flag": 0,
        # 可視化強化: ランキング入力/統計/閾値/ゼロ理由
        "ranking_input_counts": {
            "rows_total": 0,
            "rows_for_label_date": 0,
            "lagged_rows": 0,
        },
        "ranking_stats": {
            "drop3d_min": None,
            "drop3d_max": None,
            "drop3d_mean": None,
            "drop3d_median": None,
            "drop3d_nan_count": 0,
        },
        "thresholds": {
            "drop3d": 0.125,
            "atr_ratio": 0.05,
        },
        "ranking_zero_reason": None,
        "top_n": int(top_n) if top_n is not None else None,
        "label_date": None,
    }

    if not prepared_dict:
        if log_callback:
            log_callback("System3: No data provided for candidate generation")
        # Populate explicit diagnostics for the empty-input case so callers
        # can understand why no candidates were returned.
        try:
            diagnostics["ranking_input_counts"]["rows_total"] = 0
            diagnostics["ranking_zero_reason"] = "no_prepared_data"
        except Exception:
            pass
        empty_df = _make_empty_candidates_df()
        return ({}, empty_df, diagnostics) if include_diagnostics else ({}, empty_df)

    if top_n is None:
        top_n = 20  # Default value
    try:
        diagnostics["top_n"] = int(top_n)
    except Exception:
        pass

    if latest_only:
        try:
            diagnostics["ranking_source"] = "latest_only"
        except Exception:
            pass
        # 最新日のみ対象。setup==True の銘柄を drop3d 降順で上位抽出
        rows: list[dict] = []
        date_counter: dict[pd.Timestamp, int] = {}
        # 許容営業日ラグを超えて target_date に一致しない銘柄を救済用に一時保持
        lagged_rows: list[dict] = []

        target_date = None
        try:
            maybe = kwargs.get("latest_mode_date")
            if maybe is not None:
                td = pd.to_datetime(str(maybe), errors="coerce")
                if (td is not None) and not pd.isna(td):
                    target_date = pd.Timestamp(td).normalize()
        except Exception:
            target_date = None
        # Diagnostic: log resolved target_date
        try:
            if log_callback:
                log_callback(f"[DEBUG_S3] resolved target_date={target_date}")
        except Exception:
            pass

        def _to_series(obj: Any) -> pd.Series | None:
            try:
                if obj is None:
                    return None
                if isinstance(obj, pd.DataFrame):
                    return obj.iloc[-1]
                if isinstance(obj, pd.Series):
                    return obj
                return None
            except Exception:
                return None

        def _evaluate_row(
            row: pd.Series | None,
        ) -> tuple[bool, bool, bool, float, float, bool, str | None]:
            """
            Evaluate System3 setup conditions using predicate
            (no column dependency).
            """
            if row is None:
                return False, False, False, float("nan"), float("nan"), False, None

            # Use predicate for setup evaluation
            try:
                from common.system_setup_predicates import (
                    system3_setup_predicate as _s3_pred,
                )
            except Exception:
                _s3_pred = None

            setup_flag = False
            pred_reason: str | None = None
            if _s3_pred is not None:
                try:
                    res = _s3_pred(row, return_reason=True)
                    if isinstance(res, tuple):
                        setup_flag, pred_reason = bool(res[0]), res[1]
                    else:
                        setup_flag = bool(res)
                except Exception:
                    setup_flag = False

            try:
                drop_val = float(row.get("drop3d", float("nan")))
            except Exception:
                drop_val = float("nan")

            try:
                atr_val = float(row.get("atr_ratio", float("nan")))
            except Exception:
                atr_val = float("nan")

            # Phase 2 filter already passed, no need to check filter column
            filter_flag = True
            final_flag = setup_flag

            try:
                if _get_env is not None:
                    _env = _get_env()
                    # テストモードのみ drop3d の閾値を環境で緩和可能（本番は常に固定 12.5%）
                    if (
                        not final_flag
                        and hasattr(_env, "is_test_mode")
                        and bool(_env.is_test_mode())
                        and _env.min_drop3d_for_test is not None
                        and filter_flag
                        and not pd.isna(drop_val)
                    ):
                        thr = float(_env.min_drop3d_for_test)
                        if drop_val >= thr:
                            final_flag = True
            except Exception:
                pass

            # predicate_flag is now same as setup_flag
            predicate_flag = setup_flag
            return (
                setup_flag,
                predicate_flag,
                final_flag,
                drop_val,
                atr_val,
                filter_flag,
                pred_reason,
            )

        # trading-day lag helper
        try:
            from common.utils_spy import (  # noqa: WPS433
                calculate_trading_days_lag as _td_lag,
            )
        except Exception:
            _td_lag = None

        # tolerance days (orchestrator may pass max_date_lag_days)
        max_date_lag_days = 1
        try:
            lag_override = kwargs.get("max_date_lag_days")
            if lag_override is not None:
                max_date_lag_days = max(0, int(float(str(lag_override))))
        except Exception:
            max_date_lag_days = 1

        for sym, df in prepared_dict.items():
            try:
                if df is None or df.empty:
                    continue
                # 日付サニタイズ関数（System1 と同等の安全ガード）

                def _sanitize_signal_date(
                    dt_obj: object,
                    fallback: pd.Timestamp | None,
                ) -> pd.Timestamp | None:
                    try:
                        ts = pd.Timestamp(str(dt_obj)).normalize()
                    except Exception:
                        return fallback
                    if pd.isna(ts):
                        return fallback
                    y = int(getattr(ts, "year", 0) or 0)
                    if y < 1900 or y > 2262:
                        return fallback
                    return ts

                if target_date is not None:
                    if target_date in df.index:
                        last_row = _to_series(df.loc[target_date])
                        dt = _sanitize_signal_date(target_date, fallback=None)
                    else:
                        # allow fallback to latest if trading-day lag within tolerance
                        latest_idx_raw = df.index[-1]
                        latest_idx_norm = pd.Timestamp(str(latest_idx_raw)).normalize()
                        lag_days: int | None = None
                        try:
                            if _td_lag is not None:
                                lag_days = int(_td_lag(latest_idx_norm, target_date))
                            else:
                                lag_days = int((target_date - latest_idx_norm).days)
                        except Exception:
                            lag_days = None
                        if (
                            lag_days is not None
                            and lag_days >= 0
                            and lag_days <= max_date_lag_days
                        ):
                            last_row = _to_series(df.loc[latest_idx_raw])
                            dt = _sanitize_signal_date(target_date, fallback=None)
                        else:
                            # 許容超過: rows には入れず、後段の不足補完用に保存
                            last_row = _to_series(df.loc[latest_idx_raw])
                            dt = _sanitize_signal_date(target_date, fallback=None)
                            if last_row is None:
                                continue
                            (
                                setup_col_ex,
                                _predicate_ex,
                                final_ok_ex,
                                drop_val_ex,
                                atr_val_ex,
                                _filter_ex,
                                pred_reason_ex,
                            ) = _evaluate_row(last_row)
                            setup_source_ex = ""
                            predicate_pass_ex = bool(_predicate_ex)
                            fallback_pass_ex = False
                            if setup_col_ex:
                                setup_source_ex = "column"
                            elif predicate_pass_ex:
                                setup_source_ex = "predicate"
                                diagnostics["mismatch_flag"] = 1
                            elif final_ok_ex:
                                setup_source_ex = "fallback"
                                fallback_pass_ex = True
                                diagnostics["mismatch_flag"] = 1

                            if (not setup_source_ex) or pd.isna(drop_val_ex):
                                try:
                                    if pred_reason_ex:
                                        try:
                                            from common.diagnostics_utils import (
                                                record_exclude,
                                            )

                                            record_exclude(
                                                diagnostics,
                                                str(pred_reason_ex),
                                                sym,
                                            )
                                        except Exception:
                                            pass
                                except Exception:
                                    pass
                                continue
                            try:
                                from common.utils_spy import (
                                    resolve_signal_entry_date as _resolve_entry_ex,
                                )

                                entry_dt_ex = _resolve_entry_ex(dt)
                            except Exception:
                                entry_dt_ex = None
                            atr_payload_ex = 0 if pd.isna(atr_val_ex) else atr_val_ex
                            lagged_rows.append(
                                {
                                    "symbol": sym,
                                    "date": dt,
                                    "entry_date": entry_dt_ex,
                                    "drop3d": drop_val_ex,
                                    "atr_ratio": atr_payload_ex,
                                    "close": last_row.get("Close", 0),
                                    "atr10": last_row.get("atr10", 0),
                                    "_setup_via": setup_source_ex,
                                    "_predicate_pass": predicate_pass_ex,
                                    "_fallback_pass": fallback_pass_ex,
                                }
                            )
                            continue
                else:
                    last_row = _to_series(df.iloc[-1])
                    try:
                        last_idx = df.index[-1]
                        last_idx_ts = pd.Timestamp(str(last_idx)).normalize()
                        dt = _sanitize_signal_date(last_idx_ts, fallback=None)
                    except Exception:
                        last_idx_ts2 = pd.Timestamp(str(df.index[-1])).normalize()
                        dt = _sanitize_signal_date(last_idx_ts2, fallback=None)

                if last_row is None:
                    continue
                if dt is None:
                    # ラベル日が解決できない場合は除外
                    try:
                        from common.diagnostics_utils import record_exclude

                        record_exclude(diagnostics, "invalid_date_label", sym)
                    except Exception:
                        pass
                    continue

                (
                    setup_col,
                    _predicate_flag,
                    final_ok,
                    drop_val,
                    atr_val,
                    _filter_flag,
                    pred_reason,
                ) = _evaluate_row(last_row)

                # Allow fallback to existing setup column if predicate could not
                # evaluate due to missing optional fields in test fixtures.
                setup_from_column = False
                try:
                    raw_setup_val = last_row.get("setup")
                    if raw_setup_val is not None and not pd.isna(raw_setup_val):
                        setup_from_column = bool(raw_setup_val)
                except Exception:
                    setup_from_column = False

                if setup_from_column and not setup_col:
                    setup_col = True
                    final_ok = True
                    diagnostics["mismatch_flag"] = 1

                setup_source = ""
                predicate_pass = bool(_predicate_flag)
                fallback_pass = False
                if setup_from_column:
                    setup_source = "column"
                elif predicate_pass:
                    setup_source = "predicate"
                    diagnostics["mismatch_flag"] = 1
                elif final_ok:
                    setup_source = "fallback"
                    fallback_pass = True
                    diagnostics["mismatch_flag"] = 1

                if not setup_source:
                    try:
                        if pred_reason:
                            from common.diagnostics_utils import record_exclude

                            record_exclude(diagnostics, str(pred_reason), sym)
                    except Exception:
                        pass
                    continue
                if pd.isna(drop_val):
                    continue

                date_counter[dt] = date_counter.get(dt, 0) + 1
                # 明示エントリー日（翌営業日）
                try:
                    from common.utils_spy import (
                        resolve_signal_entry_date as _resolve_entry,
                    )

                    entry_dt = _resolve_entry(dt)
                except Exception:
                    entry_dt = None

                atr_payload = 0 if pd.isna(atr_val) else atr_val

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
                        "entry_date": entry_dt,
                        "drop3d": drop_val,
                        "atr_ratio": atr_payload,
                        "close": last_row.get("Close", 0),
                        "atr10": atr10_val,
                        "_setup_via": setup_source,
                        "_predicate_pass": predicate_pass,
                        "_fallback_pass": fallback_pass,
                    }
                )
            except Exception:
                continue

        # Diagnostic: summary of date_counter and a tiny preview of rows
        try:
            if log_callback:
                try:
                    dc_preview = dict(list(date_counter.items())[:5])
                    log_callback(f"[DEBUG_S3] date_counter_sample={dc_preview}")
                except Exception:
                    pass
                try:
                    if rows:
                        sample_rows = rows[:5]
                        log_callback(f"[DEBUG_S3] rows_sample={sample_rows}")
                except Exception:
                    pass
        except Exception:
            pass

        if not rows:
            # Populate diagnostic counts for zero-row fast-path so callers can
            # immediately understand why no candidates were produced.
            # Safe defaults for values used later in diagnostics
            total_sampled = 0
            drop_vals: list[float] = []
            # Ensure ranking source is explicitly marked for latest_only fast-path
            try:
                diagnostics["ranking_source"] = "latest_only"
            except Exception:
                pass
            try:
                diag_counts = diagnostics.get("ranking_input_counts", {})
                diag_counts["rows_total"] = int(len(rows))
                diag_counts["lagged_rows"] = int(len(lagged_rows))
                diag_counts["rows_for_label_date"] = 0
                if prepared_dict:
                    prepared_symbols_count = len(prepared_dict)
                else:
                    prepared_symbols_count = 0
                diag_counts["prepared_symbols"] = int(prepared_symbols_count)
                diagnostics["ranking_input_counts"] = diag_counts
            except Exception:
                pass

            # Compute simple filter-level counts from the latest row per symbol
            try:
                filter_counts: dict[str, int] = {
                    "close_lt_5": 0,
                    "dvol_le_25m": 0,
                    "atr_ratio_lt_thr": 0,
                    "drop3d_nan": 0,
                }
                drop_vals = []
                total_sampled = 0
                for s_sym, s_df in prepared_dict.items():
                    if s_df is None or getattr(s_df, "empty", True):
                        continue
                    try:
                        s_last = s_df.iloc[-1]
                        total_sampled += 1
                        # Close
                        try:
                            c = float(s_last.get("Close", float("nan")))
                            if c < 5.0:
                                filter_counts["close_lt_5"] += 1
                        except Exception:
                            filter_counts["close_lt_5"] += 1
                        # Dollar volume
                        try:
                            dv = float(s_last.get("dollarvolume20", float("nan")))
                            if dv <= 25_000_000:
                                filter_counts["dvol_le_25m"] += 1
                        except Exception:
                            filter_counts["dvol_le_25m"] += 1
                        # ATR ratio
                        try:
                            av = float(s_last.get("atr_ratio", float("nan")))
                            thr_map = diagnostics.get("thresholds", {})
                            try:
                                atr_thr = float(thr_map.get("atr_ratio", 0.05))
                            except Exception:
                                atr_thr = 0.05
                            if av < atr_thr:
                                filter_counts["atr_ratio_lt_thr"] += 1
                        except Exception:
                            filter_counts["atr_ratio_lt_thr"] += 1
                        # drop3d
                        try:
                            dv3 = s_last.get("drop3d")
                            if dv3 is None or (pd.isna(dv3)):
                                filter_counts["drop3d_nan"] += 1
                            else:
                                drop_vals.append(float(dv3))
                        except Exception:
                            filter_counts["drop3d_nan"] += 1
                    except Exception:
                        continue

                diagnostics["filter_counts"] = filter_counts
                # compute basic drop3d stats
                try:
                    if drop_vals:
                        sser = pd.Series(drop_vals)
                        rstats = diagnostics.get("ranking_stats", {})
                        rstats["drop3d_min"] = float(sser.min())
                        rstats["drop3d_max"] = float(sser.max())
                        rstats["drop3d_mean"] = float(sser.mean())
                        rstats["drop3d_median"] = float(sser.median())
                        rstats["drop3d_nan_count"] = int(total_sampled - len(drop_vals))
                        diagnostics["ranking_stats"] = rstats
                    else:
                        rstats = diagnostics.get("ranking_stats", {})
                        rstats["drop3d_nan_count"] = int(total_sampled)
                        diagnostics["ranking_stats"] = rstats
                except Exception:
                    pass
            except Exception:
                pass

            # Derive a likely zero reason from per-symbol exclude reasons or
            # from the simple filter counts we just computed.
            try:
                reason = None
                excl = diagnostics.get("exclude_reasons", {}) or {}
                try:
                    # If every sampled symbol was rejected by phase2 filter
                    if total_sampled > 0:
                        filt_count = int(excl.get("filter_phase2", 0))
                        drop3d_nan_count = int(
                            diagnostics.get("filter_counts", {}).get("drop3d_nan", 0)
                        )
                        if filt_count >= total_sampled:
                            reason = "all_filtered_phase2"
                        elif drop3d_nan_count >= total_sampled:
                            reason = "all_drop3d_nan"
                        else:
                            thr_raw = diagnostics.get("thresholds", {}).get(
                                "drop3d", 0.125
                            )
                            try:
                                thr = float(thr_raw)
                            except Exception:
                                thr = 0.125
                            if drop_vals and max(drop_vals) < thr:
                                reason = "all_below_drop3d_threshold"
                except Exception:
                    reason = None
                diagnostics["ranking_zero_reason"] = reason
            except Exception:
                diagnostics["ranking_zero_reason"] = None

            if log_callback:
                try:
                    # Representative sample logging
                    samples = []
                    taken = 0
                    for s_sym, s_df in prepared_dict.items():
                        if s_df is None or getattr(s_df, "empty", True):
                            continue
                        try:
                            s_last = s_df.iloc[-1]
                            s_dt = pd.to_datetime(str(s_df.index[-1])).normalize()
                            s_setup = bool(s_last.get("setup", False))
                            try:
                                dv3v = s_last.get("drop3d")
                                if dv3v is not None and not pd.isna(dv3v):
                                    drop_txt = f"{float(dv3v):.4f}"
                                else:
                                    drop_txt = "nan"
                            except Exception:
                                drop_txt = "nan"
                            samples.append(
                                f"{s_sym}:{s_dt.date()} setup={s_setup} d3={drop_txt}"
                            )
                            taken += 1
                            if taken >= 2:
                                break
                        except Exception:
                            continue
                    if samples:
                        prefix = "System3: DEBUG latest_only 0 candidates. "
                        log_callback(prefix + " | ".join(samples))
                except Exception:
                    pass
                log_callback("System3: latest_only fast-path produced 0 rows")
            empty_df = _make_empty_candidates_df()
            try:
                set_diagnostics_after_ranking(
                    diagnostics, final_df=None, ranking_source="latest_only"
                )
            except Exception:
                diagnostics["ranking_source"] = "latest_only"
            if include_diagnostics:
                return ({}, empty_df, diagnostics)
            return ({}, empty_df)

        df_all = pd.DataFrame(rows)
        # top-off用に元の全候補を保持
        df_all_original = df_all.copy()
        if log_callback:
            log_callback(
                f"[DEBUG_S3_ROWS] rows={len(rows)} lagged_rows={len(lagged_rows)}"
            )
            # 診断用: 入力件数
            try:
                diag_counts = diagnostics["ranking_input_counts"]
                diag_counts["rows_total"] = int(len(df_all_original))
                diag_counts["lagged_rows"] = int(len(lagged_rows))
            except Exception:
                pass

        # target_date 優先でフィルタ。0件/不足時は安全フォールバックで補充する
        try:
            filtered = df_all
            final_label_date: pd.Timestamp | None = None
            mode_date = choose_mode_date_for_latest_only(date_counter)
            label_candidate = target_date if target_date is not None else mode_date

            required_cols = ["symbol", "date", "drop3d", "atr_ratio"]
            prepared_df, input_counts = prepare_ranking_input(
                df_all,
                label_candidate,
                required_cols,
                date_col="date",
                symbol_col="symbol",
            )

            try:
                diag_counts = diagnostics.get("ranking_input_counts", {})
                for key, val in input_counts.items():
                    if isinstance(val, (int, float)):
                        diag_counts[key] = int(val)
                    else:
                        diag_counts[key] = val
                diag_counts["lagged_rows"] = int(len(lagged_rows))
                diag_counts["rows_total"] = int(len(df_all_original))
                diagnostics["ranking_input_counts"] = diag_counts
            except Exception:
                pass

            if not prepared_df.empty and label_candidate is not None:
                filtered = prepared_df
                final_label_date = pd.Timestamp(str(label_candidate)).normalize()
            elif (
                target_date is not None
                and mode_date is not None
                and mode_date != target_date
            ):
                fallback_df, mode_counts = prepare_ranking_input(
                    df_all,
                    mode_date,
                    required_cols,
                    date_col="date",
                    symbol_col="symbol",
                )
                try:
                    diag_counts = diagnostics.get("ranking_input_counts", {})
                    diag_counts["rows_for_mode_date"] = int(
                        mode_counts.get("rows_for_label_date", 0)
                    )
                    diagnostics["ranking_input_counts"] = diag_counts
                except Exception:
                    pass
                if not fallback_df.empty:
                    filtered = fallback_df
                    final_label_date = pd.Timestamp(mode_date).normalize()
            if (filtered is None or filtered.empty) and lagged_rows:
                tmp = pd.DataFrame(lagged_rows)
                if not tmp.empty:
                    filtered = tmp.copy()
                    if target_date is not None:
                        final_label_date = target_date
                    elif mode_date is not None:
                        final_label_date = pd.Timestamp(mode_date).normalize()
        except Exception:
            filtered = df_all
            final_label_date = None

        # 診断用: ラベル日・フィルタ後件数・統計
        try:
            if final_label_date is not None:
                diag_label = pd.Timestamp(str(final_label_date)).isoformat()
                diagnostics["label_date"] = diag_label
            else:
                diagnostics["label_date"] = None
        except Exception:
            diagnostics["label_date"] = None
        try:
            diag_counts = diagnostics.get("ranking_input_counts", {})
            diag_counts["rows_for_label_date"] = int(len(filtered))
            diagnostics["ranking_input_counts"] = diag_counts
        except Exception:
            pass

        # 有効な drop3d 指標の分布を可視化（NaN 含む）
        try:
            if not filtered.empty and "drop3d" in filtered.columns:
                s = pd.to_numeric(filtered["drop3d"], errors="coerce")
                if s.size > 0:
                    s_no_nan = s.dropna()
                    r_stats = diagnostics.get("ranking_stats", {})
                    if not s_no_nan.empty:
                        r_stats["drop3d_min"] = float(s_no_nan.min())
                        r_stats["drop3d_max"] = float(s_no_nan.max())
                        r_stats["drop3d_mean"] = float(s_no_nan.mean())
                        r_stats["drop3d_median"] = float(s_no_nan.median())
                    else:
                        r_stats["drop3d_min"] = None
                        r_stats["drop3d_max"] = None
                        r_stats["drop3d_mean"] = None
                        r_stats["drop3d_median"] = None
                    r_stats["drop3d_nan_count"] = int(s.isna().sum())
                    diagnostics["ranking_stats"] = r_stats
        except Exception:
            pass

        # 使用された閾値（可能なら環境から上書き）
        try:
            _drop_thr = DROP_3D_THRESHOLD
            if _get_env is not None:
                try:
                    _env = _get_env()
                    v = getattr(_env, "min_drop3d_for_test", None)
                    if (
                        hasattr(_env, "is_test_mode")
                        and bool(_env.is_test_mode())
                        and v is not None
                    ):
                        _drop_thr = float(v)
                except Exception:
                    pass
            diagnostics["thresholds"]["drop3d"] = float(_drop_thr)
        except Exception:
            pass
        try:
            _atr_thr = DEFAULT_ATR_RATIO_THRESHOLD
            if _get_env is not None:
                try:
                    _env = _get_env()
                    _val = getattr(_env, "min_atr_ratio_for_test", None)
                    if _val is not None:
                        _atr_thr = float(_val)
                except Exception:
                    pass
            diagnostics["thresholds"]["atr_ratio"] = float(_atr_thr)
        except Exception:
            pass

        if not isinstance(filtered, pd.DataFrame):
            filtered = pd.DataFrame(columns=df_all.columns)

        filtered, threshold_counts, _ = apply_thresholds(
            filtered,
            {
                "drop3d": {"op": ">=", "value": _drop_thr},
                "atr_ratio": {"op": ">=", "value": _atr_thr},
            },
            symbol_col="symbol",
        )

        if threshold_counts:
            try:
                exclude = diagnostics.get("exclude_reasons", {}) or {}
                for key, count in threshold_counts.items():
                    exclude_key = f"threshold_{key}"
                    exclude[exclude_key] = exclude.get(exclude_key, 0) + int(count)
                diagnostics["exclude_reasons"] = exclude
            except Exception:
                pass

        # ランキングして上位を確定
        ranked = filtered.sort_values("drop3d", ascending=False, kind="stable").copy()
        top_cut = ranked.head(top_n)

        # 診断用：フィルタ前の件数を記録
        try:
            diagnostics["ranking_breakdown"] = {
                "original_filtered": len(filtered),
                "top_cut_before_topoff": len(top_cut),
            }
        except Exception:
            pass

        # 足りない分を df_all_original + lagged_rows から補完（top-off）。date/entry_date を正規化。
        missing = max(0, int(top_n) - len(top_cut))
        if log_callback:
            log_callback(
                (
                    "[DEBUG_S3_TOPOFF] "
                    f"filtered={len(filtered)} top_cut={len(top_cut)} "
                    f"missing={missing} "
                    f"df_all_orig={len(df_all_original)} "
                    f"lagged={len(lagged_rows)}"
                )
            )
        # df_all_original または lagged_rows に補完候補がある場合に top-off を実行
        if missing > 0 and (len(df_all_original) > 0 or lagged_rows):
            try:
                if not top_cut.empty:
                    exists = set(top_cut["symbol"].astype(str))
                else:
                    exists = set()
                extras_pool = (
                    df_all_original.sort_values(
                        "drop3d", ascending=False, kind="stable"
                    )
                    .loc[~df_all_original["symbol"].astype(str).isin(exists)]
                    .copy()
                )
                # 許容ラグ超過の救済候補もプールに追加（重複symbolは除外）
                if lagged_rows:
                    lag_df = pd.DataFrame(lagged_rows)
                    if not lag_df.empty:
                        lag_df = lag_df.loc[~lag_df["symbol"].astype(str).isin(exists)]
                        extras_pool = pd.concat(
                            [extras_pool, lag_df],
                            ignore_index=True,
                        )
                if not extras_pool.empty:
                    if final_label_date is None:
                        try:
                            final_label_date = (
                                target_date
                                if target_date is not None
                                else max(date_counter.items(), key=lambda kv: kv[1])[0]
                            )
                        except Exception:
                            final_label_date = None
                        if final_label_date is not None:
                            extras_pool.loc[:, "date"] = final_label_date
                            try:
                                from common.utils_spy import (
                                    resolve_signal_entry_date as _resolve_entry_dt2,
                                )

                                entry_dt_val = _resolve_entry_dt2(final_label_date)
                                extras_pool.loc[:, "entry_date"] = entry_dt_val
                            except Exception:
                                extras_pool.loc[:, "entry_date"] = final_label_date
                    extras_take = extras_pool.head(missing)
                    top_cut = (
                        pd.concat([top_cut, extras_take], ignore_index=True)
                        .drop_duplicates(subset=["symbol"], keep="first")
                        .head(top_n)
                    )
            except Exception:
                pass

        df_all = top_cut
        # Feature flag: allow using Option-B finalize helper (non-breaking)
        use_option_b_utils = False
        try:
            # kwargs flag first
            if bool(kwargs.get("use_option_b_utils", False)):
                use_option_b_utils = True
            elif _get_env is not None:
                _env = _get_env()
                v = getattr(_env, "enable_option_b_system3", None)
                if v is not None and bool(v):
                    use_option_b_utils = True
        except Exception:
            use_option_b_utils = False

        meta_cols = ["_setup_via", "_predicate_pass", "_fallback_pass"]
        if "_setup_via" in df_all.columns:
            via_series = df_all["_setup_via"].fillna("").astype(str)
            diagnostics["setup_predicate_count"] = int((via_series != "").sum())

            predicate_series = (
                df_all["_predicate_pass"].fillna(False).astype(bool)
                if "_predicate_pass" in df_all.columns
                else pd.Series(False, index=df_all.index)
            )
            fallback_series = (
                df_all["_fallback_pass"].fillna(False).astype(bool)
                if "_fallback_pass" in df_all.columns
                else pd.Series(False, index=df_all.index)
            )

            predicate_only_mask = (via_series != "column") & (
                predicate_series | fallback_series
            )
            diagnostics["predicate_only_pass_count"] = int(predicate_only_mask.sum())
            if diagnostics["predicate_only_pass_count"] > 0:
                diagnostics["mismatch_flag"] = 1
        else:
            diagnostics["setup_predicate_count"] = len(df_all)
            diagnostics["predicate_only_pass_count"] = 0

        try:
            diagnostics["setup_unique_symbols"] = int(df_all["symbol"].nunique())
        except Exception:
            pass

        df_public = df_all.drop(columns=[c for c in meta_cols if c in df_all.columns])

        if use_option_b_utils:
            try:
                finalize_ranking_and_diagnostics(
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

        # ✅ 診断内訳の詳細記録
        try:
            extras = max(0, len(df_public) - len(top_cut))
            diagnostics["ranking_breakdown"]["extras_added"] = extras
            diagnostics["ranking_breakdown"]["final_count"] = len(df_public)
        except Exception:
            pass

        # ✅ 診断整合性チェック: ranked > setup は論理エラー
        if diagnostics["ranked_top_n_count"] > diagnostics["setup_predicate_count"]:
            if log_callback:
                ranked = diagnostics["ranked_top_n_count"]
                setup = diagnostics["setup_predicate_count"]
                breakdown = diagnostics.get("ranking_breakdown", {})
                log_callback(
                    f"System3: WARNING - ranked_top_n ({ranked}) > "
                    f"setup_predicate_count ({setup}). "
                    f"Breakdown: {breakdown}"
                )

        # 0 件時の原因推定（可視化用）
        if len(df_public) == 0:
            reason = "unknown"
            try:
                if filtered is not None and len(filtered) == 0:
                    reason = "no_rows_for_label_date"
                elif ("drop3d" in filtered.columns) and filtered["drop3d"].isna().all():
                    reason = "all_drop3d_nan"
                elif ("drop3d" in filtered.columns) and (
                    filtered["drop3d"].dropna().size > 0
                ):
                    # 閾値未満のみ（参考判定）
                    try:
                        _thr = float(diagnostics["thresholds"].get("drop3d", 0.125))
                        if float(filtered["drop3d"].dropna().max()) < _thr:
                            reason = "all_below_drop3d_threshold"
                    except Exception:
                        pass
            except Exception:
                reason = "unknown"
            diagnostics["ranking_zero_reason"] = reason
            if log_callback:
                stats_summary = diagnostics.get("ranking_stats")
                # Keep the constructed message on short lines so linters
                # do not complain about line length.
                left = "[DEBUG_S3_RANK0] reason=" + str(reason)
                right = " stats=" + str(stats_summary)
                msg = left + right
                log_callback(msg)

        # Build per-date list of candidate dicts (public API expectation)
        by_date_list: dict[pd.Timestamp, list[dict[str, Any]]] = {}
        if not df_public.empty:
            for dt_raw, sub in df_public.groupby("date"):
                dt = pd.Timestamp(str(dt_raw))
                sub_sorted = sub.sort_values("drop3d", ascending=False, kind="stable")
                by_date_list[dt] = []
                for rec in sub_sorted.to_dict("records"):
                    item: dict[str, Any] = {
                        "symbol": rec.get("symbol"),
                        "date": dt,
                        "drop3d": rec.get("drop3d"),
                        "atr_ratio": rec.get("atr_ratio"),
                        "close": rec.get("close"),
                    }
                    if "entry_date" in rec:
                        item["entry_date"] = rec.get("entry_date")
                    if "atr10" in rec:
                        item["atr10"] = rec.get("atr10")
                    by_date_list[dt].append(item)

        if log_callback:
            msg = (
                f"System3: latest_only fast-path -> {len(df_public)} candidates "
                f"(symbols={len(rows)})"
            )
            log_callback(msg)

        if include_diagnostics:
            return by_date_list, df_public.copy(), diagnostics
        return by_date_list, df_public.copy()

    # Aggregate all dates
    all_dates_set: set[pd.Timestamp] = set()
    for df in prepared_dict.values():
        if df is not None and not df.empty:
            all_dates_set.update(df.index)

    if not all_dates_set:
        if log_callback:
            log_callback("System3: No valid dates found in data")
        empty_df = _make_empty_candidates_df()
        if include_diagnostics:
            return ({}, empty_df, diagnostics)
        return ({}, empty_df)
    all_dates = sorted(all_dates_set)

    candidates_by_date: dict[pd.Timestamp, list[dict[str, Any]]] = {}
    all_candidates: list[dict[str, Any]] = []

    if log_callback:
        log_callback(f"System3: Generating candidates for {len(all_dates)} dates")

    # Execute drop3d ranking by date
    for i, date in enumerate(all_dates):
        date_candidates: list[dict[str, Any]] = []
        for symbol, df in prepared_dict.items():
            try:
                if df is None or date not in df.index:
                    continue
                row = cast(pd.Series, df.loc[date])
                setup_val = bool(row.get("setup", False))
                from common.system_setup_predicates import (
                    system3_setup_predicate as _s3_pred,
                )

                pred_val = _s3_pred(row)
                if pred_val:
                    diagnostics["setup_predicate_count"] += 1
                if pred_val and not setup_val:
                    diagnostics["predicate_only_pass_count"] += 1
                    diagnostics["mismatch_flag"] = 1
                if not bool(setup_val):
                    continue
                drop3d_val = cast(Any, row.get("drop3d", 0))
                try:
                    if pd.isna(drop3d_val) or float(drop3d_val) < 0.125:
                        continue
                except Exception:
                    continue

                date_candidates.append(
                    {
                        "symbol": symbol,
                        "date": date,
                        "drop3d": drop3d_val,
                        "atr_ratio": row.get("atr_ratio", 0),
                        "close": row.get("Close", 0),
                    }
                )

            except Exception:
                continue

        # Sort by drop3d descending and extract top_n
        if date_candidates:
            date_candidates.sort(key=lambda x: x["drop3d"], reverse=True)
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
            ["date", "drop3d"], ascending=[True, False]
        )
        # Feature flag path
        use_option_b_utils = False
        try:
            if bool(kwargs.get("use_option_b_utils", False)):
                use_option_b_utils = True
            elif _get_env is not None:
                _env = _get_env()
                v = getattr(_env, "enable_option_b_system3", None)
                if v is not None and bool(v):
                    use_option_b_utils = True
        except Exception:
            use_option_b_utils = False

        if use_option_b_utils:
            try:
                from common.system_candidates_utils import (
                    finalize_ranking_and_diagnostics as _finalize_diag,
                )

                _finalize_diag(
                    diagnostics,
                    candidates_df,
                    ranking_source="full_scan",
                    extras=None,
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
        # Feature flag path for empty
        use_option_b_utils = False
        try:
            if bool(kwargs.get("use_option_b_utils", False)):
                use_option_b_utils = True
            elif _get_env is not None:
                _env = _get_env()
                v = getattr(_env, "enable_option_b_system3", None)
                if v is not None and bool(v):
                    use_option_b_utils = True
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
        summary_msg = (
            f"System3: Generated {total_candidates} candidates "
            f"across {unique_dates} dates"
        )
        log_callback(summary_msg)

    # Keep original API: date -> list[dict]
    if include_diagnostics:
        return candidates_by_date, candidates_df, diagnostics
    return candidates_by_date, candidates_df


def get_total_days_system3(data_dict: dict[str, pd.DataFrame]) -> int:
    """Get total days count for System3 data.

    Args:
        data_dict: Data dictionary

    Returns:
        Maximum day count
    """
    return int(get_total_days(data_dict))


__all__ = [
    "prepare_data_vectorized_system3",
    "generate_candidates_system3",
    "get_total_days_system3",
]
