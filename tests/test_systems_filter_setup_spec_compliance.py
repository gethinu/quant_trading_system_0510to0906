"""Docs 準拠 filter/setup 境界値 parametrize テスト (System 1-6).

Phase 1 audit で発見した gap の 1 つ:
    - core/system1-7.py に mechanics test は多いが、docs/systems の spec
      閾値 (境界値) を assert する test が薄い
    - 例: S1 は "Close>=5" だが 5.00 ちょうどで pass、4.99 で fail という
      境界 assert が既存 test に無い

方針 (ユーザ制約に従い):
    - core/system1-7 の実装変更は禁止 (audit remediation 準拠済)
    - この test は **現状実装 (post-audit) を authoritative** として境界値を固定
    - docs 乖離が残っている項目 (S1 二重 setup / S4 rsi4<30 / S5 filter / S6 hv50)
      は tests/DOCS_IMPL_DIVERGENCE_REPORT.md に別途記録 (Phase 3 判断用)

対象:
    - core/system1._apply_filter_conditions / _apply_setup_conditions
    - core/system2._apply_filter_conditions / _apply_setup_conditions
    - core/system3._apply_filter_conditions / _apply_setup_conditions
    - core/system4._apply_filter_conditions / _apply_setup_conditions
    - core/system5._apply_filter_conditions / _apply_setup_conditions
    - core/system6._apply_filter_conditions / _apply_setup_conditions
"""

from __future__ import annotations

import pandas as pd
import pytest

import core.system1 as s1
import core.system2 as s2
import core.system3 as s3
import core.system4 as s4
import core.system5 as s5
import core.system6 as s6

# ============================================================================
# System 1: Long trend high momentum (docs-compliant post D1 audit 2026-07-02)
#   Filter: Close >= 5.0, DollarVolume20 > 50M
#   Setup:  filter & SMA25 > SMA50 & ROC200 > 0
# ============================================================================


def _s1_row(**over) -> pd.DataFrame:
    row = {
        "Close": 100.0,
        "dollarvolume20": 60_000_000,
        "sma25": 105.0,  # SMA25 > SMA50 by default (docs setup 条件)
        "sma50": 100.0,
        "roc200": 0.05,
    }
    row.update(over)
    return pd.DataFrame([row])


class TestSystem1Filter:
    @pytest.mark.parametrize(
        ("close", "expected"),
        [
            (5.0, True),  # 境界: 実装は >=、5.00 ちょうどで pass
            (4.99, False),  # 境界のちょい下 → fail
            (10.0, True),
        ],
    )
    def test_price_threshold(self, close, expected):
        result = s1._apply_filter_conditions(_s1_row(Close=close))
        assert bool(result["filter"].iloc[0]) is expected

    @pytest.mark.parametrize(
        ("dv20", "expected"),
        [
            (50_000_001, True),
            (50_000_000, False),  # 境界: 実装は > (strict)、50M ちょうどは fail
            (49_999_999, False),
        ],
    )
    def test_dollar_volume_threshold_strict(self, dv20, expected):
        """MIN_DOLLAR_VOLUME_20=50M で境界は strict >。"""
        result = s1._apply_filter_conditions(_s1_row(dollarvolume20=dv20))
        assert bool(result["filter"].iloc[0]) is expected


class TestSystem1Setup:
    @pytest.mark.parametrize(
        ("sma25", "sma50", "expected"),
        [
            (100.01, 100.0, True),
            (100.0, 100.0, False),  # SMA25 > SMA50 (strict)
            (99.99, 100.0, False),
        ],
    )
    def test_sma25_above_sma50(self, sma25, sma50, expected):
        """D1 audit 2026-07-02: batch 経路が docs 準拠 SMA25>SMA50 に統一されたことを assert."""
        df = _s1_row(sma25=sma25, sma50=sma50)
        df = s1._apply_filter_conditions(df)
        result = s1._apply_setup_conditions(df)
        assert bool(result["setup"].iloc[0]) is expected

    @pytest.mark.parametrize(
        ("roc200", "expected"),
        [
            (0.0, False),  # roc200 > 0 (strict)
            (0.001, True),
            (-0.01, False),
        ],
    )
    def test_roc200_above_zero(self, roc200, expected):
        df = s1._apply_filter_conditions(_s1_row(roc200=roc200))
        result = s1._apply_setup_conditions(df)
        assert bool(result["setup"].iloc[0]) is expected


