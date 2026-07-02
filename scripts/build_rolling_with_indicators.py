"""Extract rolling window data with indicators from full backup cache.

このスクリプトは ``data_cache/full_backup`` に保存されたフル履歴データを
読み込み、ローリング用キャッシュ ``data_cache/rolling`` を 330 日分
（設定値に基づく）へ再構築します。出力時には各戦略で利用する主要
インジケーター（ATR/SMA/RSI/ADX など）を事前計算して保存します。

直接 CLI から実行できるほか、``extract_rolling_from_full`` 関数を通じて
テストや他スクリプトから再利用することも可能です。
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Iterable
import concurrent.futures
from dataclasses import dataclass, field
from datetime import datetime
import json
import logging
import os
from pathlib import Path
import sys
import time
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import pandas as pd  # noqa: E402  ディレクトリ解決後にインポート

from common.cache_manager import CacheManager  # noqa: E402
from common.indicators_common import add_indicators  # noqa: E402
from common.symbol_universe import build_symbol_universe_from_settings  # noqa: E402
from common.symbols_manifest import (  # noqa: E402
    MANIFEST_FILENAME,
    load_symbol_manifest,
)
from common.utils import safe_filename  # noqa: E402
from config.settings import get_settings  # noqa: E402

# json already imported at top

LOGGER = logging.getLogger(__name__)

SUPPORTED_SUFFIXES = {".csv", ".parquet", ".feather"}

# 全指標カラム名 (case-insensitive lookup)。full_backup CSV には placeholder として
# 空 (NaN) の列が最初から存在するケースがあり、``add_indicators`` は「列があれば
# 事前計算済」と判断して recompute を skip する。stale-NaN な placeholder で全指標が
# NaN のままになる (2026-07-02 rolling 全 NaN bug の主犯) のを防ぐため、
# ``_prepare_rolling_frame`` は ``add_indicators`` 呼出前にこれらのうち全 NaN の列を
# drop する。これで add_indicators が確実に recompute する。
_INDICATOR_COLS_FOR_RECOMPUTE = (
    "atr10", "atr20", "atr40", "atr50",
    "sma25", "sma50", "sma100", "sma150", "sma200",
    "roc200", "rsi3", "rsi4", "adx7",
    "dollarvolume20", "dollarvolume50", "avgvolume50",
    "atr_ratio", "atr_pct",
    "return_3d", "return_6d", "return_pct",
    "uptwodays", "twodayup",
    "drop3d", "hv50", "min_50", "max_70",
)


def _drop_all_nan_indicator_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Drop indicator columns that are entirely NaN so ``add_indicators`` recomputes them.

    ``add_indicators`` skips recomputation when the column already exists,
    treating it as pre-computed. Placeholder columns from ``full_backup`` CSV
    are all-NaN, which causes rolling cache to inherit NaN indicators.
    This helper removes those empty placeholders so add_indicators fires.
    """
    if df is None or df.empty:
        return df
    to_drop: list[str] = []
    for col in list(df.columns):
        key = str(col).lower()
        if key not in _INDICATOR_COLS_FOR_RECOMPUTE:
            continue
        try:
            series = df[col]
            if isinstance(series, pd.DataFrame):
                # duplicate column edge case: take the last
                series = series.iloc[:, -1]
            # boolean placeholder (uptwodays/twodayup) は数値化して all-NaN 判定
            if pd.api.types.is_bool_dtype(series):
                # 全 False は placeholder 相当と見做し drop (add_indicators が再計算)
                if not series.any():
                    to_drop.append(col)
                continue
            if series.isna().all():
                to_drop.append(col)
        except Exception:  # pragma: no cover - defensive
            continue
    if to_drop:
        try:
            df = df.drop(columns=to_drop)
        except Exception:  # pragma: no cover - defensive
            pass
    return df


