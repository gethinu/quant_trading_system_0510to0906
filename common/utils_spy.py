from __future__ import annotations

from collections.abc import Iterable
from datetime import time as dtime
import os
from pathlib import Path
import sys
from typing import overload
from zoneinfo import ZoneInfo

import pandas as pd

# --- NYSE calendar cache ----------------------------------------------------
_NYSE_CALENDAR = None
_LATEST_NYSE_CACHE: dict[str, pd.Timestamp] = {}
_NEXT_NYSE_CACHE: dict[str, pd.Timestamp] = {}


def _get_nyse_calendar():
    global _NYSE_CALENDAR  # noqa: PLW0603 - simple module cache
    if _NYSE_CALENDAR is None:
        _NYSE_CALENDAR = mcal.get_calendar("NYSE")
    return _NYSE_CALENDAR
import pandas_market_calendars as mcal
import streamlit as st
from ta.trend import SMAIndicator

from common.dataframe_utils import round_dataframe
from common.i18n import tr
from config.settings import get_settings

# Optional type for NaT (pandas doesn't expose a stable public type alias)
try:  # pragma: no cover - best-effort import
    from pandas._libs.tslibs.nattype import NaTType  # type: ignore[attr-defined]
except Exception:  # fallback alias
    NaTType = type(pd.NaT)

_NY_TIMEZONE = ZoneInfo("America/New_York")


def _ui_enabled() -> bool:
    """Return True when running under Streamlit UI.

    Heuristic only: avoid querying Streamlit runtime context directly to prevent
    'missing ScriptRunContext' warnings. Prefer environment flag or command hint.
    """
    try:
        v = (os.getenv("STREAMLIT_SERVER_ENABLED") or "").strip().lower()
        if v in {"1", "true", "yes"}:
            return True
    except Exception:
        pass
    try:
        argv = " ".join(sys.argv).lower()
        if "streamlit" in argv:
            return True
    except Exception:
        pass
    return False


def _st_emit(kind: str, *args, **kwargs) -> None:
    """Safely emit Streamlit UI calls only when UI context is active.

    In CLI/batch runs, this becomes a no-op to avoid ScriptRunContext warnings.
    """
    if not _ui_enabled():
        return
    try:
        fn = getattr(st, kind, None)
        if callable(fn):
            fn(*args, **kwargs)
    except Exception:
        # Silently ignore UI errors in non-critical paths
        return


def _candidate_spy_paths(root: Path) -> list[Path]:
    """SPY.csv を探す候補パスを返す。

    （バックテストなど広期間が必要な場面では base を優先し、
     当日シグナルなどでは rolling を優先する設計だが、
     本関数は単純な候補列挙のみを行う）
    大文字小文字の違いも吸収する（クロスプラットフォームのため）。
    """

    def _case_insensitive_find(dir_path: Path, name: str) -> Path | None:
        try:
            if not dir_path.exists():
                return None
            for fn in os.listdir(dir_path):
                if fn.lower() == name.lower():
                    return dir_path / fn
        except Exception:
            return None
        return None

    names = ("SPY.csv",)
    dirs: Iterable[Path] = (
        root / "base",
        root / "full",
        root / "rolling",
    )
    out: list[Path] = []
    for d in dirs:
        for nm in names:
            p = _case_insensitive_find(d, nm)
            if p is not None:
                out.append(p)
    return out


def _read_daily_csv_any_datecol(path: Path) -> pd.DataFrame:
    """日足CSVを 'Date' または 'date' いずれでも読み込めるようにする。"""
    df = pd.read_csv(path)
    date_col = None
    if "Date" in df.columns:
        date_col = "Date"
    elif "date" in df.columns:
        date_col = "date"
    if date_col is None:
        raise ValueError("date/Date 列が見つかりません")
    df[date_col] = pd.to_datetime(df[date_col])
    df = df.sort_values(date_col).set_index(date_col)
    return df