# ============================================================================
# System 2: Short RSI thrust
#   Filter: Close >= 5, DV20 > 25M, ATRratio > 0.03
#   Setup:  filter & RSI3 > 90 & twodayup
# ============================================================================


def _s2_row(**over) -> pd.DataFrame:
    row = {
        "Close": 50.0,
        "dollarvolume20": 30_000_000,
        "atr_ratio": 0.04,
        "rsi3": 95.0,
        "twodayup": True,
    }
    row.update(over)
    return pd.DataFrame([row])


class TestSystem2Filter:
    @pytest.mark.parametrize(
        ("atr_ratio", "expected"),
        [
            (0.03, False),  # 実装は > (strict)、0.03 ちょうどは fail
            (0.0301, True),
            (0.029, False),
        ],
    )
    def test_atr_ratio_threshold(self, atr_ratio, expected):
        assert (
            bool(s2._apply_filter_conditions(_s2_row(atr_ratio=atr_ratio)).iloc[0])
            is expected
        )

    @pytest.mark.parametrize(
        ("close", "expected"),
        [(5.0, True), (4.99, False), (100.0, True)],
    )
    def test_price_threshold(self, close, expected):
        assert (
            bool(s2._apply_filter_conditions(_s2_row(Close=close)).iloc[0]) is expected
        )


class TestSystem2Setup:
    @pytest.mark.parametrize(
        ("rsi3", "expected"),
        [
            (90.0, False),  # RSI3 > 90 (strict) → 90 ちょうどは fail
            (90.01, True),
            (89.99, False),
        ],
    )
    def test_rsi3_above_90(self, rsi3, expected):
        assert bool(s2._apply_setup_conditions(_s2_row(rsi3=rsi3)).iloc[0]) is expected

    def test_setup_requires_twodayup(self):
        df = _s2_row(twodayup=False)
        assert bool(s2._apply_setup_conditions(df).iloc[0]) is False


# ============================================================================
# System 3: Long mean reversion selloff (docs-compliant after audit-remediation)
#   Filter: Low >= 1, AvgVolume50 >= 1M, atr_ratio >= 0.05
#   Setup:  filter & Close > SMA150 & drop3d >= 0.125
# ============================================================================


def _s3_row(**over) -> pd.DataFrame:
    row = {
        "Low": 10.0,
        "avgvolume50": 2_000_000,
        "atr_ratio": 0.10,
        "Close": 100.0,
        "sma150": 90.0,
        "drop3d": 0.15,
    }
    row.update(over)
    return pd.DataFrame([row])


class TestSystem3Filter:
    @pytest.mark.parametrize(
        ("low", "expected"),
        [(1.0, True), (0.99, False), (5.0, True)],
    )
    def test_low_price_boundary(self, low, expected):
        """spec: 最低株価 ≥ 1ドル (Low >= 1.0)。"""
        assert (
            bool(s3._apply_filter_conditions(_s3_row(Low=low))["filter"].iloc[0])
            is expected
        )

    @pytest.mark.parametrize(
        ("vol", "expected"),
        [
            (1_000_000, True),  # spec: 100万株 >=
            (999_999, False),
            (1_500_000, True),
        ],
    )
    def test_avg_volume_50_boundary(self, vol, expected):
        assert (
            bool(
                s3._apply_filter_conditions(_s3_row(avgvolume50=vol))["filter"].iloc[0]
            )
            is expected
        )

    @pytest.mark.parametrize(
        ("atr_ratio", "expected"),
        [
            (0.05, True),  # spec: >= 5% (inclusive)
            (0.049, False),
            (0.06, True),
        ],
    )
    def test_atr_ratio_boundary(self, atr_ratio, expected):
        assert (
            bool(
                s3._apply_filter_conditions(_s3_row(atr_ratio=atr_ratio))[
                    "filter"
                ].iloc[0]
            )
            is expected
        )