def _read_symbol_source(
    cache_manager: "CacheManager", symbol: str
) -> tuple[pd.DataFrame | None, str]:
    """Read the best available source for building rolling cache.

    Preference order:
        1. base cache (``data_cache/base/<SYM>.feather``) — already has
           ``compute_base_indicators`` applied, so indicators are correct.
           This is the authoritative source going forward.
        2. full_backup (``cm.read(symbol, "full")``) — fallback when base
           is missing (fresh install / cache-corruption recovery). Requires
           ``_prepare_rolling_frame`` to recompute indicators.

    Returns
    -------
    tuple[pd.DataFrame | None, str]
        (dataframe, source_label) where source_label is ``"base"`` or ``"full"``.
        Returns ``(None, "none")`` if neither source has usable data.
    """
    # Try base first.
    try:
        base_dir = cache_manager.full_dir.parent / "base"
        base_path = cache_manager.file_manager.detect_path(base_dir, symbol)
        if base_path.exists():
            df = cache_manager.file_manager.read_with_fallback(
                base_path, symbol, "base"
            )
            if df is not None and not getattr(df, "empty", True):
                return df, "base"
    except Exception:  # pragma: no cover - defensive; fall through to full
        pass
    # Fallback to full_backup.
    try:
        df = cache_manager.read(symbol, "full")
    except Exception:
        return None, "none"
    if df is None or getattr(df, "empty", True):
        return None, "none"
    return df, "full"


@dataclass
class ExtractionStats:
    """集計結果を保持するデータクラス。"""

    total_symbols: int = 0
    processed_symbols: int = 0
    updated_symbols: int = 0
    skipped_no_data: int = 0
    errors: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_symbols": self.total_symbols,
            "processed_symbols": self.processed_symbols,
            "updated_symbols": self.updated_symbols,
            "skipped_no_data": self.skipped_no_data,
            "errors": dict(self.errors),
        }


def _log_message(message: str, log: Callable[[str], None] | None) -> None:
    # If an external logging callable is provided (e.g. console printer),
    # use it and avoid emitting the same message via the module logger to
    # prevent duplicate lines in logs. If no external logger is provided,
    # fall back to the module logger.
    if log:
        try:
            log(message)
        except Exception:  # pragma: no cover - ログが失敗しても続行
            pass
        return
    LOGGER.info(message)


def _normalize_positive_int(value: Any | None) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _discover_symbols(full_dir: Path) -> list[str]:
    """Detect available symbols from the full backup directory."""

    symbols: set[str] = set()
    for path in full_dir.glob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() not in SUPPORTED_SUFFIXES:
            continue
        if path.name.startswith("_"):
            continue
        stem = path.stem.strip()
        if stem:
            symbols.add(stem)
    return sorted(symbols)


def _round_numeric_columns(df: pd.DataFrame, decimals: int | None) -> pd.DataFrame:
    """数値列を ``decimals`` 桁に丸めた DataFrame を返す。"""

    if decimals is None:
        return df
    try:
        dec = int(decimals)
    except (TypeError, ValueError):
        return df
    numeric = df.select_dtypes(include="number")
    if numeric.empty:
        return df
    rounded = df.copy()
    try:
        rounded[numeric.columns] = numeric.round(dec)
    except Exception:
        return df
    return rounded


