"""Smoke test for common.polygon_data.

- ``test_get_polygon_data_live_smoke`` / ``test_grouped_daily_live_smoke``
  実際に Polygon API を叩く。POLYGON_API_KEY が未設定なら skip。
  直近 Volume 桁数を print (full-market なら AAPL は 8 桁 ≒ yfinance 実測 110M)。
- ``test_get_polygon_data_schema_offline`` / ``test_grouped_daily_schema_offline``
  requests をモックし、認証情報無しでもスキーマ変換ロジックを検証する。
- ``test_get_polygon_data_missing_creds_raises``
  認証情報が無ければ ValueError で fail-fast することを検証。
"""

from __future__ import annotations

import os

import numpy as np
import pandas as pd
import pytest

import common.polygon_data as pg
from common.polygon_data import get_polygon_data, get_polygon_grouped_daily

try:  # pragma: no cover - dotenv 不在時は環境変数のみ
    from dotenv import load_dotenv

    load_dotenv(override=False)
except Exception:
    pass

EXPECTED_COLUMNS = ["Open", "High", "Low", "Close", "AdjClose", "Volume"]
_HAS_KEY = bool(os.getenv("POLYGON_API_KEY") or os.getenv("POLYGON_KEY"))


def _assert_schema(df: pd.DataFrame) -> None:
    assert isinstance(df, pd.DataFrame)
    assert list(df.columns) == EXPECTED_COLUMNS, f"columns 不一致: {list(df.columns)}"
    assert isinstance(df.index, pd.DatetimeIndex)
    assert df.index.tz is None, "index は tz-naive でなければならない (EODHD 互換)"
    assert df.index.name == "Date"
    assert df.index.is_monotonic_increasing
    for col in ["Open", "High", "Low", "Close", "AdjClose"]:
        assert df[col].dtype == np.float64, f"{col} dtype={df[col].dtype}"
    assert df["Volume"].dtype == np.int64, f"Volume dtype={df['Volume'].dtype}"


class _FakeResp:
    def __init__(self, payload, status=200):
        self._p = payload
        self.status_code = status

    def json(self):
        return self._p


def _agg(t_ms, o, h, low, c, v):
    return {"t": t_ms, "o": o, "h": h, "l": low, "c": c, "v": v, "vw": c, "n": 1}


# 2024-01-02 / 2024-01-03 の ET 深夜を UTC ms で (05:00 UTC)
_T1 = 1704178800000  # 2024-01-02 05:00:00Z
_T2 = 1704265200000  # 2024-01-03 05:00:00Z


@pytest.mark.skipif(not _HAS_KEY, reason="POLYGON_API_KEY 未設定のため skip")
def test_get_polygon_data_live_smoke(capsys):
    df = get_polygon_data("AAPL")
    assert df is not None and not df.empty, "AAPL の取得に失敗"
    _assert_schema(df)
    recent = df.tail(5)
    last_date = recent.index[-1]
    last_vol = int(recent["Volume"].iloc[-1])
    n_digits = len(str(abs(last_vol)))
    with capsys.disabled():
        print("\n===== Polygon smoke (AAPL, 直近5営業日) =====")
        print(recent[EXPECTED_COLUMNS].to_string())
        print(f"AAPL {last_date.date()} volume: {last_vol:,} = {n_digits} 桁")
        print("full-market なら 7-8 桁 (yfinance 実測 ~110M = 9桁級) を期待")
    assert last_date == last_date.normalize()


@pytest.mark.skipif(not _HAS_KEY, reason="POLYGON_API_KEY 未設定のため skip")
def test_grouped_daily_live_smoke(capsys):
    # 直近の平日を数日遡って試す (祝日/週末を避ける)
    for back in range(1, 8):
        date = (pd.Timestamp.utcnow().normalize() - pd.Timedelta(days=back)).strftime(
            "%Y-%m-%d"
        )
        gd = get_polygon_grouped_daily(date)
        if not gd.empty:
            with capsys.disabled():
                print(
                    f"\n===== Polygon Grouped Daily {date}: 1 request で {len(gd)} 銘柄 ====="
                )
                print(gd.head(3).to_string())
            assert gd.index.name == "symbol"
            assert list(gd.columns) == ["Open", "High", "Low", "Close", "Volume"]
            assert len(gd) > 1000, "全 US 銘柄なら数千件のはず"
            return
    pytest.skip("直近営業日の Grouped Daily が取得できず (休場続き等)")


def test_get_polygon_data_schema_offline(monkeypatch):
    monkeypatch.setenv("POLYGON_API_KEY", "dummy")
    # raw と adjusted で同一 payload を返す (簡略)
    payload = {
        "ticker": "AAPL",
        "status": "OK",
        "resultsCount": 2,
        "results": [
            _agg(_T1, 100.0, 102.0, 99.0, 101.5, 1234567),
            _agg(_T2, 101.0, 103.0, 100.0, 102.5, 2345678),
        ],
    }
    monkeypatch.setattr(pg.requests, "get", lambda *a, **k: _FakeResp(payload))
    monkeypatch.setattr(pg, "_throttle", lambda: None)

    df = get_polygon_data("AAPL.US")  # .US サフィックスも受理
    assert df is not None
    _assert_schema(df)
    assert len(df) == 2
    assert list(df.index.strftime("%Y-%m-%d")) == ["2024-01-02", "2024-01-03"]
    assert df["Volume"].iloc[0] == 1234567
    assert df["AdjClose"].iloc[0] == pytest.approx(101.5)


def test_grouped_daily_schema_offline(monkeypatch):
    monkeypatch.setenv("POLYGON_API_KEY", "dummy")
    payload = {
        "status": "OK",
        "resultsCount": 2,
        "results": [
            {
                "T": "AAPL",
                "o": 100.0,
                "h": 102.0,
                "l": 99.0,
                "c": 101.5,
                "v": 1234567,
                "t": _T1,
            },
            {
                "T": "MSFT",
                "o": 200.0,
                "h": 205.0,
                "l": 199.0,
                "c": 203.0,
                "v": 987654,
                "t": _T1,
            },
        ],
    }
    monkeypatch.setattr(pg.requests, "get", lambda *a, **k: _FakeResp(payload))
    monkeypatch.setattr(pg, "_throttle", lambda: None)

    gd = get_polygon_grouped_daily("2024-01-02")
    assert gd.index.name == "symbol"
    assert list(gd.columns) == ["Open", "High", "Low", "Close", "Volume"]
    assert set(gd.index) == {"AAPL", "MSFT"}
    assert gd.loc["AAPL", "Volume"] == 1234567
    assert gd["Volume"].dtype == np.int64


def test_grouped_daily_empty_on_holiday(monkeypatch):
    monkeypatch.setenv("POLYGON_API_KEY", "dummy")
    payload = {"status": "OK", "resultsCount": 0, "results": []}
    monkeypatch.setattr(pg.requests, "get", lambda *a, **k: _FakeResp(payload))
    monkeypatch.setattr(pg, "_throttle", lambda: None)
    gd = get_polygon_grouped_daily("2024-01-01")  # 元日 = 休場
    assert gd.empty
    assert list(gd.columns) == ["Open", "High", "Low", "Close", "Volume"]


def test_get_polygon_data_missing_creds_raises(monkeypatch):
    monkeypatch.setattr(pg, "_load_env", lambda: None)
    for key in ["POLYGON_API_KEY", "MASSIVE_API_KEY", "POLYGON_KEY", "MASSIVE_KEY"]:
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(ValueError):
        get_polygon_data("AAPL")
    with pytest.raises(ValueError):
        get_polygon_grouped_daily("2024-01-02")
