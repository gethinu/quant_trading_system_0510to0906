# ============================================================================
# 🧠 Context Note
# このファイルは System8（SPY オーバーナイト FOMC ドリフト）のロジック専門
#
# ⚠️ CRITICAL: System8 は SPY 固定・ロング専用・イベントカレンダー駆動。
#   指標クロスではなく「翌営業日が FOMC 声明日か」で setup を決める構造。
#   ロジック変更・他銘柄割当・イベント種別追加（CPI/NFP/QQQ 等）は禁止。
#
# 前提条件（凍結ルール v03 — 出所リポジトリ n0150_fomc_macro_event_drift_spy）：
#   - SPY のみ・ロングのみ・同時保有は1ポジション
#   - イベント源: data/events/fomc.csv の「予定された FOMC 声明日」のみ（年8回）。
#     臨時会合・電話会議・議事録公表日・非取引日開催は対象外（自動で除外）。
#   - エントリー: 声明日 T の前営業日 T-1 の引け（MOC）でロング
#   - エグジット: 声明日 T の寄り（MOO）で手仕舞い（14:00 ET 発表は持ち越さない）
#   - サイジング: イベントごとに等ノーショナル・無レバレッジ・ナンピン/マーチン禁止
#   - ストップ: なし（1泊のイベント保有。リスクはサイジングで制御）
#   - コスト: 往復 2bp（Alpaca 手数料 $0 + SPY スプレッド ~0.5-1bp/片道）
#
# ロジック単位：
#   prepare_data_vectorized_system8() → SPY データ + FOMC カレンダーから setup 付与
#   generate_candidates_system8()     → T-1 setup 日を候補化（等ノーショナル用 payload）
#
# Copilot へ：
#   → SPY 以外・イベント種別追加・レジームゲート等の「改良」は絶対に足すな（凍結 v03）
#   → 声明日が非取引日なら当該イベントは丸ごと落とす（メイクアップ日なし）
# ============================================================================

