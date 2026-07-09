"""Unit tests for equity-linked position sizing (2026-07-09).

Covers:
  - compute_position_notionals: equity×pct×weight, per-name/gross/net cap 相互作用,
    fixed_tier 後方互換, equity_deploy_pct ノブ, weight=0/equity=0 のフォールバック。
  - resolve_sizing_equity / fetch_account_equity: Alpaca 取得 & 安全フォールバック,
    test_mode (fetch 抑止) 挙動。
  - signals_json_to_orders (equity_linked, dry-run): 実 equity 連動サイジング。
  - config.SizingConfig: 既定 & env override。

厳守: dry-run / 純関数のみ。実発注は一切しない。
"""

from __future__ import annotations

import pytest

from common.alpaca_trading import (
    SIZING_EQUITY_LINKED,
    SIZING_FIXED_TIER,
    compute_position_notionals,
    fetch_account_equity,
    resolve_sizing_equity,
    signals_json_to_orders,
)


# ---------------------------------------------------------------------------
# 1. compute_position_notionals — 基本 (equity × pct × weight)
# ---------------------------------------------------------------------------
def test_equity_linked_basic_weight_times_budget():
    # equity 100k, pct 1.0, weights 0.5/0.3/0.2 (Σ=1) → 50k/30k/20k。
    # per-name cap 0.10*100k=10k で全部 clamp されないよう max_pct を緩める。
    p = compute_position_notionals(
        [(0.5, "buy"), (0.3, "buy"), (0.2, "buy")],
        mode=SIZING_EQUITY_LINKED,
        tier="small",
        equity=100_000,
        equity_deploy_pct=1.0,
        max_pct=1.0,  # per-name 実質無効
        max_gross_exposure_pct=1.0,
        max_net_exposure_pct=1.0,
    )
    assert p.deploy_budget == 100_000.0
    assert p.notionals == [50_000.0, 30_000.0, 20_000.0]
    assert p.gross_after == 100_000.0


def test_equity_deploy_pct_scales_budget():
    # pct 0.5 → deploy_budget = 50k。notional は半分。
    p = compute_position_notionals(
        [(0.5, "buy"), (0.5, "buy")],
        mode=SIZING_EQUITY_LINKED,
        tier="small",
        equity=100_000,
        equity_deploy_pct=0.5,
        max_pct=1.0,
        max_gross_exposure_pct=1.0,
        max_net_exposure_pct=1.0,
    )
    assert p.deploy_budget == 50_000.0
    assert p.notionals == [25_000.0, 25_000.0]


def test_weights_normalized_to_budget():
    # 合計 weight != 1 でも予算基準で正規化される (Σnotional == budget)。
    p = compute_position_notionals(
        [(2.0, "buy"), (1.0, "buy"), (1.0, "buy")],
        mode=SIZING_EQUITY_LINKED,
        tier="small",
        equity=40_000,
        equity_deploy_pct=1.0,
        max_pct=1.0,
        max_gross_exposure_pct=1.0,
        max_net_exposure_pct=1.0,
    )
    assert sum(p.notionals) == pytest.approx(40_000.0)
    assert p.notionals[0] == pytest.approx(20_000.0)  # 2/4 * 40k


# ---------------------------------------------------------------------------
# 2. per-name cap (max_pct × equity)
# ---------------------------------------------------------------------------
def test_per_name_cap_clamps_and_does_not_reinflate():
    # equity 100k, per-name 10% = 10k。weight 0.5 → raw 50k → clamp 10k。
    # hard cap: 余りを他へ再配分しない。
    p = compute_position_notionals(
        [(0.5, "buy"), (0.3, "buy"), (0.2, "buy")],
        mode=SIZING_EQUITY_LINKED,
        tier="small",
        equity=100_000,
        equity_deploy_pct=1.0,
        max_pct=0.10,
        max_gross_exposure_pct=1.0,
        max_net_exposure_pct=1.0,
    )
    assert p.notionals == [10_000.0, 10_000.0, 10_000.0]
    assert p.caps["per_name"]["clamped_count"] == 3
    assert p.gross_after == 30_000.0  # 再配分されない


