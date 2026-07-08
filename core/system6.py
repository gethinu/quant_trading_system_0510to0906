# ============================================================================
# 🧠 Context Note
# このファイルは System6（ショート ミーン・リバージョン 高シックスデイサージ）のロジック専門
#
# 前提条件：
#   - 6 日上昇サージを検出して空売りシグナル生成
#   - 高変動性環境で機能（HV 上昇確認）
#   - 指標は precomputed のみ使用
#   - フロー: setup() → rank() → signals() の順序実行
#   - 候補数が 0 になることは正常（サージ条件が厳しい）
#
# ロジック単位：
#   prepare_data()       → 6 日サージ計算・フィルター条件チェック
#   generate_candidates() → スコアリング＆ランキング
#   signals()           → シグナル抽出
#
# Copilot へ：
#   → candidates 数がゼロでもエラーではない。正常な現象として扱う
#   → 6 日サージ検出ロジックは絶対変更禁止（他システムとの差別化要素）
#   → ボラティリティ条件の変更は制御テストで必ず確認
# ============================================================================

"""System6 core logic (Short mean-reversion momentum burst)."""

from collections.abc import Callable
import logging
import math
import time
from typing import Any, cast

import pandas as pd
from ta.volatility import AverageTrueRange

from common.batch_processing import process_symbols_batch
from common.i18n import tr
from common.structured_logging import MetricsCollector
from common.system_candidates_utils import (
    choose_mode_date_for_latest_only,
    finalize_ranking_and_diagnostics,
    normalize_dataframe_to_by_date,
    set_diagnostics_after_ranking,
)
from common.system_setup_predicates import validate_predicate_equivalence
from common.utils import resolve_batch_size

try:
    from config.environment import get_env_config
except Exception:  # pragma: no cover - fallback for offline/static analysis
    get_env_config = None

logger = logging.getLogger(__name__)

# System6 configuration constants
MIN_PRICE = 5.0  # 最低価格フィルター（ドル）
MIN_DOLLAR_VOLUME_50 = 10_000_000  # 最低ドルボリューム50日平均（ドル）
HV50_BOUNDS_PERCENT = (10.0, 40.0)
HV50_BOUNDS_FRACTION = (0.10, 0.40)

# Shared metrics collector to avoid file handle leaks
_metrics = MetricsCollector()

SYSTEM6_BASE_COLUMNS = ["Open", "High", "Low", "Close", "Volume"]
SYSTEM6_FEATURE_COLUMNS = [
    "atr10",
    "dollarvolume50",
    "return_6d",
    "UpTwoDays",
    "filter",
    "setup",
    "hv50",
]
SYSTEM6_ALL_COLUMNS = SYSTEM6_BASE_COLUMNS + SYSTEM6_FEATURE_COLUMNS
SYSTEM6_NUMERIC_COLUMNS = ["atr10", "dollarvolume50", "return_6d", "hv50"]

# System6 Setup Constants
RETURN_6D_THRESHOLD = 0.20  # 6-day return threshold for setup


# ============================================================================
# System6 Helper Functions
# ============================================================================


def _apply_filter_conditions(df: pd.DataFrame) -> pd.DataFrame:
    """Apply System6 filter conditions, preserving existing 'filter' column if present.

    Args:
        df: DataFrame with required indicators (Low, dollarvolume50, hv50)

    Returns:
        DataFrame with 'filter' column added/updated
    """
    result = df.copy()

    low = pd.to_numeric(result["Low"], errors="coerce")
    dvol50 = pd.to_numeric(result["dollarvolume50"], errors="coerce")
    hv50 = pd.to_numeric(result["hv50"], errors="coerce")

    hv50_percent = hv50.between(*HV50_BOUNDS_PERCENT)
    hv50_fraction = hv50.between(*HV50_BOUNDS_FRACTION)
    hv50_condition = (hv50_percent | hv50_fraction).fillna(False)

    computed_filter = (
        (low >= MIN_PRICE) & (dvol50 > MIN_DOLLAR_VOLUME_50) & hv50_condition
    ).fillna(False)

    if "filter" in result.columns:
        existing = (
            pd.Series(result["filter"], index=result.index).fillna(False).astype(bool)
        )
        computed_filter = computed_filter & existing

    result["filter"] = computed_filter.astype(bool)

    return result


def _apply_setup_conditions(df: pd.DataFrame) -> pd.DataFrame:
    """Apply System6 setup conditions, preserving existing 'setup' column if present.

    Args:
        df: DataFrame with 'filter', 'return_6d', and 'UpTwoDays' columns

    Returns:
        DataFrame with 'setup' column added/updated
    """
    result = df.copy()

    return6 = pd.to_numeric(result["return_6d"], errors="coerce")
    uptwo = (
        pd.Series(result["UpTwoDays"], index=result.index).fillna(False).astype(bool)
    )

    computed_setup = (
        result["filter"].astype(bool) & (return6 > RETURN_6D_THRESHOLD) & uptwo
    ).fillna(False)

    if "setup" in result.columns:
        existing = (
            pd.Series(result["setup"], index=result.index).fillna(False).astype(bool)
        )
        computed_setup = computed_setup & existing

    result["setup"] = computed_setup.astype(bool)

    return result