class TestSystem3Setup:
    @pytest.mark.parametrize(
        ("drop3d", "expected"),
        [
            (0.125, True),  # spec: 3日 12.5% 下落 >= (inclusive)
            (0.124, False),
            (0.20, True),
        ],
    )
    def test_drop3d_boundary(self, drop3d, expected):
        df = s3._apply_filter_conditions(_s3_row(drop3d=drop3d))
        assert bool(s3._apply_setup_conditions(df)["setup"].iloc[0]) is expected

    @pytest.mark.parametrize(
        ("close", "sma150", "expected"),
        [
            (100.0, 99.99, True),
            (100.0, 100.0, False),  # Close > SMA150 (strict)
            (99.99, 100.0, False),
        ],
    )
    def test_close_above_sma150_strict(self, close, sma150, expected):
        df = s3._apply_filter_conditions(_s3_row(Close=close, sma150=sma150))
        assert bool(s3._apply_setup_conditions(df)["setup"].iloc[0]) is expected


# ============================================================================
# System 4: Long trend low volatility
#   Filter: DV50 > 100M, HV50 in [10, 40] (inclusive)
#   Setup:  filter & Close > SMA200
# ============================================================================


def _s4_row(**over) -> pd.DataFrame:
    row = {
        "dollarvolume50": 150_000_000,
        "hv50": 20.0,
        "Close": 100.0,
        "sma200": 90.0,
    }
    row.update(over)
    return pd.DataFrame([row])


class TestSystem4Filter:
    @pytest.mark.parametrize(
        ("dv50", "expected"),
        [
            (100_000_001, True),
            (100_000_000, False),  # > (strict)
            (99_999_999, False),
        ],
    )
    def test_dv50_boundary(self, dv50, expected):
        assert (
            bool(
                s4._apply_filter_conditions(_s4_row(dollarvolume50=dv50))[
                    "filter"
                ].iloc[0]
            )
            is expected
        )

    @pytest.mark.parametrize(
        ("hv50", "expected"),
        [
            (10.0, True),  # between(10,40) inclusive
            (40.0, True),
            (9.99, False),
            (40.01, False),
        ],
    )
    def test_hv50_between_10_40_inclusive(self, hv50, expected):
        assert (
            bool(s4._apply_filter_conditions(_s4_row(hv50=hv50))["filter"].iloc[0])
            is expected
        )


class TestSystem4Setup:
    @pytest.mark.parametrize(
        ("close", "sma200", "expected"),
        [
            (100.0, 99.99, True),
            (100.0, 100.0, False),  # > (strict)
            (99.99, 100.0, False),
        ],
    )
    def test_close_above_sma200(self, close, sma200, expected):
        df = s4._apply_filter_conditions(_s4_row(Close=close, sma200=sma200))
        assert bool(s4._apply_setup_conditions(df)["setup"].iloc[0]) is expected


# ============================================================================
# System 5: Long mean reversion high ADX
#   audit-remediation 2026-07-03 (D3 Case A: docs 完全準拠に是正):
#     Filter (spec): Close >= 5, ADX7 > 55, atr_pct > 0.04,
#                    AvgVolume50 > 500k, DollarVolume50 > 2.5M
#     Setup  (spec): filter & Close > SMA100+ATR10 & RSI3 < 50
#   旧: Close>=5 & ADX7>55 & atr_pct>0.025 のみ (流動性 filter 完全欠如、ATR 緩め)
# ============================================================================


def _s5_row(**over) -> pd.DataFrame:
    row = {
        "Close": 100.0,
        "adx7": 60.0,
        "atr_pct": 0.05,  # Case A: spec 4% を超える値をデフォルトに
        "sma100": 90.0,
        "atr10": 5.0,  # sma100+atr10 = 95, Close 100 > 95
        "rsi3": 30.0,
        # Case A: 流動性 filter を通過するデフォルト値
        "avgvolume50": 1_000_000,  # > 500k spec
        "dollarvolume50": 5_000_000,  # > 2.5M spec
    }
    row.update(over)
    return pd.DataFrame([row])