def get_spy_data_cached_v2(folder: str = "data_cache", mode: str = "backtest"):
    """
    SPY.csv をキャッシュから読み込む。
    - mode="backtest": data_cache/base → data_cache/full_backup の順（rolling は探索しない）
    - mode="today": data_cache/rolling → data_cache/base → data_cache/full_backup の順
    - キャッシュが見つからない場合はエラーを返す
    """
    try:
        settings = get_settings(create_dirs=True)
        root = Path(settings.DATA_CACHE_DIR)
        full_dir = Path(getattr(settings.cache, "full_dir", root / "full_backup"))
        rolling_dir = Path(getattr(settings.cache, "rolling_dir", root / "rolling"))
    except Exception:
        root = Path(folder)
        full_dir = root / "full_backup"
        rolling_dir = root / "rolling"

    # 探索順を mode で切り替え
    base_dir = root / "base"
    mode_lower = str(mode).lower()
    if mode_lower == "today":
        search_dirs: list[Path] = [rolling_dir, base_dir, full_dir]
    else:
        # backtest: rolling は探索しない
        search_dirs = [base_dir, full_dir]

    def _find_case_insensitive(d: Path, name: str) -> Path | None:
        try:
            if not d.exists():
                return None
            for fn in os.listdir(d):
                if fn.lower() == name.lower():
                    return d / fn
        except Exception:
            return None
        return None

    path: Path | None = None
    for d in search_dirs:
        p = _find_case_insensitive(d, "SPY.csv")
        if p is not None:
            path = p
            break
    if path is None or not path.exists():
        _st_emit(
            "error", tr("❌ SPY.csv が見つかりません (base/full_backup/rolling を確認)")
        )
        return None

    # backtest 時は full_backup の存在を必須とし、無ければエラーメッセージを表示
    if mode_lower != "today":
        has_full_backup = _find_case_insensitive(full_dir, "SPY.csv") is not None
        if not has_full_backup:
            try:
                _st_emit(
                    "error",
                    tr(
                        "⚠ SPY の full_backup が存在しません: {p}",
                        p=str(full_dir / "SPY.csv"),
                    ),
                )
            except Exception:
                pass

    try:
        df = _read_daily_csv_any_datecol(Path(path))

        # 直近情報の表示（UIが無い場面では無視される）
        try:
            _st_emit(
                "write", tr("✅ SPYキャッシュ最終日: {d}", d=str(df.index[-1].date()))
            )
        except Exception:
            pass

        # NYSE 最新営業日
        today = pd.Timestamp.today().normalize()
        latest_trading_day = get_latest_nyse_trading_day(today)
        try:
            msg = tr("🗓️ 直近のNYSE営業日: {d}", d=str(latest_trading_day.date()))
            _st_emit("write", msg)
        except Exception:
            pass

        # 1つ前の営業日（当日営業時間帯の影響を避ける）
        try:
            nyse = mcal.get_calendar("NYSE")
            sched = nyse.schedule(
                start_date=today - pd.Timedelta(days=7),
                end_date=today,
            )
            valid = pd.to_datetime(sched.index).normalize()
            prev_trading_day = valid[-2] if len(valid) >= 2 else latest_trading_day
        except Exception:
            prev_trading_day = latest_trading_day

        # 米東部時間を取得
        try:
            ny_time = pd.Timestamp.now(tz="America/New_York").time()
        except Exception:
            ny_time = dtime(18, 0)

        # 古い場合は警告のみ表示
        if df.index[-1].normalize() < prev_trading_day and ny_time >= dtime(18, 0):
            try:
                _st_emit("warning", tr("⚠ SPYキャッシュが古い可能性があります"))
            except Exception:
                pass
        else:
            try:
                _st_emit("write", tr("✅ SPYキャッシュは有効"))
            except Exception:
                pass

        return df

    except Exception as e:
        _st_emit("error", tr("❌ SPY読み込み失敗: {e}", e=str(e)))
        return None


