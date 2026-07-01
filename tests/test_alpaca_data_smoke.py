"""Smoke test for common.alpaca_data.get_alpaca_data.

2 種類のテストを含む:

1. ``test_get_alpaca_data_live_smoke``
   実際に Alpaca API を叩く smoke test。ALPACA_API_KEY / ALPACA_SECRET_KEY
   (または APCA_* フォールバック) が未設定なら skip する。
   直近 Volume の桁数を print し (IEX/SIP 判定材料)、shape/columns/dtypes を検証する。

2. ``test_get_alpaca_data_schema_offline``
   Alpaca SDK をモックして、認証情報が無い CI 環境でもスキーマ変換ロジック
   (columns / index tz-naive / dtypes / None 挙動) を検証する。
"""

from __future__ import annotations

import os

import numpy as np
import pandas as pd
import pytest

from common.alpaca_data import get_alpaca_data

# .env を best-effort で読み込み、live smoke の認証情報検出を可能にする
try:  # pragma: no cover - dotenv 不在時は環境変数のみ
    from dotenv import load_dotenv

    load_dotenv(override=False)
except Exception:
    pass

# 旧 get_eodhd_data と一致すべき出力スキーマ
EXPECTED_COLUMNS = ["Open", "High", "Low", "Close", "AdjClose", "Volume"]

_HAS_CREDS = bool(
    (os.getenv("ALPACA_API_KEY") or os.getenv("APCA_API_KEY_ID"))
    and (os.getenv("ALPACA_SECRET_KEY") or os.getenv("APCA_API_SECRET_KEY"))
)


def _assert_schema(df: pd.DataFrame) -> None:
    """旧 get_eodhd_data と同一スキーマであることを検証する。"""
    assert isinstance(df, pd.DataFrame)
    assert list(df.columns) == EXPECTED_COLUMNS, f"columns 不一致: {list(df.columns)}"
    # index: tz-naive DatetimeIndex, name="Date", 昇順
    assert isinstance(df.index, pd.DatetimeIndex)
    assert df.index.tz is None, "index は tz-naive でなければならない (EODHD 互換)"
    assert df.index.name == "Date"
    assert df.index.is_monotonic_increasing
    # dtypes: OHLC/AdjClose = float64, Volume = int64
    for col in ["Open", "High", "Low", "Close", "AdjClose"]:
        assert df[col].dtype == np.float64, f"{col} dtype={df[col].dtype}"
    assert df["Volume"].dtype == np.int64, f"Volume dtype={df['Volume'].dtype}"


@pytest.mark.skipif(not _HAS_CREDS, reason="Alpaca API 認証情報が未設定のため skip")
def test_get_alpaca_data_live_smoke(capsys):
    """AAPL の直近データを実取得し、スキーマと Volume 桁数を検証・print する。"""
    df = get_alpaca_data("AAPL")
    assert df is not None and not df.empty, "AAPL の取得に失敗"

    _assert_schema(df)

    recent = df.tail(5)
    assert len(recent) >= 1

    last_date = recent.index[-1]
    last_volume = int(recent["Volume"].iloc[-1])
    n_digits = len(str(abs(last_volume)))

    with capsys.disabled():
        print("\n===== Alpaca smoke test (AAPL, 直近5営業日) =====")
        print(recent[EXPECTED_COLUMNS].to_string())
        print(
            f"AAPL {last_date.date()} volume: {last_volume:,} = {n_digits} 桁 "
            f"(feed={os.getenv('ALPACA_FEED', 'iex')})"
        )
        print(
            "判定: IEX feed の Volume は取引所全体の 2-3% のため過小評価。"
            " EODHD 想定 (7-8 桁) より小さければ IEX。"
        )

    # 罠3: index が tz-naive の normalize 済 (時刻 00:00) 日付であること
    assert last_date == last_date.normalize()


def test_get_alpaca_data_schema_offline(monkeypatch):
    """Alpaca SDK をモックし、認証情報無しでもスキーマ変換を検証する。"""
    monkeypatch.setenv("ALPACA_API_KEY", "dummy")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "dummy")

    # (symbol, timestamp) の MultiIndex, tz-aware UTC を持つ擬似 Alpaca bars.df
    ts = pd.to_datetime(
        ["2024-01-02 05:00:00+00:00", "2024-01-03 05:00:00+00:00"], utc=True
    )
    index = pd.MultiIndex.from_arrays(
        [["AAPL", "AAPL"], ts], names=["symbol", "timestamp"]
    )
    fake_df = pd.DataFrame(
        {
            "open": [100.0, 101.0],
            "high": [102.0, 103.0],
            "low": [99.0, 100.0],
            "close": [101.5, 102.5],
            "volume": [1234567, 2345678],
            "trade_count": [10, 20],
            "vwap": [101.0, 102.0],
        },
        index=index,
    )

    class _FakeBars:
        df = fake_df

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        def get_stock_bars(self, req):
            return _FakeBars()

    import alpaca.data.historical as hist

    monkeypatch.setattr(hist, "StockHistoricalDataClient", _FakeClient)

    df = get_alpaca_data("AAPL.US")  # .US サフィックスも受理されること
    assert df is not None
    _assert_schema(df)
    assert len(df) == 2
    # tz-aware UTC (ET 深夜) → tz-naive の取引日に変換されていること (罠2/罠3)
    assert list(df.index.strftime("%Y-%m-%d")) == ["2024-01-02", "2024-01-03"]
    # AdjClose は close にフォールバック (モックは raw/all 同値)
    assert df["AdjClose"].iloc[0] == pytest.approx(101.5)


def test_get_alpaca_data_missing_creds_raises(monkeypatch):
    """認証情報が無ければ ValueError で fail-fast すること。"""
    # .env の再読込を無効化し、環境変数が真に空の状態を再現する
    import common.alpaca_data as ad

    monkeypatch.setattr(ad, "_load_env", lambda: None)
    for key in [
        "ALPACA_API_KEY",
        "ALPACA_SECRET_KEY",
        "APCA_API_KEY_ID",
        "APCA_API_SECRET_KEY",
    ]:
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(ValueError):
        get_alpaca_data("AAPL")
