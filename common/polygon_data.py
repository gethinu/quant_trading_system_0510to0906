"""Polygon.io (無料 tier) から日次 OHLCV を取得するデータプロバイダ。

このモジュールは EODHD 有料 API (`scripts/cache_daily_data.py:get_eodhd_data`)
を **無料の Polygon.io Stocks API** で置き換えるために追加された。
Alpaca IEX feed は出来高が全市場の約 2-4% しか反映されず min-ADV/流動性フィルタが
壊滅する (実証済) ため、full-market volume を返す Polygon を本命フォールバックとする。

設計方針 (重要):
    ``get_polygon_data`` は旧 ``get_eodhd_data`` / ``get_alpaca_data`` と
    **完全に同一のスキーマ** を返す drop-in replacement。
        - columns : ["Open", "High", "Low", "Close", "AdjClose", "Volume"]
        - index   : DatetimeIndex (name="Date", tz-naive, 昇順ソート済)
        - dtypes  : OHLC/AdjClose = float64, Volume = int64
        - 失敗/空/404 : None を返す (例外を送出しない)

Bulk の強み:
    ``get_polygon_grouped_daily(date)`` は **1 リクエストで全 US 銘柄の日足**を返す
    (Grouped Daily エンドポイント)。dv20/dv50 の pre-compute が 1 call で済むため、
    無料 tier の 5 req/min 制限が日次バッチでは実質非制約になる。

コスト:
    無料 tier ($0)。データは SIP 連結 (全取引所 + FINRA/OTC/ATS) の full-market volume。
    レート制限は 5 req/min (無料)。Grouped Daily は 1 call/日で全銘柄。

認証:
    環境変数 ``POLYGON_API_KEY`` を読む。無ければ ``ValueError`` で fail-fast。
    取得先: https://polygon.io/dashboard/signup (無料)
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime

import pandas as pd
import requests

logger = logging.getLogger(__name__)

# --- 定数 ---------------------------------------------------------------
# 旧 get_eodhd_data の返り値カラム順と完全一致させる
_OUTPUT_COLUMNS = ["Open", "High", "Low", "Close", "AdjClose", "Volume"]

_API_BASE = os.getenv("POLYGON_API_BASE", "https://api.polygon.io").rstrip("/")

# 過去データの取得開始日 (EODHD は全履歴を返すため、十分に古い日付から取得)
_DEFAULT_HISTORY_START = os.getenv("POLYGON_HISTORY_START", "2000-01-01")

# 無料 tier のレート制限は 5 req/min → 既定 13 秒間隔 (安全側)。
# 有料 tier や Grouped Daily 主体運用では env で短縮可。
_MIN_REQUEST_INTERVAL = float(os.getenv("POLYGON_MIN_REQUEST_INTERVAL", "13.0"))
_REQUEST_TIMEOUT = float(os.getenv("POLYGON_REQUEST_TIMEOUT", "30"))
_MAX_RETRIES = int(os.getenv("POLYGON_MAX_RETRIES", "3"))
_last_request_ts = 0.0


def _load_env() -> None:
    """`.env` を best-effort で読み込む (既存 env は上書きしない)。

    common/alpaca_data.py・broker_alpaca.py・notifier.py と同じパターン。
    """
    try:
        from dotenv import load_dotenv

        load_dotenv(override=False)
    except Exception:  # pragma: no cover - dotenv 不在時は環境変数のみで動作
        pass


def _get_api_key() -> str:
    """API キーを環境変数から読み込む。無ければ ValueError で fail-fast。

    Polygon.io は Massive.com にリブランドされたため、``MASSIVE_API_KEY`` /
    ``MASSIVE_KEY`` も同義キーとして受け付ける (env 変数名の揺れに両対応)。
    """
    _load_env()
    api_key = (
        os.getenv("POLYGON_API_KEY")
        or os.getenv("MASSIVE_API_KEY")
        or os.getenv("POLYGON_KEY")
        or os.getenv("MASSIVE_KEY")
    )
    if not api_key:
        raise ValueError(
            "Polygon/Massive API 認証情報が未設定です。環境変数 POLYGON_API_KEY "
            "(または MASSIVE_API_KEY) を .env に設定してください。無料 tier 取得先: "
            "https://polygon.io/dashboard/signup"
        )
    return api_key


def _to_polygon_symbol(symbol: str) -> str:
    """EODHD 形式 (``AAPL.US``) を Polygon 形式 (``AAPL``) に変換する。

    EODHD は ``.US`` サフィックスを要求するが Polygon は plain ticker のみ。
    """
    return symbol.upper().replace(".US", "")


def _throttle() -> None:
    """無料 tier のレート制限 (5 req/min) 対応の簡易 throttle。"""
    global _last_request_ts
    now = time.monotonic()
    elapsed = now - _last_request_ts
    if elapsed < _MIN_REQUEST_INTERVAL:
        time.sleep(_MIN_REQUEST_INTERVAL - elapsed)
    _last_request_ts = time.monotonic()


def _request(url: str, params: dict, api_key: str) -> dict | None:
    """Polygon API を叩き JSON dict を返す。404/失敗時は None (raise しない)。

    429 (rate limit) は指数バックオフでリトライする。
    """
    params = {**params, "apiKey": api_key}
    for attempt in range(_MAX_RETRIES):
        _throttle()
        try:
            r = requests.get(url, params=params, timeout=_REQUEST_TIMEOUT)
        except Exception as exc:  # pragma: no cover - network error は None 扱い
            logger.warning("Polygon リクエスト失敗 (%s/%s): %s", attempt + 1, _MAX_RETRIES, exc)
            continue
        if r.status_code == 200:
            return r.json()
        if r.status_code == 404:
            return None  # symbol 不在 → EODHD と同じく None
        if r.status_code == 429:
            backoff = _MIN_REQUEST_INTERVAL * (attempt + 1)
            logger.warning("Polygon 429 rate limit, %.0fs 待機 (%s/%s)", backoff, attempt + 1, _MAX_RETRIES)
            time.sleep(backoff)
            continue
        logger.warning("Polygon ステータス %s - %s", r.status_code, url)
        return None
    return None


def get_polygon_data(symbol: str) -> pd.DataFrame | None:
    """Polygon.io から日次 OHLCV を取得する。

    旧 ``get_eodhd_data(symbol)`` の drop-in replacement。同一の columns /
    index 型 / dtypes を返し、取得失敗・空応答・404 時は ``None`` を返す。
    認証情報未設定時のみ ``ValueError`` で fail-fast。

    EODHD の close (未調整) / adjusted_close (調整済) を再現するため、
    adjusted=false と adjusted=true の 2 リクエストを行う:
        - Open/High/Low/Close/Volume : adjusted=false (raw)
        - AdjClose                   : adjusted=true (split/dividend 調整済 close)
    調整済取得に失敗した場合は AdjClose=Close にフォールバックする。

    Parameters
    ----------
    symbol : str
        取得対象シンボル。``AAPL.US`` / ``AAPL`` 双方を受け付ける。

    Returns
    -------
    pd.DataFrame | None
        columns=["Open","High","Low","Close","AdjClose","Volume"],
        index=DatetimeIndex(name="Date", tz-naive, 昇順)。失敗時は None。
    """
    api_key = _get_api_key()
    api_symbol = _to_polygon_symbol(symbol)
    frm = _DEFAULT_HISTORY_START
    to = datetime.today().strftime("%Y-%m-%d")
    url = f"{_API_BASE}/v2/aggs/ticker/{api_symbol}/range/1/day/{frm}/{to}"
    base_params = {"sort": "asc", "limit": 50000}

    def _fetch(adjusted: bool) -> pd.DataFrame | None:
        data = _request(url, {**base_params, "adjusted": str(adjusted).lower()}, api_key)
        if not data:
            return None
        results = data.get("results")
        if not results:
            return None
        return pd.DataFrame(results)

    try:
        raw = _fetch(adjusted=False)
        if raw is None or raw.empty:
            logger.warning("%s: Polygon から空またはデータ無し", symbol)
            return None

        # timestamp (ms, UTC) → tz-naive の取引日に変換 (EODHD/Alpaca と揃える)
        idx = pd.to_datetime(raw["t"], unit="ms", utc=True)
        idx = idx.dt.tz_convert("America/New_York").dt.tz_localize(None).dt.normalize()

        df = pd.DataFrame(index=pd.DatetimeIndex(idx))
        df["Open"] = raw["o"].astype("float64").values
        df["High"] = raw["h"].astype("float64").values
        df["Low"] = raw["l"].astype("float64").values
        df["Close"] = raw["c"].astype("float64").values

        # 調整済 close → AdjClose (best-effort)
        adj_close = None
        try:
            adj = _fetch(adjusted=True)
            if adj is not None and not adj.empty and "c" in adj:
                adj_idx = pd.to_datetime(adj["t"], unit="ms", utc=True)
                adj_idx = adj_idx.dt.tz_convert("America/New_York").dt.tz_localize(None).dt.normalize()
                adj_close = pd.Series(adj["c"].astype("float64").values, index=pd.DatetimeIndex(adj_idx))
        except Exception as exc:  # pragma: no cover - 調整値は best-effort
            logger.warning("%s: 調整後終値の取得に失敗 (AdjClose=Close で代替) - %s", symbol, exc)

        if adj_close is not None:
            df["AdjClose"] = adj_close.reindex(df.index).astype("float64")
            df["AdjClose"] = df["AdjClose"].fillna(df["Close"])
        else:
            df["AdjClose"] = df["Close"].astype("float64")
        df["Volume"] = raw["v"].astype("int64").values

        df.index.name = "Date"
        df = df[_OUTPUT_COLUMNS]
        df = df.sort_index()
        df = df[~df.index.duplicated(keep="last")]
        return df
    except Exception as exc:  # noqa: BLE001 - 取得失敗は None (EODHD 互換)
        logger.error("%s: Polygon データ取得中のエラー - %s", symbol, exc)
        return None


def get_polygon_grouped_daily(date: str) -> pd.DataFrame:
    """指定日の **全 US 銘柄の日足** を 1 リクエストで取得する (Grouped Daily)。

    dv20/dv50 pre-compute を全銘柄まとめて 1 call で賄える Polygon の decisive
    advantage。無料 tier でも利用可能 (1 call/日)。

    Parameters
    ----------
    date : str
        取引日 (``YYYY-MM-DD``)。祝日/週末は空 DataFrame を返す。

    Returns
    -------
    pd.DataFrame
        index=symbol、columns=["Open","High","Low","Close","Volume"]。
        データ無し時は空 DataFrame (columns だけ持つ)。

    Raises
    ------
    ValueError
        認証情報未設定時のみ (fail-fast)。
    """
    api_key = _get_api_key()
    url = f"{_API_BASE}/v2/aggs/grouped/locale/us/market/stocks/{date}"
    empty = pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
    empty.index.name = "symbol"

    data = _request(url, {"adjusted": "false"}, api_key)
    if not data:
        return empty
    results = data.get("results")
    if not results:
        return empty

    df = pd.DataFrame(results)
    # T=ticker, o/h/l/c/v = OHLCV
    out = pd.DataFrame(
        {
            "Open": df["o"].astype("float64"),
            "High": df["h"].astype("float64"),
            "Low": df["l"].astype("float64"),
            "Close": df["c"].astype("float64"),
            "Volume": df["v"].astype("int64"),
        }
    )
    out.index = pd.Index(df["T"].astype(str), name="symbol")
    return out