def _normalize_to_naive_day(ts: pd.Timestamp | None) -> pd.Timestamp:
    """Normalize timestamp to tz-naive midnight."""

    if ts is None:
        ts = pd.Timestamp.now(tz=_NY_TIMEZONE)
    else:
        ts = pd.Timestamp(ts)
        tz = getattr(ts, "tzinfo", None)
        if tz is not None:
            try:
                ts = ts.tz_convert(_NY_TIMEZONE)
            except (TypeError, ValueError, AttributeError):
                try:
                    ts = pd.Timestamp(ts.to_pydatetime().astimezone(_NY_TIMEZONE))
                except Exception:
                    ts = pd.Timestamp(ts.to_pydatetime().replace(tzinfo=None))
    tz = getattr(ts, "tzinfo", None)
    if tz is not None:
        try:
            ts = ts.tz_localize(None)
        except (TypeError, ValueError, AttributeError):
            try:
                ts = ts.tz_convert(None)
            except Exception:
                ts = pd.Timestamp(ts.to_pydatetime().replace(tzinfo=None))
    return ts.normalize()


def get_latest_nyse_trading_day(today: pd.Timestamp | None = None) -> pd.Timestamp:
    today_naive = _normalize_to_naive_day(today)
    try:
        cache_key = pd.Timestamp(today_naive).strftime("%Y-%m-%d")
    except Exception:
        cache_key = str(today_naive)
    cached = _LATEST_NYSE_CACHE.get(cache_key)
    if cached is not None:
        return cached
    nyse = _get_nyse_calendar()
    sched = nyse.schedule(
        start_date=today_naive - pd.Timedelta(days=7),
        end_date=today_naive + pd.Timedelta(days=1),
    )
    valid_days = pd.to_datetime(sched.index).normalize()
    latest = valid_days[valid_days <= today_naive].max()
    try:
        _LATEST_NYSE_CACHE[cache_key] = pd.Timestamp(latest).normalize()
    except Exception:
        _LATEST_NYSE_CACHE[cache_key] = latest
    return latest


def get_next_nyse_trading_day(current: pd.Timestamp | None = None) -> pd.Timestamp:
    """NY証券取引所の翌営業日を返す。"""
    current_naive = _normalize_to_naive_day(current)
    try:
        cache_key = pd.Timestamp(current_naive).strftime("%Y-%m-%d")
    except Exception:
        cache_key = str(current_naive)
    cached = _NEXT_NYSE_CACHE.get(cache_key)
    if cached is not None:
        return cached

    nyse = _get_nyse_calendar()
    sched = nyse.schedule(
        start_date=current_naive,
        end_date=current_naive + pd.Timedelta(days=10),
    )
    valid_days = pd.to_datetime(sched.index).normalize()
    future_days = valid_days[valid_days > current_naive]
    if future_days.empty:
        raise ValueError("No upcoming NYSE trading day found")
    nxt = future_days.min()
    try:
        _NEXT_NYSE_CACHE[cache_key] = pd.Timestamp(nxt).normalize()
    except Exception:
        _NEXT_NYSE_CACHE[cache_key] = nxt
    return nxt


