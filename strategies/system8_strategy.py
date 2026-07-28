# ============================================================================
# 🧠 Context Note
# このファイルは core/system8.py（SPY オーバーナイト FOMC ドリフト）を UI/バックテスト
# 向けに適応させるラッパー層
#
# ⚠️ CRITICAL: System8 は SPY 固定・ロング専用・イベント駆動（凍結ルール v03）。
#   ロジック本体は core/system8.py。ここは orchestration とバックテスト約定のみ。
#
# 前提条件：
#   - エントリー: 声明日 T の前営業日 T-1 の引け（MOC）でロング
#   - エグジット: 声明日 T の寄り（MOO）で手仕舞い（1泊のオーバーナイト保有）
#   - サイジング: イベントごと等ノーショナル・無レバレッジ・ストップなし
#   - コスト: 往復 2bp
#
# Copilot へ：
#   → core のロジック変更は core/system8.py で実施
#   → ATR リスクサイジング/ストップは System8 には存在しない（等ノーショナル）
#   → イベント種別追加・レジームゲート等の「改良」は凍結 v03 につき禁止
# ============================================================================

"""System8 strategy — SPY overnight FOMC pre-drift (event-calendar driven)。

System1-6 の「広いユニバースから top-N を選ぶ」パターンにも、System7 の
「指標セットアップ + ATR ストップ」パターンにも当てはまらない。setup は
「翌営業日が予定 FOMC 声明日か」のみで、約定は T-1 引け → T 寄りの 1 泊。

出所: 別リポジトリ n0150_fomc_macro_event_drift_spy（rules_frozen.md v03,
GO_CANDIDATE）。監査証跡は docs/SYSTEM8_FOMC_DRIFT_MIGRATION_20260716.md を参照。
"""

from __future__ import annotations

import math
import time
from typing import Any

import pandas as pd

from common.alpaca_order import AlpacaOrderMixin
from core.system8 import (
    SYSTEM8_COST_BPS_ROUNDTRIP,
    SYSTEM8_SYMBOL,
    generate_candidates_system8,
    get_total_days_system8,
    prepare_data_vectorized_system8,
)

from .base_strategy import StrategyBase