def _prepare_rolling_frame(
    df: pd.DataFrame,
    target_days: int,
    *,
    source: str = "full",
) -> pd.DataFrame | None:
    """Normalize full-history dataframe and compute indicators for rolling cache.

    Parameters
    ----------
    df : pd.DataFrame
        Source dataframe (full_backup OHLCV or base with pre-computed indicators).
    target_days : int
        Row cap after processing. ``0`` disables tail-truncation and preserves
        all input rows — used when ``source == "base"`` so that
        ``rolling.rows == base.rows`` (fulfils the "rolling should mirror base"
        contract asserted by ``tests/test_build_rolling_writes_full_history.py``).
    source : str, default ``"full"``
        Which cache profile the frame came from. Controls two things:
          * ``"base"``: skip tail-truncation entirely (target_days ignored),
            since base already carries the intended window with valid indicators.
          * ``"full"``: apply target_days tail cap after add_indicators.
    """

    if df is None or getattr(df, "empty", True):
        return None

    try:
        work = df.copy()
    except Exception:  # pragma: no cover - defensive fallback
        work = pd.DataFrame(df)

    if "date" not in work.columns:
        if "Date" in work.columns:
            work = work.rename(columns={"Date": "date"})
        else:
            try:
                idx_series = pd.to_datetime(work.index, errors="coerce")
            except Exception:
                idx_series = None
            if idx_series is None or idx_series.isna().all():
                return None
            work = work.reset_index(drop=True)
            work["date"] = idx_series

    work["date"] = pd.to_datetime(work["date"], errors="coerce")
    work = work.dropna(subset=["date"])  # 不正日付を除外
    if work.empty:
        return None
    work = (
        work.sort_values("date")
        .drop_duplicates("date", keep="last")
        .reset_index(drop=True)
    )

    calc = work.copy()

    # Ensure we have Date column for indicator calculations, avoiding duplication
    if "Date" not in calc.columns:
        if "date" in calc.columns:
            calc["Date"] = pd.to_datetime(calc["date"], errors="coerce").dt.normalize()
            # Remove lowercase date to avoid duplication
            calc = calc.drop(columns=["date"])
        else:
            # This shouldn't happen as we normalized date earlier
            calc["Date"] = pd.to_datetime(calc.index, errors="coerce").normalize()

    # Only convert columns if PascalCase versions don't already exist,
    # and proactively drop lowercase duplicates if TitleCase already
    # exists. This handles both new data (from cache_daily_data.py with
    # PascalCase) and legacy data (with lowercase columns), minimizing
    # later duplicate cleanup.
    col_pairs = (
        ("open", "Open"),
        ("high", "High"),
        ("low", "Low"),
        ("close", "Close"),
        ("volume", "Volume"),
    )
    for src, dst in col_pairs:
        if src in calc.columns and dst not in calc.columns:
            calc[dst] = calc[src]
            calc = calc.drop(columns=[src])
        # If both exist (e.g., legacy artifacts), drop the lowercase
        # source to avoid duplicate groups
        elif src in calc.columns and dst in calc.columns:
            calc = calc.drop(columns=[src])

    # Handle AdjClose conversion and de-duplication comprehensively
    adj_synonyms = ("adjusted_close", "adj_close", "adjclose")
    if "AdjClose" in calc.columns:
        # If TitleCase exists, drop any lowercase/underscore synonyms to
        # avoid duplicate groups
        drop_src = [c for c in adj_synonyms if c in calc.columns]
        if drop_src:
            calc = calc.drop(columns=drop_src)
    else:
        # Create AdjClose from the first available synonym and drop all
        # synonyms afterwards
        for cand in adj_synonyms:
            if cand in calc.columns:
                calc["AdjClose"] = calc[cand]
                break
        # Drop any remaining synonyms (including the source) to ensure a
        # single canonical column
        drop_src = [c for c in adj_synonyms if c in calc.columns]
        if drop_src:
            calc = calc.drop(columns=drop_src)

    required = {"Open", "High", "Low", "Close"}
    if required - set(calc.columns):
        missing = ",".join(sorted(required - set(calc.columns)))
        raise ValueError(f"missing_price_columns:{missing}")

    # 指標計算に必要な過去データを確保するための lookback margin
    try:
        settings = get_settings(create_dirs=True)
        lookback_margin = int(getattr(settings.cache, "indicator_lookback_margin", 200))
    except Exception:
        lookback_margin = 200

    # add_indicators に渡す前に、target_days に加えて余分な過去を含める
    # これにより ROC200 等の長期指標が tail 部分で適切に計算される
    if target_days > 0 and lookback_margin > 0:
        prefetch_days = int(target_days) + int(lookback_margin)
        calc_for_ind = calc.copy().tail(prefetch_days)
    else:
        calc_for_ind = calc

    # ★ CRITICAL FIX (2026-07-02 rolling all-NaN bug):
    # full_backup CSV には placeholder として空 (all-NaN) の指標列が存在する。
    # ``add_indicators`` は「列があれば pre-computed」と判断し recompute を skip するため、
    # NaN が伝播して rolling の指標が全 NaN になっていた。
    # add_indicators 呼出前に stale-NaN な placeholder 列を drop し、確実に recompute させる。
    calc_for_ind = _drop_all_nan_indicator_columns(calc_for_ind)

    enriched = add_indicators(calc_for_ind)

    # Clean duplicate columns (can be skipped for performance if data is already clean)
    enriched = _clean_duplicate_columns(enriched, skip_cleanup=False)

    # normalize date column
    date_col = enriched.get("date", enriched.get("Date"))
    if date_col is not None:
        enriched["date"] = pd.to_datetime(date_col, errors="coerce")
    enriched = enriched.drop(columns=["Date"], errors="ignore")
    enriched = enriched.dropna(subset=["date"]).sort_values("date")
    # ★ base 由来のフレームは既に必要期間 (=base) 分揃っているので tail 切り詰めしない。
    # full 由来のときのみ target_days で切り詰める (indicator recompute 用の
    # lookback margin を除去して要求ウィンドウに揃える)。
    if source != "base" and target_days > 0:
        enriched = enriched.tail(int(target_days))
    enriched = enriched.reset_index(drop=True)

    cols = ["date"] + [c for c in enriched.columns if c != "date"]
    return enriched.loc[:, cols]


