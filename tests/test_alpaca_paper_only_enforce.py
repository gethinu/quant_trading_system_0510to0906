"""Paper 口座強制ガードの regression test.

`common.alpaca_trading.assert_paper_env` は実発注経路 (``dry_run=False``) の直前で
呼ばれる safety gate。以下 4 パターンを固定化する:

1. ``ALPACA_PAPER=false`` → LiveAccountGuardError を raise (基本ガード)
2. ``ALPACA_API_BASE_URL`` が live host を指す → LiveAccountGuardError (URL ガード)
3. strict mode ON かつ ``ALPACA_PAPER`` 未設定 → LiveAccountGuardError (opt-in 強制)
4. strict mode OFF かつ ``ALPACA_PAPER`` 未設定 → 通過 (paper フォールバック)

Notes:
    daily_pipeline.ps1 の paper_orders step が誤って live URL を掴む/paper env が
    外れる regression を pytest layer で block する。
"""

from __future__ import annotations

import pytest

from common.alpaca_trading import LiveAccountGuardError, assert_paper_env


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """各 test の始点で ALPACA_* env を消去 (test 間の漏れ防止)。"""
    for k in ("ALPACA_PAPER", "ALPACA_PAPER_STRICT", "ALPACA_API_BASE_URL"):
        monkeypatch.delenv(k, raising=False)
    yield


def test_paper_false_raises(monkeypatch):
    """ALPACA_PAPER=false は必ず例外。fleet 誤爆時の最終防波堤。"""
    monkeypatch.setenv("ALPACA_PAPER", "false")
    with pytest.raises(LiveAccountGuardError, match="ALPACA_PAPER"):
        assert_paper_env()


def test_paper_zero_raises(monkeypatch):
    """"0" も false 相当として例外化 (bool 文字列の幅広カバー)。"""
    monkeypatch.setenv("ALPACA_PAPER", "0")
    with pytest.raises(LiveAccountGuardError):
        assert_paper_env()


def test_live_base_url_raises(monkeypatch):
    """base URL が live (api.alpaca.markets) を指すと例外。paper=true でも block。"""
    monkeypatch.setenv("ALPACA_PAPER", "true")
    monkeypatch.setenv("ALPACA_API_BASE_URL", "https://api.alpaca.markets")
    with pytest.raises(LiveAccountGuardError, match="paper エンドポイント"):
        assert_paper_env()


def test_paper_base_url_ok(monkeypatch):
    """paper-api.alpaca.markets を指す base URL は通過する。"""
    monkeypatch.setenv("ALPACA_PAPER", "true")
    monkeypatch.setenv("ALPACA_API_BASE_URL", "https://paper-api.alpaca.markets")
    assert_paper_env()  # 例外無し


def test_strict_mode_requires_explicit_paper(monkeypatch):
    """strict mode ON かつ ALPACA_PAPER 未設定 → 例外 (明示 opt-in 強制)。"""
    monkeypatch.setenv("ALPACA_PAPER_STRICT", "1")
    # ALPACA_PAPER は fixture で消去済み
    with pytest.raises(LiveAccountGuardError, match="ALPACA_PAPER_STRICT"):
        assert_paper_env()


def test_strict_mode_with_explicit_true_ok(monkeypatch):
    """strict mode ON + ALPACA_PAPER=true は通過 (正常運用)。"""
    monkeypatch.setenv("ALPACA_PAPER_STRICT", "1")
    monkeypatch.setenv("ALPACA_PAPER", "true")
    assert_paper_env()


def test_strict_mode_off_unset_ok(monkeypatch):
    """strict mode OFF (既存動作) + ALPACA_PAPER 未設定は paper へフォールバックし通過。"""
    # 何も setenv しない
    assert_paper_env()


def test_strict_mode_empty_string_treated_as_unset(monkeypatch):
    """strict mode ON かつ ALPACA_PAPER="" (空文字) も未設定扱いで例外。"""
    monkeypatch.setenv("ALPACA_PAPER_STRICT", "yes")
    monkeypatch.setenv("ALPACA_PAPER", "")
    with pytest.raises(LiveAccountGuardError):
        assert_paper_env()