def _compute_indicators_from_frame(df: pd.DataFrame) -> pd.DataFrame:
    """System6 個別銘柄用の前処理 + 指標利用.

    ポイント:
    1. まずインデックス（日付）を正規化してから列操作
    2. OHLCV を大文字統一
    3. 事前計算済み指標はラベルアラインでそのまま利用（.values 不使用）
    4. 欠損時のみフォールバック計算
    """
    if df is None or df.empty:
        raise ValueError("empty_frame")

    # --- 日付インデックス正規化 ---
    if "Date" in df.columns:
        idx = pd.to_datetime(df["Date"], errors="coerce").dt.normalize()
    elif "date" in df.columns:
        idx = pd.to_datetime(df["date"], errors="coerce").dt.normalize()
    else:
        idx = pd.to_datetime(df.index, errors="coerce").normalize()
    x = df.copy(deep=False)
    x.index = pd.Index(idx, name="Date")
    # 無効日付除去
    x = x[~x.index.isna()]
    if x.empty:
        raise ValueError("invalid date index")
    # 重複除去（最新優先）
    if getattr(x.index, "has_duplicates", False):
        x = x[~x.index.duplicated(keep="last")]
    # ソート
    try:
        x = x.sort_index()
    except Exception:
        pass

    # --- OHLCV リネーム（小文字→大文字） ---
    rename_map: dict[str, str] = {}
    for low, up in (
        ("open", "Open"),
        ("high", "High"),
        ("low", "Low"),
        ("close", "Close"),
        ("volume", "Volume"),
    ):
        if low in x.columns and up not in x.columns:
            rename_map[low] = up
    if rename_map:
        try:
            x = x.rename(columns=rename_map)
        except Exception:
            pass

    # 必須列確認
    missing = [c for c in SYSTEM6_BASE_COLUMNS if c not in x.columns]
    if missing:
        raise ValueError(f"missing columns: {missing}")

    # 行数チェック（最低 50 行）
    if len(x) < 50:
        raise ValueError("insufficient rows")

    # --- 指標列追加（ラベルアライン） ---
    try:
        # ATR10
        if "ATR10" in x.columns:
            x["atr10"] = x["ATR10"]
        elif "atr10" in x.columns:
            # 既に小文字形がある場合はそのまま利用
            pass
        else:
            _metrics.record_metric("system6_fallback_atr10", 1, "count")
            x["atr10"] = AverageTrueRange(
                x["High"], x["Low"], x["Close"], window=10
            ).average_true_range()

        # DollarVolume50
        if "DollarVolume50" in x.columns:
            x["dollarvolume50"] = x["DollarVolume50"]
        elif "dollarvolume50" in x.columns:
            pass
        else:
            _metrics.record_metric("system6_fallback_dollarvolume50", 1, "count")
            x["dollarvolume50"] = (x["Close"] * x["Volume"]).rolling(50).mean()

        # Return_6D
        if "Return_6D" in x.columns:
            x["return_6d"] = x["Return_6D"]
        elif "return_6d" in x.columns:
            pass
        else:
            _metrics.record_metric("system6_fallback_return_6d", 1, "count")
            x["return_6d"] = x["Close"].pct_change(6)

        # UpTwoDays
        if "UpTwoDays" in x.columns:
            x["UpTwoDays"] = x["UpTwoDays"]
        elif "uptwodays" in x.columns:
            x["UpTwoDays"] = x["uptwodays"]
        else:
            _metrics.record_metric("system6_fallback_uptwodays", 1, "count")
            x["UpTwoDays"] = (x["Close"] > x["Close"].shift(1)) & (
                x["Close"].shift(1) > x["Close"].shift(2)
            )

        # HV50 (historical volatility)
        hv50_series = None
        if "HV50" in x.columns:
            hv50_series = pd.to_numeric(x["HV50"], errors="coerce")
        elif "hv50" in x.columns:
            hv50_series = pd.to_numeric(x["hv50"], errors="coerce")
        if hv50_series is None:
            _metrics.record_metric("system6_fallback_hv50", 1, "count")
            returns = pd.Series(x["Close"], index=x.index).pct_change()
            hv50_series = returns.rolling(50).std() * (252**0.5) * 100
        x["hv50"] = hv50_series

        # Apply filter and setup conditions using helpers
        x = _apply_filter_conditions(x)
        x = _apply_setup_conditions(x)
    except Exception as exc:
        raise ValueError(f"calc_error: {type(exc).__name__}: {exc}") from exc

    # 数値指標の欠損除去
    x = x.dropna(subset=SYSTEM6_NUMERIC_COLUMNS)
    if x.empty:
        raise ValueError("insufficient rows")
    return x


