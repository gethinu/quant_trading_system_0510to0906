"""共通のシステムユーティリティ関数群。

全System*.py間で共有される関数をここに集約：
- _rename_ohlcv: カラム名正規化
- _normalize_index: 日付インデックス正規化
- _prepare_source_frame: データフレーム前処理
- get_total_days: 総日数計算
- check_precomputed_indicators: プリコンピューテッド指標チェック
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

import pandas as pd

from common.i18n import tr


def _rename_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
    """OHLCV カラム名を標準形式に正規化する。

    Args:
        df: 入力データフレーム

    Returns:
        カラム名が正規化されたデータフレーム
    """
    x = df.copy(deep=False)
    rename_map = {}
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
        x = x.rename(columns=rename_map)
    return x


def _normalize_index(df: pd.DataFrame) -> pd.DataFrame:
    """日付インデックスを正規化し、重複・NaN を除去する。

    Args:
        df: 入力データフレーム

    Returns:
        正規化されたインデックスを持つデータフレーム

    Raises:
        ValueError: 有効な日付インデックスが存在しない場合
    """
    if "Date" in df.columns:
        idx = pd.to_datetime(df["Date"], errors="coerce").dt.normalize()
    elif "date" in df.columns:
        idx = pd.to_datetime(df["date"], errors="coerce").dt.normalize()
    else:
        idx = pd.to_datetime(df.index, errors="coerce").normalize()

    if idx is None:
        raise ValueError("invalid_date_index")

    try:
        # pandas >=1.5: pd.isna for Index returns ndarray-like; .all() is valid
        if pd.isna(idx).all():
            raise ValueError("invalid_date_index")
    except Exception:
        # Defensive: if idx has unexpected type
        pass

    x = df.copy()
    x.index = pd.Index(idx)
    x.index.name = "Date"
    x = x[~x.index.isna()]

    try:
        x = x.sort_index()
    except Exception:
        pass

    try:
        if getattr(x.index, "has_duplicates", False):
            x = x[~x.index.duplicated(keep="last")]
    except Exception:
        pass

    return x


def slice_latest_rows(
    df: pd.DataFrame,
    *,
    keep_columns: Sequence[str] | None = None,
    tail_rows: int = 5,
) -> pd.DataFrame:
    """Return a lightweight frame for latest-only processing.

    - Optionally keeps only the requested columns (if present).
    - Returns only the last N rows (tail_rows).
    - Uses shallow copies to avoid mutating the source frame.
    """
    if df is None or getattr(df, "empty", True):
        return df
    x = df
    try:
        x = _rename_ohlcv(x)
    except Exception:
        pass
    if keep_columns:
        try:
            cols = [c for c in keep_columns if c in x.columns]
            # Preserve date columns if present so downstream freshness checks work
            for date_col in ("Date", "date"):
                if date_col in x.columns and date_col not in cols:
                    cols.append(date_col)
            if cols:
                x = x.loc[:, cols]
        except Exception:
            pass
    try:
        if tail_rows and len(x) > int(tail_rows):
            x = x.tail(int(tail_rows))
    except Exception:
        pass
    try:
        return x.copy()
    except Exception:
        return x


def _prepare_source_frame(
    df: pd.DataFrame,
    required_columns: tuple[str, ...] = ("Open", "High", "Low", "Close", "Volume"),
    min_rows: int = 150,
) -> pd.DataFrame:
    """データフレームの基本的な前処理を行う。

    Args:
        df: 入力データフレーム
        required_columns: 必須カラム名のタプル
        min_rows: 最低必要行数

    Returns:
        前処理済みデータフレーム

    Raises:
        ValueError: データが空、カラム不足、行数不足の場合
    """
    if df is None or df.empty:
        raise ValueError("empty_frame")

    x = _rename_ohlcv(df)

    missing = [c for c in required_columns if c not in x.columns]
    if missing:
        raise ValueError(f"missing_cols:{','.join(missing)}")

    x = _normalize_index(x)

    # 数値型に変換
    for col in required_columns:
        if col in x.columns:
            x[col] = pd.to_numeric(x[col], errors="coerce")

    # 重要カラムのNaN行を削除
    x = x.dropna(subset=[c for c in ("High", "Low", "Close") if c in x.columns])

    if len(x) < min_rows:
        raise ValueError("insufficient_rows")

    return x


def get_total_days(data_dict: dict[str, pd.DataFrame]) -> int:
    """データ辞書から最大日数を計算する。

    Args:
        data_dict: シンボル -> データフレームの辞書

    Returns:
        最大日数（データが空の場合は0）
    """
    if not data_dict:
        return 0
    return max((len(df) for df in data_dict.values() if df is not None), default=0)


def get_date_range(data_dict: dict[str, pd.DataFrame]) -> tuple[str | None, str | None]:
    """データ辞書から日付範囲を取得する。

    Args:
        data_dict: シンボル -> データフレームの辞書

    Returns:
        (開始日, 終了日) の文字列タプル
    """
    if not data_dict:
        return None, None

    all_dates = []
    for df in data_dict.values():
        if not df.empty and hasattr(df.index, "min") and hasattr(df.index, "max"):
            try:
                all_dates.extend([df.index.min(), df.index.max()])
            except Exception:
                continue

    if not all_dates:
        return None, None

    min_date = min(all_dates)
    max_date = max(all_dates)

    return (
        (
            min_date.strftime("%Y-%m-%d")
            if hasattr(min_date, "strftime")
            else str(min_date)
        ),
        (
            max_date.strftime("%Y-%m-%d")
            if hasattr(max_date, "strftime")
            else str(max_date)
        ),
    )


def check_precomputed_indicators(
    data_dict: dict[str, pd.DataFrame],
    required_indicators: list[str],
    system_name: str,
    skip_callback: Callable[[str, str], None] | None = None,
) -> tuple[dict[str, pd.DataFrame], list[str]]:
    """プリコンピューテッド指標の存在をチェックし、不足している場合は早期終了する。

    Args:
        data_dict: シンボル -> データフレームの辞書
        required_indicators: 必須指標のリスト
        system_name: システム名（エラーメッセージ用）
        skip_callback: スキップ時のコールバック関数

    Returns:
        (有効なデータ辞書, エラーシンボルリスト)

    Raises:
        RuntimeError: 必要な指標が不足している場合
    """
    if not data_dict or not required_indicators:
        return data_dict, []

    valid_data_dict = {}
    error_symbols = []
    for symbol, df in data_dict.items():
        if df is None or df.empty:
            error_symbols.append(symbol)
            if skip_callback:
                skip_callback(symbol, "empty_data")
            continue

        # 必須指標の存在チェック (列名の大文字小文字やアンダースコア差分を吸収して判定)
        try:
            cols = [c for c in df.columns if isinstance(c, str)]
            # normalized column keys: lower + remove underscores/spaces for fuzzy match
            norm_cols = {c.lower().replace("_", "").replace(" ", "") for c in cols}
        except Exception:
            cols = []
            norm_cols = set()

        missing_indicators: list[str] = []
        for req in required_indicators:
            try:
                key = str(req or "").lower()
                # direct case-insensitive membership
                if any(key == c.lower() for c in cols):
                    continue
                # fuzzy membership: compare normalized forms (drop underscores/spaces)
                if key.replace("_", "") in norm_cols:
                    continue
                # not found
                missing_indicators.append(req)
            except Exception:
                missing_indicators.append(req)

        if missing_indicators:
            joined = ",".join(missing_indicators)
            error_msg = f"{system_name}_{symbol}_missing_indicators: {joined}"
            error_symbols.append(symbol)
            if skip_callback:
                skip_callback(symbol, error_msg)
            continue

        # If indicators are present, normalize the column names so that
        # downstream code can use canonical names (lowercase, no
        # underscores) like 'drop3d', 'atr_ratio', 'dollarvolume20'.
        try:
            rename_map: dict[str, str] = {}
            for req in required_indicators:
                canonical = str(req)
                # If canonical already present, no need to rename
                if canonical in df.columns:
                    continue
                # 1) case-insensitive exact match
                found = next((c for c in cols if c.lower() == canonical.lower()), None)
                if found and found != canonical:
                    rename_map[found] = canonical
                    continue
                # 2) fuzzy normalized match (lower + remove underscores/spaces)
                norm_key = canonical.lower().replace("_", "").replace(" ", "")

                def _norm(col: str) -> str:
                    return col.lower().replace("_", "").replace(" ", "")

                found2 = next((c for c in cols if _norm(str(c)) == norm_key), None)
                if found2 and found2 != canonical:
                    rename_map[found2] = canonical
                    continue
            if rename_map:
                try:
                    df = df.rename(columns=rename_map)
                except Exception:
                    pass
        except Exception:
            pass
        valid_data_dict[symbol] = df

    # 有効なデータが一つもない場合はエラー
    if not valid_data_dict:
        missing_str = ",".join(required_indicators)
        raise RuntimeError(
            tr(
                "system_precomputed_indicators_missing",
                system=system_name,
                indicators=missing_str,
            )
        )

    return valid_data_dict, error_symbols


def validate_data_frame_basic(
    df: pd.DataFrame, symbol: str, min_rows: int = 150
) -> None:
    """データフレームの基本検証を行う。

    Args:
        df: 検証するデータフレーム
        symbol: シンボル名（エラーメッセージ用）
        min_rows: 最小行数要件

    Raises:
        ValueError: データが要件を満たさない場合
    """
    if df is None or df.empty:
        raise ValueError(f"empty_data_{symbol}")

    if len(df) < min_rows:
        raise ValueError(f"insufficient_rows_{symbol}_{len(df)}<{min_rows}")

    # 基本的なOHLCVカラムの存在チェック
    required_cols = ["Open", "High", "Low", "Close", "Volume"]
    missing_cols = [col for col in required_cols if col not in df.columns]
    if missing_cols:
        raise ValueError(f"missing_columns_{symbol}_{','.join(missing_cols)}")


__all__ = [
    "_rename_ohlcv",
    "_normalize_index",
    "slice_latest_rows",
    "_prepare_source_frame",
    "get_total_days",
    "get_date_range",
    "check_precomputed_indicators",
    "validate_data_frame_basic",
]
