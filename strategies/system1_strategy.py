# ============================================================================
# 🧠 Context Note
# このファイルは core/system1.py を Streamlit UI 用に適応させるラッパー層。バックテスト＆当日実行両対応
#
# 前提条件：
#   - UI からのシグナル呼び出しフロー: symbol list → setup → rank → signals
#   - ロジックの本体は core/system1.py。このファイルは orchestration のみ
#   - Alpaca 発注対応。YAML 設定経由のパラメータ注入
#   - 最終配分は finalize_allocation() で一元化（API 契約厳守）
#
# ロジック単位：
#   generate_signals() → prepare_data + generate_candidates を順序実行
#   apply_allocation() → 当日配分・ポジション情報をまとめて finalize_allocation() へ
#   prepare_data()    → キャッシュから指標ロード
#
# Copilot へ：
#   → core のロジック変更は core/system1.py で実施（このファイルは変更禁止）
#   → finalize_allocation() API 契約は変更するな
#   → UI 用の検証は簡潔に。複雑な検査は core に任せる
# ============================================================================

"""System1 strategy wrapper class using shared core functions.

This class integrates with YAML-driven settings for backtest parameters
and relies on StrategyBase to inject risk/system-specific config.  As an
extension example, Alpaca 発注処理も組み込み、バックテストと実売双方に
対応できるようにする。
"""

from __future__ import annotations

from typing import Any, cast

import pandas as pd

from common.alpaca_order import AlpacaOrderMixin
from core.system1 import (
    generate_candidates_system1,
    get_total_days_system1,
    prepare_data_vectorized_system1,
)

from .base_strategy import StrategyBase
from .constants import STOP_ATR_MULTIPLE_SYSTEM1


