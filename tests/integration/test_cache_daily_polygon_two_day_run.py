"""Integration test: cache_daily_polygon の 2 日連続実行で履歴が単調累積すること。

Phase 1 audit の §4 パターン 2 実装。
unit test (`tests/test_cache_daily_polygon_merge.py`) が merge 関数単体の
正当性を保証するのに対し、こちらは `main()` の CLI 契約から `run_backfill`
→ `write_symbol_to_cache` → CSV/feather 書き出しまでの結線を検証する。

daily_pipeline.ps1 が実運用で叩く形態:
    python scripts/cache_daily_polygon.py --start {date} --end {date} --sleep 13

を 2 営業日ぶん連続で mock 実行し、既存 CSV に 1 日ぶんの新レコードが
append され続けることを assert する。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

import scripts.cache_daily_polygon as cdp


def _grouped_daily(symbols: list[str], base_price: float) -> pd.DataFrame:
    """Polygon Grouped Daily 応答 (index=symbol)."""
    return pd.DataFrame(
        {
            "Open": [base_price] * len(symbols),
            "High": [base_price + 1.0] * len(symbols),
            "Low": [base_price - 1.0] * len(symbols),
            "Close": [base_price + 0.5] * len(symbols),
            "Volume": [1_000_000] * len(symbols),
        },
        index=pd.Index(symbols, name="symbol"),
    )


@pytest.fixture
def tmp_cache(tmp_path, monkeypatch):
    """settings.cache.full_dir / DATA_CACHE_DIR を tmp_path に向ける。"""
    data_cache = tmp_path / "data_cache"
    (data_cache / "full_backup").mkdir(parents=True)
    (data_cache / "base").mkdir(parents=True)

    fake_settings = SimpleNamespace(
        DATA_CACHE_DIR=str(data_cache),
        cache=SimpleNamespace(
            full_dir=str(data_cache / "full_backup"),
            round_decimals=4,
        ),
    )
    # run_backfill 内の get_settings と save_base_cache 内 (common.cache_manager)
    # の get_settings を両方差し替える
    monkeypatch.setattr(
        "config.settings.get_settings",
        lambda create_dirs=True: fake_settings,
    )
    monkeypatch.setattr(
        "common.cache_manager.get_settings",
        lambda create_dirs=True: fake_settings,
    )
    return data_cache


def test_two_consecutive_daily_runs_grow_full_csv(tmp_cache, monkeypatch):
    """★ flatten bug integration 検知の decisive test。

    Day1: --start 2026-06-30 --end 2026-06-30
    Day2: --start 2026-07-01 --end 2026-07-01
    → AAPL.csv は 2 行 (履歴累積) になる。旧実装 (merge 無し) では
      Day2 で 1 行になり flatten 再現。
    """
    data_cache = tmp_cache
    full_dir = data_cache / "full_backup"

    responses = {
        "2026-06-30": _grouped_daily(["AAPL", "SPY"], 100.0),
        "2026-07-01": _grouped_daily(["AAPL", "SPY"], 101.0),
    }
    monkeypatch.setattr(
        cdp, "get_polygon_grouped_daily",
        lambda ds: responses.get(ds, pd.DataFrame()),
    )

    # Day1 実行
    rc = cdp.main([
        "--start", "2026-06-30", "--end", "2026-06-30", "--sleep", "0",
    ])
    assert rc == 0
    aapl_after_d1 = pd.read_csv(full_dir / "AAPL.csv", parse_dates=["Date"])
    assert len(aapl_after_d1) == 1
    d1_date = aapl_after_d1["Date"].iloc[0]

    # Day2 実行 (daily_pipeline.ps1 と同形態)
    rc = cdp.main([
        "--start", "2026-07-01", "--end", "2026-07-01", "--sleep", "0",
    ])
    assert rc == 0

    aapl_after_d2 = pd.read_csv(full_dir / "AAPL.csv", parse_dates=["Date"])
    # ── flatten 検知 3 点セット ──
    assert len(aapl_after_d2) == 2, (
        f"flatten 再現: Day2 実行後の AAPL.csv が {len(aapl_after_d2)} 行 "
        "(期待値 2)。既存 CSV を上書きしている = flatten bug。"
    )
    assert aapl_after_d2["Date"].iloc[0] == d1_date, "Day1 データが消えた"
    assert aapl_after_d2["Date"].iloc[-1] == pd.Timestamp("2026-07-01")

    # base feather も同じく累積している
    base_df = pd.read_feather(data_cache / "base" / "AAPL.feather")
    assert len(base_df) == 2, f"base feather が {len(base_df)} 行 (full と乖離)"


def test_symbols_filter_only_affects_targets(tmp_cache, monkeypatch):
    """--symbols で指定した銘柄のみが cache に書かれる。"""
    data_cache = tmp_cache
    full_dir = data_cache / "full_backup"

    responses = {
        "2026-06-30": _grouped_daily(["AAPL", "MSFT", "SPY"], 100.0),
    }
    monkeypatch.setattr(
        cdp, "get_polygon_grouped_daily",
        lambda ds: responses.get(ds, pd.DataFrame()),
    )

    rc = cdp.main([
        "--start", "2026-06-30", "--end", "2026-06-30",
        "--sleep", "0", "--symbols", "AAPL,SPY",
    ])
    assert rc == 0
    written = {p.stem for p in full_dir.glob("*.csv")}
    assert written == {"AAPL", "SPY"}
    assert not (full_dir / "MSFT.csv").exists()


def test_holiday_empty_response_does_not_touch_cache(tmp_cache, monkeypatch):
    """祝日/週末で空応答の日は cache を触らない (既存 CSV は不変)。"""
    data_cache = tmp_cache
    full_dir = data_cache / "full_backup"

    # step1: 有効日 1 日ぶん書き込む
    responses = {"2026-06-30": _grouped_daily(["AAPL"], 100.0)}
    monkeypatch.setattr(
        cdp, "get_polygon_grouped_daily",
        lambda ds: responses.get(ds, pd.DataFrame()),
    )
    cdp.main(["--start", "2026-06-30", "--end", "2026-06-30", "--sleep", "0"])
    csv_path = full_dir / "AAPL.csv"
    before = csv_path.read_bytes()

    # step2: 空応答の日 (=祝日想定) を fetch → cache は触らない
    monkeypatch.setattr(
        cdp, "get_polygon_grouped_daily",
        lambda ds: pd.DataFrame(),
    )
    # 平日だが空応答の日 (2026-07-03 は金曜)。history_cutoff は今日から 730 日前
    # なので、この日 (今日より未来 or 730日以内) では WARNING 経路にも入らない。
    rc = cdp.main(["--start", "2026-07-03", "--end", "2026-07-03", "--sleep", "0"])
    # 空応答のみだと "取得できた営業日が 0 日" → return 0 (log error)
    assert rc == 0

    after = csv_path.read_bytes()
    assert before == after, "空応答の日に既存 cache が改変された = 履歴破壊"


def test_max_symbols_caps_written_count(tmp_cache, monkeypatch):
    """--max-symbols で書き込み数を制限できる。"""
    data_cache = tmp_cache
    full_dir = data_cache / "full_backup"

    responses = {
        "2026-06-30": _grouped_daily(["A", "B", "C", "D", "E"], 100.0),
    }
    monkeypatch.setattr(
        cdp, "get_polygon_grouped_daily",
        lambda ds: responses.get(ds, pd.DataFrame()),
    )
    rc = cdp.main([
        "--start", "2026-06-30", "--end", "2026-06-30",
        "--sleep", "0", "--max-symbols", "3",
    ])
    assert rc == 0
    written = {p.stem for p in full_dir.glob("*.csv")}
    assert len(written) == 3
