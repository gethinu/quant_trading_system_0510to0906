"""CAP_USE_REAL_EQUITY (2026-08-20) — portfolio cap の equity 基準を実 equity に。

検証内容:
    1. flag 既定 OFF: resolve_cap_equity() は (None, "disabled")。
    2. flag ON の解決順 (env override -> snapshot -> unresolved)。
    3. _apply_portfolio_caps は equity_source=None で **従来 report と完全一致**
       (後方互換 = 新 key を一切足さない)。
    4. equity_source を渡したときだけ caps.equity_base_usd / caps.equity_source が付く。
    5. finalize_allocation は cap_equity=None で従来 (default_capital) の分母、
       cap_equity 指定でその分母に切り替わる。サイジング (position_value) は不変。
"""

from __future__ import annotations

import json
from pathlib import Path
import sys

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common.cap_equity import (  # noqa: E402
    FLAG_ENV,
    OVERRIDE_ENV,
    cap_real_equity_enabled,
    resolve_cap_equity,
)
from core.final_allocation import _apply_portfolio_caps  # noqa: E402

_NOOP_CAPS = {
    "max_total_positions": 70,
    "max_long_positions": 40,
    "max_short_positions": 30,
    "max_gross_exposure_pct": 1.0,
    "max_net_exposure_pct": 0.5,
}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """各テストは flag/override 未設定の素の環境から始める。"""
    monkeypatch.delenv(FLAG_ENV, raising=False)
    monkeypatch.delenv(OVERRIDE_ENV, raising=False)
    monkeypatch.setenv("TEST_MODE", "1")  # Alpaca を叩かせない
    yield


def _df(n_long: int, n_short: int, pv: float = 1000.0) -> pd.DataFrame:
    rows = [
        {"symbol": f"L{i}", "system": "system1", "side": "long", "position_value": pv}
        for i in range(n_long)
    ]
    rows += [
        {"symbol": f"S{i}", "system": "system2", "side": "short", "position_value": pv}
        for i in range(n_short)
    ]
    return pd.DataFrame(rows)


def _caps(df, equity, equity_source=None):
    return _apply_portfolio_caps(
        df,
        caps=_NOOP_CAPS,
        active_positions=None,
        symbol_system_map=None,
        long_systems=["system1"],
        short_systems=["system2"],
        equity=equity,
        equity_source=equity_source,
    )


# ---------------------------------------------------------------- flag 既定 OFF


def test_flag_defaults_off():
    assert cap_real_equity_enabled() is False
    assert resolve_cap_equity() == (None, "disabled")


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", "yes", "on", "y"])
def test_flag_truthy_values(monkeypatch, raw):
    monkeypatch.setenv(FLAG_ENV, raw)
    assert cap_real_equity_enabled() is True


@pytest.mark.parametrize("raw", ["", "0", "false", "no", "off"])
def test_flag_falsy_values(monkeypatch, raw):
    monkeypatch.setenv(FLAG_ENV, raw)
    assert cap_real_equity_enabled() is False


# ------------------------------------------------------------------- 解決順


def test_env_override_wins(monkeypatch):
    monkeypatch.setenv(FLAG_ENV, "1")
    monkeypatch.setenv(OVERRIDE_ENV, "123456.78")
    eq, src = resolve_cap_equity(allow_fetch=False)
    assert eq == pytest.approx(123456.78)
    assert src == f"env:{OVERRIDE_ENV}"


def test_snapshot_fallback(monkeypatch, tmp_path):
    monkeypatch.setenv(FLAG_ENV, "1")
    (tmp_path / "alpaca_snapshot_20260818.json").write_text(
        json.dumps({"account": {"equity": 100132.49}}), encoding="utf-8"
    )
    (tmp_path / "alpaca_snapshot_20260819.json").write_text(
        json.dumps({"account": {"equity": 99788.27}}), encoding="utf-8"
    )
    eq, src = resolve_cap_equity(allow_fetch=False, results_dir=tmp_path)
    assert eq == pytest.approx(99788.27)  # 最新 (降順 sort の先頭)
    assert src == "snapshot:20260819"