def _normalize_daily_index(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    x = df.copy()
    if "Date" in x.columns:
        idx = pd.to_datetime(x["Date"], errors="coerce").dt.normalize()
    elif "date" in x.columns:
        idx = pd.to_datetime(x["date"], errors="coerce").dt.normalize()
    else:
        idx = pd.to_datetime(x.index, errors="coerce").normalize()
    x.index = pd.Index(idx, name="Date")
    x = x[~x.index.isna()]
    try:
        x = x.sort_index()
        if getattr(x.index, "has_duplicates", False):
            x = x[~x.index.duplicated(keep="last")]
    except Exception:
        pass
    return x


def _find_col_ci(df: pd.DataFrame, expected: str) -> str | None:
    target = str(expected).lower()
    for col in df.columns:
        if str(col).lower() == target:
            return str(col)
    return None


def _build_spy_gate_map(spy_df: pd.DataFrame | None, *, sma_window: int) -> dict[pd.Timestamp, bool]:
    if spy_df is None or spy_df.empty:
        return {}
    x = _normalize_daily_index(spy_df)
    if x.empty:
        return {}
    close_col = _find_col_ci(x, "Close")
    if close_col is None:
        return {}
    close = pd.to_numeric(x[close_col], errors="coerce")
    sma_col = _find_col_ci(x, f"sma{sma_window}")
    if sma_col is not None:
        sma = pd.to_numeric(x[sma_col], errors="coerce")
    else:
        sma = close.rolling(sma_window, min_periods=sma_window).mean()
    gate = (close > sma) & close.notna() & sma.notna()
    return {pd.Timestamp(dt).normalize(): bool(val) for dt, val in gate.items()}


def _extract_signal_date(candidate: dict[str, Any], entry_dt: pd.Timestamp) -> pd.Timestamp | None:
    raw = candidate.get("date", candidate.get("Date", None))
    if raw is None:
        raw = entry_dt
    ts = pd.to_datetime(raw, errors="coerce")
    if pd.isna(ts):
        return None
    return pd.Timestamp(ts).normalize()


def _apply_spy_gate_to_candidates(
    candidates_by_date: dict,
    merged_df: pd.DataFrame | None,
    *,
    spy_gate: dict[pd.Timestamp, bool],
) -> tuple[dict, pd.DataFrame | None, int, int]:
    if not spy_gate:
        before = 0
        for entries in candidates_by_date.values():
            if isinstance(entries, dict):
                before += len(entries)
            elif isinstance(entries, list):
                before += len(entries)
        return candidates_by_date, merged_df, before, before

    filtered_by_date: dict = {}
    before_count = 0
    after_count = 0

    for key, entries in candidates_by_date.items():
        entry_dt = pd.to_datetime(key, errors="coerce")
        entry_dt = pd.Timestamp(entry_dt).normalize() if pd.notna(entry_dt) else pd.NaT

        if isinstance(entries, dict):
            before_count += len(entries)
            kept: dict = {}
            for sym, payload in entries.items():
                payload_map = payload if isinstance(payload, dict) else {}
                sig_dt = _extract_signal_date(payload_map, entry_dt)
                if sig_dt is not None and spy_gate.get(sig_dt, False):
                    kept[sym] = payload
            if kept:
                filtered_by_date[key] = kept
                after_count += len(kept)
        elif isinstance(entries, list):
            before_count += len(entries)
            kept_list: list = []
            for item in entries:
                if not isinstance(item, dict):
                    continue
                sig_dt = _extract_signal_date(item, entry_dt)
                if sig_dt is not None and spy_gate.get(sig_dt, False):
                    kept_list.append(item)
            if kept_list:
                filtered_by_date[key] = kept_list
                after_count += len(kept_list)

    filtered_df: pd.DataFrame | None = merged_df
    if merged_df is not None and not merged_df.empty:
        x = merged_df.copy()
        if "date" in x.columns:
            sig = pd.to_datetime(x["date"], errors="coerce").dt.normalize()
        elif "Date" in x.columns:
            sig = pd.to_datetime(x["Date"], errors="coerce").dt.normalize()
        else:
            sig = pd.to_datetime(x.index, errors="coerce").normalize()
        mask = sig.map(lambda v: bool(spy_gate.get(pd.Timestamp(v), False)) if pd.notna(v) else False)
        filtered_df = x[mask].copy()
        if filtered_df.empty:
            filtered_df = x.iloc[0:0].copy()

    return filtered_by_date, filtered_df, before_count, after_count


class System1Strategy(AlpacaOrderMixin, StrategyBase):
    SYSTEM_NAME = "system1"

    def __init__(self) -> None:
        super().__init__()

    def prepare_data(
        self,
        raw_data_or_symbols: dict | list[str],
        reuse_indicators: bool | None = None,
        **kwargs: Any,
    ) -> dict:
        """System1のデータ準備（共通テンプレート + フォールバック対応）"""
        return cast(
            dict,
            self._prepare_data_template(
                raw_data_or_symbols,
                prepare_data_vectorized_system1,
                reuse_indicators=reuse_indicators,
                **kwargs,
            ),
        )

    def generate_candidates(self, data_dict, market_df=None, **kwargs):
        """候補生成（共通メソッド使用）"""
        top_n = self._get_top_n_setting(kwargs.get("top_n"))
        latest_only = bool(kwargs.get("latest_only", False))

        # Extract progress/log callbacks from kwargs if present
        progress_callback = kwargs.get("progress_callback", kwargs.get("on_progress"))
        log_callback = kwargs.get("log_callback", kwargs.get("on_log"))

        # perf snapshot 計測（存在しない場合はノーオペ）
        try:  # noqa: SIM105
            from common.perf_snapshot import get_global_perf

            _perf = get_global_perf()
            if _perf is not None:
                _perf.mark_system_start(self.SYSTEM_NAME)
        except Exception:  # pragma: no cover
            pass
        # 未知の追加キーワード（latest_mode_date / max_date_lag_days 等）もコアへ透過
        # ただし、明示引数として渡すキーは衝突を避けるため除外
        extra_kwargs = dict(kwargs)
        for k in (
            "latest_only",
            "top_n",
            "progress_callback",
            "on_progress",
            "log_callback",
            "on_log",
        ):
            if k in extra_kwargs:
                extra_kwargs.pop(k, None)
        result = generate_candidates_system1(
            data_dict,
            top_n=top_n,
            latest_only=latest_only,
            progress_callback=progress_callback,
            log_callback=log_callback,
            **extra_kwargs,
        )

        spy_df = market_df
        if (spy_df is None or getattr(spy_df, "empty", True)) and isinstance(data_dict, dict):
            maybe_spy = data_dict.get("SPY")
            if isinstance(maybe_spy, pd.DataFrame):
                spy_df = maybe_spy
        spy_gate = _build_spy_gate_map(spy_df, sma_window=100)

        if isinstance(result, tuple) and len(result) == 3:
            candidates_by_date, merged_df, diagnostics = result
            (
                candidates_by_date,
                merged_df,
                before_cnt,
                after_cnt,
            ) = _apply_spy_gate_to_candidates(candidates_by_date, merged_df, spy_gate=spy_gate)
            diagnostics["spy_gate_condition"] = "SPY close > SMA100"
            diagnostics["spy_gate_total_candidates_before"] = int(before_cnt)
            diagnostics["spy_gate_total_candidates_after"] = int(after_cnt)
            diagnostics["spy_gate_dropped"] = int(max(0, before_cnt - after_cnt))
            diagnostics["ranked_top_n_count"] = int(after_cnt)
            self.last_diagnostics = diagnostics
            if merged_df is not None:
                try:
                    merged_df.attrs["system1_diagnostics"] = diagnostics
                except Exception:
                    pass
            result_tuple = (candidates_by_date, merged_df)
            if log_callback and before_cnt != after_cnt:
                log_callback(
                    f"System1: SPY gate filtered {before_cnt - after_cnt} candidates "
                    f"(remaining={after_cnt})"
                )
        elif isinstance(result, tuple) and len(result) == 2:
            candidates_by_date, merged_df = result
            (
                candidates_by_date,
                merged_df,
                before_cnt,
                after_cnt,
            ) = _apply_spy_gate_to_candidates(candidates_by_date, merged_df, spy_gate=spy_gate)
            result_tuple = (candidates_by_date, merged_df)
            self.last_diagnostics = None
            if log_callback and before_cnt != after_cnt:
                log_callback(
                    f"System1: SPY gate filtered {before_cnt - after_cnt} candidates "
                    f"(remaining={after_cnt})"
                )
        else:  # Fallback for unexpected shapes
            self.last_diagnostics = None
            # 型が想定外の場合はそのまま返す（呼び出し側が安全に扱う）
            result_tuple = result
        try:  # noqa: SIM105
            from common.perf_snapshot import get_global_perf as _gpf

            _p2 = _gpf()
            if _p2 is not None:
                candidate_count = self._compute_candidate_count(result_tuple)
                _p2.mark_system_end(
                    self.SYSTEM_NAME,
                    symbol_count=len(data_dict or {}),
                    candidate_count=candidate_count,
                )
        except Exception:  # pragma: no cover
            pass
        return result_tuple

    def calculate_position_size(
        self,
        capital: float,
        entry_price: float,
        stop_price: float,
        *,
        risk_pct: float | None = None,
        max_pct: float | None = None,
        **kwargs,
    ) -> int:
        risk = self._resolve_pct(risk_pct, "risk_pct", 0.02)
        max_alloc = self._resolve_pct(max_pct, "max_pct", 0.10)
        return self._calculate_position_size_core(
            capital,
            entry_price,
            stop_price,
            risk,
            max_alloc,
        )

    def compute_entry(
        self,
        df: pd.DataFrame,
        candidate: dict,
        _current_capital: float,
    ) -> tuple[float, float] | None:
        """
        翌日寄り付きで成行仕掛けし、ATR20×5 を損切りに設定。

        Args:
            df: 価格データ
            candidate: エントリー候補情報
            _current_capital: 現在資本（未使用、インターフェース互換性のため）

        Returns:
            (entry_price, stop_price) または None
        """
        result = self._compute_entry_common(
            df,
            candidate,
            atr_column="atr20",
            stop_multiplier=self.config.get(
                "stop_atr_multiple",
                STOP_ATR_MULTIPLE_SYSTEM1,
            ),
        )
        if result is None:
            return None
        entry_price, stop_price, _ = result
        return entry_price, stop_price

    def get_total_days(self, data_dict: dict) -> int:
        return int(get_total_days_system1(data_dict))

    def compute_exit(
        self,
        df: pd.DataFrame,
        entry_idx: int,
        _entry_price: float,
        stop_price: float,
    ) -> tuple[float, pd.Timestamp]:
        """
        System1 exit for long trend-following:
        - Initial ATR stop (5*ATR20 below entry)
        - 25% trailing stop (ratchets upward with new highs)
        - No fixed profit target / no fixed max-hold day

        Args:
            df: 価格データ
            entry_idx: エントリーインデックス
            _entry_price: エントリー価格（未使用、インターフェース互換性のため）
            stop_price: ストップ価格

        Returns:
            (exit_price, exit_date): 決済価格と日付のタプル
        """
        n = len(df)
        if n == 0:
            return float(stop_price), pd.Timestamp.utcnow().normalize()
        if entry_idx < 0:
            entry_idx = 0
        if entry_idx >= n:
            entry_idx = n - 1

        trail_pct = float(self.config.get("trailing_pct", 0.25))
        base_stop = float(stop_price)
        try:
            highest = float(df.iloc[entry_idx]["High"])
        except Exception:
            highest = float(_entry_price) if _entry_price else float(stop_price)

        for idx in range(entry_idx, n):
            row = df.iloc[idx]
            try:
                high = float(row["High"])
                low = float(row["Low"])
            except Exception:
                continue

            if high > highest:
                highest = high

            if trail_pct > 0:
                trailing_stop = highest * (1.0 - trail_pct)
                effective_stop = max(base_stop, trailing_stop)
            else:
                effective_stop = base_stop

            if low <= effective_stop:
                return float(effective_stop), pd.Timestamp(str(df.index[idx]))

        return float(df.iloc[-1]["Close"]), pd.Timestamp(str(df.index[-1]))