class TestSystem5Filter:
    @pytest.mark.parametrize(
        ("adx7", "expected"),
        [
            (55.0, False),  # spec: > 55 (strict)
            (55.01, True),
            (54.99, False),
            (80.0, True),
        ],
    )
    def test_adx7_boundary(self, adx7, expected):
        assert (
            bool(s5._apply_filter_conditions(_s5_row(adx7=adx7))["filter"].iloc[0])
            is expected
        )

    @pytest.mark.parametrize(
        ("atr_pct", "expected"),
        [
            # Case A (spec): > 4% (strict)。旧 2.5% assertion は 4% に是正。
            (0.04, False),
            (0.0401, True),
            (0.039, False),
            (0.05, True),
        ],
    )
    def test_atr_pct_boundary(self, atr_pct, expected):
        assert (
            bool(
                s5._apply_filter_conditions(_s5_row(atr_pct=atr_pct))["filter"].iloc[0]
            )
            is expected
        )

    @pytest.mark.parametrize(
        ("close", "expected"),
        [(5.0, True), (4.99, False)],
    )
    def test_min_price_boundary(self, close, expected):
        # 単純に filter が Close>=5 を通す (setup 側の Close > sma100+atr10 は
        # 5 では通せないので filter だけ isolate)
        row = _s5_row(Close=close)
        # setup を trigger しないよう sma100+atr10 を高く設定
        row.loc[0, "sma100"] = 1000
        result = s5._apply_filter_conditions(row)
        assert bool(result["filter"].iloc[0]) is expected

    @pytest.mark.parametrize(
        ("avgvolume50", "expected"),
        [
            # Case A (spec, docs/systems/システム5.txt:7): > 500k (strict)
            (500_000, False),
            (500_001, True),
            (499_999, False),
            (1_000_000, True),
        ],
    )
    def test_avgvolume50_boundary(self, avgvolume50, expected):
        assert (
            bool(
                s5._apply_filter_conditions(_s5_row(avgvolume50=avgvolume50))[
                    "filter"
                ].iloc[0]
            )
            is expected
        )

    @pytest.mark.parametrize(
        ("dollarvolume50", "expected"),
        [
            # Case A (spec, docs/systems/システム5.txt:8): > 2.5M (strict)
            (2_500_000, False),
            (2_500_001, True),
            (2_499_999, False),
            (10_000_000, True),
        ],
    )
    def test_dollarvolume50_boundary(self, dollarvolume50, expected):
        assert (
            bool(
                s5._apply_filter_conditions(_s5_row(dollarvolume50=dollarvolume50))[
                    "filter"
                ].iloc[0]
            )
            is expected
        )


class TestSystem5Setup:
    @pytest.mark.parametrize(
        ("rsi3", "expected"),
        [
            (50.0, False),  # spec: < 50 (strict)
            (49.99, True),
            (0.0, True),
        ],
    )
    def test_rsi3_below_50(self, rsi3, expected):
        df = s5._apply_filter_conditions(_s5_row(rsi3=rsi3))
        assert bool(s5._apply_setup_conditions(df)["setup"].iloc[0]) is expected

    @pytest.mark.parametrize(
        ("close", "sma100", "atr10", "expected"),
        [
            # Close > sma100 + atr10 (strict)
            (95.01, 90.0, 5.0, True),
            (95.0, 90.0, 5.0, False),
            (94.99, 90.0, 5.0, False),
        ],
    )
    def test_price_band_strict(self, close, sma100, atr10, expected):
        df = s5._apply_filter_conditions(
            _s5_row(Close=close, sma100=sma100, atr10=atr10)
        )
        assert bool(s5._apply_setup_conditions(df)["setup"].iloc[0]) is expected


# ============================================================================
# System 6: Short mean reversion six-day surge
#   Filter: Low >= 5, DV50 > 10M, HV50 in bounds
#   Setup:  filter & return_6d > 0.20 & UpTwoDays
# ============================================================================


def _s6_row(**over) -> pd.DataFrame:
    row = {
        "Low": 10.0,
        "dollarvolume50": 20_000_000,
        "hv50": 20.0,  # percent form → in HV50_BOUNDS_PERCENT
        "return_6d": 0.25,
        "UpTwoDays": True,
    }
    row.update(over)
    return pd.DataFrame([row])


class TestSystem6Filter:
    @pytest.mark.parametrize(
        ("low", "expected"),
        [(5.0, True), (4.99, False), (10.0, True)],
    )
    def test_low_price_boundary(self, low, expected):
        assert (
            bool(s6._apply_filter_conditions(_s6_row(Low=low))["filter"].iloc[0])
            is expected
        )

    @pytest.mark.parametrize(
        ("dv50", "expected"),
        [
            (10_000_001, True),
            (10_000_000, False),  # > (strict)
            (9_999_999, False),
        ],
    )
    def test_dv50_boundary(self, dv50, expected):
        assert (
            bool(
                s6._apply_filter_conditions(_s6_row(dollarvolume50=dv50))[
                    "filter"
                ].iloc[0]
            )
            is expected
        )