def test_broken_snapshot_is_skipped(monkeypatch, tmp_path):
    monkeypatch.setenv(FLAG_ENV, "1")
    (tmp_path / "alpaca_snapshot_20260819.json").write_text(
        "{not json", encoding="utf-8"
    )
    (tmp_path / "alpaca_snapshot_20260818.json").write_text(
        json.dumps({"account": {"equity": 100132.49}}), encoding="utf-8"
    )
    eq, src = resolve_cap_equity(allow_fetch=False, results_dir=tmp_path)
    assert eq == pytest.approx(100132.49)
    assert src == "snapshot:20260818"


def test_unresolved_returns_none(monkeypatch, tmp_path):
    monkeypatch.setenv(FLAG_ENV, "1")
    eq, src = resolve_cap_equity(allow_fetch=False, results_dir=tmp_path)
    assert eq is None
    assert src == "unresolved"


def test_non_positive_override_is_ignored(monkeypatch, tmp_path):
    monkeypatch.setenv(FLAG_ENV, "1")
    monkeypatch.setenv(OVERRIDE_ENV, "0")
    eq, src = resolve_cap_equity(allow_fetch=False, results_dir=tmp_path)
    assert (eq, src) == (None, "unresolved")


# -------------------------------------------------- report の後方互換 (OFF 経路)


def test_report_has_no_new_keys_when_source_omitted():
    """equity_source を渡さない = 現行経路。report は従来と 1 key も違わない。"""
    _out, report = _caps(_df(3, 3), 100000.0)
    assert "equity_base_usd" not in report["caps"]
    assert "equity_source" not in report["caps"]
    assert set(report["caps"]) == {
        "max_total",
        "max_long",
        "max_short",
        "gross_cap_usd",
        "net_cap_usd",
    }


def test_off_path_is_bit_identical_to_baseline():
    """flag OFF (equity_source=None) の report を、旧実装が出す形と突合する。"""
    df = _df(4, 2, pv=500.0)
    _out, report = _caps(df, 100000.0)
    baseline = {
        "applied": True,
        "held": {"long": 0, "short": 0, "total": 0},
        "held_unmapped": {"long": 0, "short": 0, "total": 0},
        "caps": {
            "max_total": 70,
            "max_long": 40,
            "max_short": 30,
            "gross_cap_usd": 100000.0,
            "net_cap_usd": 50000.0,
        },
        "allow": {"long": 40, "short": 30, "total": 70},
        "kept": {"long": 4, "short": 2, "total": 6},
        "trimmed": {},
        "new_long_usd": 2000.0,
        "new_short_usd": 1000.0,
    }
    assert json.dumps(report, sort_keys=True) == json.dumps(baseline, sort_keys=True)


def test_report_gains_keys_when_source_given():
    _out, report = _caps(_df(3, 3), 99788.27, equity_source="snapshot:20260819")
    assert report["caps"]["equity_base_usd"] == pytest.approx(99788.27)
    assert report["caps"]["equity_source"] == "snapshot:20260819"
    # 分母が変われば cap の $ 値も追随する
    assert report["caps"]["net_cap_usd"] == pytest.approx(round(99788.27 * 0.5, 2))
    assert report["caps"]["gross_cap_usd"] == pytest.approx(round(99788.27 * 1.0, 2))


# ------------------------------------------------ cap が実際に効くこと (ON/OFF 差)


def test_real_equity_tightens_when_below_default():
    """equity < default_capital なら net cap は縮み、trim が増えうる。"""
    # 件数 cap を十分緩めて exposure cap だけが binding になる状況を作る
    # (既定の max_long_positions=40 だと件数 cap が先に効いて equity 差が出ない —
    #  これが本番で観測された状況そのものなので test_count_cap_... 側で別途固定する)
    caps = {
        **_NOOP_CAPS,
        "max_net_exposure_pct": 0.5,
        "max_long_positions": 200,
        "max_total_positions": 200,
    }
    df = _df(60, 0, pv=1000.0)  # long のみ 60 本 = $60,000
    args = dict(
        caps=caps,
        active_positions=None,
        symbol_system_map=None,
        long_systems=["system1"],
        short_systems=["system2"],
    )
    _o_fixed, r_fixed = _apply_portfolio_caps(df, equity=100000.0, **args)
    _o_real, r_real = _apply_portfolio_caps(
        df, equity=80000.0, equity_source="test", **args
    )
    # 100k 基準 = net cap $50,000 -> 50 本で頭打ち
    assert r_fixed["kept"]["long"] == 50
    # 80k 基準 = net cap $40,000 -> 40 本で頭打ち
    assert r_real["kept"]["long"] == 40
    assert r_real["trimmed"]["net_exposure"] > r_fixed["trimmed"]["net_exposure"]


