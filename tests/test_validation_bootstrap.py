"""Unit tests for the moving-block bootstrap (common.validation.bootstrap)."""

from __future__ import annotations

import numpy as np

from common.validation.bootstrap import (
    moving_block_bootstrap,
    optimal_block_length,
    _moving_block_indices,
)


def test_optimal_block_length():
    assert optimal_block_length(1) == 1
    assert optimal_block_length(1000) == 10  # 1000**(1/3)


def test_moving_block_indices_valid_range():
    rng = np.random.default_rng(0)
    idx = _moving_block_indices(50, 5, rng)
    assert idx.size == 50
    assert idx.min() >= 0 and idx.max() < 50


def test_bootstrap_deterministic_with_seed():
    rng = np.random.default_rng(1)
    r = rng.normal(0.0005, 0.01, 400)
    a = moving_block_bootstrap(r, n_boot=300, seed=42)
    b = moving_block_bootstrap(r, n_boot=300, seed=42)
    assert a.to_dict() == b.to_dict()


def test_bootstrap_ci_brackets_point_estimate():
    rng = np.random.default_rng(2)
    r = rng.normal(0.001, 0.01, 750)
    res = moving_block_bootstrap(r, n_boot=1000, seed=5)
    assert res.ci_low <= res.point_estimate <= res.ci_high
    assert res.n_obs == 750 and res.n_boot == 1000


def test_bootstrap_pvalue_low_for_strong_signal():
    rng = np.random.default_rng(4)
    r = rng.normal(0.0015, 0.007, 1200)  # clearly positive Sharpe
    res = moving_block_bootstrap(r, n_boot=1500, seed=9)
    assert res.p_value_le_zero < 0.05


def test_bootstrap_pvalue_high_for_noise():
    rng = np.random.default_rng(6)
    r = rng.normal(0.0, 0.01, 1000)
    res = moving_block_bootstrap(r, n_boot=1500, seed=9)
    assert 0.2 < res.p_value_le_zero < 0.8


def test_bootstrap_handles_empty():
    res = moving_block_bootstrap(np.array([]), n_boot=100)
    assert res.n_obs == 0 and res.n_boot == 0
