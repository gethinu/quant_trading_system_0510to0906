"""SPY の日足データを取得し CacheManager を使って保存するスクリプト（エラー解析強化版）."""

# ruff: noqa: I001
import argparse
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
import pandas as pd
import requests

# Ensure project root is on sys.path so `import common` works when invoked directly.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import common  # noqa: F401
from common.cache_manager import CacheManager
from common.structured_logging import get_trading_logger
from common.trace_context import ProcessingPhase, trace_context
from common.trading_errors import (
    DataError,
    ErrorCode,
    ErrorContext,
    NetworkError,
    RetryPolicy,
    SystemError,
    TradingError,
    retry_with_backoff,
)
from config.settings import get_settings

# .envからAPIキー取得
load_dotenv()

# Initialize logger
logger = get_trading_logger()


def _log_trading_error_safe(trading_logger, err, context=None) -> None:
    """Guard against older logger implementations missing helper methods."""
    try:
        if hasattr(trading_logger, "log_trading_error"):
            trading_logger.log_trading_error(err, context)
            return
    except Exception:
        pass
    try:
        if hasattr(trading_logger, "log_error"):
            trading_logger.log_error(err, context)
            return
    except Exception:
        pass


def fetch_and_cache_spy_from_eodhd(folder=None, group=None):
    """EODHD API から SPY データを取得し CacheManager で保存する（エラー解析強化版）。"""
    symbol = "SPY"

    with trace_context(
        phase=ProcessingPhase.LOAD, system="spy_recovery", symbol=symbol
    ) as ctx:
        logger.get_logger("spy_recovery").info(
            f"Starting SPY cache recovery with trace_id: {ctx.trace_id}"
        )

        try:
            # Phase 1: Configuration and initialization
            settings, cache_manager, api_key = _initialize_components()

            # Phase 2: Data fetch with retry policy
            df = _fetch_spy_data_with_retry(api_key, symbol)

            # Phase 3: Data validation and normalization
            df = _validate_and_normalize_data(df, symbol)

            # Phase 4: Cache storage
            _store_to_cache(cache_manager, symbol, df)

            logger.get_logger("spy_recovery").info(
                "✅ SPY データ復旧が正常に完了しました",
                extra={
                    "symbol": symbol,
                    "rows_processed": len(df),
                    "date_range": f"{df['date'].min()} to {df['date'].max()}",
                },
            )
            print("✅ SPY データを CacheManager 経由で保存しました")
            return True

        except TradingError as e:
            _log_trading_error_safe(
                logger, e, {"operation": "spy_recovery", "symbol": symbol}
            )
            print(f"❌ [{e.error_code.value}] {e.message}", file=sys.stderr)
            if e.retryable:
                print("⚠️  このエラーは再試行可能です", file=sys.stderr)
            return False

        except Exception as e:
            # Classify unknown exceptions
            error_ctx = ErrorContext(
                timestamp=time.strftime("%Y-%m-%d %H:%M:%S"),
                phase="spy_recovery",
                symbol=symbol,
            )
            trading_error = SystemError(
                f"Unexpected error during SPY recovery: {str(e)}",
                ErrorCode.SYS002E,
                context=error_ctx,
                cause=e,
            )
            _log_trading_error_safe(logger, trading_error)
            print(f"❌ [SYS002E] 予期しないエラー: {str(e)}", file=sys.stderr)
            return False


def _initialize_components():
    """Initialize settings, cache manager, and API key."""
    try:
        settings = get_settings(create_dirs=True)
        cache_manager = CacheManager(settings)
        api_key = settings.EODHD_API_KEY

        if not api_key:
            raise DataError(
                "EODHD_API_KEY が設定されていません",
                ErrorCode.NET004E,
                context=ErrorContext(
                    timestamp=time.strftime("%Y-%m-%d %H:%M:%S"), phase="initialization"
                ),
            )

        return settings, cache_manager, api_key

    except Exception as e:
        if isinstance(e, TradingError):
            raise
        raise SystemError(
            f"設定またはCacheManager初期化に失敗: {e}",
            ErrorCode.SYS001E,
            context=ErrorContext(
                timestamp=time.strftime("%Y-%m-%d %H:%M:%S"), phase="initialization"
            ),
            cause=e,
        )


