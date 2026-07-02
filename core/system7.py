# ============================================================================
# 🧠 Context Note
# このファイルは System7（SPY ショート カタストロフィー・ヘッジ）のロジック専門
#
# ⚠️ CRITICAL: System7 は SPY 固定のヘッジ専用。ロジック変更・他銘柄割当は禁止
#
# 前提条件：
#   - SPY のみを対象（他銘柄への適用は禁止）
#   - ポートフォリオ全体のダウンサイドヘッジ（トレード資本の 20%）
#   - マーケット暴落時の損失軽減目的
#   - 指標は precomputed のみ使用
#
# ロジック単位：
#   prepare_data_vectorized_system7() → SPY データ準備
#   generate_candidates_system7()     → 空売りシグナル生成（キャッシュ経由）
#
# Copilot へ：
#   → SPY 以外の銘柄割当提案は絶対受け入れるな
#   → system7.py のロジック変更は、必ず Core Team で事前合意必須
#   → ヘッジ目的を忘れずに。収益最大化ではなく損失軽減が目的
# ============================================================================

"""System7 core logic (SPY short catastrophe hedge)。

System7 は SPY 専用のため、prepare_data/generate_candidates のみ共通化。
run_backtest は strategy 側にカスタム実装が残る。
"""

import logging
import os
from typing import Any, Callable, Tuple

import pandas as pd

from common.system_candidates_utils import set_diagnostics_after_ranking
from common.system_setup_predicates import validate_predicate_equivalence
from common.utils_spy import resolve_signal_entry_date

# System7 configuration constants
MIN_ROWS_FOR_CACHE = 300  # Minimum rows required to enable caching
RECOMPUTE_CONTEXT_DAYS = 70  # Days of context needed for indicator recomputation
MIN_50_WINDOW = 50  # 50-day low window for setup detection
RECENT_WINDOW_SIZE = 50  # Window size for recent candidate counting

logger = logging.getLogger(__name__)


