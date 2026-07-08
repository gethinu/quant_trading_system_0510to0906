"""S1 setup 3 経路統一 regression test (D1 audit 2026-07-02).

Docs-vs-impl divergence audit で発見された D1 の再発防止:
    batch 経路 (`core.system1._apply_setup_conditions`)
    row 経路   (`core.system1.system1_row_passes_setup`)
    predicate  (`common.system_setup_predicates.system1_setup_predicate`)
の 3 つが同一銘柄集合を setup として返すことを assert する。

Docs 上の正しい setup 条件 (`docs/systems/システム1.txt`):
    - 25日SMA > 50日SMA (個別銘柄)
    - Phase 2 filter (Close>=5, DollarVolume20>50M) を通過
    - ROC200 > 0 (impl 拡張、docs 上は ranking 用のみ)
    - SPY > SMA100 gate は orchestrator (common/today_signals.py) 側で適用

D1 修正前は batch 経路のみ Close>SMA200 で他 2 経路と齟齬していた。
本 test は 3 経路が **常に同一 boolean** を返すこと (subscriber consistency) を
property-check する。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from common.system_setup_predicates import system1_setup_predicate
from core.system1 import (
    _apply_filter_conditions,
    _apply_setup_conditions,
    system1_row_passes_setup,
)


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------
def _make_universe(seed: int = 20260702, n: int = 400) -> pd.DataFrame:
    """Synthetic universe covering all filter/setup boundary conditions.

    Columns follow S1 pipeline expectations: Close, dollarvolume20,
    sma25, sma50, roc200. Spread wide across pass/fail boundaries so
    that no path can trivially agree by returning all False.
    """
    rng = np.random.default_rng(seed)
    df = pd.DataFrame(
        {
            "Close": rng.uniform(3.0, 200.0, n),
            "dollarvolume20": rng.uniform(10_000_000, 200_000_000, n),
            "sma25": rng.uniform(50.0, 150.0, n),
            "sma50": rng.uniform(50.0, 150.0, n),
            "roc200": rng.uniform(-0.5, 0.5, n),
        }
    )
    return df


# ---------------------------------------------------------------------------
# Batch vs row vs predicate consistency
# ---------------------------------------------------------------------------
class TestS1SetupThreePathUnification:
    def test_batch_vs_predicate_identical_mask(self):
        """Batch 経路と predicate 経路が同一 boolean 配列を返す。"""
        df = _make_universe()
        batch_setup = (
            _apply_setup_conditions(_apply_filter_conditions(df.copy()))["setup"]
            .astype(bool)
            .to_numpy()
        )
        predicate_setup = np.array(
            [bool(system1_setup_predicate(row)) for _, row in df.iterrows()]
        )
        # Two ways: same mask, and identical count
        assert (batch_setup == predicate_setup).all(), (
            "batch と predicate で setup 判定が食い違う行があります "
            f"(batch True={batch_setup.sum()}, predicate True={predicate_setup.sum()})"
        )
        assert int(batch_setup.sum()) == int(predicate_setup.sum())

    def test_batch_vs_row_identical_mask(self):
        """Batch 経路と row 経路が同一 boolean 配列を返す。

        Row 経路 (`system1_row_passes_setup`) は phase-2 filter を pre-flight
        済み前提。ここでは filter を batch で計算して pass 行のみに絞り、
        両者で setup 条件のみを比較する (fair 比較)。
        """
        df = _make_universe()
        with_filter = _apply_filter_conditions(df.copy())
        filter_mask = with_filter["filter"].astype(bool).to_numpy()

        batch_setup_full = (
            _apply_setup_conditions(with_filter.copy())["setup"].astype(bool).to_numpy()
        )

        row_setup: list[bool] = []
        for idx, row in with_filter.iterrows():
            if not bool(row["filter"]):
                # Row 経路は filter 通過を前提としているため、filter fail 行は
                # そのまま False として扱う (batch も filter で切っている)
                row_setup.append(False)
                continue
            passes, _flags, _reason = system1_row_passes_setup(row)
            row_setup.append(bool(passes))
        row_setup_arr = np.asarray(row_setup)

        # Only compare where filter passed (batch も filter で AND されている)
        assert (batch_setup_full[filter_mask] == row_setup_arr[filter_mask]).all(), (
            "batch と row 経路で setup 判定が食い違う行があります "
            f"(batch True={batch_setup_full[filter_mask].sum()}, "
            f"row True={row_setup_arr[filter_mask].sum()})"
        )

    def test_all_three_paths_produce_same_symbol_set(self):
        """Batch / row / predicate 3 経路が同一の候補集合 (symbol set) を返す。

        integration 的な集合等価性の assert。ここが赤くなるということは
        D1 のような divergence が再発したことを意味する。
        """
        df = _make_universe()
        with_filter = _apply_filter_conditions(df.copy())

        batch_set = set(
            with_filter.index[
                _apply_setup_conditions(with_filter.copy())["setup"].astype(bool)
            ].tolist()
        )
        predicate_set = set(
            idx
            for idx, row in with_filter.iterrows()
            if bool(system1_setup_predicate(row))
        )
        row_set = set(
            idx
            for idx, row in with_filter.iterrows()
            if bool(row["filter"]) and system1_row_passes_setup(row)[0]
        )

        assert batch_set == predicate_set, (
            f"batch({len(batch_set)}) != predicate({len(predicate_set)}); "
            f"diff={batch_set ^ predicate_set}"
        )
        assert batch_set == row_set, (
            f"batch({len(batch_set)}) != row({len(row_set)}); "
            f"diff={batch_set ^ row_set}"
        )


# ---------------------------------------------------------------------------
# Docs 準拠 condition assertion (redundant safety net)
# ---------------------------------------------------------------------------
class TestS1SetupDocsCompliance:
    @pytest.mark.parametrize(
        ("sma25", "sma50", "expected"),
        [
            (100.01, 100.0, True),
            (100.0, 100.0, False),  # strict >
            (99.99, 100.0, False),
        ],
    )
    def test_sma25_strictly_greater_than_sma50(self, sma25, sma50, expected):
        """Batch 経路が SMA25 > SMA50 (strict) を実装していること。"""
        df = pd.DataFrame(
            [
                {
                    "Close": 100.0,
                    "dollarvolume20": 60_000_000,
                    "sma25": sma25,
                    "sma50": sma50,
                    "roc200": 0.05,
                }
            ]
        )
        result = _apply_setup_conditions(_apply_filter_conditions(df))
        assert bool(result["setup"].iloc[0]) is expected

    def test_setup_does_not_depend_on_sma200(self):
        """SMA200 の値が変わっても setup 判定は変わらない (D1 修正後の docs 準拠)。"""
        base_row = {
            "Close": 100.0,
            "dollarvolume20": 60_000_000,
            "sma25": 110.0,
            "sma50": 100.0,
            "roc200": 0.05,
        }
        df_lo = pd.DataFrame([{**base_row, "sma200": 50.0}])  # Close >> SMA200
        df_hi = pd.DataFrame([{**base_row, "sma200": 200.0}])  # Close << SMA200

        result_lo = _apply_setup_conditions(_apply_filter_conditions(df_lo))
        result_hi = _apply_setup_conditions(_apply_filter_conditions(df_hi))
        assert bool(result_lo["setup"].iloc[0]) is True
        assert (
            bool(result_hi["setup"].iloc[0]) is True
        ), "SMA200 の値が setup 判定に影響してはならない (D1 修正後)"