class System8Strategy(AlpacaOrderMixin, StrategyBase):
    """SPY 専用のオーバーナイト FOMC プレドリフト（ロング）。

    - セットアップ: 翌営業日が予定 FOMC 声明日 T（= 当日は T-1）
    - エントリー: T-1 の引け（MOC）でロング
    - エグジット: T の寄り（MOO）
    - サイジング: 等ノーショナル（現在資本 × position_pct）、無レバレッジ、ストップなし
    - コスト: 往復 2bp
    """

    SYSTEM_NAME = "system8"

    def get_trading_side(self) -> str:
        """System8 はロング戦略。"""
        return "long"

    # ------------------------------------------------------------------
    # データ準備 / 候補生成
    # ------------------------------------------------------------------
    def prepare_data(
        self,
        raw_data_or_symbols: dict | list,
        reuse_indicators: bool | None = None,
        **kwargs,
    ) -> dict:
        """System8 のデータ準備（共通テンプレート使用）。

        core 側は指標を必要とせず OHLC + FOMC カレンダーのみで setup を付与する。
        """
        kwargs.pop("single_mode", None)
        return self._prepare_data_template(
            raw_data_or_symbols,
            prepare_data_vectorized_system8,
            reuse_indicators=reuse_indicators,
            **kwargs,
        )

    def generate_candidates(
        self,
        data_dict: dict,
        market_df: pd.DataFrame | None = None,
        **kwargs,
    ) -> tuple[dict, pd.DataFrame | None]:
        """T-1 setup 日を候補化して (candidates_by_date, merged_df) を返す。"""
        kwargs.pop("single_mode", None)
        result = generate_candidates_system8(
            data_dict,
            include_diagnostics=True,
            **kwargs,
        )
        if isinstance(result, tuple) and len(result) == 3:
            candidates_by_date, merged_df, diagnostics = result
            self.last_diagnostics = diagnostics
            return (candidates_by_date, merged_df)
        if isinstance(result, tuple) and len(result) == 2:
            self.last_diagnostics = None
            return result
        self.last_diagnostics = None
        return (result, None)

    # ------------------------------------------------------------------
    # サイジング（等ノーショナル / ストップなし）
    # ------------------------------------------------------------------
    def calculate_position_size(
        self,
        capital: float,
        entry_price: float,
        stop_price: float | None = None,
        *,
        risk_pct: float | None = None,
        max_pct: float | None = None,
        **kwargs,
    ) -> int:
        """等ノーショナルのポジションサイズ（株数）を返す。

        System8 にはストップが無いためリスク幅ベースのサイジングは使わない。
        現在資本 × position_pct を entry_price で割った株数（切り捨て）。
        position_pct は無レバレッジ前提で [0, 1] にクランプ（既定 1.0）。
        """
        capital_val = self._safe_float(capital)
        entry_val = self._safe_float(entry_price)
        if capital_val <= 0 or entry_val <= 0:
            return 0
        # 単一銘柄・同時1ポジション・無レバレッジのため既定は満額（1.0）。
        position_pct = self._resolve_pct(max_pct, "position_pct", 1.0)
        position_pct = max(0.0, min(1.0, position_pct))  # 無レバレッジ
        if position_pct == 0.0:
            return 0
        shares = math.floor((capital_val * position_pct) / entry_val)
        return max(int(shares), 0)

    # ------------------------------------------------------------------
    # バックテスト（T-1 引け → T 寄りの 1 泊オーバーナイト）
    # ------------------------------------------------------------------
    def run_backtest(
        self,
        data_dict: dict,
        candidates_by_date: dict,
        capital: float,
        **kwargs,
    ) -> pd.DataFrame:
        """イベントごとに 1 泊のオーバーナイト損益を積み上げる。

        同時保有は構造上発生しない（イベントは数週間以上離れている）。
        """
        results: list[dict] = []
        if SYSTEM8_SYMBOL not in data_dict:
            return pd.DataFrame()

        df: pd.DataFrame = data_dict[SYSTEM8_SYMBOL]
        if df is None or df.empty:
            return pd.DataFrame()

        on_progress = kwargs.get("on_progress")
        on_log = kwargs.get("on_log")
        start_time = time.time()

        cost_bps = float(
            self.config.get("cost_bps_roundtrip", SYSTEM8_COST_BPS_ROUNDTRIP)
        )
        cost_frac = cost_bps / 1e4  # 往復コスト（比率）

        capital_current = float(capital)
        items = sorted(candidates_by_date.items())
        total = len(items)

        for i, (setup_date, cand) in enumerate(items, 1):
            for payload in self._iter_payloads(cand):
                trade = self._simulate_event(
                    df, setup_date, payload, capital_current, cost_frac
                )
                if trade is None:
                    continue
                capital_current += float(trade["pnl"])
                results.append(trade)

            if on_progress:
                try:
                    on_progress(i, total, start_time)
                except Exception:
                    pass
            if on_log and (i % 10 == 0 or i == total):
                try:
                    on_log(f"💹 バックテスト進捗 {int(i)}/{int(total)} イベント")
                except Exception:
                    pass

        return pd.DataFrame(results)

    @staticmethod
    def _iter_payloads(cand: Any) -> list[dict]:
        """candidates_by_date の値（{"SPY": payload} / list / dict）を payload 列に正規化。"""
        if isinstance(cand, dict):
            # {"SPY": payload} 形式
            if SYSTEM8_SYMBOL in cand and isinstance(cand[SYSTEM8_SYMBOL], dict):
                return [cand[SYSTEM8_SYMBOL]]
            # {symbol: payload, ...} 形式
            payloads = [v for v in cand.values() if isinstance(v, dict)]
            if payloads:
                return payloads
            # payload 自体が dict のケース
            if "entry_date" in cand or "event_date" in cand:
                return [cand]
            return []
        if isinstance(cand, list):
            return [v for v in cand if isinstance(v, dict)]
        return []

    def _simulate_event(
        self,
        df: pd.DataFrame,
        setup_date: Any,
        payload: dict,
        capital_current: float,
        cost_frac: float,
    ) -> dict | None:
        """1 イベント分の約定を計算して結果 dict を返す（不成立時は None）。"""
        setup_ts = pd.Timestamp(setup_date)
        event_date = payload.get("event_date")
        if event_date is None or pd.isna(event_date):
            return None
        event_ts = pd.Timestamp(event_date)

        entry_idx = self._locate(df, setup_ts)
        exit_idx = self._locate(df, event_ts)
        if entry_idx < 0 or exit_idx < 0 or exit_idx <= entry_idx:
            return None

        try:
            entry_price = float(df.iloc[entry_idx]["Close"])  # MOC of T-1
            exit_price = float(df.iloc[exit_idx]["Open"])  # MOO of T
        except Exception:
            return None
        if entry_price <= 0 or exit_price <= 0:
            return None

        shares = self.calculate_position_size(capital_current, entry_price)
        if shares <= 0:
            return None

        gross_pnl = (exit_price - entry_price) * shares  # ロング
        cost = cost_frac * entry_price * shares  # 往復 2bp（entry ノーショナル基準）
        pnl = gross_pnl - cost
        return_pct = (pnl / capital_current * 100.0) if capital_current else 0.0

        return {
            "symbol": SYSTEM8_SYMBOL,
            "entry_date": df.index[entry_idx],
            "exit_date": df.index[exit_idx],
            "entry_price": round(entry_price, 4),
            "exit_price": round(exit_price, 4),
            "shares": int(shares),
            "pnl": round(pnl, 2),
            "return_%": round(return_pct, 4),
        }

    @staticmethod
    def _locate(df: pd.DataFrame, ts: pd.Timestamp) -> int:
        """df.index 内での ts の位置を返す（見つからなければ -1）。"""
        try:
            idxers = df.index.get_indexer([ts])
            return int(idxers[0]) if len(idxers) else -1
        except Exception:
            return -1

    def get_total_days(self, data_dict: dict) -> int:
        return int(get_total_days_system8(data_dict))
