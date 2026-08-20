"""指値エントリーの約定判定（バックテスト忠実度）の回帰テスト。

System3 / System5 / System6 は前日終値から離した **指値** で仕掛ける。
2026-08-20 以前のバックテストは指値が必ず約定する前提だったため、
実際には板が届かなかった候補まで建玉として数え、勝率を過大評価していた
（`docs/BACKTEST_LIMIT_FILL_FIX_20260820.md` を参照）。

ここで固定する規約:
  - long  (buy limit)  : ``Low[entry_bar]  <= limit`` なら約定、約定値は指値
  - short (sell limit) : ``High[entry_bar] >= limit`` なら約定、約定値は指値
  - 届かなければ ``compute_entry`` は None を返し、建玉は作られない
"""

from __future__ import annotations

import pandas as pd
import pytest

from common.backtest_utils import simulate_trades_with_risk
from strategies.system3_strategy import System3Strategy
from strategies.system5_strategy import System5Strategy
from strategies.system6_strategy import System6Strategy

ENTRY_DATE = "2025-01-02"


def _frame(lows: list[float], highs: list[float]) -> pd.DataFrame:
    """prev_close=100 固定の 6 本バー。Low/High だけをケースごとに差し替える。"""
    dates = pd.date_range("2025-01-01", periods=len(lows), freq="D")
    closes = [100.0] * len(lows)
    return pd.DataFrame(
        {
            "Open": closes,
            "High": highs,
            "Low": lows,
            "Close": closes,
            "ATR10": [1.0] * len(lows),
        },
        index=dates,
    )


# --------------------------------------------------------------------------
# System3: long limit = prev_close * 0.93 = 93.00
# --------------------------------------------------------------------------
class TestSystem3LimitFill:
    def test_no_fill_when_bar_never_reaches_limit(self):
        # 当日安値 93.01 は指値 93.00 に 1 セント届かない -> 不約定
        df = _frame([99.0, 93.01, 99.0, 99.0, 99.0, 99.0], [101.0] * 6)
        result = System3Strategy().compute_entry(df, {"entry_date": ENTRY_DATE}, 1e5)
        assert result is None

    def test_fills_at_limit_price_when_bar_reaches_it(self):
        df = _frame([99.0, 92.5, 99.0, 99.0, 99.0, 99.0], [101.0] * 6)
        result = System3Strategy().compute_entry(df, {"entry_date": ENTRY_DATE}, 1e5)
        assert result is not None
        entry_price, stop_price = result
        assert entry_price == pytest.approx(93.00)
        assert stop_price < entry_price

    def test_exact_touch_fills(self):
        # 安値がちょうど指値 -> 約定する（境界は約定側）
        df = _frame([99.0, 93.00, 99.0, 99.0, 99.0, 99.0], [101.0] * 6)
        result = System3Strategy().compute_entry(df, {"entry_date": ENTRY_DATE}, 1e5)
        assert result is not None
        assert result[0] == pytest.approx(93.00)


# --------------------------------------------------------------------------
# System5: long limit = prev_close * 0.97 = 97.00
# --------------------------------------------------------------------------
class TestSystem5LimitFill:
    def test_no_fill_when_bar_never_reaches_limit(self):
        df = _frame([99.0, 97.01, 99.0, 99.0, 99.0, 99.0], [101.0] * 6)
        result = System5Strategy().compute_entry(df, {"entry_date": ENTRY_DATE}, 1e5)
        assert result is None

    def test_fills_at_limit_price_when_bar_reaches_it(self):
        df = _frame([99.0, 96.0, 99.0, 99.0, 99.0, 99.0], [101.0] * 6)
        result = System5Strategy().compute_entry(df, {"entry_date": ENTRY_DATE}, 1e5)
        assert result is not None
        entry_price, stop_price = result
        assert entry_price == pytest.approx(97.00)
        assert stop_price < entry_price


# --------------------------------------------------------------------------
# System6: short limit = prev_close * 1.05 = 105.00
# --------------------------------------------------------------------------
class TestSystem6LimitFill:
    def test_no_fill_when_bar_never_reaches_limit(self):
        # 当日高値 104.99 は売り指値 105.00 に届かない -> 不約定
        df = _frame([99.0] * 6, [101.0, 104.99, 101.0, 101.0, 101.0, 101.0])
        result = System6Strategy().compute_entry(df, {"entry_date": ENTRY_DATE}, 1e5)
        assert result is None

    def test_fills_at_limit_price_when_bar_reaches_it(self):
        df = _frame([99.0] * 6, [101.0, 106.0, 101.0, 101.0, 101.0, 101.0])
        result = System6Strategy().compute_entry(df, {"entry_date": ENTRY_DATE}, 1e5)
        assert result is not None
        entry_price, stop_price = result
        assert entry_price == pytest.approx(105.00)
        assert stop_price > entry_price  # short


# --------------------------------------------------------------------------
# 欠損バー: fail-closed（幻の建玉を作らない）
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "strategy_cls, side",
    [(System3Strategy, "long"), (System5Strategy, "long"), (System6Strategy, "short")],
)
def test_nan_bar_does_not_fill(strategy_cls, side):
    lows = [99.0] * 6
    highs = [101.0] * 6
    if side == "long":
        lows[1] = float("nan")
    else:
        highs[1] = float("nan")
    df = _frame(lows, highs)
    assert strategy_cls().compute_entry(df, {"entry_date": ENTRY_DATE}, 1e5) is None


# --------------------------------------------------------------------------
# エンジン側: 不約定の候補はトレードとして記録されない
# --------------------------------------------------------------------------
def _run_engine(df: pd.DataFrame) -> pd.DataFrame:
    strategy = System3Strategy()
    candidates = {
        pd.Timestamp(ENTRY_DATE): [
            {"symbol": "TEST", "entry_date": pd.Timestamp(ENTRY_DATE)}
        ]
    }
    trades, _logs = simulate_trades_with_risk(
        candidates, {"TEST": df}, 100_000.0, strategy, side="long"
    )
    return trades


def test_engine_skips_unfilled_candidate():
    df = _frame([99.0, 93.01, 99.0, 99.0, 99.0, 99.0], [101.0] * 6)
    assert _run_engine(df).empty


def test_engine_books_filled_candidate():
    df = _frame([99.0, 92.5, 99.0, 99.0, 99.0, 99.0], [101.0] * 6)
    trades = _run_engine(df)
    assert not trades.empty
    assert float(trades.iloc[0]["entry_price"]) == pytest.approx(93.00)