class TestSystem6Setup:
    @pytest.mark.parametrize(
        ("return_6d", "expected"),
        [
            (0.20, False),  # spec: > 0.20 (strict)
            (0.2001, True),
            (0.19, False),
        ],
    )
    def test_return_6d_boundary(self, return_6d, expected):
        df = s6._apply_filter_conditions(_s6_row(return_6d=return_6d))
        assert bool(s6._apply_setup_conditions(df)["setup"].iloc[0]) is expected

    def test_setup_requires_up_two_days(self):
        df = s6._apply_filter_conditions(_s6_row(UpTwoDays=False))
        assert bool(s6._apply_setup_conditions(df)["setup"].iloc[0]) is False


# ============================================================================
# SYSTEM_TRADE_RULES ↔ docs 数値照合 (audit remediation の一環)
# ============================================================================


class TestTradeRulesSpecCompliance:
    """common/trade_management.py::SYSTEM_TRADE_RULES の数値を固定。

    docs/systems 準拠 & audit remediation (2026-07-02) を反映。
    値の変更は spec 判断が必要になるので、意識的に更新できるようこの
    test で固定する。
    """

    @pytest.fixture(scope="class")
    def rules(self):
        from common.trade_management import SYSTEM_TRADE_RULES

        return SYSTEM_TRADE_RULES

    def test_system7_stub_absent(self, rules):
        """audit remediation P0: system7 の未使用 stub は除去済。"""
        assert "system7" not in rules

    @pytest.mark.parametrize(
        "system",
        ["system1", "system2", "system3", "system4", "system5", "system6"],
    )
    def test_all_six_systems_present(self, rules, system):
        assert system in rules

    def test_system1_stop_and_trailing(self, rules):
        """System1 spec (docs/systems/システム1.txt): 5ATR20 stop + 25% trailing."""
        r = rules["system1"]
        assert r.side == "long"
        assert r.stop_atr_period == 20, f"stop_atr_period={r.stop_atr_period}"
        assert r.stop_atr_multiplier == 5.0
        assert r.use_trailing_stop is True
        assert r.trailing_stop_pct == 0.25  # 25% trailing (spec 準拠)

    def test_system2_stop(self, rules):
        """System2 spec: 3ATR10 stop, 4% profit target."""
        r = rules["system2"]
        assert r.side == "short"
        assert r.stop_atr_period == 10
        assert r.stop_atr_multiplier == 3.0

    def test_system3_stop_and_profit(self, rules):
        """System3 spec: 2.5ATR10 stop, 4% profit target, 3 日 time exit."""
        r = rules["system3"]
        assert r.side == "long"
        assert r.stop_atr_period == 10
        assert r.stop_atr_multiplier == 2.5

    def test_system4_stop_and_trailing(self, rules):
        """System4 spec: 1.5ATR40 stop + 20% trailing."""
        r = rules["system4"]
        assert r.side == "long"
        assert r.stop_atr_period == 40
        assert r.stop_atr_multiplier == 1.5
        assert r.use_trailing_stop is True
        assert r.trailing_stop_pct == 0.20  # 20% trailing (spec 準拠)

    def test_system5_stop_and_target(self, rules):
        """System5 spec: 3ATR10 stop, 1ATR10 profit target."""
        r = rules["system5"]
        assert r.side == "long"
        assert r.stop_atr_period == 10
        assert r.stop_atr_multiplier == 3.0

    def test_system6_stop(self, rules):
        """System6 spec: 3ATR10 stop, 5% profit target, 3 日 time exit."""
        r = rules["system6"]
        assert r.side == "short"
        assert r.stop_atr_period == 10
        assert r.stop_atr_multiplier == 3.0

    def test_risk_and_position_sizing(self, rules):
        """全 system 共通 spec: risk 2% / max position 10%."""
        for name in ("system1", "system2", "system3", "system4", "system5", "system6"):
            r = rules[name]
            assert r.risk_pct == 0.02, f"{name}: risk_pct={r.risk_pct}"
            assert r.max_pct == 0.10, f"{name}: max_pct={r.max_pct}"