# ---------------------------------------------------------------------------
# 3. gross cap (max_gross_exposure_pct × equity)
# ---------------------------------------------------------------------------
def test_gross_cap_scales_whole_book():
    # pct 2.0 → deploy 200k > gross_cap 100k。全体を 0.5 倍に縮小。
    p = compute_position_notionals(
        [(0.5, "buy"), (0.5, "buy")],
        mode=SIZING_EQUITY_LINKED,
        tier="small",
        equity=100_000,
        equity_deploy_pct=2.0,
        max_pct=1.0,
        max_gross_exposure_pct=1.0,
        max_net_exposure_pct=1.0,
    )
    assert p.gross_after == pytest.approx(100_000.0)
    assert p.caps["gross"]["scale"] == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# 4. net cap (max_net_exposure_pct × equity)
# ---------------------------------------------------------------------------
def test_net_cap_scales_dominant_long_side():
    # 10 long @0.09 + 1 short @0.10, equity 100k。
    # gross=100k(=cap), net=|90k-10k|=80k > 50k → long を (10k+50k)/90k 倍。
    entries = [(0.09, "buy")] * 10 + [(0.10, "sell")]
    p = compute_position_notionals(
        entries,
        mode=SIZING_EQUITY_LINKED,
        tier="small",
        equity=100_000,
        equity_deploy_pct=1.0,
        max_pct=0.10,
        max_gross_exposure_pct=1.0,
        max_net_exposure_pct=0.5,
    )
    assert p.net_after == pytest.approx(50_000.0, abs=1.0)
    assert p.short_after == pytest.approx(10_000.0, abs=1.0)
    assert p.caps["net"]["scaled_side"] == "long"


def test_net_cap_scales_dominant_short_side():
    entries = [(0.10, "buy")] + [(0.09, "sell")] * 10
    p = compute_position_notionals(
        entries,
        mode=SIZING_EQUITY_LINKED,
        tier="small",
        equity=100_000,
        equity_deploy_pct=1.0,
        max_pct=0.10,
        max_gross_exposure_pct=1.0,
        max_net_exposure_pct=0.5,
    )
    assert p.net_after == pytest.approx(50_000.0, abs=1.0)
    assert p.caps["net"]["scaled_side"] == "short"


def test_balanced_book_no_net_scaling():
    # long$ == short$ → net 0 → net cap 発火しない。
    p = compute_position_notionals(
        [(0.5, "buy"), (0.5, "sell")],
        mode=SIZING_EQUITY_LINKED,
        tier="small",
        equity=100_000,
        equity_deploy_pct=1.0,
        max_pct=1.0,
        max_gross_exposure_pct=1.0,
        max_net_exposure_pct=0.5,
    )
    assert p.net_after == pytest.approx(0.0)
    assert "net" not in p.caps


# ---------------------------------------------------------------------------
# 5. fixed_tier 後方互換 (dollar cap を掛けない)
# ---------------------------------------------------------------------------
def test_fixed_tier_matches_legacy_and_ignores_equity():
    # tier small = $1000, weight 0.5/0.3/0.2 → 500/300/200。equity は無視。
    p = compute_position_notionals(
        [(0.5, "buy"), (0.3, "buy"), (0.2, "buy")],
        mode=SIZING_FIXED_TIER,
        tier="small",
        equity=999_999,
        equity_deploy_pct=1.0,
        max_pct=0.10,
        max_gross_exposure_pct=1.0,
        max_net_exposure_pct=0.5,
    )
    assert p.deploy_budget == 1000.0
    assert p.notionals == [500.0, 300.0, 200.0]
    assert "per_name" not in p.caps  # tier 経路には dollar cap 掛けない


def test_fixed_tier_no_dollar_cap_even_if_concentrated():
    # 単一銘柄 weight 1.0, tier large=$100k → per-name 10% など掛からず 100k。
    p = compute_position_notionals(
        [(1.0, "buy")],
        mode=SIZING_FIXED_TIER,
        tier="large",
        equity=100_000,
        max_pct=0.10,
        max_gross_exposure_pct=1.0,
        max_net_exposure_pct=0.5,
    )
    assert p.notionals == [100_000.0]  # clamp されない (後方互換)