"""System8 core logic (SPY overnight FOMC pre-drift)。

System8 は SPY 専用のイベント駆動戦略のため、System1-6 の指標/セットアップ
テンプレート（フィルター→ランキング）には当てはまらない。「今日が予定 FOMC 声明日
の前営業日 (T-1) か」だけが setup 条件であり、実約定は T-1 の引け（MOC）と
声明日 T の寄り（MOO）で完結する 1 泊のオーバーナイト保有。

出所（別リポジトリ・監査証跡）:
    strategies/n0150_fomc_macro_event_drift_spy/rules_frozen.md (v03, GO_CANDIDATE)
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# === System8 configuration constants ===
SYSTEM8_SYMBOL = "SPY"  # 固定シンボル（ロング専用）
# 往復コスト（bp）。Alpaca 手数料 $0 + SPY スプレッド ~0.5-1bp/片道 = 2bp RT。
SYSTEM8_COST_BPS_ROUNDTRIP = 2.0
# 既定のイベントカレンダー（git 追跡の静的参照データ）。
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FOMC_CALENDAR_PATH = _PROJECT_ROOT / "data" / "events" / "fomc.csv"
# 「予定された声明」として扱う event_type（臨時会合等は含めない）。
_SCHEDULED_EVENT_TYPES = frozenset({"fomc"})


def load_fomc_event_dates(
    calendar_path: str | Path | None = None,
) -> pd.DatetimeIndex:
    """FOMC 予定声明日を正規化した DatetimeIndex で返す。

    Args:
        calendar_path: fomc.csv のパス。None なら DEFAULT_FOMC_CALENDAR_PATH。

    Returns:
        重複除去・昇順ソート済みの声明日 DatetimeIndex（tz-naive・normalize 済み）。
        ファイル欠損や読み込み失敗時は空の DatetimeIndex。
    """
    path = (
        Path(calendar_path) if calendar_path is not None else DEFAULT_FOMC_CALENDAR_PATH
    )
    if not path.exists():
        logger.warning("System8: FOMC calendar not found at %s", path)
        return pd.DatetimeIndex([])
    try:
        df = pd.read_csv(path)
    except Exception as e:  # pragma: no cover - 防御
        logger.warning("System8: failed to read FOMC calendar %s: %s", path, e)
        return pd.DatetimeIndex([])
    if "event_date" not in df.columns:
        logger.warning("System8: FOMC calendar missing 'event_date' column")
        return pd.DatetimeIndex([])
    # event_type があれば予定声明のみに絞る（無ければ全行を声明扱い）。
    if "event_type" in df.columns:
        type_norm = df["event_type"].astype(str).str.strip().str.lower()
        df = df[type_norm.isin(_SCHEDULED_EVENT_TYPES)]
    dates = pd.to_datetime(df["event_date"], errors="coerce").dropna()
    normalized = pd.DatetimeIndex(dates).normalize().unique().sort_values()
    return normalized


def _next_nyse_trading_day(ts: pd.Timestamp) -> pd.Timestamp:
    """``ts`` の翌 NYSE 取引日を返す（見つからなければ NaT）。

    実運用環境では repo 正準の ``common.utils_spy.resolve_signal_entry_date`` を使う
    （System7 と同一のカレンダー基準）。当該モジュールは指標スタック（ta 等）を
    import するため、それが利用できない環境では pandas_market_calendars に直接
    フォールバックする（System8 をイベント計算に対して自己完結にするため）。
    """
    ts = pd.Timestamp(ts).normalize()
    try:  # canonical path（本番環境）
        from common.utils_spy import resolve_signal_entry_date

        nxt = resolve_signal_entry_date(ts)
        if nxt is not None and not pd.isna(nxt):
            return pd.Timestamp(nxt).normalize()
    except Exception as e:  # pragma: no cover - 指標スタック未整備時のみ
        logger.debug("System8: utils_spy unavailable, using mcal fallback: %s", e)
    try:  # fallback: 市場カレンダー直接
        import pandas_market_calendars as mcal

        nyse = mcal.get_calendar("NYSE")
        sched = nyse.schedule(
            start_date=ts + pd.Timedelta(days=1),
            end_date=ts + pd.Timedelta(days=10),
        )
        valid = pd.to_datetime(sched.index).normalize()
        future = valid[valid > ts]
        if len(future) > 0:
            return pd.Timestamp(future.min()).normalize()
    except Exception as e:  # pragma: no cover - カレンダー未整備時
        logger.debug("System8: mcal next-trading-day failed: %s", e)
    return pd.NaT


def _normalized_index(df_raw: pd.DataFrame) -> pd.DataFrame:
    """入力 SPY DataFrame の index を tz-naive・normalize 済み日付に揃える。"""
    if "Date" in df_raw.columns:
        df = df_raw.copy()
        df.index = pd.Index(pd.to_datetime(df["Date"]).dt.normalize())
    else:
        df = df_raw.copy()
        df.index = pd.Index(pd.to_datetime(df.index).normalize())
    return df


def prepare_data_vectorized_system8(
    raw_data_dict: dict[str, pd.DataFrame] | None,
    *,
    fomc_calendar_path: str | Path | None = None,
    progress_callback: Callable[[int, int], None] | None = None,
    log_callback: Callable[[str], None] | None = None,
    skip_callback: Callable[[str], None] | None = None,
    reuse_indicators: bool = True,
    **kwargs: Any,
) -> dict[str, pd.DataFrame]:
    """SPY データに FOMC カレンダー由来の setup を付与して返す。

    System8 は指標を必要とせず、OHLC と FOMC カレンダーのみで setup を決める。
    付与カラム:
      - ``fomc_event``: その日が予定 FOMC 声明日 T か（取引日として index に存在する場合）
      - ``setup``: その日が声明日の前営業日 T-1 か（= エントリー実行日）
      - ``fomc_event_date``: setup 日に対応する声明日 T（それ以外は NaT）

    非取引日に落ちた声明日は SPY index に存在しないため、その T-1 も生成されず
    イベントごと丸ごと除外される（凍結ルール「メイクアップ日なし」を満たす）。
    """
    prepared_dict: dict[str, pd.DataFrame] = {}
    raw_data_dict = raw_data_dict or {}
    try:
        df_raw = raw_data_dict.get(SYSTEM8_SYMBOL)
        if df_raw is None:
            raise ValueError(f"{SYSTEM8_SYMBOL} data missing")
        df = _normalized_index(df_raw)

        event_dates = load_fomc_event_dates(fomc_calendar_path)

        n = len(df)
        setup = np.zeros(n, dtype=bool)
        fomc_event = np.zeros(n, dtype=bool)
        event_date_col: list[Any] = [pd.NaT] * n

        if n > 0 and len(event_dates) > 0:
            # index 上で FOMC 声明日に一致する行位置（= 取引日 T）。
            event_mask = df.index.isin(event_dates)
            fomc_event = np.asarray(event_mask, dtype=bool)
            event_positions = np.flatnonzero(event_mask)
            for pos in event_positions:
                if pos - 1 < 0:
                    # T-1 が履歴外（最初の行が声明日）→ エントリー不能につき除外。
                    continue
                setup[pos - 1] = True
                event_date_col[pos - 1] = df.index[pos]

            # 前方（ライブ/当日）エッジ: 最終行の翌 NYSE 取引日が予定 FOMC 声明日なら、
            # その声明日 T はまだ価格 index に現れていない（未来の暦日）ため上の
            # ヒストリカル判定では拾えない。最終行のみ市場カレンダーで T-1 を判定する。
            # （声明日が非取引日なら翌取引日が T をスキップ→ setup にならず、イベントは
            # 自然に除外される。）
            last_pos = n - 1
            if not setup[last_pos] and not fomc_event[last_pos]:
                nxt = _next_nyse_trading_day(df.index[last_pos])
                if nxt is not None and not pd.isna(nxt) and nxt in event_dates:
                    setup[last_pos] = True
                    event_date_col[last_pos] = nxt

        df["fomc_event"] = fomc_event
        df["setup"] = setup
        df["fomc_event_date"] = pd.Series(event_date_col, index=df.index)

        prepared_dict[SYSTEM8_SYMBOL] = df
    except Exception as e:
        logger.debug("System8: Failed to prepare data for %s: %s", SYSTEM8_SYMBOL, e)
        if skip_callback:
            try:
                skip_callback(f"{SYSTEM8_SYMBOL} の処理をスキップしました: {e}")
            except Exception:
                pass

    if log_callback:
        try:
            n_setup = (
                int(prepared_dict[SYSTEM8_SYMBOL]["setup"].sum())
                if SYSTEM8_SYMBOL in prepared_dict
                else 0
            )
            log_callback(
                f"SPY FOMC カレンダー適用完了（setup=T-1 引け, exit=T 寄り, "
                f"setup 日数={n_setup}）"
            )
        except Exception:
            pass
    if progress_callback:
        try:
            progress_callback(1, 1)
        except Exception:
            pass

    return prepared_dict


def _build_setup_payload(
    df: pd.DataFrame, setup_date: pd.Timestamp
) -> dict[str, object]:
    """setup 日（T-1）の候補 payload を作る。

    entry_price は setup 日終値（MOC の proxy。シグナル生成時点で確定済みの既知値で
    look-ahead ではない）。exit（T 寄り）は未来値のため backtest 側で参照する。
    """
    row = df.loc[setup_date]
    event_date = row.get("fomc_event_date")
    close_val = row.get("Close")
    payload: dict[str, object] = {
        "entry_date": setup_date,  # MOC 実行日（T-1）
        "event_date": event_date,  # FOMC 声明日 T
        "exit_date": event_date,  # MOO 実行日（= T）
        "entry_price": (float(close_val) if pd.notna(close_val) else None),
        "stop_price": None,  # ストップなし（1泊イベント保有）
    }
    return payload


def generate_candidates_system8(
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
    tuple[dict[pd.Timestamp, dict[str, dict[str, object]]], pd.DataFrame | None]
    | tuple[
        dict[pd.Timestamp, dict[str, dict[str, object]]],
        pd.DataFrame | None,
        dict[str, Any],
    ]
):
    """System8 候補を生成する。

    候補は「エントリー実行日（T-1）」でキーした dict-of-dicts
    ``{Timestamp(setup_date): {"SPY": payload}}`` で返す（System1-7 と同じ器）。
    System8 は SPY 単一・イベントごと最大1件のため top_n は実質無効。
    """
    diagnostics: dict[str, Any] = {
        "ranking_source": None,
        "setup_predicate_count": 0,
        "ranked_top_n_count": 0,
        "predicate_only_pass_count": 0,
        "mismatch_flag": 0,
    }

    def _ret(
        normalized: dict[pd.Timestamp, dict[str, dict[str, object]]],
        merged: pd.DataFrame | None,
    ):
        if progress_callback:
            try:
                progress_callback(1, 1)
            except Exception:
                pass
        if include_diagnostics:
            return (normalized, merged, diagnostics)
        return (normalized, merged)

    df = prepared_dict.get(SYSTEM8_SYMBOL) if prepared_dict else None
    if df is None or df.empty or "setup" not in df.columns:
        return _ret({}, None)

    # === Fast Path（当日シグナル抽出用）===
    if latest_only:
        last_date = df.index[-1]
        try:
            is_setup = bool(df.iloc[-1].get("setup", False))
        except Exception:
            is_setup = False
        if not is_setup:
            if log_callback:
                try:
                    log_callback(
                        f"System8: latest_only 0 candidates. "
                        f"date={pd.Timestamp(last_date).date()} setup=False"
                    )
                except Exception:
                    pass
            return _ret({}, None)
        payload = _build_setup_payload(df, pd.Timestamp(last_date))
        normalized = {pd.Timestamp(last_date): {SYSTEM8_SYMBOL: payload}}
        diagnostics["ranking_source"] = "latest_only"
        diagnostics["setup_predicate_count"] = 1
        diagnostics["ranked_top_n_count"] = 1
        df_fast = pd.DataFrame(
            [{"symbol": SYSTEM8_SYMBOL, "date": pd.Timestamp(last_date), "rank": 1}]
        )
        if log_callback:
            try:
                log_callback("System8: latest_only fast-path -> 1 candidate")
            except Exception:
                pass
        return _ret(normalized, df_fast)

    # === Full Historical Path（backtest）===
    try:
        setup_days = df.index[df["setup"].to_numpy(dtype=bool)]
    except Exception as e:
        logger.debug("System8: Failed to select setup days: %s", e)
        setup_days = pd.DatetimeIndex([])

    normalized_full: dict[pd.Timestamp, dict[str, dict[str, object]]] = {}
    for setup_date in setup_days:
        ts = pd.Timestamp(setup_date)
        payload = _build_setup_payload(df, ts)
        # 声明日 T が index 外（=非取引日に落ちた）の場合は候補化しない。
        event_date = payload.get("event_date")
        if event_date is None or pd.isna(event_date):
            continue
        normalized_full[ts] = {SYSTEM8_SYMBOL: payload}

    diagnostics["ranking_source"] = "full_scan"
    diagnostics["setup_predicate_count"] = len(normalized_full)
    diagnostics["ranked_top_n_count"] = 1 if normalized_full else 0

    if log_callback:
        try:
            log_callback(
                f"候補日数: {len(normalized_full)}（予定 FOMC 声明日の前営業日 T-1）"
            )
        except Exception:
            pass
    return _ret(normalized_full, None)


def get_total_days_system8(data_dict: dict[str, pd.DataFrame]) -> int:
    """データに含まれるユニーク日数（System7 と同じ集計）。"""
    all_dates: set[Any] = set()
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
    "SYSTEM8_SYMBOL",
    "SYSTEM8_COST_BPS_ROUNDTRIP",
    "DEFAULT_FOMC_CALENDAR_PATH",
    "load_fomc_event_dates",
    "prepare_data_vectorized_system8",
    "generate_candidates_system8",
    "get_total_days_system8",
]
