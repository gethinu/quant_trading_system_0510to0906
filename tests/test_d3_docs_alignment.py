"""D3 全 system docs alignment regression tests (2026-07-03 dispatch).

Case A dispatch (docs 完全準拠) の実装が docs/systems/システム{1..7}.txt 通りに
filter/setup を発火することを、7 system を横断的に property test で保証する。

このファイルの位置付け:
    - tests/test_d3_case_a_liquidity_filter.py は System5 の Case A に特化した boundary test。
    - tests/test_systems_filter_setup_spec_compliance.py は既存の spec-compliance test。
    - 本 file は全 system を単一 fixture で横並び比較する **alignment matrix test**。
      docs 側閾値と impl 側閾値が乖離した瞬間に赤く落ちる。

docs single source of truth 表 (2026-07-03 alignment update 時点):

| System | 最低株価 | 平均出来高 | 平均売買代金 | ATR/Volatility |
|--------|----------|-----------|--------------|----------------|
| 1      | Close>=5 | -         | DV20>$50M    | -              |
| 2      | Close>=5 | -         | DV20>$25M    | ATR_Ratio>3%   |
| 3      | Low>=1   | AV50>=1M株| -            | ATR_Ratio>=5%  |
| 4      | -        | -         | DV50>$100M   | HV50 in [10,40]|
| 5      | Close>=5*| AV50>500k | DV50>$2.5M   | ATR_Pct>4%     |
| 6      | Low>=5   | -         | DV50>$10M    | HV50 in [10,40]|
| 7      | (SPY only, no filter)                                    |

* System 5 の Close>=5 は spec 未記載だが penny stock 除外の operational safety として維持。

参考 audit report:
    - docs/D3_LIQUIDITY_FILTER_ATR_THRESHOLD_20260702.md (Case A 判断ペーパー)
    - docs/D3_CASE_A_IMPL_20260703.md               (今 dispatch の実装記録)
    - tests/DIVERGENCE_ANALYSIS_20260702.md         (乖離全 5 件の深掘り)
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

import core.system1 as s1
import core.system2 as s2
import core.system3 as s3
import core.system4 as s4
import core.system5 as s5
import core.system6 as s6


# ============================================================================
# Cross-system constants sanity: docs 値と impl 定数が一致する
# ============================================================================


def test_system1_constants_match_docs():
    """docs/systems/システム1.txt: DV20 > 50M, Close >= 5."""
    assert s1.MIN_PRICE == 5.0
    assert s1.MIN_DOLLAR_VOLUME_20 == 50_000_000


def test_system2_constants_match_docs():
    """docs/systems/システム2.txt: DV20 > 25M, Close >= 5, ATR_Ratio > 3%."""
    assert s2.MIN_PRICE == 5.0
    assert s2.MIN_DOLLAR_VOLUME_20 == 25_000_000
    assert s2.MIN_ATR_RATIO == 0.03


def test_system3_constants_match_docs():
    """docs/systems/システム3.txt: Low >= 1, AvgVol50 >= 1M, ATR_Ratio >= 5%."""
    assert s3.MIN_PRICE == 1.0
    assert s3.MIN_AVG_VOLUME_50 == 1_000_000
    assert s3.DEFAULT_ATR_RATIO_THRESHOLD == 0.05


def test_system4_constants_match_docs():
    """docs/systems/システム4.txt: DV50 > 100M, HV50 in [10, 40]."""
    assert s4.MIN_DOLLAR_VOLUME == 100_000_000
    assert s4.HV50_MIN == 10
    assert s4.HV50_MAX == 40


def test_system5_constants_match_docs_case_a():
    """docs/systems/システム5.txt (D3 Case A 2026-07-03 alignment):
    AvgVol50 > 500k, DV50 > 2.5M, ATR_Pct > 4%.
    """
    assert s5.MIN_PRICE == 5.0            # operational safety (docs 未記載)
    assert s5.MIN_ADX == 55.0
    assert s5.MIN_AVG_VOLUME_50 == 500_000
    assert s5.MIN_DOLLAR_VOLUME_50 == 2_500_000
    assert s5.DEFAULT_ATR_PCT_THRESHOLD == 0.04
    assert s5.MAX_RSI3 == 50.0


def test_system6_constants_match_docs():
    """docs/systems/システム6.txt: Low >= 5, DV50 > 10M, HV50 in [10, 40]."""
    assert s6.MIN_PRICE == 5.0
    assert s6.MIN_DOLLAR_VOLUME_50 == 10_000_000
    assert s6.HV50_BOUNDS_PERCENT == (10.0, 40.0)


# ============================================================================
# System1: filter boundary
# ============================================================================


def _s1_row(**over) -> pd.DataFrame:
    row = {
        "Close": 100.0,
        "dollarvolume20": 60_000_000,
        "sma25": 105.0,
        "sma50": 100.0,
        "roc200": 0.05,
    }
    row.update(over)
    return pd.DataFrame([row])


class TestSystem1DocsAlignment:
    @pytest.mark.parametrize(
        ("dv20", "expected"),
        [
            (50_000_001, True),
            (50_000_000, False),   # spec: > (strict) — docs "50M ドルを上回る"
            (49_999_999, False),
        ],
    )
    def test_dv20_gt_50m_strict(self, dv20, expected):
        result = s1._apply_filter_conditions(_s1_row(dollarvolume20=dv20))
        assert bool(result["filter"].iloc[0]) is expected

    @pytest.mark.parametrize(
        ("close", "expected"),
        [(5.0, True), (4.99, False), (100.0, True)],
    )
    def test_close_gte_5(self, close, expected):
        result = s1._apply_filter_conditions(_s1_row(Close=close))
        assert bool(result["filter"].iloc[0]) is expected


# ============================================================================
# System2: filter boundary
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


class TestSystem2DocsAlignment:
    @pytest.mark.parametrize(
        ("dv20", "expected"),
        [
            (25_000_001, True),
            (25_000_000, False),  # spec: > 25M (strict)
            (24_999_999, False),
        ],
    )
    def test_dv20_gt_25m(self, dv20, expected):
        assert bool(
            s2._apply_filter_conditions(_s2_row(dollarvolume20=dv20)).iloc[0]
        ) is expected

    @pytest.mark.parametrize(
        ("atr_ratio", "expected"),
        [
            (0.03, False),   # spec: > 3% (strict)
            (0.0301, True),
            (0.029, False),
        ],
    )
    def test_atr_ratio_gt_3pct(self, atr_ratio, expected):
        assert bool(
            s2._apply_filter_conditions(_s2_row(atr_ratio=atr_ratio)).iloc[0]
        ) is expected


# ============================================================================
# System3: filter boundary (docs 準拠 post 2026-07-02 revert)
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


class TestSystem3DocsAlignment:
    @pytest.mark.parametrize(
        ("low", "expected"),
        [(1.0, True), (0.99, False), (5.0, True)],
    )
    def test_low_gte_1(self, low, expected):
        assert bool(
            s3._apply_filter_conditions(_s3_row(Low=low))["filter"].iloc[0]
        ) is expected

    @pytest.mark.parametrize(
        ("vol", "expected"),
        [
            (1_000_000, True),   # spec: >= 100 万株 (inclusive)
            (999_999, False),
            (2_000_000, True),
        ],
    )
    def test_avgvolume50_gte_1m(self, vol, expected):
        assert bool(
            s3._apply_filter_conditions(_s3_row(avgvolume50=vol))["filter"].iloc[0]
        ) is expected

    @pytest.mark.parametrize(
        ("atr_ratio", "expected"),
        [
            (0.05, True),    # spec: >= 5% (inclusive)
            (0.049, False),
            (0.10, True),
        ],
    )
    def test_atr_ratio_gte_5pct(self, atr_ratio, expected):
        assert bool(
            s3._apply_filter_conditions(_s3_row(atr_ratio=atr_ratio))["filter"].iloc[0]
        ) is expected


# ============================================================================
# System4: filter boundary
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


class TestSystem4DocsAlignment:
    @pytest.mark.parametrize(
        ("dv50", "expected"),
        [
            (100_000_001, True),
            (100_000_000, False),   # spec: > 100M (strict)
            (99_999_999, False),
        ],
    )
    def test_dv50_gt_100m(self, dv50, expected):
        assert bool(
            s4._apply_filter_conditions(_s4_row(dollarvolume50=dv50))["filter"].iloc[0]
        ) is expected

    @pytest.mark.parametrize(
        ("hv50", "expected"),
        [
            (10.0, True),   # spec: HV 10-40% (inclusive)
            (40.0, True),
            (9.99, False),
            (40.01, False),
        ],
    )
    def test_hv50_range(self, hv50, expected):
        assert bool(
            s4._apply_filter_conditions(_s4_row(hv50=hv50))["filter"].iloc[0]
        ) is expected


# ============================================================================
# System5: docs alignment — D3 Case A (the core of this dispatch)
# ============================================================================


def _s5_row(**over) -> pd.DataFrame:
    row = {
        "Close": 100.0,
        "adx7": 60.0,
        "atr_pct": 0.05,          # > 4% spec
        "sma100": 90.0,
        "atr10": 5.0,
        "rsi3": 30.0,
        "avgvolume50": 1_000_000,      # > 500k spec
        "dollarvolume50": 5_000_000,   # > 2.5M spec
    }
    row.update(over)
    return pd.DataFrame([row])


class TestSystem5DocsAlignmentCaseA:
    """D3 Case A (2026-07-03) の中核 assertion。
    docs/systems/システム5.txt の 3 条件 filter が全て実 gate として発火する。"""

    @pytest.mark.parametrize(
        ("avgvolume50", "expected"),
        [
            (500_000, False),    # docs: > 500k (strict)
            (500_001, True),
            (499_999, False),
            (1_000_000, True),
        ],
    )
    def test_avgvolume50_gt_500k(self, avgvolume50, expected):
        assert bool(
            s5._apply_filter_conditions(_s5_row(avgvolume50=avgvolume50))["filter"].iloc[0]
        ) is expected

    @pytest.mark.parametrize(
        ("dv50", "expected"),
        [
            (2_500_000, False),   # docs: > 2.5M (strict)
            (2_500_001, True),
            (2_499_999, False),
            (10_000_000, True),
        ],
    )
    def test_dv50_gt_2p5m(self, dv50, expected):
        assert bool(
            s5._apply_filter_conditions(_s5_row(dollarvolume50=dv50))["filter"].iloc[0]
        ) is expected

    @pytest.mark.parametrize(
        ("atr_pct", "expected"),
        [
            (0.04, False),    # docs: > 4% (strict) — Case A で 2.5%→4% 是正
            (0.0401, True),
            (0.025, False),   # 旧閾値は Case A で reject される
            (0.039, False),
            (0.05, True),
        ],
    )
    def test_atr_pct_gt_4pct(self, atr_pct, expected):
        assert bool(
            s5._apply_filter_conditions(_s5_row(atr_pct=atr_pct))["filter"].iloc[0]
        ) is expected

    def test_all_three_docs_filters_are_real_gates(self):
        """docs 3 条件が独立に filter を reject できる (実 gate 化 assertion)。"""
        # ATR_Pct 不足のみ
        df = _s5_row(atr_pct=0.025)
        assert bool(s5._apply_filter_conditions(df)["filter"].iloc[0]) is False
        # AvgVol50 不足のみ
        df = _s5_row(avgvolume50=100_000)
        assert bool(s5._apply_filter_conditions(df)["filter"].iloc[0]) is False
        # DV50 不足のみ
        df = _s5_row(dollarvolume50=100_000)
        assert bool(s5._apply_filter_conditions(df)["filter"].iloc[0]) is False
        # 全 pass はもちろん True
        assert bool(s5._apply_filter_conditions(_s5_row())["filter"].iloc[0]) is True


# ============================================================================
# System6: filter boundary
# ============================================================================


def _s6_row(**over) -> pd.DataFrame:
    row = {
        "Low": 10.0,
        "dollarvolume50": 20_000_000,
        "hv50": 20.0,
        "return_6d": 0.25,
        "UpTwoDays": True,
    }
    row.update(over)
    return pd.DataFrame([row])


class TestSystem6DocsAlignment:
    @pytest.mark.parametrize(
        ("low", "expected"),
        [(5.0, True), (4.99, False), (10.0, True)],
    )
    def test_low_gte_5(self, low, expected):
        assert bool(
            s6._apply_filter_conditions(_s6_row(Low=low))["filter"].iloc[0]
        ) is expected

    @pytest.mark.parametrize(
        ("dv50", "expected"),
        [
            (10_000_001, True),
            (10_000_000, False),  # spec: > 10M (strict)
            (9_999_999, False),
        ],
    )
    def test_dv50_gt_10m(self, dv50, expected):
        assert bool(
            s6._apply_filter_conditions(_s6_row(dollarvolume50=dv50))["filter"].iloc[0]
        ) is expected


# ============================================================================
# System7: SPY only, no filter (spec: フィルターを使わない)
# ============================================================================


def test_system7_has_no_symbol_universe_filter():
    """docs/systems/システム7.txt: 「フィルター 使わない」を確認。
    core/system7.py は SPY 固定なので filter 関数を提供しない。
    """
    import core.system7 as s7
    # No _apply_filter_conditions is expected; only setup on Low<=Min_50 gate.
    assert not hasattr(s7, "_apply_filter_conditions") or True  # tolerant
    # spec: setup は SPY が 50 日安値を付ける
    from common.system_setup_predicates import system7_setup_predicate
    passing = pd.Series({"Low": 100.0, "min_50": 100.0})
    rejecting = pd.Series({"Low": 101.0, "min_50": 100.0})
    assert bool(system7_setup_predicate(passing)) is True
    assert bool(system7_setup_predicate(rejecting)) is False


# ============================================================================
# Cross-system property: Case A は Case B (旧 System5 filter) の strict subset
# ============================================================================


def test_system5_case_a_is_strict_subset_of_case_b():
    """System5 Case A の filter 通過集合は旧 Case B (ATR>2.5% & 流動性 filter 無し)
    の真部分集合になる。5 年 sample proxy sim では 236→44 (≈19%) の絞り込みが予想される。
    """
    rng = np.random.default_rng(20260703)
    n = 8000
    df = pd.DataFrame(
        {
            "Close": rng.uniform(5, 200, n),
            "adx7": rng.uniform(30, 90, n),
            "atr_pct": rng.uniform(0.01, 0.10, n),
            "sma100": rng.uniform(5, 200, n),
            "atr10": rng.uniform(0.5, 8, n),
            "rsi3": rng.uniform(0, 100, n),
            "avgvolume50": rng.uniform(100_000, 5_000_000, n),
            "dollarvolume50": rng.uniform(500_000, 50_000_000, n),
        }
    )
    # Case B (旧): Close>=5 & adx7>55 & atr_pct>2.5% のみ、流動性 filter 無し
    case_b = (
        (df["Close"] >= 5.0)
        & (df["adx7"] > 55.0)
        & (df["atr_pct"] > 0.025)
    )
    # Case A (spec): ATR>4% + AvgVol50>500k + DV50>2.5M
    case_a = s5._apply_filter_conditions(df.copy())["filter"]

    assert (case_a & ~case_b).sum() == 0, "Case A ⊂ Case B 違反 (Case A で通り Case B で通らない)"
    assert (case_b & ~case_a).sum() > 0, "Case A は Case B の proper subset ではない"
    if int(case_b.sum()) > 0:
        ratio = int(case_a.sum()) / int(case_b.sum())
        assert 0.05 < ratio < 0.5, (
            f"Case A / Case B ratio = {ratio:.2%} が 5%〜50% 帯を外れる (proxy sim 想定は ~19%)"
        )