# ---------------------------------------------------------------------------
# 6. フォールバック / エッジ
# ---------------------------------------------------------------------------
def test_zero_total_weight_equal_split():
    p = compute_position_notionals(
        [(0.0, "buy"), (0.0, "buy")],
        mode=SIZING_EQUITY_LINKED,
        tier="small",
        equity=100_000,
        equity_deploy_pct=1.0,
        max_pct=1.0,
        max_gross_exposure_pct=1.0,
        max_net_exposure_pct=1.0,
    )
    assert p.notionals == [50_000.0, 50_000.0]  # 均等割り


def test_zero_equity_returns_zeros():
    p = compute_position_notionals(
        [(0.5, "buy"), (0.5, "buy")],
        mode=SIZING_EQUITY_LINKED,
        tier="small",
        equity=0.0,
        equity_deploy_pct=1.0,
    )
    assert p.notionals == [0.0, 0.0]
    assert p.deploy_budget == 0.0


def test_nonpositive_pct_falls_back_to_default():
    # pct <= 0 は既定 0.5 に安全フォールバック (誤設定でゼロ発注しない)。
    p = compute_position_notionals(
        [(1.0, "buy")],
        mode=SIZING_EQUITY_LINKED,
        tier="small",
        equity=100_000,
        equity_deploy_pct=0.0,
        max_pct=1.0,
        max_gross_exposure_pct=1.0,
        max_net_exposure_pct=1.0,
    )
    assert p.deploy_budget == 50_000.0  # 100k × 既定 0.5


def test_empty_entries():
    p = compute_position_notionals(
        [], mode=SIZING_EQUITY_LINKED, tier="small", equity=100_000
    )
    assert p.notionals == []


# ---------------------------------------------------------------------------
# 7. equity 解決 (fetch + fallback + test_mode)
# ---------------------------------------------------------------------------
class _FakeAccount:
    def __init__(self, equity):
        self.equity = equity


class _FakeAcctClient:
    def __init__(self, equity=None, raise_exc=False):
        self._equity = equity
        self._raise = raise_exc

    def get_account(self):
        if self._raise:
            raise RuntimeError("no creds")
        return _FakeAccount(self._equity)


def test_fetch_account_equity_success():
    assert fetch_account_equity(_FakeAcctClient(equity="106252.5")) == 106252.5


def test_fetch_account_equity_failure_returns_none():
    assert fetch_account_equity(_FakeAcctClient(raise_exc=True)) is None


def test_fetch_account_equity_zero_returns_none():
    assert fetch_account_equity(_FakeAcctClient(equity=0)) is None


def test_resolve_equity_fixed_tier_passthrough():
    eq, src = resolve_sizing_equity(
        10_000.0, mode=SIZING_FIXED_TIER, client=_FakeAcctClient(equity=999)
    )
    assert eq == 10_000.0 and "fixed_tier" in src


def test_resolve_equity_fetch_success():
    eq, src = resolve_sizing_equity(
        10_000.0,
        mode=SIZING_EQUITY_LINKED,
        client=_FakeAcctClient(equity=106252.0),
        allow_fetch=True,
    )
    assert eq == 106252.0 and src == "alpaca"


def test_resolve_equity_fetch_failure_falls_back():
    eq, src = resolve_sizing_equity(
        10_000.0,
        mode=SIZING_EQUITY_LINKED,
        client=_FakeAcctClient(raise_exc=True),
        allow_fetch=True,
    )
    assert eq == 10_000.0 and src.startswith("fallback")


def test_resolve_equity_test_mode_skips_fetch(monkeypatch):
    # TEST_MODE 環境では fetch しない (creds を叩かない・従来挙動)。
    monkeypatch.setenv("TEST_MODE", "1")
    eq, src = resolve_sizing_equity(
        10_000.0,
        mode=SIZING_EQUITY_LINKED,
        client=_FakeAcctClient(equity=999999),  # 使われないはず
    )
    assert eq == 10_000.0 and "test_mode" in src


def test_resolve_equity_allow_fetch_false_uses_fallback():
    eq, src = resolve_sizing_equity(
        10_000.0,
        mode=SIZING_EQUITY_LINKED,
        client=_FakeAcctClient(equity=999999),
        allow_fetch=False,
    )
    assert eq == 10_000.0 and "fetch_disabled" in src


