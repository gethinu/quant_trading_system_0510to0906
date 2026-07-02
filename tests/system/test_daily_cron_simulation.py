"""System test: 30 営業日の daily cron 実行 sim + flatten detection guardrail.

Phase 1 audit §4 パターン 3 実装。
`daily_pipeline.ps1` cron を 30 営業日 sim し、cache file の行数が
**単調非減少** invariant を保つことを file 単位で assert する。

これは今回の bug が「初日の 1 回目で全 file が 500 → 1 に flatten される」
性質を持っていたため、たった 1 日の実行で違反が捕捉できる. 30 日回すのは
複数営業日での安定性を追加確認するため.
"""

from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest

import scripts.cache_daily_polygon as cdp


def _grouped_daily(symbols: list[str], price: float) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "Open": [price] * len(symbols),
            "High": [price + 1.0] * len(symbols),
            "Low": [price - 1.0] * len(symbols),
            "Close": [price + 0.5] * len(symbols),
            "Volume": [1_000_000] * len(symbols),
        },
        index=pd.Index(symbols, name="symbol"),
    )


@pytest.fixture
def tmp_cache(tmp_path, monkeypatch):
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
    monkeypatch.setattr(
        "config.settings.get_settings",
        lambda create_dirs=True: fake_settings,
    )
    monkeypatch.setattr(
        "common.cache_manager.get_settings",
        lambda create_dirs=True: fake_settings,
    )
    return data_cache


def test_thirty_day_cron_never_shrinks_any_cache_file(tmp_cache, monkeypatch):
    """★ file 単位の単調非減少 invariant.

    30 営業日を cron sim (毎日 `main(['--start', d, '--end', d])`) し、
    各営業日の後で以下を assert:

      1. すべての full_backup CSV 行数が **前回以上**
      2. すべての銘柄が最終的に 30 行に到達 (累積の完全性)

    旧実装 (merge 無し) では 2 日目に (1) が破綻し、
    30 日後も全 file 1 行のままになる (flatten 再現).
    """
    data_cache = tmp_cache
    full_dir = data_cache / "full_backup"

    # 30 営業日ぶんの mock response (2026-06-01..30 は月曜開始)
    symbols = ["AAPL", "MSFT", "SPY", "NVDA", "AMZN"]
    bdays = pd.bdate_range("2026-06-01", periods=30)
    responses = {
        d.strftime("%Y-%m-%d"): _grouped_daily(symbols, 100.0 + i * 0.5)
        for i, d in enumerate(bdays)
    }
    monkeypatch.setattr(
        cdp, "get_polygon_grouped_daily",
        lambda ds: responses.get(ds, pd.DataFrame()),
    )

    row_history: dict[str, int] = {}
    for i, d in enumerate(bdays, 1):
        ds = d.strftime("%Y-%m-%d")
        rc = cdp.main(["--start", ds, "--end", ds, "--sleep", "0"])
        assert rc == 0, f"day {i} ({ds}): main returned {rc}"

        for csv_path in full_dir.glob("*.csv"):
            df = pd.read_csv(csv_path)
            n = len(df)
            prev = row_history.get(csv_path.name, 0)
            # ★ 核心 invariant: cache file は日次運転で絶対に縮まない
            assert n >= prev, (
                f"day {i} ({ds}): {csv_path.name} が {prev} 行 → {n} 行 に縮小。"
                "flatten bug 再発検知。"
            )
            row_history[csv_path.name] = n

    # 30 営業日累積で全銘柄が 30 行に到達
    for sym in symbols:
        assert row_history[f"{sym}.csv"] == 30, (
            f"{sym}: 30 日 cron 実行後 {row_history[f'{sym}.csv']} 行 (期待値 30)."
        )


def test_intermittent_polygon_outage_never_shrinks_cache(tmp_cache, monkeypatch):
    """Polygon が 5 日中 2 日 outage の場合でも既存 cache が縮まない.

    outage 日は grouped daily が空 df を返す想定. `run_backfill` は
    "取得できた営業日が 0 日" で return するので既存 cache に触らない.
    """
    data_cache = tmp_cache
    full_dir = data_cache / "full_backup"

    bdays = pd.bdate_range("2026-06-01", periods=5)
    responses = {}
    for i, d in enumerate(bdays):
        ds = d.strftime("%Y-%m-%d")
        if i in (1, 3):  # 2 日目と 4 日目が outage
            responses[ds] = pd.DataFrame()
        else:
            responses[ds] = _grouped_daily(["AAPL"], 100.0 + i)

    monkeypatch.setattr(
        cdp, "get_polygon_grouped_daily",
        lambda ds: responses.get(ds, pd.DataFrame()),
    )

    seen_rows: list[int] = []
    for d in bdays:
        ds = d.strftime("%Y-%m-%d")
        cdp.main(["--start", ds, "--end", ds, "--sleep", "0"])
        csv = full_dir / "AAPL.csv"
        if csv.exists():
            seen_rows.append(len(pd.read_csv(csv)))
        else:
            seen_rows.append(0)

    # 単調非減少
    for i in range(1, len(seen_rows)):
        assert seen_rows[i] >= seen_rows[i - 1], (
            f"day {i}: 行数 {seen_rows[i - 1]} → {seen_rows[i]} に縮小 "
            f"(outage 日を含む sequence でも縮小は禁止). 履歴={seen_rows}"
        )
    # 有効日 3 日 (i=0,2,4) ぶんが最終的に累積
    assert seen_rows[-1] == 3, f"outage を除いた 3 日ぶんの累積が {seen_rows[-1]}"