def _clean_duplicate_columns(
    df: pd.DataFrame, skip_cleanup: bool = False
) -> pd.DataFrame:
    """Remove duplicate columns, keeping PascalCase/uppercase versions."""
    if df is None or df.empty:
        return df

    # Skip cleanup if requested (for performance when data is already clean)
    if skip_cleanup:
        return df

    columns = df.columns.tolist()
    duplicates_to_remove = []

    # Build case-insensitive mapping to find duplicates
    col_mapping = {}
    for col in columns:
        key = col.lower()
        if key not in col_mapping:
            col_mapping[key] = []
        col_mapping[key].append(col)

    # For each group of similar columns, keep the best one
    for _key, similar_cols in col_mapping.items():
        if len(similar_cols) <= 1:
            continue

        # Priority order: PascalCase > ALL_CAPS > lowercase
        priority_scores = []
        for col in similar_cols:
            if col.isupper():  # ATR10, SMA25, etc.
                score = 3
            elif col[0].isupper():  # Open, Close, DollarVolume20, etc.
                score = 2
            elif "_" in col:  # adjusted_close, return_3d, etc.
                score = 1
            else:  # lowercase: atr10, sma25, etc.
                score = 0
            priority_scores.append((score, col))

        # Sort by priority (highest first) and keep the best one
        priority_scores.sort(reverse=True)
        # best_col = priority_scores[0][1]  # Unused variable removed

        # Mark others for removal
        for _, col in priority_scores[1:]:
            duplicates_to_remove.append(col)

    # Remove duplicate columns (should not occur with fixed data processing)
    if duplicates_to_remove:
        # Only show error message if duplicates still occur (indicates a problem)
        removed_cols = ", ".join(duplicates_to_remove)
        print(
            f"⚠️ 予期しない重複列を検出・削除: {len(duplicates_to_remove)}列 ({removed_cols})"
        )
        df = df.drop(columns=duplicates_to_remove)

    return df


def _process_symbol_worker(args: tuple) -> tuple[str, bool, str | None]:
    """Worker function run in a separate process.

    Returns (symbol, success_flag, message). message is None on success,
    or 'no_data' / error message on failure.
    """
    symbol, target_days, round_decimals, nan_warnings = args
    try:
        settings = get_settings(create_dirs=True)
        cm = CacheManager(settings)
        try:
            source_df, source_label = _read_symbol_source(cm, symbol)
        except Exception as exc:
            return (symbol, False, f"read_error:{exc}")
        if source_df is None or getattr(source_df, "empty", True):
            return (symbol, False, "no_data")
        enriched = _prepare_rolling_frame(
            source_df, target_days, source=source_label
        )
        if enriched is None or getattr(enriched, "empty", True):
            return (symbol, False, "no_data")
        if round_decimals is not None:
            try:
                enriched = _round_numeric_columns(enriched, round_decimals)
            except Exception:
                pass
        try:
            # Write both CSV and Feather formats
            _write_dual_format(cm, enriched, symbol)
        except Exception as exc:
            return (symbol, False, f"write_error:{exc}")
        return (symbol, True, None)
    except Exception as exc:
        return (symbol, False, f"{type(exc).__name__}:{exc}")


