"""Alpaca (無料 IEX feed) から日次 OHLCV を取得するデータプロバイダ。

このモジュールは EODHD 有料 API (`scripts/cache_daily_data.py:get_eodhd_data`)
を **無料の Alpaca Market Data API** で置き換えるために追加された。

設計方針 (重要):
    ``get_alpaca_data`` は旧 ``get_eodhd_data`` と **完全に同一のスキーマ** を返す。
    呼び出し側から見て透過的に差し替え可能であること (drop-in replacement) を保証する。

    旧 ``get_eodhd_data(symbol: str) -> pd.DataFrame | None`` の返り値:
        - columns : ["Open", "High", "Low", "Close", "AdjClose", "Volume"]
        - index   : DatetimeIndex (name="Date", tz-naive, 昇順ソート済)
        - dtypes  : OHLC/AdjClose = float64, Volume = int64
        - 失敗/空 : None を返す (例外を送出しない)

コスト:
    Alpaca の無料 tier は **IEX feed のみ**。SIP feed は有料 ($99/月) のため既定 OFF。
    IEX feed の出来高は NASDAQ/NYSE 全体の 2〜3% しか反映されないため、
    Volume は過小評価される (README の "Data source" 章 / 既知の罠を参照)。

認証:
    環境変数 ``ALPACA_API_KEY`` / ``ALPACA_SECRET_KEY`` を読む。
    既存リポジトリの慣習 (``APCA_API_KEY_ID`` / ``APCA_API_SECRET_KEY``) も
    フォールバックとして受け付ける。いずれも無ければ ``ValueError`` で fail-fast。
"""

from __future__ import annotations

import logging
import os
import time

import pandas as pd

logger = logging.getLogger(__name__)

# --- 定数 ---------------------------------------------------------------
# 旧 get_eodhd_data の返り値カラム順と完全一致させる
_OUTPUT_COLUMNS = ["Open", "High", "Low", "Close", "AdjClose", "Volume"]

# Alpaca 無料 tier の feed。SIP は有料なので既定は IEX。
_DEFAULT_FEED = os.getenv("ALPACA_FEED", "iex")

# 無料 tier のレート制限は 200 req/min。控えめな throttle を挟む。
_MIN_REQUEST_INTERVAL = float(os.getenv("ALPACA_MIN_REQUEST_INTERVAL", "0.35"))
_last_request_ts = 0.0

# 過去データの取得開始日 (EODHD は全履歴を返すため、十分に古い日付から取得)
_DEFAULT_HISTORY_START = os.getenv("ALPACA_HISTORY_START", "2000-01-01")


def _get_credentials() -> tuple[str, str]:
    """API キーを環境変数から読み込む。無ければ ValueError で fail-fast。"""
    api_key = os.getenv("ALPACA_API_KEY") or os.getenv("APCA_API_KEY_ID")
    secret_key = os.getenv("ALPACA_SECRET_KEY") or os.getenv("APCA_API_SECRET_KEY")
    if not api_key or not secret_key:
        raise ValueError(
            "Alpaca API 認証情報が未設定です。環境変数 ALPACA_API_KEY と "
            "ALPACA_SECRET_KEY (または APCA_API_KEY_ID / APCA_API_SECRET_KEY) "
            "を .env に設定してください。取得先: https://app.alpaca.markets/"
        )
    return api_key, secret_key


def _to_alpaca_symbol(symbol: str) -> str:
    """EODHD 形式 (``AAPL.US``) を Alpaca 形式 (``AAPL``) に変換する。

    EODHD は ``.US`` サフィックスを要求するが Alpaca は plain ticker のみ。
    既に plain な場合はそのまま返す。
    """
    return symbol.upper().replace(".US", "")


def _throttle() -> None:
    """無料 tier のレート制限 (200 req/min) 対応の簡易 throttle。"""
    global _last_request_ts
    now = time.monotonic()
    elapsed = now - _last_request_ts
    if elapsed < _MIN_REQUEST_INTERVAL:
        time.sleep(_MIN_REQUEST_INTERVAL - elapsed)
    _last_request_ts = time.monotonic()