# ---------------------------------------------------------------------------
# 8. signals_json_to_orders (equity_linked, dry-run) 統合
# ---------------------------------------------------------------------------
def _json():
    return {
        "date": "2026-07-08",
        "systems": {
            "sys1": {
                "signals": [
                    {
                        "symbol": "AAPL",
                        "side": "BUY",
                        "entry_price": 100.0,
                        "weight": 0.5,
                    },
                    {
                        "symbol": "MSFT",
                        "side": "BUY",
                        "entry_price": 200.0,
                        "weight": 0.3,
                    },
                ]
            },
            "sys2": {
                "signals": [
                    {
                        "symbol": "TSLA",
                        "side": "SELL",
                        "entry_price": 250.0,
                        "weight": 0.2,
                    },
                ]
            },
        },
    }


def test_signals_json_equity_linked_uses_equity():
    # equity 50k, pct 1.0, per-name cap 緩め → notional = weight*50k。
    orders = signals_json_to_orders(
        _json(),
        tier="small",
        dry_run=True,
        sizing_mode=SIZING_EQUITY_LINKED,
        account_equity=50_000.0,
        equity_deploy_pct=1.0,
        max_pct=1.0,
        max_gross_exposure_pct=1.0,
        max_net_exposure_pct=1.0,
    )
    by = {o.symbol: o.notional_usd for o in orders}
    assert by["AAPL"] == pytest.approx(25_000.0)  # 0.5*50k
    assert by["MSFT"] == pytest.approx(15_000.0)
    assert by["TSLA"] == pytest.approx(10_000.0)


def test_signals_json_equity_linked_applies_per_name_cap():
    # equity 50k, per-name 10% = 5k。AAPL 0.5 → 25k → clamp 5k。
    orders = signals_json_to_orders(
        _json(),
        tier="small",
        dry_run=True,
        sizing_mode=SIZING_EQUITY_LINKED,
        account_equity=50_000.0,
        equity_deploy_pct=1.0,
        max_pct=0.10,
        max_gross_exposure_pct=1.0,
        max_net_exposure_pct=1.0,
    )
    by = {o.symbol: o.notional_usd for o in orders}
    assert by["AAPL"] == pytest.approx(5_000.0)  # clamped


def test_signals_json_default_mode_is_equity_linked():
    # 明示 mode/pct 無し → 既定 equity_linked + equity_deploy_pct 0.5。
    # cap を無効化して「予算=equity×0.5」だけを確認 (cap 相互作用は別テスト)。
    orders = signals_json_to_orders(
        _json(),
        tier="small",
        dry_run=True,
        account_equity=10_000.0,
        max_pct=1.0,
        max_gross_exposure_pct=1.0,
        max_net_exposure_pct=1.0,
    )
    total = sum((o.notional_usd or 0.0) for o in orders)
    # equity_linked 10k × 既定 pct 0.5 = 5k を配分 (tier small $1k でも 10k でもない)
    assert total == pytest.approx(5_000.0)


# ---------------------------------------------------------------------------
# 9. config.SizingConfig
# ---------------------------------------------------------------------------
def test_settings_sizing_defaults():
    from config.settings import SizingConfig

    d = SizingConfig()
    assert d.mode == "equity_linked"
    assert d.equity_deploy_pct == 0.5


def test_settings_sizing_env_override(monkeypatch):
    from config import settings as st

    monkeypatch.setenv("SIZING_MODE", "fixed_tier")
    monkeypatch.setenv("EQUITY_DEPLOY_PCT", "0.5")
    cfg = st._build_sizing_config({"mode": "equity_linked", "equity_deploy_pct": 1.0})
    assert cfg.mode == "fixed_tier"
    assert cfg.equity_deploy_pct == 0.5


def test_settings_sizing_invalid_pct_falls_back(monkeypatch):
    from config import settings as st

    monkeypatch.delenv("EQUITY_DEPLOY_PCT", raising=False)
    monkeypatch.delenv("SIZING_MODE", raising=False)
    cfg = st._build_sizing_config({"mode": "weird", "equity_deploy_pct": -3})
    assert cfg.mode == "equity_linked"  # 未知 mode → 既定
    assert cfg.equity_deploy_pct == 0.5  # 負値 → 既定 0.5