def _write_dual_format(cm: CacheManager, df: pd.DataFrame, symbol: str) -> None:
    """Write both CSV and Feather formats for better performance."""
    import shutil

    # Get rolling directory
    rolling_dir = cm.rolling_dir
    rolling_dir.mkdir(parents=True, exist_ok=True)

    # Apply rounding if configured
    round_dec = getattr(getattr(cm, "rolling_cfg", None), "round_decimals", None)
    from common.dataframe_utils import round_dataframe

    df_to_write = round_dataframe(df, round_dec)

    # Write CSV (for compatibility)
    csv_path = rolling_dir / f"{symbol}.csv"
    csv_tmp = rolling_dir / f"{symbol}.csv.tmp"
    try:
        # Use standard pandas CSV writing with explicit format settings
        df_to_write.to_csv(csv_tmp, index=True, float_format="%.6f")
        shutil.move(csv_tmp, csv_path)
    finally:
        if csv_tmp.exists():
            csv_tmp.unlink(missing_ok=True)

    # Write Feather (for performance)
    feather_path = rolling_dir / f"{symbol}.feather"
    feather_tmp = rolling_dir / f"{symbol}.feather.tmp"
    try:
        df_to_write.reset_index(drop=True).to_feather(feather_tmp)
        shutil.move(feather_tmp, feather_path)
    finally:
        if feather_tmp.exists():
            feather_tmp.unlink(missing_ok=True)


def _resolve_symbol_universe(
    cache_manager: CacheManager,
    symbols: Iterable[str] | None,
    log: Callable[[str], None] | None,
) -> list[str]:
    if symbols is not None:
        return [s for s in (sym.strip() for sym in symbols) if s]

    manifest_symbols = load_symbol_manifest(cache_manager.full_dir)
    if manifest_symbols:
        try:
            msg = (
                f"ℹ️ cache_daily_data マニフェスト({MANIFEST_FILENAME}) から "
                f"{len(manifest_symbols)} 銘柄を読み込みました"
            )
            _log_message(msg, log)
        except Exception:
            _log_message(
                f"ℹ️ cache_daily_data マニフェスト({MANIFEST_FILENAME}) を読み込みました",
                log,
            )

        available = _discover_symbols(cache_manager.full_dir)
        available_set = {sym.upper() for sym in available}
        filtered = [sym for sym in manifest_symbols if sym.upper() in available_set]

        if filtered:
            missing = len(manifest_symbols) - len(filtered)
            if missing:
                _log_message(
                    (
                        f"ℹ️ full_backup に未存在の {missing} 銘柄を除外し {len(filtered)} 銘柄を処理対象とします"
                    ),
                    log,
                )
            return filtered

        if available:
            _log_message(
                (
                    "⚠️ マニフェスト銘柄が full_backup に存在しないため "
                    f"full_backup を走査した {len(available)} 銘柄を利用します"
                ),
                log,
            )
            return available

        _log_message(
            "⚠️ full_backup ディレクトリから処理対象を検出できませんでした", log
        )
        return []

    # cache_daily_data と同一ロジックで銘柄集合を構築
    try:
        settings = getattr(cache_manager, "settings", None)
        fetched = build_symbol_universe_from_settings(settings, logger=LOGGER)
    except Exception as exc:  # pragma: no cover - ログのみ
        _log_message(f"⚠️ NASDAQ/EODHD ユニバース取得に失敗: {exc}", log)
        fetched = []

    if fetched:
        safe_symbols = list(dict.fromkeys(safe_filename(sym) for sym in fetched))
        available = _discover_symbols(cache_manager.full_dir)
        if available:
            available_set = {sym.upper() for sym in available}
            filtered = [sym for sym in safe_symbols if sym.upper() in available_set]
            missing = len(safe_symbols) - len(filtered)
            if missing:
                _log_message(
                    (
                        f"ℹ️ NASDAQ/EODHD ユニバース {len(safe_symbols)} 件のうち "
                        f"{missing} 件が full_backup に存在しないため除外します"
                    ),
                    log,
                )
            if filtered:
                return filtered
            # 取得したユニバースと full_backup に重複が無い場合は
            # full_backup を走査した銘柄を利用する（テスト互換性のため）
            _log_message(
                (
                    "⚠️ NASDAQ/EODHD ユニバースが full_backup に存在しないため "
                    f"full_backup を走査した {len(available)} 銘柄を利用します"
                ),
                log,
            )
            return available

        _log_message(
            f"ℹ️ NASDAQ/EODHD ユニバース {len(safe_symbols)} 銘柄を処理対象とします",
            log,
        )
        return safe_symbols

    discovered = _discover_symbols(cache_manager.full_dir)
    _log_message(
        (
            f"ℹ️ マニフェスト未検出のため full_backup を走査して {len(discovered)} 銘柄を検出しました"
        ),
        log,
    )
    return discovered


