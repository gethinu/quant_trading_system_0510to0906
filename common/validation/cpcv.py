"""Combinatorial Purged Cross-Validation (CPCV) with purge + embargo.

López de Prado, *Advances in Financial Machine Learning*, ch. 7 & 12.

The ordered timeline is partitioned into ``n_groups`` contiguous blocks. Every
combination of ``k_test`` blocks forms one test set; the remaining blocks form
the training set, from which we then:

* **purge** any observation whose label span ``[t0, t1]`` overlaps the test
  window (prevents look-ahead leakage from overlapping labels), and
* **embargo** a further fraction of observations immediately after each test
  block (prevents leakage from serial correlation across the boundary).

Producing every ``C(n_groups, k_test)`` combination yields
``phi = C(n_groups, k_test) * k_test / n_groups`` distinct backtest paths, which
is exactly the multiplicity a Deflated Sharpe Ratio must correct for.

For the trading-system use case the "observation" is a signal date and the
label span is the trade holding period (entry_date -> exit_date). This module is
engine-agnostic: it only produces date folds; :mod:`common.validation.evaluate`
runs the existing backtest engines over them.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations
from math import comb

import numpy as np
import pandas as pd


def n_backtest_paths(n_groups: int, k_test: int) -> int:
    """Number of distinct CPCV backtest paths phi[N, k]."""
    if k_test <= 0 or k_test >= n_groups:
        return 0
    return int(comb(n_groups, k_test) * k_test / n_groups)


def n_combinations(n_groups: int, k_test: int) -> int:
    return int(comb(n_groups, k_test))


def _contiguous_blocks(positions: list[int]) -> list[tuple[int, int]]:
    """Collapse a sorted list of ints into (start, end) inclusive runs."""
    if not positions:
        return []
    blocks: list[tuple[int, int]] = []
    start = prev = positions[0]
    for p in positions[1:]:
        if p == prev + 1:
            prev = p
        else:
            blocks.append((start, prev))
            start = prev = p
    blocks.append((start, prev))
    return blocks


def combinatorial_purged_splits(
    n_samples: int,
    *,
    n_groups: int = 6,
    k_test: int = 2,
    embargo_pct: float = 0.01,
    t1_pos: np.ndarray | None = None,
):
    """Yield ``(train_pos, test_pos, combo)`` integer-position splits.

    Parameters
    ----------
    n_samples : int
        Number of ordered observations.
    n_groups, k_test : int
        Partition into ``n_groups`` blocks, test on every combination of
        ``k_test`` of them.
    embargo_pct : float
        Fraction of ``n_samples`` embargoed after each test block.
    t1_pos : np.ndarray, optional
        For each observation ``i``, the integer position at which its label
        ends (``>= i``). Defaults to ``i`` (point labels).
    """
    if not (1 <= k_test < n_groups):
        raise ValueError("require 1 <= k_test < n_groups")
    idx = np.arange(n_samples)
    if n_samples < n_groups:
        raise ValueError(f"n_samples={n_samples} < n_groups={n_groups}")
    groups = np.array_split(idx, n_groups)
    embargo = int(round(float(embargo_pct) * n_samples))
    if t1_pos is None:
        t1_pos = idx.copy()
    t1_pos = np.asarray(t1_pos)

    for combo in combinations(range(n_groups), k_test):
        test_pos = np.concatenate([groups[g] for g in combo])
        train_mask = np.ones(n_samples, dtype=bool)
        train_mask[test_pos] = False
        for (b0, b1) in _contiguous_blocks(sorted(test_pos.tolist())):
            emb_end = min(n_samples - 1, b1 + embargo)
            train_positions = np.where(train_mask)[0]
            if train_positions.size:
                j0 = train_positions
                j1 = t1_pos[train_positions]
                # overlap of label [j0, j1] with test+embargo window [b0, emb_end]
                overlaps = ~((j1 < b0) | (j0 > emb_end))
                train_mask[train_positions[overlaps]] = False
        yield idx[train_mask], test_pos, combo


@dataclass(frozen=True)
class DateFold:
    combo: tuple[int, ...]
    test_dates: frozenset
    train_dates: frozenset

    @property
    def n_test(self) -> int:
        return len(self.test_dates)

    @property
    def n_train(self) -> int:
        return len(self.train_dates)


def cpcv_date_folds(
    dates,
    *,
    n_groups: int = 6,
    k_test: int = 2,
    embargo_pct: float = 0.01,
    label_end_by_date: dict | None = None,
) -> list[DateFold]:
    """Build CPCV folds over an ordered set of dates.

    ``label_end_by_date`` maps a signal date to the trade's exit date (label
    end). If omitted, labels are treated as points (purge then only removes
    exact-boundary overlaps; embargo still applies).
    """
    di = pd.DatetimeIndex(sorted(pd.to_datetime(pd.Index(list(dates))).unique()))
    n = len(di)
    if n < n_groups:
        raise ValueError(f"only {n} unique dates for n_groups={n_groups}")

    if label_end_by_date:
        ends = pd.to_datetime(
            [label_end_by_date.get(d, d) for d in di]
        )
        # position of the last signal date <= label end
        t1_pos = np.searchsorted(di.values, ends.values, side="right") - 1
        t1_pos = np.maximum(t1_pos, np.arange(n))
        t1_pos = np.minimum(t1_pos, n - 1)
    else:
        t1_pos = None

    folds: list[DateFold] = []
    for train_pos, test_pos, combo in combinatorial_purged_splits(
        n, n_groups=n_groups, k_test=k_test, embargo_pct=embargo_pct, t1_pos=t1_pos
    ):
        folds.append(
            DateFold(
                combo=tuple(combo),
                test_dates=frozenset(pd.Timestamp(x) for x in di[test_pos]),
                train_dates=frozenset(pd.Timestamp(x) for x in di[train_pos]),
            )
        )
    return folds


def filter_candidates_by_dates(candidates_by_date: dict, allowed_dates) -> dict:
    """Return a shallow-filtered ``candidates_by_date`` restricted to a fold.

    Keys are normalized to ``pd.Timestamp`` for comparison, matching how the
    engines look them up.
    """
    allowed = {pd.Timestamp(d) for d in allowed_dates}
    out = {}
    for k, v in candidates_by_date.items():
        if pd.Timestamp(k) in allowed:
            out[k] = v
    return out


def label_end_map_from_trades(trades_df: pd.DataFrame) -> dict:
    """Map entry_date -> max exit_date, for purge label spans."""
    if trades_df is None or trades_df.empty:
        return {}
    df = trades_df[["entry_date", "exit_date"]].copy()
    df["entry_date"] = pd.to_datetime(df["entry_date"])
    df["exit_date"] = pd.to_datetime(df["exit_date"])
    return df.groupby("entry_date")["exit_date"].max().to_dict()