def get_signal_target_trading_day(now: pd.Timestamp | None = None) -> pd.Timestamp:
    """Determine the trading day to target when running today's signal extraction."""

    def _ensure_ny_timestamp(ts: pd.Timestamp | None) -> pd.Timestamp:
        if ts is None:
            return pd.Timestamp.now(tz="America/New_York")
        raw = pd.Timestamp(ts)
        tzinfo = getattr(raw, "tzinfo", None)
        if tzinfo is None:
            try:
                localized = raw.tz_localize(
                    "America/New_York", ambiguous="NaT", nonexistent="NaT"
                )
                if pd.isna(localized):
                    raise ValueError
                return localized
            except Exception:
                return raw.tz_localize("UTC").tz_convert("America/New_York")
        try:
            return raw.tz_convert("America/New_York")
        except Exception:
            return raw.tz_localize("America/New_York")

    try:
        ny_now = _ensure_ny_timestamp(now)
    except Exception:
        ny_now = pd.Timestamp.now(tz="America/New_York")

    latest = get_latest_nyse_trading_day(ny_now)
    target = latest

    try:
        ny_date = ny_now.date()
    except Exception:
        ny_date = None
    try:
        latest_date = pd.Timestamp(latest).date()
    except Exception:
        latest_date = None
    try:
        ny_time = ny_now.time()
    except Exception:
        ny_time = None

    need_next = False
    if ny_date is not None and latest_date is not None and ny_date > latest_date:
        need_next = True
    if ny_time is not None and ny_time >= dtime(16, 0):
        need_next = True

    if need_next:
        try:
            target = get_next_nyse_trading_day(latest)
        except Exception:
            target = latest

    return pd.Timestamp(target).normalize()


# --- Lightweight NYSE calendar memoization and helpers ---
_NYSE_SCHEDULE_CACHE: dict[tuple[pd.Timestamp, pd.Timestamp], pd.DatetimeIndex] = {}


def get_nyse_valid_days(
    start: pd.Timestamp, end: pd.Timestamp, use_cache: bool = True
) -> pd.DatetimeIndex:
    """Return normalized NYSE trading days between [start, end].

    Caches results per (start,end) to avoid repeated calendar queries in a single run.
    """
    start_n = pd.Timestamp(start).normalize()
    end_n = pd.Timestamp(end).normalize()
    key = (start_n, end_n)
    if use_cache and key in _NYSE_SCHEDULE_CACHE:
        return _NYSE_SCHEDULE_CACHE[key]
    try:
        import pandas_market_calendars as mcal

        nyse = mcal.get_calendar("NYSE")
        sched = nyse.schedule(start_date=start_n, end_date=end_n)
        valid = pd.to_datetime(sched.index).normalize()
    except Exception:
        # Fallback: empty valid days if calendar fails
        valid = pd.DatetimeIndex([])
    if use_cache:
        _NYSE_SCHEDULE_CACHE[key] = valid
    return valid


def calculate_trading_days_lag(
    cache_date: pd.Timestamp, target_date: pd.Timestamp
) -> int:
    """Calculate trading-day lag from cache_date to target_date (0 if same/younger)."""
    cache_n = pd.Timestamp(cache_date).normalize()
    target_n = pd.Timestamp(target_date).normalize()
    if cache_n >= target_n:
        return 0
    valid = get_nyse_valid_days(cache_n, target_n)
    # Count trading days strictly after cache_n up to and including target_n
    between = valid[(valid > cache_n) & (valid <= target_n)]
    return int(len(between))


def describe_trading_gap(cache_date: pd.Timestamp, target_date: pd.Timestamp) -> str:
    """Explain the gap between cache_date and target_date in human terms.

    Returns a short reason like: "理由: 週末 2日, 祝日 1日 → 営業日差 1日"
    """
    cache_n = pd.Timestamp(cache_date).normalize()
    target_n = pd.Timestamp(target_date).normalize()
    try:
        # Calendar range inclusive of endpoints
        all_days = pd.date_range(cache_n, target_n, freq="D")
        valid = set(get_nyse_valid_days(cache_n, target_n).date)
        weekend = 0
        holiday = 0
        for d in all_days:
            if d.normalize() == cache_n:
                continue
            if d.date() not in valid:
                if d.weekday() >= 5:
                    weekend += 1
                else:
                    holiday += 1
        lag = calculate_trading_days_lag(cache_n, target_n)
        parts = []
        if weekend:
            parts.append(f"週末 {weekend}日")
        if holiday:
            parts.append(f"祝日 {holiday}日")
        cause = ", ".join(parts) if parts else "非営業日あり"
        return f"理由: {cause} → 営業日差 {lag}日"
    except Exception:
        delta = max(0, int((target_n - cache_n).days))
    return f"理由: 暦日差 {delta}日（営業日判定に失敗）"


