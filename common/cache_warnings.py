# common/cache_warnings.py
"""
キャッシュ関連の警告・ログ集約処理
"""

from __future__ import annotations

from collections import defaultdict
import logging
import os
import threading

logger = logging.getLogger(__name__)


class RollingIssueAggregator:
    """
    rolling cache未整備ログを集約し、冗長出力を制御するクラス。

    環境変数:
    - COMPACT_TODAY_LOGS=1: 集約機能有効化
    - ROLLING_ISSUES_VERBOSE_HEAD=N: 先頭N件のみ詳細WARNING、以降はDEBUG
    """

    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if hasattr(self, "_initialized"):
            return
        self._initialized = True

        self._issues: dict[str, list[str]] = defaultdict(list)
        self._lock_issues = threading.Lock()
        self._compact_mode = os.getenv("COMPACT_TODAY_LOGS") == "1"
        self._verbose_head = int(os.getenv("ROLLING_ISSUES_VERBOSE_HEAD", "5"))
        self._count_by_category: dict[str, int] = defaultdict(int)

    def _refresh_config(self) -> None:
        """Refresh compact settings from environment if they changed."""
        compact = os.getenv("COMPACT_TODAY_LOGS") == "1"
        try:
            verbose_head = int(os.getenv("ROLLING_ISSUES_VERBOSE_HEAD", "5"))
        except Exception:
            verbose_head = self._verbose_head
        if compact != self._compact_mode:
            self._compact_mode = compact
        if verbose_head != self._verbose_head:
            self._verbose_head = verbose_head

    def report_issue(self, category: str, symbol: str, message: str = "") -> None:
        """
        rolling cache の未整備問題を報告する。

        Args:
            category: 問題カテゴリ（例: "missing_rolling", "insufficient_data"）
            symbol: 対象シンボル
            message: 追加メッセージ（省略可）
        """
        # Allow runtime changes of compact settings
        try:
            self._refresh_config()
        except Exception:
            pass
        if not self._compact_mode:
            # 通常モードでも has_issue() 判定を可能にするため内部リストへ登録する
            # （compact でない場合は冗長制御は不要だが、重複抑制ロジックが利用する）
            try:
                with self._lock_issues:
                    self._issues[category].append(symbol)
                    self._count_by_category[category] += 1
            except Exception:
                # 記録失敗してもログ出力自体は継続
                pass

            # 通常モード: insufficient_data は DEBUG、その他は WARNING
            full_msg = f"[{category}] {symbol}"
            if message:
                full_msg += f": {message}"
            if category == "insufficient_data":
                logger.debug(full_msg)
            else:
                logger.warning(full_msg)
            return

        # コンパクトモード: 集約処理
        with self._lock_issues:
            self._issues[category].append(symbol)
            self._count_by_category[category] += 1
            count = self._count_by_category[category]

            if count <= self._verbose_head:
                # 先頭N件のみ詳細WARNING
                full_msg = f"[{category}] {symbol}"
                if message:
                    full_msg += f": {message}"
                logger.warning(full_msg)
            else:
                # N件を超えたらDEBUGレベル
                full_msg = f"[{category}] {symbol}"
                if message:
                    full_msg += f": {message}"
                logger.debug(full_msg)

    # -------------------- public query helpers -------------------- #

    def has_issue(self, category: str, symbol: str) -> bool:
        """指定カテゴリでシンボルが既に報告済みかを返す。

        コンパクトモード/通常モードいずれでも機能するように、
        通常モード時は O(1) 判定用に内部キャッシュへも登録する。
        """
        try:
            if category in self._issues:
                if symbol in self._issues[category]:
                    return True
            return False
        except Exception:
            return False

    def already_reported(
        self, category: str, symbol: str
    ) -> bool:  # backward-friendly alias
        """`has_issue` のエイリアス。外部呼び出しの可読性を重視。"""
        return self.has_issue(category, symbol)

    def _output_summary(self) -> None:
        """集約結果をサマリー出力する（プロセス終了時など）。"""
        if not self._compact_mode or not self._issues:
            return

        logger.info("=== Rolling Cache Issues Summary ===")
        for category, symbols in self._issues.items():
            count = len(symbols)
            if count <= 3:
                symbol_list = ", ".join(symbols)
            else:
                symbol_list = f"{', '.join(symbols[:3])}, ... ({count} total)"
            logger.info(f"[{category}]: {symbol_list}")


# グローバルインスタンス
_rolling_issue_aggregator = RollingIssueAggregator()


def report_rolling_issue(category: str, symbol: str, message: str = "") -> None:
    """
    rolling cache の未整備問題をグローバルアグリゲーターに報告する。

    Args:
        category: 問題カテゴリ（例: "missing_rolling", "insufficient_data"）
        symbol: 対象シンボル
        message: 追加メッセージ（省略可）
    """
    _rolling_issue_aggregator.report_issue(category, symbol, message)


def get_rolling_issue_aggregator() -> RollingIssueAggregator:
    """グローバルアグリゲーターインスタンスを取得。"""
    return _rolling_issue_aggregator