def prepare_data_vectorized_system6(
    raw_data_dict: dict[str, pd.DataFrame] | None,
    *,
    progress_callback: Callable[[str], None] | None = None,
    log_callback: Callable[[str], None] | None = None,
    skip_callback: Callable[[str, str], None] | None = None,
    batch_size: int | None = None,
    use_process_pool: bool = False,
    max_workers: int | None = None,
    **kwargs: Any,
) -> dict[str, pd.DataFrame]:
    """System6 data preparation using standard batch processing pattern"""

    if not raw_data_dict:
        if log_callback:
            log_callback("System6: No raw data provided, returning empty dict")
        return {}

    target_symbols = list(raw_data_dict.keys())

    if log_callback:
        log_callback(f"System6: Starting processing for {len(target_symbols)} symbols")

    # Create a closure to pass raw_data_dict to the compute function
    def _compute_indicators_with_data(symbol: str) -> tuple[str, pd.DataFrame | None]:
        """Indicator calculation function that uses provided raw data"""
        df = raw_data_dict.get(symbol)
        if df is None or df.empty:
            return symbol, None

        try:
            prepared = _compute_indicators_from_frame(df)
            return symbol, prepared
        except Exception:
            return symbol, None

    # Execute batch processing using standard pattern
    results, error_symbols = process_symbols_batch(
        target_symbols,
        _compute_indicators_with_data,
        batch_size=batch_size,
        use_process_pool=use_process_pool,
        max_workers=max_workers,
        progress_callback=progress_callback,
        log_callback=log_callback,
        skip_callback=skip_callback,
        system_name="System6",
    )

    # Validate setup column vs predicate equivalence
    validate_predicate_equivalence(results, "System6", log_fn=log_callback)

    return cast(dict[str, pd.DataFrame], results)


