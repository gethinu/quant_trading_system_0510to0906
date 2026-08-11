"""Unit tests for CPCV purge/embargo (common.validation.cpcv)."""

from __future__ import annotations

from math import comb

import numpy as np
import pandas as pd

from common.validation.cpcv import (
    cpcv_date_folds,
    combinatorial_purged_splits,
    filter_candidates_by_dates,
    label_end_map_from_trades,
    n_backtest_paths,
    n_combinations,
)


def test_path_and_combination_counts():
    assert n_combinations(6, 2) == comb(6, 2) == 15
    assert n_backtest_paths(6, 2) == int(15 * 2 / 6) == 5
    assert n_backtest_paths(10, 2) == 9


def test_splits_count_and_disjoint():
    splits = list(combinatorial_purged_splits(60, n_groups=6, k_test=2, embargo_pct=0.0))
    assert len(splits) == 15
    for train, test, combo in splits:
        assert set(train).isdisjoint(set(test))
        assert len(combo) == 2


def test_purge_removes_overlapping_labels():
    # Labels span 5 positions ahead -> train obs adjacent to test must be purged.
    n = 60
    t1 = np.minimum(np.arange(n) + 5, n - 1)
    no_purge = list(
        combinatorial_purged_splits(n, n_groups=6, k_test=2, embargo_pct=0.0)
    )
    with_purge = list(
        combinatorial_purged_splits(
            n, n_groups=6, k_test=2, embargo_pct=0.0, t1_pos=t1
        )
    )
    # Same test sets, but purged training sets are strictly smaller on average.
    tot_no = sum(len(tr) for tr, _, _ in no_purge)
    tot_pu = sum(len(tr) for tr, _, _ in with_purge)
    assert tot_pu < tot_no


def test_embargo_removes_forward_neighbors():
    n = 60
    none = list(combinatorial_purged_splits(n, n_groups=6, k_test=1, embargo_pct=0.0))
    emb = list(combinatorial_purged_splits(n, n_groups=6, k_test=1, embargo_pct=0.1))
    assert sum(len(tr) for tr, _, _ in emb) < sum(len(tr) for tr, _, _ in none)


def test_cpcv_date_folds_structure():
    dates = pd.bdate_range("2020-01-01", periods=60)
    folds = cpcv_date_folds(dates, n_groups=6, k_test=2, embargo_pct=0.02)
    assert len(folds) == 15
    for f in folds:
        assert f.test_dates.isdisjoint(f.train_dates)
        assert f.n_test > 0


def test_cpcv_date_folds_with_trade_labels_purges():
    dates = pd.bdate_range("2020-01-01", periods=60)
    # every entry holds ~10 business days
    label_end = {d: d + pd.Timedelta(days=14) for d in dates}
    plain = cpcv_date_folds(dates, n_groups=6, k_test=2, embargo_pct=0.0)
    purged = cpcv_date_folds(
        dates, n_groups=6, k_test=2, embargo_pct=0.0, label_end_by_date=label_end
    )
    assert sum(f.n_train for f in purged) < sum(f.n_train for f in plain)


def test_filter_candidates_by_dates():
    dates = pd.bdate_range("2020-01-01", periods=5)
    cbd = {d: [{"symbol": "AAA"}] for d in dates}
    keep = {dates[0], dates[2]}
    out = filter_candidates_by_dates(cbd, keep)
    assert set(pd.Timestamp(k) for k in out) == keep


def test_label_end_map_from_trades():
    tr = pd.DataFrame(
        {
            "entry_date": pd.to_datetime(["2020-01-01", "2020-01-01", "2020-01-02"]),
            "exit_date": pd.to_datetime(["2020-01-05", "2020-01-08", "2020-01-06"]),
        }
    )
    m = label_end_map_from_trades(tr)
    assert m[pd.Timestamp("2020-01-01")] == pd.Timestamp("2020-01-08")