@overload
def resolve_signal_entry_date(base_date: None) -> pd.Timestamp: ...


@overload
def resolve_signal_entry_date(
    base_date: int | float | str | pd.Timestamp,
) -> pd.Timestamp: ...


def resolve_signal_entry_date(
    base_date: int | float | str | pd.Timestamp | None,
) -> pd.Timestamp:
    """シグナル日から翌営業日（取引予定日）を算出する。

    - base_date が欠損・変換不可の場合は NaT を返す。
    - get_next_nyse_trading_day の結果は常に tz-naive な日付に正規化する。
    """

    if base_date is None or (isinstance(base_date, float) and pd.isna(base_date)):
        return pd.NaT
    try:
        ts = pd.Timestamp(base_date)
    except Exception:
        return pd.NaT
    if pd.isna(ts):
        return pd.NaT
    ts = _normalize_to_naive_day(ts)
    try:
        entry_candidate = get_next_nyse_trading_day(ts)
    except Exception:
        return pd.NaT
    if pd.isna(entry_candidate):
        return pd.NaT
    entry_ts = pd.Timestamp(entry_candidate)
    try:
        entry_ts = entry_ts.tz_localize(None)
    except Exception:
        pass
    return entry_ts.normalize()


def get_spy_data_cached(folder: str = "data_cache"):
    """
    旧バージョンの SPY キャッシュ読み込み関数。
    - v2 リゾルバを使用（backtest: base→full_backup, today: rolling→base→full_backup）
    """
    # v2 実装へ委譲（探索順切替は v2 に集約）
    try:
        return get_spy_data_cached_v2(folder)
    except Exception:
        # v2 側に一本化するため、従来の data_cache 直下フォールバックは廃止
        return get_spy_data_cached_v2(folder)


def _find_spy_csv(dir_path: Path) -> Path | None:
    try:
        if not dir_path.exists():
            return None
        for fn in os.listdir(dir_path):
            if fn.lower() == "spy.csv":
                return dir_path / fn
    except Exception:
        return None
    return None


def _persist_spy_with_indicators(spy_df: pd.DataFrame) -> None:
    """Persist augmented SPY data to rolling/base cache for reuse."""

    if spy_df is None or getattr(spy_df, "empty", True):
        return
    try:
        settings = get_settings(create_dirs=True)
        root = Path(settings.DATA_CACHE_DIR)
        rolling_dir = Path(getattr(settings.cache, "rolling_dir", root / "rolling"))
        base_dir = root / "base"
    except Exception:
        root = Path("data_cache")
        rolling_dir = root / "rolling"
        base_dir = root / "base"

    for target_dir in (rolling_dir, base_dir):
        path = _find_spy_csv(target_dir)
        if path is None or not path.exists():
            continue
        try:
            df_to_save = spy_df.copy()
            if "Date" in df_to_save.columns:
                df_to_save["Date"] = pd.to_datetime(
                    df_to_save["Date"], errors="coerce"
                ).dt.normalize()
                df_to_save = df_to_save.dropna(subset=["Date"]).sort_values("Date")
                try:
                    settings = get_settings(create_dirs=True)
                    round_dec = getattr(settings.cache, "round_decimals", None)
                except Exception:
                    round_dec = None
                try:
                    out_df = round_dataframe(df_to_save, round_dec)
                except Exception:
                    out_df = df_to_save
                out_df.to_csv(path, index=False)
            else:
                idx = pd.to_datetime(df_to_save.index, errors="coerce").normalize()
                df_to_save = df_to_save.loc[~idx.isna()].copy()
                df_to_save.index = pd.Index(idx[~idx.isna()])
                df_to_save.sort_index(inplace=True)
                try:
                    settings = get_settings(create_dirs=True)
                    round_dec = getattr(settings.cache, "round_decimals", None)
                except Exception:
                    round_dec = None
                try:
                    out_df = round_dataframe(df_to_save, round_dec)
                except Exception:
                    out_df = df_to_save
                out_df.to_csv(path, index_label="Date")
        except Exception:
            continue
        else:
            break


