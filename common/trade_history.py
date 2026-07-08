"""Trade history tracking and persistence.

このモジュールはAlpacaへの注文送信履歴を永続化し、分析・監査に使用します。

Features:
    - JSONL形式での追記型ログ（data/trade_history.jsonl）
    - 注文成功/失敗の両方を記録
    - タイムスタンプ、システム、シンボル、数量、価格などを保存
"""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
from typing import Any

import pandas as pd


class TradeHistoryLogger:
    """Alpaca注文履歴のロガー"""

    def __init__(self, history_file: Path | str = Path("data/trade_history.jsonl")):
        """
        Args:
            history_file: 履歴を保存するJSONLファイルのパス
        """
        self.history_file = Path(history_file)
        self.history_file.parent.mkdir(parents=True, exist_ok=True)

    def log_orders(
        self,
        results_df: pd.DataFrame,
        *,
        paper_mode: bool = True,
        run_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """注文結果をJSONL形式で記録

        Args:
            results_df: submit_orders_df()の返り値
            paper_mode: ペーパートレードかどうか
            run_id: 実行ID（オプション）
            metadata: 追加メタデータ（オプション）
        """
        if results_df is None or results_df.empty:
            return

        timestamp = datetime.now().isoformat()
        run_id = run_id or f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

        with self.history_file.open("a", encoding="utf-8") as f:
            for _, row in results_df.iterrows():
                record = {
                    "timestamp": timestamp,
                    "run_id": run_id,
                    "paper_mode": paper_mode,
                    "symbol": str(row.get("symbol", "")),
                    "side": str(row.get("side", "")),
                    "qty": int(row.get("qty", 0)),
                    "price": (
                        float(row.get("price", 0.0))
                        if row.get("price") not in (None, "")
                        else None
                    ),
                    "order_id": str(row.get("order_id", "")),
                    "status": str(row.get("status", "")),
                    "system": str(row.get("system", "")),
                    "order_type": str(row.get("order_type", "")),
                    "time_in_force": str(row.get("time_in_force", "")),
                    "entry_date": str(row.get("entry_date", "")),
                    "error": str(row.get("error", "")) if row.get("error") else None,
                }

                # メタデータの追加
                if metadata:
                    record["metadata"] = metadata

                f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def get_recent_trades(
        self, limit: int = 100, paper_only: bool = False
    ) -> pd.DataFrame:
        """最近の注文履歴を取得

        Args:
            limit: 取得する件数上限
            paper_only: ペーパートレードのみに絞り込むか

        Returns:
            注文履歴のDataFrame
        """
        if not self.history_file.exists():
            return pd.DataFrame()

        records: list[dict[str, Any]] = []
        with self.history_file.open("r", encoding="utf-8") as f:
            for line in f:
                try:
                    record = json.loads(line.strip())
                    if paper_only and not record.get("paper_mode", True):
                        continue
                    records.append(record)
                except json.JSONDecodeError:
                    continue

        if not records:
            return pd.DataFrame()

        # 最新から limit 件取得
        df = pd.DataFrame(records[-limit:])
        return df

    def get_stats(self, days: int = 30, paper_only: bool = True) -> dict[str, Any]:
        """過去N日間の統計を取得

        Args:
            days: 集計対象の日数
            paper_only: ペーパートレードのみに絞り込むか

        Returns:
            統計情報の辞書
        """
        df = self.get_recent_trades(limit=10000, paper_only=paper_only)
        if df.empty:
            return {
                "total_orders": 0,
                "successful_orders": 0,
                "failed_orders": 0,
                "total_symbols": 0,
                "total_quantity": 0,
            }

        # 日付フィルタ
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        cutoff = datetime.now() - pd.Timedelta(days=days)
        df = df[df["timestamp"] >= cutoff]

        success_df = df[df["status"].str.contains("accept", case=False, na=False)]
        failed_df = df[df["error"].notna()]

        return {
            "total_orders": len(df),
            "successful_orders": len(success_df),
            "failed_orders": len(failed_df),
            "total_symbols": df["symbol"].nunique(),
            "total_quantity": df["qty"].sum(),
            "systems": df["system"].value_counts().to_dict(),
            "sides": df["side"].value_counts().to_dict(),
        }


# シングルトンインスタンス
_default_logger: TradeHistoryLogger | None = None


def get_trade_history_logger(
    history_file: Path | str | None = None,
) -> TradeHistoryLogger:
    """デフォルトのトレード履歴ロガーを取得

    Args:
        history_file: カスタムファイルパス（オプション）

    Returns:
        TradeHistoryLoggerインスタンス
    """
    global _default_logger
    if _default_logger is None or history_file is not None:
        _default_logger = TradeHistoryLogger(
            history_file or Path("data/trade_history.jsonl")
        )
    return _default_logger


__all__ = ["TradeHistoryLogger", "get_trade_history_logger"]