def _fetch_spy_data_with_retry(api_key: str, symbol: str) -> pd.DataFrame:
    """Fetch SPY data with retry policy."""

    def _fetch_data():
        api_symbol = symbol.lower()
        url = f"https://eodhistoricaldata.com/api/eod/{api_symbol}.US?api_token={api_key}&period=d&fmt=json"

        try:
            logger.get_logger("spy_recovery").info(f"Fetching data from: {url}")
            print(f"[INFO] Fetching URL: {url}")

            response = requests.get(url, timeout=30)
            response.raise_for_status()

            data = response.json()

            if not isinstance(data, list) or len(data) == 0:
                raise DataError(
                    "データが空です。APIキーまたはリクエスト制限を確認してください。",
                    ErrorCode.DAT007E,
                    context=ErrorContext(
                        timestamp=time.strftime("%Y-%m-%d %H:%M:%S"),
                        phase="data_fetch",
                        symbol=symbol,
                    ),
                )

            df = pd.DataFrame(data)
            logger.get_logger("spy_recovery").info(
                f"データ取得成功: {len(df)} 行",
                extra={"symbol": symbol, "rows": len(df)},
            )
            print(f"[INFO] 取得件数: {len(df)}")
            return df

        except requests.exceptions.RequestException as e:
            if "timeout" in str(e).lower():
                raise NetworkError(
                    f"API接続がタイムアウトしました: {e}",
                    ErrorCode.NET002E,
                    context=ErrorContext(
                        timestamp=time.strftime("%Y-%m-%d %H:%M:%S"),
                        phase="data_fetch",
                        symbol=symbol,
                    ),
                    cause=e,
                    retryable=True,
                )
            else:
                raise NetworkError(
                    f"API接続に失敗しました: {e}",
                    ErrorCode.NET001E,
                    context=ErrorContext(
                        timestamp=time.strftime("%Y-%m-%d %H:%M:%S"),
                        phase="data_fetch",
                        symbol=symbol,
                    ),
                    cause=e,
                    retryable=True,
                )

    # Retry policy for network operations
    retry_policy = RetryPolicy(
        max_attempts=3, base_delay=2.0, max_delay=30.0, backoff_factor=2.0
    )

    return retry_with_backoff(
        _fetch_data,
        policy=retry_policy,
        error_context=ErrorContext(
            timestamp=time.strftime("%Y-%m-%d %H:%M:%S"),
            phase="data_fetch_with_retry",
            symbol=symbol,
        ),
    )


def _validate_and_normalize_data(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """Validate and normalize fetched data."""
    try:
        # 正規化: 日付列を作り、標準的な列名にする
        df["date"] = pd.to_datetime(df["date"])

        # 列名の正規化（CacheManagerが期待する形式）
        rename_map = {
            "date": "date",
            "open": "open",
            "high": "high",
            "low": "low",
            "volume": "volume",
        }

        # adjusted_close を close として扱う（優先）
        if "adjusted_close" in df.columns:
            rename_map["adjusted_close"] = "close"
            if "close" in df.columns:
                # 元の close は raw_close として保持
                df["raw_close"] = df["close"]
        elif "close" in df.columns:
            rename_map["close"] = "close"

        df = df.rename(columns=rename_map)

        # 必須列の確認
        required_cols = {"date", "open", "high", "low", "close", "volume"}
        if not required_cols.issubset(df.columns):
            missing = required_cols - set(df.columns)
            raise DataError(
                f"必須列が不足しています: {missing}",
                ErrorCode.DAT003E,
                context=ErrorContext(
                    timestamp=time.strftime("%Y-%m-%d %H:%M:%S"),
                    phase="data_validation",
                    symbol=symbol,
                    additional={"missing_columns": list(missing)},
                ),
            )

        # 日付でソート
        df = df.sort_values("date").reset_index(drop=True)

        # Data quality checks
        if df["close"].isna().any():
            raise DataError(
                "価格データに欠損値が含まれています",
                ErrorCode.DAT002E,
                context=ErrorContext(
                    timestamp=time.strftime("%Y-%m-%d %H:%M:%S"),
                    phase="data_validation",
                    symbol=symbol,
                ),
            )

        logger.get_logger("spy_recovery").info(
            "データ正規化完了",
            extra={
                "symbol": symbol,
                "rows": len(df),
                "date_range": f"{df['date'].min()} to {df['date'].max()}",
                "columns": list(df.columns),
            },
        )

        return df

    except Exception as e:
        if isinstance(e, TradingError):
            raise
        raise DataError(
            f"データの正規化に失敗しました: {e}",
            ErrorCode.DAT002E,
            context=ErrorContext(
                timestamp=time.strftime("%Y-%m-%d %H:%M:%S"),
                phase="data_validation",
                symbol=symbol,
            ),
            cause=e,
        )


def _store_to_cache(cache_manager: CacheManager, symbol: str, df: pd.DataFrame):
    """Store data to cache with error handling."""
    try:
        # CacheManagerのupsert_bothを使用して保存
        cache_manager.upsert_both(symbol, df)

        logger.get_logger("spy_recovery").info(
            "Cache storage completed",
            extra={"symbol": symbol, "rows": len(df), "storage_method": "upsert_both"},
        )

        print("   - full_backup: 全指標付きデータ")
        print("   - base: ベース指標付きデータ")
        print("   - rolling: 直近300+30日のデータ")

    except Exception as e:
        raise DataError(
            f"CacheManager による保存に失敗しました: {e}",
            ErrorCode.DAT001E,
            context=ErrorContext(
                timestamp=time.strftime("%Y-%m-%d %H:%M:%S"),
                phase="cache_storage",
                symbol=symbol,
            ),
            cause=e,
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="SPY の日足を取得し CacheManager でキャッシュへ保存"
    )
    parser.add_argument(
        "--out",
        dest="out",
        default=None,
        help="非推奨: CacheManager が設定から自動決定します",
    )
    parser.add_argument(
        "--group",
        choices=["full_backup", "base", "rolling"],
        default=None,
        help="非推奨: CacheManager が自動的に全グループに保存します",
    )
    args = parser.parse_args()

    if args.out or args.group:
        print(
            "⚠️  --out と --group オプションは CacheManager により自動処理されるため無視されます"
        )

    fetch_and_cache_spy_from_eodhd()
