"""System8（SPY オーバーナイト FOMC プレドリフト）の決定的テスト。

ネットワーク・ライブ発注は一切行わない。小さな SPY 価格系列と既知の FOMC 日付
（テンポラリ CSV）だけで、カレンダー読込・setup 付与・候補生成・オーバーナイト
バックテスト損益を検証する。

出所戦略: 別リポジトリ n0150_fomc_macro_event_drift_spy（rules_frozen.md v03）。
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from core.system8 import (
    SYSTEM8_SYMBOL,
    generate_candidates_system8,
    get_total_days_system8,
    load_fomc_event_dates,
    prepare_data_vectorized_system8,
)
from strategies.system8_strategy import System8Strategy

# --- フィクスチャ -----------------------------------------------------------

# 取引日（営業日）: 2024-01-15 .. 2024-02-15。
_FIXTURE_DATES = pd.bdate_range("2024-01-15", "2024-02-15")

# 予定 FOMC 声明日 T = 2024-01-31（水曜・取引日）。setup 日 T-1 = 2024-01-30。
_EVENT_DATE = pd.Timestamp("2024-01-31")
_SETUP_DATE = pd.Timestamp("2024-01-30")

# エントリー（T-1 引け）とエグジット（T 寄り）の価格を固定して損益を計算可能にする。
_ENTRY_CLOSE = 100.0  # Close of 2024-01-30 (MOC)
_EXIT_OPEN = 101.0  # Open of 2024-01-31 (MOO) → +1.0/share overnight


def _make_spy_df() -> pd.DataFrame:
    """既知の Open/Close を持つ SPY 日足フィクスチャを作る。"""
    n = len(_FIXTURE_DATES)
    df = pd.DataFrame(
        {
            "Open": [100.0] * n,
            "High": [102.0] * n,
            "Low": [98.0] * n,
            "Close": [100.0] * n,
            "Volume": [1_000_000] * n,
        },
        index=_FIXTURE_DATES,
    )
    df.loc[_SETUP_DATE, "Close"] = _ENTRY_CLOSE
    df.loc[_EVENT_DATE, "Open"] = _EXIT_OPEN
    return df


def _write_fomc_csv(tmp_path: Path, dates: list[str]) -> Path:
    """テスト用 FOMC カレンダー CSV を書き出してパスを返す。"""
    rows = "\n".join(
        f"{d},18:00,fomc,,,,,test,2026-07-16T00:00:00+00:00" for d in dates
    )
    header = "event_date,event_time_utc,event_type,actual,forecast,previous,surprise_z,source,fetch_ts"
    path = tmp_path / "fomc.csv"
    path.write_text(f"{header}\n{rows}\n", encoding="utf-8")
    return path


# --- load_fomc_event_dates --------------------------------------------------


def test_load_fomc_event_dates_parses_and_filters(tmp_path: Path) -> None:
    # 予定 fomc 2 件 + 別種別 1 件（除外されるべき）。
    path = tmp_path / "fomc.csv"
    path.write_text(
        "event_date,event_type\n"
        "2024-01-31,fomc\n"
        "2024-03-20,fomc\n"
        "2024-02-14,cpi\n",
        encoding="utf-8",
    )
    dates = load_fomc_event_dates(path)
    assert list(dates) == [pd.Timestamp("2024-01-31"), pd.Timestamp("2024-03-20")]


def test_load_fomc_event_dates_missing_file_returns_empty(tmp_path: Path) -> None:
    dates = load_fomc_event_dates(tmp_path / "does_not_exist.csv")
    assert len(dates) == 0


def test_bundled_calendar_loads_and_covers_range() -> None:
    """リポジトリ同梱の data/events/fomc.csv が読め、年8回×多年をカバーする。"""
    dates = load_fomc_event_dates()  # 既定パス
    assert len(dates) >= 8 * 15  # 2006-2027 なら 100+ 件
    assert dates.min() <= pd.Timestamp("2006-12-31")
    assert dates.max() >= pd.Timestamp("2027-01-01")


# --- prepare_data -----------------------------------------------------------


def test_prepare_data_marks_setup_on_t_minus_1(tmp_path: Path) -> None:
    path = _write_fomc_csv(tmp_path, ["2024-01-31"])
    prepared = prepare_data_vectorized_system8(
        {SYSTEM8_SYMBOL: _make_spy_df()}, fomc_calendar_path=path
    )
    df = prepared[SYSTEM8_SYMBOL]
    # setup は T-1 のみ True。
    assert bool(df.loc[_SETUP_DATE, "setup"]) is True
    assert bool(df.loc[_EVENT_DATE, "setup"]) is False
    assert int(df["setup"].sum()) == 1
    # fomc_event は T のみ True。
    assert bool(df.loc[_EVENT_DATE, "fomc_event"]) is True
    # setup 日に対応する event_date が T を指す。
    assert pd.Timestamp(df.loc[_SETUP_DATE, "fomc_event_date"]) == _EVENT_DATE


def test_prepare_data_drops_non_trading_day_event(tmp_path: Path) -> None:
    # 2024-01-06 は土曜（非取引日）→ index に存在せず setup 生成されない。
    path = _write_fomc_csv(tmp_path, ["2024-01-06"])
    prepared = prepare_data_vectorized_system8(
        {SYSTEM8_SYMBOL: _make_spy_df()}, fomc_calendar_path=path
    )
    df = prepared[SYSTEM8_SYMBOL]
    assert int(df["setup"].sum()) == 0
    assert int(df["fomc_event"].sum()) == 0


# --- generate_candidates ----------------------------------------------------


def test_generate_candidates_full_scan(tmp_path: Path) -> None:
    path = _write_fomc_csv(tmp_path, ["2024-01-31"])
    prepared = prepare_data_vectorized_system8(
        {SYSTEM8_SYMBOL: _make_spy_df()}, fomc_calendar_path=path
    )
    candidates, merged = generate_candidates_system8(prepared)
    assert merged is None
    assert set(candidates.keys()) == {_SETUP_DATE}
    payload = candidates[_SETUP_DATE][SYSTEM8_SYMBOL]
    assert pd.Timestamp(payload["event_date"]) == _EVENT_DATE
    assert pd.Timestamp(payload["exit_date"]) == _EVENT_DATE
    assert payload["entry_price"] == pytest.approx(_ENTRY_CLOSE)
    assert payload["stop_price"] is None  # ストップなし


def test_generate_candidates_latest_only_no_setup_today(tmp_path: Path) -> None:
    # 最終行が setup でない場合、latest_only は空。
    path = _write_fomc_csv(tmp_path, ["2024-01-31"])
    prepared = prepare_data_vectorized_system8(
        {SYSTEM8_SYMBOL: _make_spy_df()}, fomc_calendar_path=path
    )
    candidates, _df, diag = generate_candidates_system8(
        prepared, latest_only=True, include_diagnostics=True
    )
    assert candidates == {}
    assert diag["ranking_source"] is None


def test_generate_candidates_latest_only_setup_today(tmp_path: Path) -> None:
    # 最終行が setup になるようフィクスチャを組む: 声明日を最終行の翌営業日にできない
    # ため、setup 日（T-1）が末尾になるよう短い系列を作る。
    dates = pd.bdate_range("2024-01-29", "2024-01-30")  # 30 が末尾（= T-1）
    df = pd.DataFrame(
        {
            "Open": [100.0, 100.0],
            "High": [101.0, 101.0],
            "Low": [99.0, 99.0],
            "Close": [100.0, _ENTRY_CLOSE],
            "Volume": [1, 1],
        },
        index=dates,
    )
    path = _write_fomc_csv(tmp_path, ["2024-01-31"])
    prepared = prepare_data_vectorized_system8(
        {SYSTEM8_SYMBOL: df}, fomc_calendar_path=path
    )
    candidates, df_fast, diag = generate_candidates_system8(
        prepared, latest_only=True, include_diagnostics=True
    )
    assert diag["ranking_source"] == "latest_only"
    assert len(candidates) == 1
    assert df_fast is not None and not df_fast.empty


# --- run_backtest / sizing --------------------------------------------------


def test_strategy_end_to_end_backtest(tmp_path: Path) -> None:
    strat = System8Strategy()
    # position_pct を満額に固定（config 依存を排除して決定的に）。
    strat.config["position_pct"] = 1.0
    strat.config["cost_bps_roundtrip"] = 2.0

    path = _write_fomc_csv(tmp_path, ["2024-01-31"])
    prepared = strat.prepare_data(
        {SYSTEM8_SYMBOL: _make_spy_df()}, fomc_calendar_path=path
    )
    candidates, _ = strat.generate_candidates(prepared)
    capital = 100_000.0
    trades = strat.run_backtest(prepared, candidates, capital)

    assert len(trades) == 1
    row = trades.iloc[0]
    assert row["symbol"] == SYSTEM8_SYMBOL
    assert pd.Timestamp(row["entry_date"]) == _SETUP_DATE
    assert pd.Timestamp(row["exit_date"]) == _EVENT_DATE
    assert row["entry_price"] == pytest.approx(_ENTRY_CLOSE)
    assert row["exit_price"] == pytest.approx(_EXIT_OPEN)
    # shares = floor(100000 / 100) = 1000
    assert int(row["shares"]) == 1000
    # gross = (101-100)*1000 = 1000 ; cost = 2bp * 100 * 1000 = 20 ; pnl = 980
    assert row["pnl"] == pytest.approx(980.0)


def test_calculate_position_size_equal_notional_no_stop() -> None:
    strat = System8Strategy()
    strat.config["position_pct"] = 1.0
    # ストップ引数なしでも等ノーショナルで株数を返す。
    assert strat.calculate_position_size(100_000.0, 100.0) == 1000
    # position_pct=0.5 → 半額。
    strat.config["position_pct"] = 0.5
    assert strat.calculate_position_size(100_000.0, 100.0) == 500
    # レバレッジは掛からない（position_pct>1 は 1.0 にクランプ）。
    strat.config["position_pct"] = 2.0
    assert strat.calculate_position_size(100_000.0, 100.0) == 1000


def test_get_trading_side_is_long() -> None:
    assert System8Strategy().get_trading_side() == "long"


def test_get_total_days() -> None:
    df = _make_spy_df()
    assert get_total_days_system8({SYSTEM8_SYMBOL: df}) == len(_FIXTURE_DATES)