def get_alpaca_data(symbol: str) -> pd.DataFrame | None:
    """Alpaca (IEX feed) から日次 OHLCV を取得する。

    旧 ``get_eodhd_data(symbol)`` の drop-in replacement。
    同一の columns / index 型 / dtypes を持つ DataFrame を返し、
    取得失敗・空応答時は ``None`` を返す (例外は送出しない)。

    認証情報が未設定の場合のみ ``ValueError`` で fail-fast する
    (これはデータ取得失敗ではなく設定エラーであるため)。

    Parameters
    ----------
    symbol : str
        取得対象シンボル。EODHD 形式 (``AAPL.US``) / plain (``AAPL``) の双方を受け付ける。

    Returns
    -------
    pd.DataFrame | None
        columns=["Open","High","Low","Close","AdjClose","Volume"],
        index=DatetimeIndex(name="Date", tz-naive, 昇順)。失敗時は None。
    """
    # 認証情報チェック (設定エラーは fail-fast)。SDK import もここで遅延評価。
    api_key, secret_key = _get_credentials()

    try:
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
    except ImportError as exc:  # pragma: no cover - 依存欠如は設定エラー扱い
        raise ValueError(
            "alpaca-py がインストールされていません。"
            "`pip install 'alpaca-py>=0.30.0'` を実行してください。"
        ) from exc

    api_symbol = _to_alpaca_symbol(symbol)
    client = StockHistoricalDataClient(api_key, secret_key)

    def _fetch(adjustment: str) -> pd.DataFrame | None:
        """指定 adjustment で bars を取得し MultiIndex を解いて返す。"""
        req = StockBarsRequest(
            symbol_or_symbols=api_symbol,
            timeframe=TimeFrame.Day,
            start=pd.Timestamp(_DEFAULT_HISTORY_START),
            feed=_DEFAULT_FEED,
            adjustment=adjustment,
        )
        _throttle()
        bars = client.get_stock_bars(req)
        raw = bars.df
        if raw is None or raw.empty:
            return None
        # get_stock_bars は (symbol, timestamp) の MultiIndex。symbol レベルを除去。
        if isinstance(raw.index, pd.MultiIndex):
            raw = raw.xs(api_symbol, level="symbol")
        return raw

    try:
        # 生値 (raw) の OHLCV → EODHD の close (未調整) に対応
        raw_df = _fetch("raw")
        if raw_df is None or raw_df.empty:
            logger.warning("%s: Alpaca から空またはデータ無し", symbol)
            return None

        # 調整後終値 → EODHD の adjusted_close に対応 (split+dividend)
        adj_close: pd.Series | None = None
        try:
            adj_df = _fetch("all")
            if adj_df is not None and not adj_df.empty and "close" in adj_df:
                adj_close = adj_df["close"]
        except Exception as exc:  # pragma: no cover - 調整値は best-effort
            logger.warning("%s: 調整後終値の取得に失敗 (AdjClose=Close で代替) - %s", symbol, exc)

        df = pd.DataFrame(index=raw_df.index)
        df["Open"] = raw_df["open"].astype("float64")
        df["High"] = raw_df["high"].astype("float64")
        df["Low"] = raw_df["low"].astype("float64")
        df["Close"] = raw_df["close"].astype("float64")
        if adj_close is not None:
            # index を raw に揃えて欠損は Close で補完
            df["AdjClose"] = adj_close.reindex(raw_df.index).astype("float64")
            df["AdjClose"] = df["AdjClose"].fillna(df["Close"])
        else:
            df["AdjClose"] = df["Close"].astype("float64")
        df["Volume"] = raw_df["volume"].astype("int64")

        # --- index を EODHD と一致させる (罠2/罠3) ---
        # Alpaca の Day bar timestamp は tz-aware UTC (ET 深夜 = bar 開始)。
        # ET に変換して日付部分だけ残し、tz-naive の DatetimeIndex にする。
        # これで EODHD の日付表現 (取引日 00:00, tz-naive) と一致する。
        idx = pd.DatetimeIndex(df.index)
        if idx.tz is not None:
            idx = idx.tz_convert("America/New_York").tz_localize(None)
        idx = idx.normalize()
        df.index = idx
        df.index.name = "Date"

        df = df[_OUTPUT_COLUMNS]
        df = df.sort_index()
        # 重複 index を除去 (最終行を採用)
        df = df[~df.index.duplicated(keep="last")]
        return df
    except Exception as exc:  # noqa: BLE001 - 取得失敗は None を返す (EODHD 互換)
        logger.error("%s: Alpaca データ取得中のエラー - %s", symbol, exc)
        return None
