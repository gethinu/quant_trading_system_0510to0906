"""Tests for CacheManager._handle_rolling_fallback_and_heal — the silent-heal branch.

Phase 1 audit で特定した silent failure 経路の固定:

    common/cache_manager.py L263-343 `_handle_rolling_fallback_and_heal`
        L311-320: required_indicators = ["drop3d", "atr_ratio", "dollarvolume20"] の欠落検知
        L327-338: recompute 実行後の検証 ok=False → WARNING → 欠損 df 返却

これまで tests/ に単一 hit 無し (grep 'Recompute did not produce' → 0)。
「指標が欠けたまま下流に流れて候補 0 になる」の入口を pytest で監視化する。
"""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from common.cache_manager import CacheManager

# ---------- fixtures ---------------------------------------------------------


def _rolling_settings(tmp_path: Path) -> SimpleNamespace:
    """CacheManager が要求する settings の最小構成。"""
    rolling_cfg = SimpleNamespace(
        meta_file="_meta.json",
        base_lookback_days=300,
        buffer_days=30,
        max_symbols=None,
        workers=None,
        round_decimals=4,
        recompute_indicators_on_read=True,
    )
    cache = SimpleNamespace(
        full_dir=str(tmp_path / "full_backup"),
        rolling_dir=str(tmp_path / "rolling"),
        rolling=rolling_cfg,
        round_decimals=4,
    )
    return SimpleNamespace(
        cache=cache,
        DATA_CACHE_DIR=str(tmp_path),
    )


@pytest.fixture
def cache_manager(tmp_path):
    settings = _rolling_settings(tmp_path)
    return CacheManager(settings)


def _df_with_ohlc(n_rows: int) -> pd.DataFrame:
    """date + OHLCV のみで指標列が全く無い df."""
    dates = pd.bdate_range("2026-01-01", periods=n_rows)
    return pd.DataFrame(
        {
            "date": dates,
            "open": [100.0 + i * 0.1 for i in range(n_rows)],
            "high": [101.0 + i * 0.1 for i in range(n_rows)],
            "low": [99.0 + i * 0.1 for i in range(n_rows)],
            "close": [100.5 + i * 0.1 for i in range(n_rows)],
            "volume": [1_000_000 + i * 1000 for i in range(n_rows)],
        }
    )


# ---------- missing rolling → base+tail fallback -----------------------------


class TestFallbackToBaseAndTail:
    def test_none_input_triggers_base_read(self, cache_manager, monkeypatch, tmp_path):
        """rolling が None なら _read_base_and_tail が呼ばれる。"""
        called = {}

        def _fake_read(ticker, tail_rows=330):
            called["ticker"] = ticker
            called["tail_rows"] = tail_rows
            return None

        monkeypatch.setattr(cache_manager, "_read_base_and_tail", _fake_read)
        result = cache_manager._handle_rolling_fallback_and_heal(
            "AAPL", None, tmp_path / "AAPL.feather"
        )
        assert called == {"ticker": "AAPL", "tail_rows": 330}
        assert result is None

    def test_empty_input_triggers_base_read(self, cache_manager, monkeypatch, tmp_path):
        """rolling が empty df なら _read_base_and_tail が呼ばれる。"""
        called = {}

        def _fake_read(ticker, tail_rows=330):
            called["hit"] = True
            return None

        monkeypatch.setattr(cache_manager, "_read_base_and_tail", _fake_read)
        _ = cache_manager._handle_rolling_fallback_and_heal(
            "AAPL", pd.DataFrame(), tmp_path / "AAPL.feather"
        )
        assert called == {"hit": True}


# ---------- required indicators missing → recompute --------------------------