def generate_candidates_system6(
    prepared_dict: dict[str, pd.DataFrame],
    *,
    top_n: int = 10,
    progress_callback: Callable[[str], None] | None = None,
    log_callback: Callable[[str], None] | None = None,
    skip_callback: Callable[[str, str], None] | None = None,
    batch_size: int | None = None,
    latest_only: bool = False,
    latest_mode_date: pd.Timestamp | None = None,
    include_diagnostics: bool = False,
    **kwargs: Any,
) -> (
    tuple[dict[pd.Timestamp, dict[str, dict[str, Any]]], pd.DataFrame | None]
    | tuple[
        dict[pd.Timestamp, dict[str, dict[str, Any]]],
        pd.DataFrame | None,
        dict[str, Any],
    ]
):
    """Generate System6 candidates.

    Added fast-path (latest_only=True): O(symbols) processing using only the last row
    of each DataFrame. Returns normalized mapping {date: {symbol: payload}}.
    """
    # diagnostics payload (opt-in)
    diagnostics: dict[str, Any] = {
        "ranking_source": None,  # str | None
        "setup_predicate_count": 0,  # int
        "ranked_top_n_count": 0,  # int
        "predicate_only_pass_count": 0,  # int
        "mismatch_flag": 0,  # int flag
    }

    # Option-B finalize helper toggle (env or kwargs)
    try:
        # kwargs 優先（明示指定）
        if isinstance(kwargs.get("use_option_b_utils"), bool):
            use_option_b_utils = bool(kwargs.get("use_option_b_utils"))
        else:
            # 環境フラグで段階導入
            from config.environment import get_env_config as _get_env

            use_option_b_utils = bool(
                getattr(_get_env(), "enable_option_b_system6", False)
            )
    except Exception:
        use_option_b_utils = False

    # --- 自動 latest_only 切替 -------------------------------------------------
    # 目的: 当日シグナル用途 (バックテスト以外) では高速パスを強制し、
    #       System6 の全日付フルスキャンによる遅延を避ける。
    # 条件:
    #   - 呼び出しで latest_only=False でも、環境変数 system6_force_latest_only が True
    #   - env.full_scan_today が False （明示 full 走査要求がない）
    #   - include_diagnostics は影響なし（fast path も診断返却対応済み）
    try:  # 環境依存のため失敗しても安全に継続
        # 遅延インポートで初期化コスト最小化
        from config.environment import get_env_config

        env = get_env_config()
        # PyTest 実行中はテストの明示指定（latest_only=False など）を尊重して強制切替しない
        import os as _os  # ローカルインポートで名前衝突回避

        running_pytest = bool(_os.environ.get("PYTEST_CURRENT_TEST"))

        if (
            not latest_only
            and getattr(env, "system6_force_latest_only", False)
            and not getattr(env, "full_scan_today", False)
            and not running_pytest
        ):
            latest_only = True  # 強制切替
            if logger:
                logger.info(
                    "System6: forcing latest_only "
                    "(system6_force_latest_only=1, full_scan_today=0)"
                )
                if log_callback:
                    try:
                        log_callback(
                            "System6: forcing latest_only "
                            "(system6_force_latest_only=1, full_scan_today=0)"
                        )
                    except Exception:
                        pass
                try:  # メトリクス環境が無い状況でも安全に続行
                    _metrics.record_metric(
                        "system6_forced_latest_only",
                        1,
                        "count",
                        stage="system6",
                    )
                except Exception:  # noqa: BLE001 - ログ最適化目的で握りつぶし
                    pass
    except Exception:
        pass

    candidates_by_date: dict[pd.Timestamp, list] = {}

    # === Fast Path: latest_only ===
    if latest_only:
        try:
            rows: list[dict[str, Any]] = []
            date_counter: dict[pd.Timestamp, int] = {}
            # 正規化した基準日（指定があればそれを優先）
            target_dt: pd.Timestamp | None = None
            if latest_mode_date is not None:
                try:
                    target_dt = pd.Timestamp(latest_mode_date).normalize()
                except Exception:
                    target_dt = None
            try:
                from common.system_setup_predicates import (
                    system6_setup_predicate as _s6_pred,
                )
            except Exception:
                _s6_pred = None

            for sym, df in prepared_dict.items():
                if df is None or df.empty:
                    continue
                if "return_6d" not in df.columns:
                    continue
                # 対象日を選択（指定があればその日、なければ最終行）
                if target_dt is not None:
                    # インデックスは normalize 済み前提
                    try:
                        if target_dt in df.index:
                            last_row = df.loc[target_dt]
                            # loc で Series 以外が来たら最終要素へ
                            if hasattr(last_row, "iloc") and (
                                getattr(last_row, "ndim", 1) > 1
                            ):
                                last_row = last_row.iloc[-1]
                            dt = target_dt
                        else:
                            # 対象日のデータが無ければスキップ
                            continue
                    except Exception:
                        continue
                else:
                    last_row = df.iloc[-1]
                    try:
                        # 安全にスカラー日時へ変換（型検査対策で文字列経由）
                        idx_last = df.index[-1]
                        parsed = pd.to_datetime(str(idx_last), errors="coerce")
                        if pd.isna(parsed):
                            continue
                        dt = pd.Timestamp(parsed).normalize()
                    except Exception:
                        continue

                # Ensure last_row is a Series (not DataFrame)
                if not isinstance(last_row, pd.Series):
                    if isinstance(last_row, pd.DataFrame) and not last_row.empty:
                        last_row = last_row.iloc[-1]
                    else:
                        continue

                setup_ok = False
                setup_from_column = False
                setup_value_available = False
                setup_source = ""
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
                fallback_pass = False
                if _s6_pred is not None:
                    try:
                        predicate_pass = bool(_s6_pred(last_row))
                        predicate_evaluated = True
                    except Exception:
                        predicate_pass = False

                if not predicate_evaluated and not setup_value_available:
                    # Predicate fallback when precomputed setup is unavailable
                    try:
                        ret_6d_val = last_row.get("return_6d")
                        if ret_6d_val is not None:
                            ret_6d_float = float(ret_6d_val)
                            uptwo = bool(
                                last_row.get("uptwodays") or last_row.get("UpTwoDays")
                            )
                            predicate_pass = (
                                ret_6d_float > RETURN_6D_THRESHOLD
                            ) and uptwo
                            predicate_evaluated = True
                            fallback_pass = bool(predicate_pass)
                    except Exception:
                        predicate_pass = False
                        predicate_evaluated = False

                if setup_from_column:
                    diagnostics["setup_predicate_count"] = (
                        int(diagnostics.get("setup_predicate_count", 0)) + 1
                    )
                    setup_ok = True
                    setup_source = "column"
                    if predicate_evaluated and not predicate_pass:
                        diagnostics["mismatch_flag"] = 1
                elif predicate_pass:
                    diagnostics["predicate_only_pass_count"] = (
                        int(diagnostics.get("predicate_only_pass_count", 0)) + 1
                    )
                    diagnostics["mismatch_flag"] = 1
                    setup_ok = True
                    setup_source = "fallback" if fallback_pass else "predicate"

                if not setup_ok:
                    continue

                # 必要指標取得 (存在しない場合はスキップ)
                # return_6d はスカラーに正規化してから float へ
                try:
                    val = last_row[
                        "return_6d"
                    ]  # 型: Any（Series ではなくスカラー想定）
                except Exception:
                    continue
                # to_numeric で Series になる可能性を排除するため 1 要素 Series 経由で取得
                try:
                    _tmp = pd.Series([val], dtype="object")
                    coerced = pd.to_numeric(_tmp, errors="coerce").iloc[0]
                except Exception:
                    continue
                # float へ強制変換
                try:
                    return_6d = float(coerced)
                except Exception:
                    continue
                if math.isnan(return_6d):
                    continue
                atr10 = last_row.get("atr10", None)
                date_counter[dt] = date_counter.get(dt, 0) + 1
                entry_price = last_row.get("Close") if "Close" in df else None
                rows.append(
                    {
                        "symbol": sym,
                        "date": dt,
                        "return_6d": return_6d,
                        "atr10": atr10,
                        "entry_price": entry_price,
                        "_setup_via": setup_source,
                        "_predicate_pass": bool(predicate_pass),
                        "_fallback_pass": bool(fallback_pass),
                    }
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
                                s_ret = s_last.get("return_6d", float("nan"))
                                try:
                                    s_ret_f = float(s_ret)
                                except Exception:
                                    s_ret_f = float("nan")
                                samples.append(
                                    (
                                        f"{s_sym}: date={s_dt.date()} setup={s_setup} "
                                        f"return_6d={s_ret_f:.4f}"
                                    )
                                )
                                taken += 1
                                if taken >= 2:
                                    break
                            except Exception:
                                continue
                        if samples:
                            try:
                                debug_msg = (
                                    "System6: DEBUG latest_only 0 candidates. "
                                    + " | ".join(samples)
                                )
                                log_callback(debug_msg)
                            except Exception:
                                pass
                    except Exception:
                        pass
                # 0件でも latest_only 起因であることを診断に反映
                diagnostics["ranking_source"] = "latest_only"
                # Option-B: 0件でも extras を診断に載せる（UI観測向上）
                if use_option_b_utils:
                    try:
                        extras = {
                            "system6_total_candidates": 0,
                            "system6_unique_entry_dates": 0,
                            "system6_processed_symbols_candidates": len(prepared_dict),
                        }
                        finalize_ranking_and_diagnostics(
                            diagnostics,
                            ranked_df=None,
                            ranking_source="latest_only",
                            extras=extras,
                        )
                        # 構造化メトリクスログ（UIで視認しやすくする）
                        if log_callback:
                            import json as _json

                            payload = {
                                "msg": "[System6] latest_only metrics",
                                "system": "system6",
                                "mode": "latest_only",
                                "candidates": 0,
                                "unique_dates": 0,
                                "processed_symbols": len(prepared_dict),
                            }
                            try:
                                log_callback(_json.dumps(payload, ensure_ascii=False))
                            except Exception:
                                pass
                    except Exception:
                        pass
                # 一貫性のため: Option-B 有無に関わらず既存の診断再計算ヘルパを呼び、
                # ranked_top_n_count などの標準キーを確実に埋める
                try:
                    set_diagnostics_after_ranking(
                        diagnostics, final_df=None, ranking_source="latest_only"
                    )
                except Exception:
                    diagnostics["ranking_source"] = "latest_only"
                # ゼロ候補時は一意シンボル数も 0 で安定化
                diagnostics["setup_unique_symbols"] = 0
                return ({}, None, diagnostics) if include_diagnostics else ({}, None)
            df_all = pd.DataFrame(rows)
            # 指定があればその日で揃え、無ければ最頻日で揃える（欠落シンボル耐性）
            if target_dt is not None:
                df_all = df_all[df_all["date"] == target_dt]
            else:
                mode_date = choose_mode_date_for_latest_only(date_counter)
                if mode_date is not None:
                    df_all = df_all[df_all["date"] == mode_date]
            df_all = df_all.sort_values("return_6d", ascending=False, kind="stable")
            df_all = df_all.head(int(top_n)) if top_n else df_all
            # rank 付与（従来互換）
            total = len(df_all)
            df_all.loc[:, "rank"] = list(range(1, total + 1))
            df_all.loc[:, "rank_total"] = total

            if "_setup_via" in df_all.columns:
                via_series = df_all["_setup_via"].fillna("").astype(str)
                diagnostics["setup_predicate_count"] = int((via_series != "").sum())

                if "_predicate_pass" in df_all.columns:
                    predicate_series = (
                        df_all["_predicate_pass"].fillna(False).astype(bool)
                    )
                else:
                    predicate_series = pd.Series(False, index=df_all.index)

                if "_fallback_pass" in df_all.columns:
                    fallback_series = (
                        df_all["_fallback_pass"].fillna(False).astype(bool)
                    )
                else:
                    fallback_series = pd.Series(False, index=df_all.index)

                predicate_only_mask = (via_series != "column") & (
                    predicate_series | fallback_series
                )
                diagnostics["predicate_only_pass_count"] = int(
                    predicate_only_mask.sum()
                )
            else:
                diagnostics["setup_predicate_count"] = len(df_all)
                diagnostics["predicate_only_pass_count"] = 0

            try:
                diagnostics["setup_unique_symbols"] = int(df_all["symbol"].nunique())
            except Exception:
                diagnostics["setup_unique_symbols"] = len(df_all)

            meta_cols = ["_setup_via", "_predicate_pass", "_fallback_pass"]
            df_public = df_all.drop(
                columns=[c for c in meta_cols if c in df_all.columns]
            )

            normalized = normalize_dataframe_to_by_date(df_public)

            if log_callback:
                try:
                    log_callback(
                        "System6: latest_only fast-path -> "
                        f"{len(df_public)} candidates (symbols={len(rows)})"
                    )
                except Exception:
                    pass
            if use_option_b_utils:
                try:
                    extras = {
                        "system6_total_candidates": len(df_public),
                        "system6_unique_entry_dates": len(normalized),
                        "system6_processed_symbols_candidates": len(prepared_dict),
                    }
                    finalize_ranking_and_diagnostics(
                        diagnostics,
                        ranked_df=df_public,
                        ranking_source="latest_only",
                        extras=extras,
                    )
                    # 構造化メトリクスログを併せて出力
                    if log_callback:
                        import json as _json

                        payload = {
                            "msg": "[System6] latest_only metrics",
                            "system": "system6",
                            "mode": "latest_only",
                            "candidates": len(df_public),
                            "unique_dates": len(normalized),
                            "processed_symbols": len(prepared_dict),
                        }
                        try:
                            log_callback(_json.dumps(payload, ensure_ascii=False))
                        except Exception:
                            pass
                except Exception:
                    # フォールバック: 既存処理
                    set_diagnostics_after_ranking(
                        diagnostics, final_df=df_public, ranking_source="latest_only"
                    )
            else:
                set_diagnostics_after_ranking(
                    diagnostics, final_df=df_public, ranking_source="latest_only"
                )
            # Fast-path でも最終メトリクスを記録する（テスト要件: 指標が存在すること）
            try:
                _metrics.record_metric(
                    "system6_total_candidates", len(df_public), "count"
                )
                # 正規化後のエントリ日数（通常は 1 日分）
                unique_dates = len(normalized)
                _metrics.record_metric(
                    "system6_unique_entry_dates", unique_dates, "count"
                )
                # 処理したシンボル数（高速パスでは prepared_dict の要素数ベースで十分）
                _metrics.record_metric(
                    "system6_processed_symbols_candidates",
                    len(prepared_dict),
                    "count",
                )
            except Exception:
                # メトリクス環境無しでも続行
                pass
            # 完了メッセージは例外を握りつぶさず呼び出し側へ伝播（テスト要件）
            if log_callback:
                completion_msg = (
                    "📊 System6 候補生成完了: "
                    f"{len(df_public)}件の候補 ("
                    f"{len(normalized)}日分, {len(prepared_dict)}シンボル処理)"
                )
                log_callback(completion_msg)
            if include_diagnostics:
                # latest_only の高速パスでは DataFrame を第2戻り値に返す（System6 仕様）
                return (normalized, df_public.copy(), diagnostics)
            else:
                # latest_only の高速パスでは DataFrame を第2戻り値に返す（System6 仕様）
                return (normalized, df_public.copy())
        except Exception as e:
            if log_callback:
                try:
                    log_callback(f"System6: fast-path failed -> fallback ({e})")
                except Exception:
                    pass
            # fall through to full path
    total = len(prepared_dict)

    # 追加最適化: COMPACT ログや高速化モード時に filter/setup 集計を抑制するフラグ
    collect_counts = True
    try:
        from config.environment import get_env_config

        env2 = get_env_config()
        # compact_logs かつ latest_only 強制無効化されていない → 集計省略許容
        if getattr(env2, "compact_logs", False):
            # 明示的にフル走査要求がある場合は保持
            if not getattr(env2, "full_scan_today", False):
                collect_counts = False
    except Exception:
        pass

    if batch_size is None:
        try:
            from config.settings import get_settings

            batch_size = get_settings(create_dirs=False).data.batch_size
        except Exception:
            batch_size = 100
        # System6では非常に大きなバッチサイズで高速処理（候補抽出は軽い処理）
        batch_size = max(batch_size, 2000)  # 最小2000に設定
        batch_size = resolve_batch_size(total, batch_size)
    start_time = time.time()
    batch_start = time.time()
    processed, skipped = 0, 0
    skipped_missing_cols = 0
    filter_passed = 0  # フィルター条件通過数
    setup_passed = 0  # セットアップ条件通過数
    buffer: list[str] = []

    # 処理開始のログを追加
    if log_callback:
        log_callback(
            f"📊 System6 候補抽出開始: {total}銘柄を処理中... (バッチサイズ: {batch_size})"
        )

    for sym, df in prepared_dict.items():
        # featherキャッシュの健全性チェック
        if df is None or df.empty:
            skipped += 1
            continue
        missing_cols = [c for c in SYSTEM6_ALL_COLUMNS if c not in df.columns]
        if missing_cols:
            skipped += 1
            skipped_missing_cols += 1
            continue
        if df[SYSTEM6_NUMERIC_COLUMNS].isnull().any().any():
            # NaN警告は個別に出力せず、統計のみ記録
            pass

        # last_price（直近終値）を取得
        last_price = None
        if "Close" in df.columns and not df["Close"].empty:
            last_price = df["Close"].iloc[-1]

        # 統計計算：フィルター通過数とセットアップ通過数をカウント（累積日数）
        if collect_counts:
            if "filter" in df.columns:
                try:
                    filter_passed += int(df["filter"].sum())
                except Exception:
                    pass
            if "setup" in df.columns:
                try:
                    setup_passed += int(df["setup"].sum())
                except Exception:
                    pass

        try:
            # まず最終行付近の軽量チェックで高速脱出（全期間 any() の前）
            setup_col = df.get("setup")
            if setup_col is None:
                skipped += 1
                continue
            # 末尾 8 行程度で True がなければ全体 any() を評価、それでも無ければ早期スキップ
            tail_window = setup_col.tail(min(8, len(setup_col)))
            if not tail_window.any():
                if not setup_col.any():  # 本当に 1 度も True なし
                    skipped += 1
                    continue
            # ここまで来たら従来どおり全 True 行抽出
            setup_days = df[df["setup"] == 1]
            if setup_days.empty:
                skipped += 1
                continue
            for date, row in setup_days.iterrows():
                # 日付変換を簡略化（営業日補正なしで高速化）
                if isinstance(date, pd.Timestamp):
                    entry_date = date
                else:
                    # 安全な型のみ受け付ける（文字列 / 日付 / 数値インデックス想定）
                    if isinstance(date, (str, int, float)) or hasattr(date, "__str__"):
                        try:
                            maybe_date = pd.to_datetime(str(date), errors="coerce")
                            if pd.isna(maybe_date):
                                continue
                            entry_date = pd.Timestamp(maybe_date).normalize()
                        except Exception:
                            continue
                    else:
                        continue

                rec = {
                    "symbol": sym,
                    "entry_date": entry_date,
                    "entry_price": last_price,
                    "return_6d": row["return_6d"],
                    "atr10": row["atr10"],
                }
                candidates_by_date.setdefault(entry_date, []).append(rec)
                try:
                    if bool(row.get("setup", False)):
                        diagnostics["setup_predicate_count"] += 1
                except Exception:
                    pass
        except Exception:
            skipped += 1

        processed += 1
        buffer.append(sym)
        if progress_callback:
            try:
                progress_callback(f"{processed}/{total}")
            except Exception:
                pass
        effective_batch_size = batch_size if batch_size is not None else 100
        if (
            processed % effective_batch_size == 0 or processed == total
        ) and log_callback:
            elapsed = time.time() - start_time
            remain = (elapsed / processed) * (total - processed) if processed else 0
            em, es = divmod(int(elapsed), 60)
            rm, rs = divmod(int(remain), 60)

            # System6の詳細統計を計算
            total_candidates = sum(len(cands) for cands in candidates_by_date.values())

            msg = tr(
                "📊 System6 進捗: {done}/{total} | "
                "フィルター通過: {filter_passed}日 | セットアップ通過: {setup_passed}日 | "
                "候補: {candidates}件\n"
                "⏱️ 経過: {em}m{es}s | 残り: ~{rm}m{rs}s | "
                "スキップ: {skipped}銘柄 (列不足: {missing_cols}銘柄)",
                done=processed,
                total=total,
                filter_passed=filter_passed,
                setup_passed=setup_passed,
                candidates=total_candidates,
                em=em,
                es=es,
                rm=rm,
                rs=rs,
                skipped=skipped,
                missing_cols=skipped_missing_cols,
            )
            if buffer:
                sample = ", ".join(buffer[:10])
                more = len(buffer) - len(buffer[:10])
                if more > 0:
                    sample = f"{sample}, ...(+{more} more)"
                msg += "\n" + tr("🔍 処理中銘柄: {names}", names=sample)
            try:
                log_callback(msg)
            except Exception:
                pass

            # バッチ性能記録
            batch_duration = time.time() - batch_start
            if batch_duration > 0:
                symbols_per_second = len(buffer) / batch_duration
                _metrics.record_metric(
                    "system6_candidates_batch_duration", batch_duration, "seconds"
                )
                _metrics.record_metric(
                    "system6_candidates_symbols_per_second",
                    symbols_per_second,
                    "rate",
                )

            batch_start = time.time()
            buffer.clear()

    limit_n = int(top_n)
    for date in list(candidates_by_date.keys()):
        rows = candidates_by_date.get(date, [])
        if not rows:
            candidates_by_date[date] = []
            continue
        df = pd.DataFrame(rows)
        if df.empty:
            candidates_by_date[date] = []
            continue
        df = df.sort_values("return_6d", ascending=False)
        total = len(df)
        df.loc[:, "rank"] = list(range(1, total + 1))
        df.loc[:, "rank_total"] = total
        limited = df.head(limit_n)
        candidates_by_date[date] = limited.to_dict("records")

    # 候補抽出の集計サマリーはログにのみ出力
    if skipped > 0 and log_callback:
        summary_lines = [f"⚠️ 候補抽出中にスキップ: {skipped} 件"]
        if skipped_missing_cols:
            summary_lines.append(f"  └─ 必須列欠落: {skipped_missing_cols} 件")
        try:
            for line in summary_lines:
                log_callback(line)
        except Exception:
            pass

    # 最終メトリクス記録
    total_candidates = sum(
        len(candidates) for candidates in candidates_by_date.values()
    )
    unique_dates = len(candidates_by_date)
    _metrics.record_metric("system6_total_candidates", total_candidates, "count")
    _metrics.record_metric("system6_unique_entry_dates", unique_dates, "count")
    _metrics.record_metric("system6_processed_symbols_candidates", processed, "count")

    if log_callback:
        try:
            log_callback(
                "📊 System6 候補生成完了: "
                f"{total_candidates}件の候補 ({unique_dates}日分, {processed}シンボル処理)"
            )
        except Exception:
            pass

    # Normalize list structure to dict-of-dicts for consistency
    normalized_full: dict[pd.Timestamp, dict[str, dict[str, Any]]] = {}
    for dt, recs in candidates_by_date.items():
        symbol_dict: dict[str, dict[str, Any]] = {}
        for rec in recs:
            sym_val = rec.get("symbol") if isinstance(rec, dict) else None
            if not isinstance(sym_val, str) or not sym_val:
                continue
            # rec may contain entry_date; unify key name 'date' for DF compatibility
            payload = {
                str(k): v for k, v in rec.items() if k not in ("symbol", "entry_date")
            }
            # 保持: 元々 'entry_date' をキー化しているのでそのまま payload にも残す
            payload["entry_date"] = rec.get("entry_date")
            symbol_dict[sym_val] = payload
        normalized_full[pd.Timestamp(dt)] = symbol_dict
    # diagnostics for full path
    last_dt = None
    try:
        last_dt = max(normalized_full.keys()) if normalized_full else None
    except Exception:
        last_dt = None

    if use_option_b_utils:
        try:
            # last_dt のレコードを DataFrame 化して finalize に渡す（件数を自動集計）
            ranked_df_last = None
            if last_dt is not None:
                try:
                    items = normalized_full.get(last_dt, {}) or {}
                    ranked_df_last = pd.DataFrame(
                        ({"symbol": k, **(v or {})} for k, v in items.items())
                    )
                except Exception:
                    ranked_df_last = None
            extras = {
                "system6_total_candidates": total_candidates,
                "system6_unique_entry_dates": unique_dates,
                "system6_processed_symbols_candidates": processed,
            }
            finalize_ranking_and_diagnostics(
                diagnostics,
                ranked_df=ranked_df_last,
                ranking_source=diagnostics.get("ranking_source") or "full_scan",
                extras=extras,
            )
            # 構造化メトリクスログ
            if log_callback:
                import json as _json

                payload = {
                    "msg": "[System6] full_scan metrics",
                    "system": "system6",
                    "mode": "full_scan",
                    "candidates": int(total_candidates),
                    "unique_dates": int(unique_dates),
                    "processed_symbols": int(processed),
                }
                try:
                    log_callback(_json.dumps(payload, ensure_ascii=False))
                except Exception:
                    pass
        except Exception:
            # フォールバック: 既存ダイアグ
            set_diagnostics_after_ranking(
                diagnostics,
                final_df=None,
                ranking_source=diagnostics.get("ranking_source") or "full_scan",
            )
            try:
                if last_dt is not None:
                    diagnostics["ranked_top_n_count"] = len(
                        normalized_full.get(last_dt, {})
                    )
                else:
                    diagnostics["ranked_top_n_count"] = 0
            except Exception:
                diagnostics["ranked_top_n_count"] = 0
    else:
        set_diagnostics_after_ranking(
            diagnostics,
            final_df=None,
            ranking_source=diagnostics.get("ranking_source") or "full_scan",
        )
        # System6 full path custom: use normalized_full dict size for ranked count
        try:
            if last_dt is not None:
                diagnostics["ranked_top_n_count"] = len(
                    normalized_full.get(last_dt, {})
                )
            else:
                diagnostics["ranked_top_n_count"] = 0
        except Exception:
            diagnostics["ranked_top_n_count"] = 0

    if include_diagnostics:
        return (normalized_full, None, diagnostics)
    else:
        return (normalized_full, None)


def get_total_days_system6(data_dict: dict[str, pd.DataFrame]) -> int:
    all_dates = set()
    for df in data_dict.values():
        if df is None or df.empty:
            continue
        if "Date" in df.columns:
            dates = pd.to_datetime(df["Date"]).dt.normalize()
        elif "date" in df.columns:
            dates = pd.to_datetime(df["date"]).dt.normalize()
        else:
            dates = pd.to_datetime(df.index).normalize()
        all_dates.update(dates)
    return len(all_dates)


__all__ = [
    "prepare_data_vectorized_system6",
    "generate_candidates_system6",
    "get_total_days_system6",
]