def extract_rolling_from_full(
    cache_manager: CacheManager,
    *,
    symbols: Iterable[str] | None = None,
    target_days: int | None = None,
    max_symbols: int | None = None,
    log: Callable[[str], None] | None = None,
    nan_warnings: bool = False,
    workers: int | None = None,
    adaptive: bool = True,
) -> ExtractionStats:
    """Extract rolling window slices from full backup cache and persist them.

    ``max_symbols`` can be used to cap the number of symbols processed.  When
    not provided explicitly the method falls back to
    ``cache_manager.rolling_cfg.max_symbols`` if it is configured with a
    positive integer value.
    """

    # Record start time
    start_time = time.time()
    start_dt = datetime.fromtimestamp(start_time).strftime("%Y-%m-%d %H:%M:%S")

    if target_days is None:
        try:
            target_days = int(
                cache_manager.rolling_cfg.base_lookback_days
                + cache_manager.rolling_cfg.buffer_days
            )
        except Exception:
            target_days = 330
    target_days = max(1, int(target_days))

    symbol_list = _resolve_symbol_universe(cache_manager, symbols, log)

    stats = ExtractionStats(total_symbols=len(symbol_list))

    if not symbol_list:
        _log_message("対象シンボルが見つかりませんでした。", log)
        return stats

    _log_message(f"🕐 開始時刻: {start_dt}", log)
    _log_message(
        f"🔁 rolling 再構築を開始: {len(symbol_list)} 銘柄 | 期間={target_days}営業日",
        log,
    )

    try:
        # tests may provide a SimpleNamespace without nested attributes;
        # fall back safely
        round_decimals = getattr(
            getattr(cache_manager, "rolling_cfg", None), "round_decimals", None
        )
        if round_decimals is None:
            settings_obj = getattr(cache_manager, "settings", None)
            cache_obj = getattr(settings_obj, "cache", None)
            round_decimals = getattr(cache_obj, "round_decimals", None)
    except Exception:
        round_decimals = None

    # Determine initial worker count preference
    cfg_workers = getattr(getattr(cache_manager, "rolling_cfg", None), "workers", None)
    # If explicit workers passed to function, it takes precedence
    if workers is None:
        workers = cfg_workers

    # Serial fallback if workers not specified
    if workers is None:
        # keep original sequential behavior
        for idx, symbol in enumerate(symbol_list, start=1):
            stats.processed_symbols += 1
            try:
                source_df, source_label = _read_symbol_source(cache_manager, symbol)
            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}"
                stats.errors[symbol] = message
                _log_message(
                    f"⚠️ {symbol}: base/full 読み込みに失敗 ({message})", log
                )
                continue

            if source_df is None or getattr(source_df, "empty", True):
                stats.skipped_no_data += 1
                _log_message(
                    f"⏭️ {symbol}: base/full どちらもデータ無しのためスキップ", log
                )
                continue

            try:
                enriched = _prepare_rolling_frame(
                    source_df, target_days, source=source_label
                )
            except Exception as exc:  # pragma: no cover - logging only
                message = f"{type(exc).__name__}: {exc}"
                stats.errors[symbol] = message
                _log_message(f"⚠️ {symbol}: インジ計算に失敗 ({message})", log)
                continue

            if enriched is None or getattr(enriched, "empty", True):
                stats.skipped_no_data += 1
                _log_message(f"⏭️ {symbol}: 有効なローリングデータ無し", log)
                continue

            try:
                enriched = _round_numeric_columns(enriched, round_decimals)
                cache_manager.write_atomic(enriched, symbol, "rolling")
            except Exception as exc:  # pragma: no cover - logging only
                message = f"{type(exc).__name__}: {exc}"
                stats.errors[symbol] = message
                _log_message(f"⚠️ {symbol}: rolling 書き込みに失敗 ({message})", log)
                continue

            stats.updated_symbols += 1
            if idx % 100 == 0 or idx == len(symbol_list):
                _log_message(f"✅ 進捗: {idx}/{len(symbol_list)} 銘柄処理完了", log)
    else:
        # Parallel execution with adaptive concurrency control
        try:
            workers = int(workers)
        except Exception:
            workers = 0

        # establish sensible bounds
        cpu = os.cpu_count() or 1
        max_possible = max(1, min(32, int(cpu * 2), len(symbol_list)))
        if workers and workers > 0:
            initial_workers = int(workers)
        else:
            settings_obj = getattr(cache_manager, "settings", None)
            cache_obj = getattr(settings_obj, "cache", None)
            rolling_obj = getattr(cache_obj, "rolling", None)
            try:
                initial_workers = int(getattr(rolling_obj, "workers", 4) or 4)
            except Exception:
                initial_workers = 4
        current_workers = max(1, min(initial_workers, max_possible))

        _log_message(
            (
                f"ℹ️ 並列処理: 初期ワーカー={current_workers} "
                f"最大ワーカー={max_possible} 適応型={'有効' if adaptive else '無効'}"
            ),
            log,
        )

        args_list = [
            (symbol, target_days, round_decimals, nan_warnings)
            for symbol in symbol_list
        ]

        # prepare progress output file
        try:
            settings_obj = getattr(cache_manager, "settings", None)
            cache_obj = getattr(settings_obj, "cache", None)
            rolling_obj = getattr(cache_obj, "rolling", None)
            report_seconds = int(
                getattr(rolling_obj, "adaptive_report_seconds", 10) or 10
            )
        except Exception:
            report_seconds = 10

        logs_dir_candidate = (
            getattr(cache_manager.settings.outputs, "logs_dir", None)
            or getattr(cache_manager.settings, "LOGS_DIR", None)
            or "logs"
        )
        logs_dir_path = Path(str(logs_dir_candidate))
        try:
            logs_dir_path.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        progress_path = logs_dir_path / "rolling_progress.json"

        # create executor with upper bound; we will control active submissions
        with concurrent.futures.ProcessPoolExecutor(max_workers=max_possible) as exe:
            next_idx = 0
            active: dict[concurrent.futures.Future, tuple[str, float]] = {}

            # adaptive measurement
            window_durations: list[float] = []
            window_count = 8
            prev_throughput = None

            while stats.processed_symbols < len(symbol_list):
                # submit tasks until reaching current_workers
                while len(active) < current_workers and next_idx < len(args_list):
                    args = args_list[next_idx]
                    fut = exe.submit(_process_symbol_worker, args)
                    active[fut] = (args[0], time.time())
                    next_idx += 1

                if not active:
                    break

                done, _ = concurrent.futures.wait(
                    active.keys(), return_when=concurrent.futures.FIRST_COMPLETED
                )
                for fut in done:
                    symbol, start_ts = active.pop(fut)
                    stats.processed_symbols += 1
                    end_ts = time.time()
                    duration = max(0.0001, end_ts - start_ts)
                    window_durations.append(duration)
                    # keep window size bounded
                    if len(window_durations) > window_count:
                        window_durations.pop(0)

                    try:
                        sym, ok, message = fut.result()
                    except Exception as exc:
                        stats.errors[symbol] = str(exc)
                        _log_message(f"⚠️ {symbol}: worker 例外 ({exc})", log)
                        continue

                    if not ok:
                        if message == "no_data":
                            stats.skipped_no_data += 1
                            _log_message(
                                f"⏭️ {symbol}: full データ無しのためスキップ", log
                            )
                        else:
                            stats.errors[symbol] = message or "error"
                            _log_message(f"⚠️ {symbol}: 処理失敗 ({message})", log)
                    else:
                        stats.updated_symbols += 1

                # write progress JSON periodically
                try:
                    now_ts = int(time.time())
                    if (
                        not progress_path.exists()
                        or now_ts - int(progress_path.stat().st_mtime) >= report_seconds
                    ):
                        prog = {
                            "total": stats.total_symbols,
                            "processed": stats.processed_symbols,
                            "updated": stats.updated_symbols,
                            "skipped": stats.skipped_no_data,
                            "errors": len(stats.errors),
                            "current_workers": current_workers,
                            "recent_window_seconds": [
                                round(d, 3) for d in window_durations
                            ],
                            "timestamp": now_ts,
                        }
                        try:
                            with open(progress_path, "w", encoding="utf-8") as pf:
                                json.dump(prog, pf, ensure_ascii=False)
                        except Exception:
                            pass
                except Exception:
                    pass

                # report progress periodically
                if (
                    stats.processed_symbols % 100 == 0
                    or stats.processed_symbols == len(symbol_list)
                ):
                    _log_message(
                        f"✅ 進捗: {stats.processed_symbols}/{len(symbol_list)} 銘柄処理完了",
                        log,
                    )

                # adaptive adjustment: evaluate throughput over window
                if adaptive and len(window_durations) >= max(4, window_count // 2):
                    window_time = sum(window_durations)
                    if window_time <= 0:
                        continue
                    throughput = len(window_durations) / window_time
                    # try small adjustments: increase or decrease by 1
                    if prev_throughput is None:
                        prev_throughput = throughput
                    else:
                        # if throughput improved notably, try increasing workers
                        if (
                            throughput > prev_throughput * 1.02
                            and current_workers < max_possible
                        ):
                            current_workers += 1
                            _log_message(
                                f"ℹ️ ワーカー数を増やします -> {current_workers}", log
                            )
                            prev_throughput = throughput
                        # if throughput degraded notably, decrease workers
                        elif (
                            throughput < prev_throughput * 0.98 and current_workers > 1
                        ):
                            current_workers = max(1, current_workers - 1)
                            _log_message(
                                f"ℹ️ ワーカー数を減らします -> {current_workers}", log
                            )
                            prev_throughput = throughput
                        else:
                            # small/no change, keep current
                            prev_throughput = throughput

    # Calculate completion time and duration
    end_time = time.time()
    end_dt = datetime.fromtimestamp(end_time).strftime("%Y-%m-%d %H:%M:%S")
    duration_seconds = end_time - start_time

    # Format duration as H:MM:SS
    hours = int(duration_seconds // 3600)
    minutes = int((duration_seconds % 3600) // 60)
    seconds = int(duration_seconds % 60)
    duration_str = f"{hours}:{minutes:02d}:{seconds:02d}"

    _log_message(f"🕐 終了時刻: {end_dt}", log)
    _log_message(f"⏰ 所要時間: {duration_str}", log)
    _log_message(
        "✅ rolling 再構築完了: "
        + f"対象={stats.total_symbols} | 更新={stats.updated_symbols} | "
        + f"欠損={stats.skipped_no_data} | エラー={len(stats.errors)}",
        log,
    )
    return stats


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="full_backup から rolling を再構築し主要インジケーターを付与",
    )
    parser.add_argument(
        "--symbols",
        nargs="+",
        help="処理対象シンボル（未指定時は cache_daily_data マニフェスト/全銘柄）",
    )
    parser.add_argument(
        "--target-days",
        type=int,
        help="ローリングに保持する営業日数（既定: 設定値 base+buffer）",
    )
    parser.add_argument(
        "--max-symbols",
        type=int,
        help="処理上限銘柄数（0 以下で無制限。既定: 設定値 rolling.max_symbols）",
    )
    parser.add_argument(
        "--nan-warnings",
        action="store_true",
        help="指標 NaN 警告を有効化（既定: 無効、ログ抑止）",
    )
    parser.add_argument(
        "--workers",
        type=int,
        help="並列ワーカー数の上限（未指定で設定値またはデフォルトを使用）",
    )
    parser.add_argument(
        "--no-adaptive",
        action="store_true",
        help="適応的ワーカー調整を無効化（既定: 有効）",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO)
    parser = _build_parser()
    args = parser.parse_args(argv)

    settings = get_settings(create_dirs=True)
    cache_manager = CacheManager(settings)

    def _console_log(msg: str) -> None:
        LOGGER.info(msg)

    stats = extract_rolling_from_full(
        cache_manager,
        symbols=args.symbols,
        target_days=args.target_days,
        max_symbols=args.max_symbols,
        log=_console_log,
        nan_warnings=bool(getattr(args, "nan_warnings", False)),
        workers=getattr(args, "workers", None),
        adaptive=(not bool(getattr(args, "no_adaptive", False))),
    )

    if stats.errors:
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
