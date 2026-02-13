# ============================================================================
# 🧠 Context Note
# このファイルは core/system2.py（ショート RSI スパイク）を UI 用に適応させるラッパー層
#
# 前提条件：
#   - ロジック本体は core/system2.py。このファイルは orchestration のみ
#   - Alpaca 発注対応。当日信号と backtest の両対応
#   - RSI3 > 90 の過熱サインが前提
#   - 最終配分は finalize_allocation() で一元化
#
# ロジック単位：
#   generate_signals()    → prepare_data + generate_candidates を順序実行
#   apply_allocation()    → 当日配分情報をまとめて渡す
#   _build_diagnostics()  → setup count など診断情報構築
#
# Copilot へ：
#   → core のロジック変更は core/system2.py で実施
#   → ショート戦略のため 2 日連続上昇ロジックは必須条件
#   → setup 条件の変更は制御テストで必ず確認
# ============================================================================

# strategies/system2_strategy.py
from __future__ import annotations

import numpy as np
import pandas as pd

from common.alpaca_order import AlpacaOrderMixin
from common.system_diagnostics import (
    SystemDiagnosticSpec,
    build_system_diagnostics,
    numeric_greater_than,
)
from core.system2 import (
    generate_candidates_system2,
    get_total_days_system2,
    prepare_data_vectorized_system2,
)

from .base_strategy import StrategyBase
from .constants import (
    ENTRY_MIN_GAP_PCT_DEFAULT,
    PROFIT_TAKE_PCT_DEFAULT_4,
    STOP_ATR_MULTIPLE_DEFAULT,
)