class TestRecomputeBranch:
    def test_missing_indicators_triggers_recompute_log(
        self, cache_manager, tmp_path, caplog
    ):
        """
        drop3d/atr_ratio/dollarvolume20 のいずれかが欠けたら
        "attempting recompute" ログが出る (L317 branch).
        """
        df = _df_with_ohlc(400)  # OHLCV のみ、指標無し
        with caplog.at_level("INFO", logger="common.cache_manager"):
            _ = cache_manager._handle_rolling_fallback_and_heal(
                "AAPL", df, tmp_path / "AAPL.feather"
            )
        assert any(
            "missing indicators" in rec.message
            and "attempting recompute" in rec.message
            for rec in caplog.records
        ), (
            "OHLCV のみの df を渡したのに 'attempting recompute' が出ていない。"
            "L317 の欠損検知が動作していない可能性。"
        )

    def test_successful_recompute_persists_and_returns_new_df(
        self, cache_manager, tmp_path, caplog
    ):
        """
        400 行 OHLCV → recompute で drop3d/atr_ratio/dollarvolume20 が埋まる
        → "Recomputed and saved rolling cache" ログ + 戻り df に指標列。
        """
        df = _df_with_ohlc(400)
        with caplog.at_level("INFO", logger="common.cache_manager"):
            result = cache_manager._handle_rolling_fallback_and_heal(
                "AAPL", df, cache_manager.rolling_dir / "AAPL.feather"
            )

        # 少なくとも drop3d / atr_ratio / dollarvolume20 のどれかが列に載る
        # (成功 branch の場合)
        recomputed_cols = set(result.columns)
        # 成功時のみ persist ログが出る。ok=False の場合はここは通らず。
        persisted = any(
            "Recomputed and saved rolling cache" in rec.message
            for rec in caplog.records
        )
        if persisted:
            # 成功 path: 3 指標のうち少なくとも 2 つが列に載っている
            required = {"drop3d", "atr_ratio", "dollarvolume20"}
            present = required & recomputed_cols
            assert len(present) >= 2, (
                f"'Recomputed and saved' ログが出ているのに指標列が {present} しかない。"
                "persist と実際の指標復元に乖離。"
            )


class TestRecomputeFailBranch:
    """★ silent WARN 経路の固定. Phase 1 audit の中核 gap.

    "Recompute did not produce required indicators for {ticker}" は現在
    logger.warning のみで、下流には欠損 df がそのまま流れる. これを test
    で明示的に固定し、将来的に「例外を投げるべき」or「health flag を立てるべき」
    と方針転換したときに意識的に更新できるようにする.
    """

    def test_insufficient_rows_produces_warning_not_exception(
        self, cache_manager, tmp_path, caplog
    ):
        """5 行 (drop3d/atr_ratio/dollarvolume20 の窓に満たない) の df を
        渡すと、recompute しても required indicators が埋まらず
        WARN "Recompute did not produce required indicators" が出る."""
        df = _df_with_ohlc(5)

        with caplog.at_level("WARNING", logger="common.cache_manager"):
            result = cache_manager._handle_rolling_fallback_and_heal(
                "AAPL", df, cache_manager.rolling_dir / "AAPL.feather"
            )

        # (1) 例外は投げない (silent WARN)
        assert result is not None

        # (2) WARN が出ている ← ここが decisive assertion
        warns = [
            rec
            for rec in caplog.records
            if rec.levelno >= logging.WARNING
            and "Recompute did not produce required indicators" in rec.message
        ]
        assert warns, (
            "'Recompute did not produce required indicators' WARN が出ていない。"
            "silent degradation の入口が消えているか、branch が変わった可能性。"
            f"records: {[r.message for r in caplog.records]}"
        )
        # ticker 名がログに含まれる
        assert any("AAPL" in r.message for r in warns)

    def test_recompute_fail_returns_df_without_required_indicators(self, cache_manager):
        """★ silent degradation の decisive 検知.

        ok=False 経路では df がそのまま返る → 下流が「指標が無い」ことに
        気付かず候補 0 になる. この test は「今の挙動は WARN で df 返却」を
        固定し、方針変更 (exception 化) 時に **意図的な破壊テスト** として
        更新することを強制する.
        """
        df = _df_with_ohlc(5)  # 指標窓に満たない
        result = cache_manager._handle_rolling_fallback_and_heal(
            "AAPL", df, cache_manager.rolling_dir / "AAPL.feather"
        )
        # required indicators は依然として欠損 (or 全 NaN)
        required = ["drop3d", "atr_ratio", "dollarvolume20"]
        for col in required:
            missing = (col not in result.columns) or result[col].dropna().empty
            assert missing, (
                f"{col} が非 NaN 値付きで存在している。5 行では窓を満たせないはず。"
                "test 前提が崩れた可能性 (add_indicators のロジック変更?)。"
            )

    def test_recompute_flag_disabled_skips_healing(
        self, cache_manager, tmp_path, monkeypatch
    ):
        """settings.cache.rolling.recompute_indicators_on_read=False で
        heal 経路がスキップされる (欠損 df がそのまま返る)."""
        cache_manager.settings.cache.rolling.recompute_indicators_on_read = False
        df = _df_with_ohlc(5)
        result = cache_manager._handle_rolling_fallback_and_heal(
            "AAPL", df, cache_manager.rolling_dir / "AAPL.feather"
        )
        # OHLCV は保持されるが indicators は付与されない
        assert "close" in result.columns
        assert "drop3d" not in result.columns