def prepare_data_vectorized_system7(
    raw_data_dict: dict[str, pd.DataFrame] | None,
    *,
    progress_callback: Callable[[int, int], None] | None = None,
    log_callback: Callable[[str], None] | None = None,
    skip_callback: Callable[[str], None] | None = None,
    reuse_indicators: bool = True,
    **kwargs: Any,
) -> dict[str, pd.DataFrame]:
    """Compute indicators for SPY and cache the result."""
    cache_dir = "data_cache/indicators_system7_cache"
    os.makedirs(cache_dir, exist_ok=True)
    prepared_dict: dict[str, pd.DataFrame] = {}
    raw_data_dict = raw_data_dict or {}
    try:
        df_raw = raw_data_dict.get("SPY")
        if df_raw is None:
            raise ValueError("SPY data missing")
        if "Date" in df_raw.columns:
            df = df_raw.copy()
            df.index = pd.Index(pd.to_datetime(df["Date"]).dt.normalize())
        else:
            df = df_raw.copy()
            df.index = pd.Index(pd.to_datetime(df.index).normalize())

        # Early exit: check required precomputed indicators exist (lowercase)
        if "atr50" not in df.columns:
            raise RuntimeError(
                "IMMEDIATE_STOP: System7 missing indicator atr50 for SPY. "
                "Daily signal execution must be stopped."
            )

        cache_path = os.path.join(cache_dir, "SPY.feather")
        use_cache = bool(reuse_indicators and len(df) >= MIN_ROWS_FOR_CACHE)
        cached: pd.DataFrame | None = None
        if use_cache and os.path.exists(cache_path):
            try:
                cached = pd.read_feather(cache_path)
                cached["Date"] = pd.to_datetime(cached["Date"]).dt.normalize()
                cached.set_index("Date", inplace=True)
            except Exception as e:
                logger.debug(f"System7: Failed to read cache for SPY: {e}")
                cached = None

        def _calc_indicators(src: pd.DataFrame) -> pd.DataFrame:
            """プリコンピューテッド指標版：ATR50計算除去、早期終了追加"""
            x = src.copy()

            # Check if precomputed ATR50 exists
            if "atr50" not in x.columns:
                raise RuntimeError(
                    "IMMEDIATE_STOP: System7 missing indicator atr50. "
                    "Daily signal execution must be stopped."
                )

            # Use precomputed ATR50 (lowercase) and create uppercase version
            # for consistency
            x["ATR50"] = x["atr50"]

            # Use precomputed indicators for min_50 and max_70
            if "Min_50" in x.columns:
                x["min_50"] = x["Min_50"]
            elif "min_50" in x.columns:
                # Already exists, no action needed
                pass
            else:
                raise RuntimeError(
                    "IMMEDIATE_STOP: System7 missing indicator min_50. "
                    "Daily signal execution must be stopped."
                )

            if "Max_70" in x.columns:
                x["max_70"] = x["Max_70"]
            elif "max_70" in x.columns:
                # Already exists, no action needed
                pass
            else:
                raise RuntimeError(
                    "IMMEDIATE_STOP: System7 missing indicator max_70. "
                    "Daily signal execution must be stopped."
                )

            x["setup"] = x["Low"] <= x["min_50"]
            return x

        if use_cache and cached is not None and not cached.empty:
            last_date = cached.index.max()
            new_rows = df[df.index > last_date]
            if new_rows.empty:
                result_df = cached
            else:
                context_start = last_date - pd.Timedelta(days=RECOMPUTE_CONTEXT_DAYS)
                recompute_src = df[df.index >= context_start]
                recomputed = _calc_indicators(recompute_src)
                recomputed = recomputed[recomputed.index > last_date]
                # 既存の max_70 を優先して結合
                result_df = pd.concat([cached, recomputed])
                if "max_70" in cached.columns and "max_70" in recomputed.columns:
                    # cached 側の値を優先（重複期間は cached を保持）
                    result_df.loc[cached.index, "max_70"] = cached["max_70"]
                try:
                    result_df.reset_index().to_feather(cache_path)
                except Exception as e:
                    logger.debug(f"System7: Failed to save cache for SPY: {e}")
                    pass
        else:
            result_df = _calc_indicators(df)
            try:
                if use_cache:
                    result_df.reset_index().to_feather(cache_path)
            except Exception as e:
                logger.debug(f"System7: Failed to save initial cache for SPY: {e}")
                pass
        # テスト互換: 返却範囲は入力 df のインデックスに厳密一致させる
        try:
            result_df = result_df.reindex(df.index)
        except Exception as e:
            logger.debug(f"System7: Failed to reindex result for SPY: {e}")
            pass
        prepared_dict["SPY"] = result_df
    except Exception as e:
        logger.debug(f"System7: Failed to prepare data for SPY: {e}")
        if skip_callback:
            try:
                skip_callback(f"SPY の処理をスキップしました: {e}")
            except Exception:
                pass

    if log_callback:
        try:
            log_callback(
                "SPY インジケーター計算完了(ATR50, min_50, max_70, setup: Low<=min_50)"
            )
        except Exception:
            pass
    if progress_callback:
        try:
            progress_callback(1, 1)
        except Exception:
            pass

    # Validate setup column vs predicate equivalence (SPY single symbol)
    validate_predicate_equivalence(prepared_dict, "System7", log_fn=log_callback)

    return prepared_dict


