"""Unit tests for scripts/cache_daily_polygon.py — the flatten-bug epicentre.

このモジュールは 2026-07-02 の catastrophic silent bug (12,443 base file が
500 日 → 1 行に flatten され誰にも検知されなかった) 再発防止の第一線。

対象:
    scripts/cache_daily_polygon.py
        - iter_business_days
        - pivot_to_symbol_frames
        - _merge_with_existing_full_csv    ← flatten 防止の要
        - write_symbol_to_cache            ← daily_pipeline.ps1 が叩く write path
        - main (CLI: --start==--end, dry-run, invalid date range)

未 test だった理由:
    grep -rln 'cache_daily_polygon' tests/ → 0 hit (Phase 1 audit)。
    398 行の production write スクリプトに単一 test が無かった。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd
import pytest

import scripts.cache_daily_polygon as cdp

# ---------- fixtures ---------------------------------------------------------


def _ohlcv_frame(dates: list[str], base_price: float = 100.0) -> pd.DataFrame:
    """EODHD/Alpaca 互換の Date-indexed OHLCV frame (AdjClose=Close)."""
    idx = pd.to_datetime(dates)
    n = len(dates)
    df = pd.DataFrame(
        {
            "Open": [base_price + i * 0.1 for i in range(n)],
            "High": [base_price + 1.0 + i * 0.1 for i in range(n)],
            "Low": [base_price - 1.0 + i * 0.1 for i in range(n)],
            "Close": [base_price + 0.5 + i * 0.1 for i in range(n)],
            "AdjClose": [base_price + 0.5 + i * 0.1 for i in range(n)],
            "Volume": [1_000_000 + i * 1000 for i in range(n)],
        },
        index=pd.Index(idx, name="Date"),
    )
    return df


def _grouped_daily(symbols: list[str], base_price: float) -> pd.DataFrame:
    """Polygon Grouped Daily 応答形式 (index=symbol)."""
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


# ---------- iter_business_days ----------------------------------------------


class TestIterBusinessDays:
    def test_yields_weekdays_only(self):
        # 2026-07-04 (Sat) と 2026-07-05 (Sun) を挟む範囲
        result = list(cdp.iter_business_days(date(2026, 7, 3), date(2026, 7, 6)))
        assert result == [date(2026, 7, 3), date(2026, 7, 6)]

    def test_single_day_weekday(self):
        result = list(cdp.iter_business_days(date(2026, 7, 2), date(2026, 7, 2)))
        assert result == [date(2026, 7, 2)]

    def test_single_day_weekend_yields_nothing(self):
        result = list(cdp.iter_business_days(date(2026, 7, 4), date(2026, 7, 4)))
        assert result == []

    def test_end_before_start_yields_nothing(self):
        result = list(cdp.iter_business_days(date(2026, 7, 10), date(2026, 7, 5)))
        assert result == []


# ---------- pivot_to_symbol_frames ------------------------------------------


class TestPivotToSymbolFrames:
    def test_pivots_multi_day_to_per_symbol(self):
        panel = {
            "2026-07-01": _grouped_daily(["AAPL", "MSFT"], 100.0),
            "2026-07-02": _grouped_daily(["AAPL", "MSFT"], 101.0),
        }
        frames = cdp.pivot_to_symbol_frames(panel)
        assert set(frames.keys()) == {"AAPL", "MSFT"}
        for sym, df in frames.items():
            assert list(df.columns) == [
                "Open",
                "High",
                "Low",
                "Close",
                "AdjClose",
                "Volume",
            ]
            assert df.index.name == "Date"
            assert len(df) == 2
            # AdjClose == Close (unadjusted grouped daily)
            assert (df["AdjClose"] == df["Close"]).all()
            # 昇順
            assert df.index.is_monotonic_increasing

    def test_symbol_filter_applied(self):
        panel = {"2026-07-01": _grouped_daily(["AAPL", "MSFT", "SPY"], 100.0)}
        frames = cdp.pivot_to_symbol_frames(panel, symbols={"AAPL", "SPY"})
        assert set(frames.keys()) == {"AAPL", "SPY"}

    def test_duplicate_date_keeps_last(self):
        # 同一 Date が 2 回入る (fetch retry 由来を想定)
        panel = {
            "2026-07-01": _grouped_daily(["AAPL"], 100.0),
        }
        # 手動で 2 レコード目を追加した panel
        panel["2026-07-01"] = pd.concat(
            [panel["2026-07-01"], _grouped_daily(["AAPL"], 200.0)]
        )
        frames = cdp.pivot_to_symbol_frames(panel)
        assert len(frames["AAPL"]) == 1

    def test_empty_panel_returns_empty_dict(self):
        assert cdp.pivot_to_symbol_frames({}) == {}


# ---------- _merge_with_existing_full_csv (flatten 防止の要) ----------------


class TestMergeWithExistingFullCsv:
    """既存 CSV との Date キー merge = 履歴保存の要。

    このメソッドが働いていなかった (旧実装は素の to_csv 上書き) ことが
    12,443 file の flatten bug の直接原因。
    """

    def test_new_file_returns_new_df_as_is(self, tmp_path: Path):
        new_df = _ohlcv_frame(["2026-07-02"]).reset_index()
        result = cdp._merge_with_existing_full_csv(new_df, tmp_path / "nonexistent.csv")
        assert len(result) == 1
        assert pd.to_datetime(result["Date"].iloc[0]) == pd.Timestamp("2026-07-02")

    def test_existing_500_rows_plus_one_day_yields_501(self, tmp_path: Path):
        """★ flatten bug 検知の decisive assertion。

        既存 500 日 CSV に翌日 1 行を append したら 501 行になる。
        旧実装 (merge 無し) はここで len==1 になる (= flatten 再現)。
        """
        hist = _ohlcv_frame(
            [d.strftime("%Y-%m-%d") for d in pd.bdate_range("2024-08-01", periods=500)]
        )
        csv_path = tmp_path / "AAPL.csv"
        hist.reset_index().to_csv(csv_path, index=False)

        new_day = _ohlcv_frame(["2026-07-02"]).reset_index()
        merged = cdp._merge_with_existing_full_csv(new_day, csv_path)

        assert len(merged) == 501, (
            f"flatten bug 再発: 500日 + 1日 → {len(merged)} 行 "
            "(期待値 501)。既存 CSV の merge が effective でない。"
        )
        # 最古日が保存されていること (履歴破壊の決定的判定)
        assert pd.to_datetime(merged["Date"].min()) == pd.to_datetime("2024-08-01")
        # 最新日が新規側 (翌日) であること
        assert pd.to_datetime(merged["Date"].max()) == pd.to_datetime("2026-07-02")
        # 単調増加 + 重複なし
        dates = pd.to_datetime(merged["Date"])
        assert dates.is_monotonic_increasing
        assert dates.is_unique

    def test_same_day_duplicate_keeps_new(self, tmp_path: Path):
        """同一 Date が既存にも new にもある場合、new (Close 200) が残る。"""
        hist = pd.DataFrame(
            {
                "Date": ["2026-07-02"],
                "Open": [100.0],
                "High": [101.0],
                "Low": [99.0],
                "Close": [100.0],
                "AdjClose": [100.0],
                "Volume": [1_000_000],
            }
        )
        csv_path = tmp_path / "AAPL.csv"
        hist.to_csv(csv_path, index=False)

        new_df = pd.DataFrame(
            {
                "Date": pd.to_datetime(["2026-07-02"]),
                "Open": [200.0],
                "High": [201.0],
                "Low": [199.0],
                "Close": [200.0],
                "AdjClose": [200.0],
                "Volume": [2_000_000],
            }
        )
        merged = cdp._merge_with_existing_full_csv(new_df, csv_path)
        assert len(merged) == 1
        assert float(merged["Close"].iloc[0]) == 200.0  # new 優先

    def test_lowercase_date_column_normalized(self, tmp_path: Path):
        """既存 CSV が 'date' 小文字列でも merge できる。"""
        hist = pd.DataFrame(
            {
                "date": ["2026-07-01"],
                "Open": [100.0],
                "High": [101.0],
                "Low": [99.0],
                "Close": [100.0],
                "AdjClose": [100.0],
                "Volume": [1_000_000],
            }
        )
        csv_path = tmp_path / "AAPL.csv"
        hist.to_csv(csv_path, index=False)

        new_df = _ohlcv_frame(["2026-07-02"]).reset_index()
        merged = cdp._merge_with_existing_full_csv(new_df, csv_path)
        assert len(merged) == 2

    def test_empty_existing_csv_falls_through(self, tmp_path: Path):
        """既存 CSV が empty (header だけ) の場合 new_df のみ返す。"""
        csv_path = tmp_path / "AAPL.csv"
        csv_path.write_text("Date,Open,High,Low,Close,AdjClose,Volume\n")
        new_df = _ohlcv_frame(["2026-07-02"]).reset_index()
        merged = cdp._merge_with_existing_full_csv(new_df, csv_path)
        assert len(merged) == 1

    def test_unreadable_existing_csv_returns_new_only(self, tmp_path: Path, caplog):
        """既存 CSV 読取失敗時: 現状は new_df のみ返す (= 履歴破壊の再発経路)。

        NOTE: この branch は将来的に .bak 退避 or fail-fast にすべき (Phase 1 audit
        追加提案 1)。current behavior を固定して、変更時に意識的に更新できるようにする。
        """
        csv_path = tmp_path / "AAPL.csv"
        csv_path.write_bytes(b"\xff\xfe garbage that pandas cannot parse\x00\x01\x02")
        new_df = _ohlcv_frame(["2026-07-02"]).reset_index()

        with caplog.at_level("WARNING"):
            merged = cdp._merge_with_existing_full_csv(new_df, csv_path)

        # current: warning ログを出しつつ new のみ返す
        assert len(merged) == 1
        # 本テストの本質は「読取失敗を silent に握り潰さず loud に WARNING する」こと。
        # 実装の message は英語 "read failure ... new only." (旧: 日本語フレーズ) のため、
        # 言語非依存に「WARNING レベル + 該当ファイルへの言及」で loudness を固定する。
        assert any(
            rec.levelname == "WARNING" and csv_path.name in rec.message
            for rec in caplog.records
        ), "既存 CSV 読取失敗時に WARNING が出ていない = silent path"


# ---------- write_symbol_to_cache (round-trip) ------------------------------


class TestWriteSymbolToCache:
    """write_symbol_to_cache 全体のラウンドトリップ検証。

    save_base_cache は config.settings.get_settings() を叩くため、
    settings.DATA_CACHE_DIR を tmp_path に向ける monkeypatch を挟む。
    """

    @pytest.fixture
    def tmp_settings(self, tmp_path, monkeypatch):
        """get_settings を tmp_path 向けに差し替える最小 fixture。"""
        from types import SimpleNamespace

        data_cache = tmp_path / "data_cache"
        (data_cache / "full_backup").mkdir(parents=True)
        (data_cache / "base").mkdir(parents=True)

        # get_settings を呼ぶ場所 (common.cache_manager) を patch
        fake_settings = SimpleNamespace(
            DATA_CACHE_DIR=str(data_cache),
            cache=SimpleNamespace(
                full_dir=str(data_cache / "full_backup"),
                round_decimals=4,
            ),
        )
        monkeypatch.setattr(
            "common.cache_manager.get_settings",
            lambda create_dirs=True: fake_settings,
        )
        return fake_settings, data_cache

    def test_fresh_write_creates_csv_and_feather(self, tmp_settings):
        settings, data_cache = tmp_settings
        full_dir = data_cache / "full_backup"

        df = _ohlcv_frame(
            [d.strftime("%Y-%m-%d") for d in pd.bdate_range("2026-06-01", periods=30)]
        )
        ok = cdp.write_symbol_to_cache("AAPL", df, full_dir=full_dir, round_decimals=4)
        assert ok is True
        assert (full_dir / "AAPL.csv").exists()
        assert (data_cache / "base" / "AAPL.feather").exists()
        # 一時ファイルは残らない
        assert not (full_dir / "AAPL.csv.tmp").exists()

    def test_multi_day_history_survives_single_day_write(self, tmp_settings):
        """★ flatten bug 再発防止の decisive assertion (integration 相当)。

        1) 既存 200 営業日を write (これで CSV 200 行が確立)
        2) 翌日 1 行だけ write
        3) 結果: 201 行になり、最古日は #1 と一致し続ける
        """
        settings, data_cache = tmp_settings
        full_dir = data_cache / "full_backup"

        # step 1: 200 日を write (round decimals 4)
        hist_dates = [
            d.strftime("%Y-%m-%d") for d in pd.bdate_range("2025-09-01", periods=200)
        ]
        hist_df = _ohlcv_frame(hist_dates)
        assert (
            cdp.write_symbol_to_cache(
                "AAPL", hist_df, full_dir=full_dir, round_decimals=4
            )
            is True
        )

        csv_path = full_dir / "AAPL.csv"
        after_hist = pd.read_csv(csv_path, parse_dates=["Date"])
        assert len(after_hist) == 200
        first_date = after_hist["Date"].min()

        # step 2: 翌 1 営業日を write (daily_pipeline.ps1 が叩く形態)
        next_day = pd.bdate_range(hist_df.index.max() + pd.Timedelta(days=1), periods=1)
        new_day_df = _ohlcv_frame([next_day[0].strftime("%Y-%m-%d")])
        assert (
            cdp.write_symbol_to_cache(
                "AAPL", new_day_df, full_dir=full_dir, round_decimals=4
            )
            is True
        )

        # step 3: 履歴が残っていることを assert (この 3 点で flatten を捕捉)
        result = pd.read_csv(csv_path, parse_dates=["Date"])
        assert len(result) == 201, (
            f"flatten 再発: 200日 write 後に 1日 write したら {len(result)} 行 "
            "(期待値 201)。write_symbol_to_cache 中の merge が働いていない。"
        )
        assert (
            result["Date"].min() == first_date
        ), "最古日が消えた = 履歴上書き = flatten bug 再現"
        # 単調増加 + 重複 0
        assert result["Date"].is_monotonic_increasing
        assert result["Date"].is_unique

        # base feather も同じ行数まで累積している必要がある (save_base_cache は
        # 上書きだが merged_full を渡しているので OK になるはず)
        base_path = data_cache / "base" / "AAPL.feather"
        base_df = pd.read_feather(base_path)
        assert len(base_df) == 201, (
            f"base feather が {len(base_df)} 行 (full と乖離)。"
            "base 経路の履歴同期が壊れている。"
        )

    def test_repeated_same_day_write_is_idempotent(self, tmp_settings):
        """同日を 3 回連続 write しても行数が増えない (重複蓄積しない)。"""
        settings, data_cache = tmp_settings
        full_dir = data_cache / "full_backup"

        df = _ohlcv_frame(["2026-07-02"])
        for _ in range(3):
            assert (
                cdp.write_symbol_to_cache(
                    "AAPL", df, full_dir=full_dir, round_decimals=4
                )
                is True
            )
        result = pd.read_csv(full_dir / "AAPL.csv", parse_dates=["Date"])
        assert len(result) == 1

    def test_atomic_write_no_leftover_tmp(self, tmp_settings):
        """.csv.tmp が leftover しない (atomic replace 経路)。"""
        settings, data_cache = tmp_settings
        full_dir = data_cache / "full_backup"

        df = _ohlcv_frame(["2026-07-02"])
        cdp.write_symbol_to_cache("AAPL", df, full_dir=full_dir, round_decimals=4)
        # tmp ファイルが残らない (replace 済み)
        tmp_files = list(full_dir.glob("*.tmp"))
        assert tmp_files == []


# ---------- main() CLI 契約 --------------------------------------------------


class TestMainCliContract:
    def test_end_before_start_returns_nonzero(self):
        rc = cdp.main(["--start", "2026-07-10", "--end", "2026-07-05", "--sleep", "0"])
        assert rc == 1

    def test_dry_run_does_not_write(self, tmp_path, monkeypatch):
        """--dry-run: fetch/pivot だけ行い、cache 書き込みをスキップ。"""
        from types import SimpleNamespace

        data_cache = tmp_path / "data_cache"
        (data_cache / "full_backup").mkdir(parents=True)
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
        # Grouped Daily を mock
        monkeypatch.setattr(
            cdp,
            "get_polygon_grouped_daily",
            lambda ds: _grouped_daily(["AAPL", "SPY"], 100.0),
        )

        rc = cdp.main(
            [
                "--start",
                "2026-07-02",
                "--end",
                "2026-07-02",
                "--sleep",
                "0",
                "--dry-run",
            ]
        )
        assert rc == 0
        # cache に何も書かれていない
        assert list((data_cache / "full_backup").glob("*.csv")) == []