class System2Strategy(AlpacaOrderMixin, StrategyBase):
    SYSTEM_NAME = "system2"

    def get_trading_side(self) -> str:
        """System2 はショート戦略"""
        return "short"

    # -------------------------------
    # データ準備（共通コアへ委譲）
    # -------------------------------
    def prepare_data(
        self,
        raw_data_or_symbols,
        reuse_indicators: bool | None = None,
        **kwargs,
    ):
        """System2のデータ準備（共通テンプレート使用）"""
        return self._prepare_data_template(
            raw_data_or_symbols,
            prepare_data_vectorized_system2,
            reuse_indicators=reuse_indicators,
            **kwargs,
        )

    # -------------------------------
    # 候補生成（共通コアへ委譲）
    # -------------------------------
    def generate_candidates(self, data_dict, market_df=None, **kwargs):
        """候補生成（共通メソッド使用）"""
        top_n = self._get_top_n_setting(kwargs.get("top_n"))
        latest_only = bool(kwargs.get("latest_only", False))
        try:  # noqa: SIM105
            from common.perf_snapshot import get_global_perf

            _perf = get_global_perf()
            if _perf is not None:
                _perf.mark_system_start(self.SYSTEM_NAME)
        except Exception:  # pragma: no cover
            pass
        result = generate_candidates_system2(
            data_dict,
            top_n=top_n,
            latest_only=latest_only,
            include_diagnostics=True,
        )
        if isinstance(result, tuple) and len(result) == 3:
            candidates_by_date, merged_df, diagnostics = result
            self.last_diagnostics = diagnostics
            result = (candidates_by_date, merged_df)
        elif isinstance(result, tuple) and len(result) == 2:
            candidates_by_date, merged_df = result
            self.last_diagnostics = build_system_diagnostics(
                self.SYSTEM_NAME,
                data_dict,
                candidates_by_date,
                top_n=top_n,
                latest_only=latest_only,
                spec=SystemDiagnosticSpec(
                    rank_metric_name="adx7",
                    rank_predicate=numeric_greater_than("adx7", 0.0),
                ),
            )
            result = (candidates_by_date, merged_df)
        else:
            self.last_diagnostics = None
        try:  # noqa: SIM105
            from common.perf_snapshot import get_global_perf as _gpf

            _p2 = _gpf()
            if _p2 is not None:
                candidate_count = self._compute_candidate_count(result)
                _p2.mark_system_end(
                    self.SYSTEM_NAME,
                    symbol_count=len(data_dict or {}),
                    candidate_count=candidate_count,
                )
        except Exception:  # pragma: no cover
            pass
        return result

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

    # -------------------------------
    # 共通シミュレーター用フック（System2ルール）
    # -------------------------------
    def compute_entry(self, df: pd.DataFrame, candidate: dict, _current_capital: float):
        """
        エントリー価格とストップを返す（ショート）。
        - 前日終値の +4% 指値ショート（当日高値が指値に到達した場合のみ約定）
        - 高寄りしていれば、より有利な寄付価格で約定

        Args:
            df: 価格データ
            candidate: エントリー候補情報
            _current_capital: 現在資本（未使用、インターフェース互換性のため）

        Returns:
            (entry_price, stop_price) または None
        """
        try:
            entry_loc = df.index.get_loc(candidate["entry_date"])
        except Exception:
            return None
        if isinstance(entry_loc, slice) or isinstance(entry_loc, np.ndarray):
            return None
        if not isinstance(entry_loc, int | np.integer):
            return None
        entry_idx = int(entry_loc)
        if entry_idx <= 0 or entry_idx >= len(df):
            return None
        prior_close = float(df.iloc[entry_idx - 1]["Close"])
        min_gap = float(self.config.get("entry_min_gap_pct", ENTRY_MIN_GAP_PCT_DEFAULT))
        limit_price = prior_close * (1 + min_gap)
        try:
            open_px = float(df.iloc[entry_idx]["Open"])
            high_px = float(df.iloc[entry_idx]["High"])
        except Exception:
            return None

        # 指値未到達なら不成立
        if high_px < limit_price:
            return None

        # ショート売り指値: 高寄りなら寄付で有利約定、そうでなければ指値約定
        entry_price = open_px if open_px >= limit_price else limit_price

        atr = None
        for col in ("atr10", "ATR10"):
            try:
                atr = float(df.iloc[entry_idx - 1][col])
                break
            except Exception:
                continue
        if atr is None:
            return None
        stop_mult = float(
            self.config.get("stop_atr_multiple", STOP_ATR_MULTIPLE_DEFAULT)
        )
        stop_price = entry_price + stop_mult * atr
        return entry_price, stop_price

    def compute_exit(
        self,
        df: pd.DataFrame,
        entry_idx: int,
        entry_price: float,
        stop_price: float,
    ):
        """利確/損切りロジック。
        - ストップ到達: その日の高値>=stop で当日決済
        - 利確到達: 前日終値で判定し、翌日大引けで決済
        - 未達: 2営業日待っても利確に届かない場合は3日目の大引けで決済
        返り値: (exit_price, exit_date)
        """
        profit_take_pct = float(
            self.config.get("profit_take_pct", PROFIT_TAKE_PCT_DEFAULT_4)
        )
        # System2 spec: evaluate target up to "2 days after entry", otherwise exit
        # at the following close.
        max_hold_days = int(self.config.get("max_hold_days", 2))

        for offset in range(max_hold_days + 1):
            idx = entry_idx + offset
            if idx >= len(df):
                break
            row = df.iloc[idx]
            # ストップ到達（ショート）
            if float(row["High"]) >= stop_price:
                return stop_price, df.index[idx]
            # 利確判定（ショートの含み益）
            gain = (entry_price - float(row["Close"])) / entry_price
            if gain >= profit_take_pct:
                exit_idx = min(idx + 1, len(df) - 1)
                return float(df.iloc[exit_idx]["Close"]), df.index[exit_idx]

        exit_idx = min(entry_idx + max_hold_days + 1, len(df) - 1)
        return float(df.iloc[exit_idx]["Close"]), df.index[exit_idx]

    def compute_pnl(self, entry_price: float, exit_price: float, shares: int) -> float:
        """ショートのPnL - 基底クラスのメソッドを使用。"""
        return self.compute_pnl_short(entry_price, exit_price, shares)

    # --- テスト用の最小RSI3計算 ---
    def prepare_minimal_for_test(self, raw_data_dict: dict) -> dict:
        out = {}
        for sym, df in raw_data_dict.items():
            x = df.copy()
            close = x["Close"].astype(float)
            delta = close.diff()
            gain = delta.clip(lower=0).rolling(3).mean()
            loss = -delta.clip(upper=0).rolling(3).mean()
            rs = gain / loss.replace(0, pd.NA)
            x["rsi3"] = 100 - (100 / (1 + rs))
            out[sym] = x
        return out

    def get_total_days(self, data_dict: dict) -> int:
        return get_total_days_system2(data_dict)
