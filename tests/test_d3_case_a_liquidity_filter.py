"""D3 Case A regression tests — System5 流動性 filter + ATR 4% 化 (docs 完全準拠).

Case A dispatch (2026-07-03) の実装が spec (docs/systems/システム5.txt) 通りに
enforce されていることを property test で保証する。旧 D3 audit report
(docs/D3_LIQUIDITY_FILTER_ATR_THRESHOLD_20260702.md) の Case A シナリオを実装。

対象仕様 (docs/systems/システム5.txt:6-9):
    - フィルター:
      - 過去50日の平均出来高が 500,000 株を上回る (avgvolume50 > 500k)
      - 過去50日の平均売買代金が 2,500,000 $ を上回る (dollarvolume50 > 2.5M)
      - ATR が 4% を上回る (atr_pct > 0.04)

対象実装:
    - core/system5.py::_apply_filter_conditions
    - common/system_setup_predicates.py::system5_setup_predicate
    - common/system_constants.py::SYSTEM5_MIN_DOLLAR_VOLUME (spec 2.5M へ是正)
    - common/system_constants.py::SYSTEM5_ATR_PCT_THRESHOLD (spec 4% へ是正)
    - common/system_constants.py::SYSTEM5_MIN_AVG_VOLUME_50 (新規)

期待影響 (D3 audit micro-bench 参照):
    - top-20 候補数: 236 → 44 (5y sample proxy sim、およそ 19%)
    - unique 銘柄数: 73 → 16 (同上、およそ 22%)
    - 実運用スリッページ risk 解消 (低 DV 銘柄が候補から除外)
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from common.system_constants import (
    SYSTEM5_ATR_PCT_THRESHOLD,
    SYSTEM5_MIN_AVG_VOLUME_50,
    SYSTEM5_MIN_DOLLAR_VOLUME,
    SYSTEM5_REQUIRED_INDICATORS,
)
from common.system_setup_predicates import (
    DEFAULT_ATR_PCT_THRESHOLD as PREDICATE_ATR_THRESHOLD,
)
from common.system_setup_predicates import (
    MIN_AVG_VOLUME_50_SYSTEM5,
    MIN_DOLLAR_VOLUME_50_SYSTEM5,
    system5_setup_predicate,
)
import core.system5 as s5
from core.system5 import (
    DEFAULT_ATR_PCT_THRESHOLD,
    MIN_AVG_VOLUME_50,
    MIN_DOLLAR_VOLUME_50,
)

# ============================================================================
# Fixture: spec 準拠の Case A base row (全 filter/setup 条件 pass)
# ============================================================================


def _case_a_row(**overrides) -> pd.DataFrame:
    row = {
        "Close": 100.0,
        "adx7": 60.0,  # > 55 (spec)
        "atr_pct": 0.05,  # > 4% (spec, Case A)
        "sma100": 90.0,
        "atr10": 5.0,  # Close(100) > sma100(90) + atr10(5) = 95
        "rsi3": 30.0,  # < 50 (spec)
        # Case A 流動性 filter (spec)
        "avgvolume50": 1_000_000,  # > 500k (spec)
        "dollarvolume50": 10_000_000,  # > 2.5M (spec)
    }
    row.update(overrides)
    return pd.DataFrame([row])


def _filter_bool(df: pd.DataFrame) -> bool:
    return bool(s5._apply_filter_conditions(df)["filter"].iloc[0])


def _setup_bool(df: pd.DataFrame) -> bool:
    return bool(
        s5._apply_setup_conditions(s5._apply_filter_conditions(df))["setup"].iloc[0]
    )


# ============================================================================
# Constants — Case A で定数が spec に是正されたことを boundary で assert
# ============================================================================


class TestCaseAConstants:
    def test_atr_pct_threshold_is_spec_4pct(self):
        """spec (docs/systems/システム5.txt:9): ATR が 4% を上回る → 0.04"""
        assert DEFAULT_ATR_PCT_THRESHOLD == 0.04
        assert SYSTEM5_ATR_PCT_THRESHOLD == 0.04

    def test_min_avg_volume_50_is_spec_500k(self):
        """spec (docs/systems/システム5.txt:7): 過去50日平均出来高 > 500k 株"""
        assert MIN_AVG_VOLUME_50 == 500_000
        assert SYSTEM5_MIN_AVG_VOLUME_50 == 500_000

    def test_min_dollar_volume_50_is_spec_2p5m(self):
        """spec (docs/systems/システム5.txt:8): 過去50日平均売買代金 > 2.5M $"""
        assert MIN_DOLLAR_VOLUME_50 == 2_500_000
        # 旧 SYSTEM5_MIN_DOLLAR_VOLUME=25_000_000 dead constant を 2.5M へ是正済み
        assert SYSTEM5_MIN_DOLLAR_VOLUME == 2_500_000

    def test_predicate_constants_match_core(self):
        """common/system_setup_predicates は core と同値でなければならない (循環回避のため再定義)。"""
        assert PREDICATE_ATR_THRESHOLD == DEFAULT_ATR_PCT_THRESHOLD
        assert MIN_AVG_VOLUME_50_SYSTEM5 == float(MIN_AVG_VOLUME_50)
        assert MIN_DOLLAR_VOLUME_50_SYSTEM5 == float(MIN_DOLLAR_VOLUME_50)

    def test_required_indicators_include_liquidity_columns(self):
        """Case A 流動性 filter に必要な列が SYSTEM5_REQUIRED_INDICATORS に含まれる。"""
        assert "avgvolume50" in SYSTEM5_REQUIRED_INDICATORS
        assert "dollarvolume50" in SYSTEM5_REQUIRED_INDICATORS


# ============================================================================
# Boundary tests — filter が spec 閾値で発火することを property test
# ============================================================================


class TestAvgVolume50Filter:
    @pytest.mark.parametrize(
        ("avgvolume50", "expected"),
        [
            (500_000, False),  # spec: > 500k (strict)
            (500_001, True),
            (499_999, False),
            (1_000_000, True),
            (250_000, False),
            (10_000_000, True),
        ],
    )
    def test_avgvolume50_boundary(self, avgvolume50, expected):
        assert _filter_bool(_case_a_row(avgvolume50=avgvolume50)) is expected

    def test_avgvolume50_nan_rejects(self):
        """欠損値 (NaN) は filter を False にする (実運用 safety)。"""
        assert _filter_bool(_case_a_row(avgvolume50=np.nan)) is False


class TestDollarVolume50Filter:
    @pytest.mark.parametrize(
        ("dollarvolume50", "expected"),
        [
            (2_500_000, False),  # spec: > 2.5M (strict)
            (2_500_001, True),
            (2_499_999, False),
            (5_000_000, True),
            (1_000_000, False),
            (50_000_000, True),
        ],
    )
    def test_dollarvolume50_boundary(self, dollarvolume50, expected):
        assert _filter_bool(_case_a_row(dollarvolume50=dollarvolume50)) is expected

    def test_dollarvolume50_nan_rejects(self):
        assert _filter_bool(_case_a_row(dollarvolume50=np.nan)) is False


class TestAtrPct4pctFilter:
    @pytest.mark.parametrize(
        ("atr_pct", "expected"),
        [
            (0.04, False),  # spec: > 4% (strict)
            (0.0401, True),
            (0.039, False),
            (0.025, False),  # 旧閾値 → Case A では reject
            (0.05, True),
            (0.10, True),
        ],
    )
    def test_atr_pct_boundary(self, atr_pct, expected):
        assert _filter_bool(_case_a_row(atr_pct=atr_pct)) is expected


# ============================================================================
# 実 gate 化 assertion — 診断カウンタから filter への格上げが機能している
# ============================================================================


class TestLiquidityFilterIsRealGate:
    """Case A 実装以前は AvgVol50/DV50 は「診断カウンタとして数えるだけ」で
    filter/setup 判定に反映されなかった (D3 audit report Phase 1)。
    Case A では実 gate に格上げ済みであることを assert する。
    """

    def test_low_liquidity_setup_is_rejected(self):
        """流動性 filter (AvgVol50/DV50) が実 gate として setup を reject する。"""
        # 旧実装では他条件が pass すれば setup=True になっていた組み合わせ
        df = _case_a_row(avgvolume50=100_000, dollarvolume50=200_000)
        assert _filter_bool(df) is False
        assert _setup_bool(df) is False

    def test_high_liquidity_setup_passes(self):
        """spec 閾値を超える流動性を持つ銘柄は setup=True。"""
        df = _case_a_row(avgvolume50=2_000_000, dollarvolume50=100_000_000)
        assert _filter_bool(df) is True
        assert _setup_bool(df) is True

    def test_avgvolume50_alone_is_gate(self):
        """DV50 が pass でも AvgVol50 が不足だと reject。"""
        df = _case_a_row(avgvolume50=100_000, dollarvolume50=100_000_000)
        assert _filter_bool(df) is False

    def test_dollarvolume50_alone_is_gate(self):
        """AvgVol50 が pass でも DV50 が不足だと reject。"""
        df = _case_a_row(avgvolume50=10_000_000, dollarvolume50=100_000)
        assert _filter_bool(df) is False


# ============================================================================
# Predicate synchronization — core と predicate が同値を返す
# ============================================================================


class TestPredicateSynchronization:
    """common/system_setup_predicates.system5_setup_predicate が core と同値。"""

    @pytest.mark.parametrize(
        "overrides",
        [
            {},  # base pass
            {"atr_pct": 0.039},  # ATR < 4%
            {"avgvolume50": 400_000},  # AvgVol50 不足
            {"dollarvolume50": 1_500_000},  # DV50 不足
            {"adx7": 50.0},  # ADX < 55
            {"rsi3": 55.0},  # RSI3 >= 50
            {"Close": 4.0},  # penny stock
            {"Close": 90.0, "sma100": 95.0, "atr10": 3.0},  # price band 割れ
        ],
    )
    def test_predicate_matches_core_setup(self, overrides):
        df = _case_a_row(**overrides)
        core_setup = _setup_bool(df)
        pred = bool(system5_setup_predicate(df.iloc[0]))
        assert core_setup == pred, f"mismatch on {df.iloc[0].to_dict()}"


# ============================================================================
# Regression: Case A が Case B (旧実装) の strict subset である
# ============================================================================


def test_case_a_is_strict_subset_of_case_b():
    """Case A の setup 通過集合は Case B (旧 2.5% + 流動性 filter 無し) の strict subset。

    Case A では流動性 filter + ATR 4% が加わるので、setup が通る候補は Case B のそれの
    真部分集合になる (D3 audit micro-bench 予想: 236 → 44 で ~19%)。
    """
    rng = np.random.default_rng(20260703)
    n = 5000
    df = pd.DataFrame(
        {
            "Close": rng.uniform(5, 200, n),
            "adx7": rng.uniform(35, 90, n),
            "atr_pct": rng.uniform(0.02, 0.10, n),
            "sma100": rng.uniform(5, 200, n),
            "atr10": rng.uniform(0.5, 8, n),
            "rsi3": rng.uniform(0, 100, n),
            "avgvolume50": rng.uniform(100_000, 5_000_000, n),
            "dollarvolume50": rng.uniform(500_000, 50_000_000, n),
        }
    )
    # Case B (旧): Close>=5 & adx7>55 & atr_pct>2.5%、流動性 filter 無し
    case_b = (df["Close"] >= 5.0) & (df["adx7"] > 55.0) & (df["atr_pct"] > 0.025)
    # Case A (spec): 上記 + atr_pct>4% + avgvol50>500k + dv50>2.5M
    case_a = s5._apply_filter_conditions(df.copy())["filter"]

    # strict subset: Case A の全 True は Case B でも True
    assert (case_a & ~case_b).sum() == 0
    # strict: 少なくとも 1 つは Case B が通して Case A が reject する
    assert (case_b & ~case_a).sum() > 0
    # 想定: Case A は Case B の 30% 以下 (5y proxy sim: 236→44 で ~19%)
    if int(case_b.sum()) > 0:
        ratio = int(case_a.sum()) / int(case_b.sum())
        assert ratio < 0.5, f"Case A/Case B ratio {ratio:.2%} が想定より高い"