def get_spy_with_indicators(spy_df=None):
    """
    SPY に SMA100 / SMA200 を付与（フィルターは戦略側で判定する）
    """

    loaded_from_cache = False
    if spy_df is None:
        # Prefer today cache (rolling) for the latest values, fallback to base/full.
        loaded_from_cache = True
        spy_df = get_spy_data_cached_v2(mode="today")
        if spy_df is None:
            spy_df = get_spy_data_cached_v2()

    if spy_df is not None and not getattr(spy_df, "empty", True):
        if isinstance(spy_df, pd.Series):
            spy_df = spy_df.to_frame().T
        elif not isinstance(spy_df, pd.DataFrame):
            try:
                spy_df = pd.DataFrame(spy_df)
            except Exception:
                return spy_df

        spy_df = spy_df.copy()

        if isinstance(spy_df.columns, pd.MultiIndex):
            flattened_cols: list[str] = []
            for col in spy_df.columns:
                if isinstance(col, tuple):
                    flattened = next(
                        (part for part in col if part not in (None, "")),
                        None,
                    )
                    if flattened is None:
                        flattened = col[-1] if col else ""
                    flattened_cols.append(flattened)
                else:
                    flattened_cols.append(col)
            spy_df.columns = flattened_cols

        # Close 列名のばらつきに対応（close/AdjClose/adjusted_close 等）
        if "Close" not in spy_df.columns:
            if "close" in spy_df.columns:
                spy_df["Close"] = spy_df["close"]
            elif "AdjClose" in spy_df.columns:
                spy_df["Close"] = spy_df["AdjClose"]
            elif "adjclose" in spy_df.columns:
                spy_df["Close"] = spy_df["adjclose"]
            elif "adjusted_close" in spy_df.columns:
                spy_df["Close"] = spy_df["adjusted_close"]
            else:
                try:
                    _st_emit(
                        "warning",
                        tr(
                            "❗SPYの終値列が見つかりません: {cols}",
                            cols=str(list(spy_df.columns)),
                        ),
                    )
                except Exception:
                    pass
                return spy_df

        close_series = spy_df["Close"]
        if isinstance(close_series, pd.DataFrame):
            # DataFrame -> first column Series (only if shape[1] > 0)
            if close_series.shape[1] > 0:
                try:
                    first_col = close_series.columns[0]
                    close_series = close_series[first_col]
                except Exception:
                    close_series = close_series.squeeze()
            else:
                close_series = close_series.squeeze()

        if isinstance(close_series, pd.Series):
            if len(close_series) == len(spy_df.index):
                close_series = pd.Series(close_series.to_numpy(), index=spy_df.index)
            else:
                close_series = close_series.reindex(spy_df.index)
        else:
            try:
                close_series = pd.Series(close_series, index=spy_df.index)
            except Exception:
                close_series = pd.Series(close_series)
                if len(close_series) == len(spy_df.index):
                    close_series.index = spy_df.index
                else:
                    return spy_df

        close_numeric = pd.to_numeric(close_series, errors="coerce")

        spy_df["SMA100"] = SMAIndicator(
            close_numeric,
            window=100,
        ).sma_indicator()
        spy_df["SMA200"] = SMAIndicator(
            close_numeric,
            window=200,
        ).sma_indicator()

        if loaded_from_cache:
            try:
                _persist_spy_with_indicators(spy_df)
            except Exception:
                pass

    return spy_df