def test_count_cap_is_independent_of_equity():
    """件数 cap は equity に一切依存しない (本 fix の効果範囲を明示する回帰)。"""
    caps = {**_NOOP_CAPS, "max_long_positions": 10}
    df = _df(30, 0, pv=100.0)
    args = dict(
        caps=caps,
        active_positions=None,
        symbol_system_map=None,
        long_systems=["system1"],
        short_systems=["system2"],
    )
    _o1, r1 = _apply_portfolio_caps(df, equity=100000.0, **args)
    _o2, r2 = _apply_portfolio_caps(df, equity=250000.0, equity_source="test", **args)
    assert r1["allow"]["long"] == r2["allow"]["long"] == 10
    assert r1["kept"]["long"] == r2["kept"]["long"] == 10
    assert r1["trimmed"] == r2["trimmed"] == {"long_count": 20}


# ------------------------------------------------ finalize_allocation の配線


class _DummyStrategy:
    SYSTEM_NAME = "system"

    def __init__(self, max_positions: int = 3) -> None:
        self.config = {"max_positions": max_positions, "risk_pct": 0.05, "max_pct": 0.2}

    def calculate_position_size(
        self,
        capital: float,
        entry_price: float,
        stop_price: float,
        *,
        risk_pct: float,
        max_pct: float,
    ) -> int:
        risk_per_share = abs(entry_price - stop_price)
        if risk_per_share <= 0:
            return 0
        return int(
            min(capital * risk_pct / risk_per_share, capital * max_pct / entry_price)
        )


def _candidates(symbols, system, score_start):
    rows = []
    score = score_start
    for sym in symbols:
        rows.append(
            {
                "symbol": sym,
                "score": score,
                "entry_price": 100.0 + score,
                "stop_price": 95.0,
                "system": system,
            }
        )
        score -= 1
    return pd.DataFrame(rows)


def _finalize(**kwargs):
    from core.final_allocation import finalize_allocation

    per_system = {
        "system1": _candidates(["AAA", "BBB", "CCC"], "system1", 10),
        "system6": _candidates(["XXX", "YYY", "ZZZ"], "system6", 5),
    }
    strategies = {"system1": _DummyStrategy(3), "system6": _DummyStrategy(3)}
    return finalize_allocation(
        per_system,
        strategies=strategies,
        positions=[],
        symbol_system_map={},
        include_trade_management=False,
        **kwargs,
    )


def _caps_diag(summary):
    return (summary.system_diagnostics or {}).get("portfolio_caps") or {}


def test_finalize_without_cap_equity_keeps_100k_denominator():
    """既定 (cap_equity 未指定) = 現行本番。分母は default_capital=100,000。"""
    _df, summary = _finalize()
    caps = _caps_diag(summary)["caps"]
    assert "equity_source" not in caps
    assert "equity_base_usd" not in caps
    assert caps["gross_cap_usd"] == pytest.approx(100000.0)


def test_finalize_with_cap_equity_switches_denominator_only():
    """cap_equity を渡すと cap の $ 値だけが変わり、サイジングは不変。"""
    df_off, sum_off = _finalize()
    df_on, sum_on = _finalize(cap_equity=80000.0, cap_equity_source="snapshot:test")

    caps_off = _caps_diag(sum_off)["caps"]
    caps_on = _caps_diag(sum_on)["caps"]
    assert caps_off["gross_cap_usd"] == pytest.approx(100000.0)
    assert caps_on["gross_cap_usd"] == pytest.approx(80000.0)
    assert caps_on["equity_base_usd"] == pytest.approx(80000.0)
    assert caps_on["equity_source"] == "snapshot:test"

    # position_value (サイジング) は equity 基準の差し替えで動かない
    if "position_value" in df_off.columns and "position_value" in df_on.columns:
        assert list(df_off["position_value"]) == list(df_on["position_value"])


@pytest.mark.parametrize("bad", [None, 0.0, -1.0])
def test_finalize_ignores_non_positive_cap_equity(bad):
    """0 / 負 / None は無視して従来の default_capital に落ちる (fail-safe)。"""
    _df, summary = _finalize(cap_equity=bad, cap_equity_source="bogus")
    caps = _caps_diag(summary)["caps"]
    assert caps["gross_cap_usd"] == pytest.approx(100000.0)
    assert "equity_source" not in caps