def generate_candidates_system7(
    prepared_dict: dict[str, pd.DataFrame],
    *,
    top_n: int | None = None,
    progress_callback: Callable[[int, int], None] | None = None,
    log_callback: Callable[[str], None] | None = None,
    batch_size: int | None = None,
    latest_only: bool = False,
    include_diagnostics: bool = False,
    **kwargs: Any,
) -> (
    Tuple[dict[pd.Timestamp, dict[str, dict[str, object]]], pd.DataFrame | None]
    | Tuple[
        dict[pd.Timestamp, dict[str, dict[str, object]]],
        pd.DataFrame | None,
        dict[str, Any],
    ]
):
    """Generate System7 candidates.

    Optimization notes:
    - Added fast-path (latest_only=True) using only the last row of SPY.
      This preserves trading logic because System7 entries always occur the
      NEXT trading day after a setup (50-day low break). Historical scanning
      is only required when backtesting; for same-day extraction we only need
      to know whether *today* is a setup day.
    - Normalized return structure to dict-of-dicts
        { entry_date: { symbol: {payload...} } }
      matching Systems1–6 for orchestration uniformity.
    """

    diagnostics = {
        "ranking_source": None,
        "setup_predicate_count": 0,
        "ranked_top_n_count": 0,
        "predicate_only_pass_count": 0,
        "mismatch_flag": 0,
    }

    if "SPY" not in prepared_dict:
        return ({}, None, diagnostics) if include_diagnostics else ({}, None)

    df = prepared_dict.get("SPY")
    if df is None or df.empty:
        return ({}, None, diagnostics) if include_diagnostics else ({}, None)

    # === Fast Path ===
    if latest_only:
        try:
            last_row = df.iloc[-1]

            # Use predicate-based evaluation (no setup column dependency)
            from common.system_setup_predicates import (
                system7_setup_predicate as _s7_pred,
            )

            setup_ok = False
            try:
                setup_ok = bool(_s7_pred(last_row))
            except Exception as e:
                logger.debug(f"System7: Setup predicate failed for SPY: {e}")
                setup_ok = False

            if setup_ok:
                diagnostics["setup_predicate_count"] = (
                    int(diagnostics.get("setup_predicate_count") or 0) + 1
                )

                setup_date = df.index[-1]
                entry_date = resolve_signal_entry_date(setup_date)
                if not pd.isna(entry_date):
                    # last_price（直近終値）:
                    # System7 の実発注は entry_date (翌営業日) の寄り付き成行。
                    # ここでの entry_price は候補 payload の表示/サイジング用 proxy として
                    # setup 日の終値を使う (シグナル生成時点で確定済みの既知値)。
                    # 実約定 Open は strategies/system7_strategy.py が entry_idx で使用。
                    # 未来データは参照しておらず look-ahead ではない。
                    entry_price = None
                    if "Close" in df.columns and not df["Close"].empty:
                        entry_price = df["Close"].iloc[-1]
                    atr_val = last_row.get("ATR50")
                    if atr_val is None:
                        atr_val = last_row.get("atr50")
                    rows = [
                        {
                            "symbol": "SPY",
                            "date": entry_date,
                            "ATR50": atr_val,
                            "entry_price": entry_price,
                        }
                    ]
                    df_fast = pd.DataFrame(rows)
                    # rank 付与（単一シンボル）
                    df_fast.loc[:, "rank"] = 1
                    df_fast.loc[:, "rank_total"] = 1
                    normalized: dict[pd.Timestamp, dict[str, dict[str, object]]] = {}
                    symbol_payload = {
                        k: v for k, v in rows[0].items() if k not in ("symbol", "date")
                    }
                    symbol_payload["entry_date"] = entry_date
                    normalized[pd.Timestamp(entry_date)] = {"SPY": symbol_payload}
                    if log_callback:
                        try:
                            log_callback(
                                "System7: latest_only fast-path -> 1 candidate"
                            )
                        except Exception as e:
                            logger.debug(f"System7: Log callback failed: {e}")
                            pass
                    set_diagnostics_after_ranking(
                        diagnostics, final_df=df_fast, ranking_source="latest_only"
                    )
                    if progress_callback:
                        try:
                            progress_callback(1, 1)
                        except Exception as e:
                            logger.debug(f"System7: Progress callback failed: {e}")
                            pass
                    return (
                        (normalized, df_fast, diagnostics)
                        if include_diagnostics
                        else (normalized, df_fast)
                    )
            # no setup today
            if log_callback:
                try:
                    # DEBUGサンプリング: SPY最終行の状態を出力
                    s_dt = pd.to_datetime(str(df.index[-1])).normalize()
                    s_setup = bool(last_row.get("setup", False))
                    s_close = last_row.get("Close", float("nan"))
                    try:
                        s_close_f = float(s_close)
                    except Exception:
                        s_close_f = float("nan")
                    log_callback(
                        (
                            "System7: DEBUG latest_only 0 candidates. "
                            f"SPY: date={s_dt.date()} setup={s_setup} "
                            f"close={s_close_f:.2f}"
                        )
                    )
                except Exception as e:
                    logger.debug(f"System7: Debug log failed: {e}")
                    pass
            if progress_callback:
                try:
                    progress_callback(1, 1)
                except Exception as e:
                    logger.debug(f"System7: Progress callback failed (no setup): {e}")
                    pass
            return ({}, None, diagnostics) if include_diagnostics else ({}, None)
        except Exception as e:  # fallback to full scan
            logger.debug(f"System7: Fast-path failed, falling back to full scan: {e}")
            if log_callback:
                try:
                    log_callback(f"System7: fast-path failed -> fallback ({e})")
                except Exception:
                    pass

    # === Full Historical Path (backtest or fallback) ===
    candidates_by_date: dict[pd.Timestamp, list] = {}
    limit_n: int | None
    if top_n is None:
        limit_n = None
    else:
        try:
            limit_n = max(0, int(top_n))
        except (TypeError, ValueError):
            limit_n = None
    try:
        setup_days = df[df["setup"]]
    except Exception as e:
        logger.debug(f"System7: Failed to filter setup days: {e}")
        setup_days = pd.DataFrame()

    for date, row in setup_days.iterrows():
        try:
            entry_date = resolve_signal_entry_date(pd.to_datetime(str(date)))
        except Exception as e:
            logger.debug(f"System7: Failed to resolve entry date for {date}: {e}")
            continue
        if pd.isna(entry_date):
            continue
        if limit_n == 0:
            continue
        # last_price（直近終値）
        last_price = None
        if "Close" in df.columns and not df["Close"].empty:
            last_price = df["Close"].iloc[-1]
        try:
            atr_val_full = row.get("ATR50") if hasattr(row, "get") else row["ATR50"]
        except Exception as e:
            logger.debug(f"System7: Failed to get ATR50 for {date}: {e}")
            atr_val_full = None
        rec = {
            "symbol": "SPY",
            "entry_date": entry_date,
            "ATR50": atr_val_full,
            "entry_price": last_price,
        }
        bucket = candidates_by_date.setdefault(entry_date, [])
        if limit_n is not None and len(bucket) >= limit_n:
            continue
        bucket.append(rec)

    if log_callback:
        try:
            all_dates = (
                pd.Index(pd.to_datetime(df.index).normalize()).unique().sort_values()
            )
            window_size = int(
                min(RECENT_WINDOW_SIZE, len(all_dates)) or RECENT_WINDOW_SIZE
            )
            if window_size > 0:
                recent_set = set(all_dates[-window_size:])
            else:
                recent_set = set()
            count_recent = sum(1 for d in candidates_by_date.keys() if d in recent_set)
            log_callback(
                f"候補日数: {count_recent} (直近({count_recent}/{window_size})日間, "
                f"{MIN_50_WINDOW}日安値由来の翌営業日数)"
            )
        except Exception as e:
            logger.debug(f"System7: Failed to compute recent window stats: {e}")
    if progress_callback:
        try:
            progress_callback(1, 1)
        except Exception as e:
            logger.debug(f"System7: Progress callback failed (full scan): {e}")
            pass

    # Normalize list structure to dict-of-dicts
    normalized_full: dict[pd.Timestamp, dict[str, dict[str, object]]] = {}
    for dt, recs in candidates_by_date.items():
        payload_map: dict[str, dict[str, object]] = {}
        for rec in recs:
            sym_val = rec.get("symbol") if isinstance(rec, dict) else None
            if sym_val != "SPY":
                continue
            payload = {k: v for k, v in rec.items() if k not in ("symbol",)}
            payload_map["SPY"] = payload
        normalized_full[pd.Timestamp(dt)] = payload_map
    # full scan: diagnostics を安定して更新
    set_diagnostics_after_ranking(
        diagnostics, final_df=None, ranking_source="full_scan"
    )
    # System7 full path custom: use normalized_full dict size for ranked count
    if normalized_full:
        try:
            last_dt = max(normalized_full.keys())
            diagnostics["ranked_top_n_count"] = len(normalized_full.get(last_dt, {}))
        except Exception as e:
            logger.debug(f"System7: Failed to compute ranked count: {e}")
            diagnostics["ranked_top_n_count"] = 0
    else:
        diagnostics["ranked_top_n_count"] = 0
    if include_diagnostics:
        return (normalized_full, None, diagnostics)
    else:
        return (normalized_full, None)


def get_total_days_system7(data_dict: dict[str, pd.DataFrame]) -> int:
    all_dates = set()
    for df in data_dict.values():
        if df is None or df.empty:
            continue
        if "Date" in df.columns:
            dates = pd.to_datetime(df["Date"]).dt.normalize()
        else:
            dates = pd.to_datetime(df.index).normalize()
        all_dates.update(dates)
    return len(all_dates)


__all__ = [
    "prepare_data_vectorized_system7",
    "generate_candidates_system7",
    "get_total_days_system7",
]
