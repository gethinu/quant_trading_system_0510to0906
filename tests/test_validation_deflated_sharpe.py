"""Unit tests for PSR / DSR math (common.validation.deflated_sharpe)."""

from __future__ import annotations

import numpy as np

from common.validation import deflated_sharpe as D


def test_norm_cdf_known_values():
    assert abs(D.norm_cdf(0.0) - 0.5) < 1e-12
    assert abs(D.norm_cdf(1.96) - 0.9750021) < 1e-5
    assert abs(D.norm_cdf(-1.96) - 0.0249979) < 1e-5


def test_norm_ppf_roundtrip():
    for p in (0.001, 0.01, 0.1, 0.25, 0.5, 0.75, 0.9, 0.99, 0.999):
        assert abs(D.norm_cdf(D.norm_ppf(p)) - p) < 1e-6


def test_expected_max_sharpe_increases_with_trials():
    vals = [D.expected_max_sharpe(n, 0.1) for n in (1, 2, 10, 100, 1000)]
    assert vals[0] == 0.0
    assert all(b > a for a, b in zip(vals, vals[1:]))


def test_moments_gaussian():
    rng = np.random.default_rng(0)
    r = rng.normal(0, 1, 200_00)
    assert abs(D.skewness(r)) < 0.1
    assert abs(D.kurtosis(r) - 3.0) < 0.15  # non-excess ~ 3


def test_psr_monotonic_in_observed_sharpe():
    lo = D.probabilistic_sharpe_ratio(0.05, 0.0, 500)
    hi = D.probabilistic_sharpe_ratio(0.15, 0.0, 500)
    assert 0.0 <= lo <= hi <= 1.0


def test_dsr_deflates_with_more_trials():
    rng = np.random.default_rng(3)
    r = rng.normal(0.0008, 0.01, 1000)  # modest positive drift
    d1 = D.deflated_sharpe_ratio(r, 1)
    d50 = D.deflated_sharpe_ratio(r, 50, sr_variance=0.1**2)
    d1000 = D.deflated_sharpe_ratio(r, 1000, sr_variance=0.1**2)
    assert d1.deflated_sharpe >= d50.deflated_sharpe >= d1000.deflated_sharpe
    assert d1.n_trials == 1 and d1000.n_trials == 1000


def test_dsr_pure_noise_not_significant():
    rng = np.random.default_rng(7)
    r = rng.normal(0.0, 0.01, 800)  # zero-mean noise
    d = D.deflated_sharpe_ratio(r, 100, sr_variance=0.1**2)
    assert not d.passed
    assert d.deflated_sharpe < 0.95


def test_dsr_strong_signal_single_trial_passes():
    rng = np.random.default_rng(11)
    r = rng.normal(0.0015, 0.008, 1500)  # strong, genuine
    d = D.deflated_sharpe_ratio(r, 1)
    assert d.deflated_sharpe > 0.95 and d.passed
