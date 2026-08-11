"""Probabilistic and Deflated Sharpe Ratio (PSR / DSR).

Implements Bailey & López de Prado (2012, 2014):

* Probabilistic Sharpe Ratio - the probability that the true Sharpe exceeds a
  benchmark, accounting for the skewness and kurtosis of returns and the sample
  length.
* Deflated Sharpe Ratio - PSR against a benchmark that is *deflated* for the
  number of independent trials ``N`` (multiplicity / selection bias). This is
  the "last line of defence" against overfitting from trying many strategies.

Everything is implemented with numpy + the standard library (no scipy). The
normal CDF uses ``math.erf``; the inverse normal uses the Acklam rational
approximation (|error| < 1.15e-9).

Units: all Sharpe quantities passed to the DSR/PSR math must be *per-period*
(non-annualized) and consistent with each other. Use
``deannualize_sharpe`` to convert an annualized Sharpe.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
import math

import numpy as np

EULER_MASCHERONI = 0.5772156649015329


# --------------------------------------------------------------------------- #
# Normal distribution helpers (no scipy)
# --------------------------------------------------------------------------- #
def norm_cdf(x: float) -> float:
    """Standard-normal CDF via the error function."""
    return 0.5 * (1.0 + math.erf(float(x) / math.sqrt(2.0)))


def norm_ppf(p: float) -> float:
    """Inverse standard-normal CDF (Acklam's algorithm)."""
    if p <= 0.0:
        return -math.inf
    if p >= 1.0:
        return math.inf
    a = [-3.969683028665376e01, 2.209460984245205e02, -2.759285104469687e02,
         1.383577518672690e02, -3.066479806614716e01, 2.506628277459239e00]
    b = [-5.447609879822406e01, 1.615858368580409e02, -1.556989798598866e02,
         6.680131188771972e01, -1.328068155288572e01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e00,
         -2.549732539343734e00, 4.374664141464968e00, 2.938163982698783e00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e00,
         3.754408661907416e00]
    plow, phigh = 0.02425, 1.0 - 0.02425
    if p < plow:
        q = math.sqrt(-2.0 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
               ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0)
    if p > phigh:
        q = math.sqrt(-2.0 * math.log(1.0 - p))
        return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
                ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0)
    q = p - 0.5
    r = q * q
    return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / \
           (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1.0)


# --------------------------------------------------------------------------- #
# Moment helpers
# --------------------------------------------------------------------------- #
def _clean(returns) -> np.ndarray:
    r = np.asarray(returns, dtype=float)
    return r[~np.isnan(r)]


def skewness(returns) -> float:
    r = _clean(returns)
    if r.size < 3:
        return 0.0
    s = r.std(ddof=0)
    if s <= 0:
        return 0.0
    return float(np.mean(((r - r.mean()) / s) ** 3))


def kurtosis(returns) -> float:
    """Non-excess kurtosis (normal == 3.0)."""
    r = _clean(returns)
    if r.size < 4:
        return 3.0
    s = r.std(ddof=0)
    if s <= 0:
        return 3.0
    return float(np.mean(((r - r.mean()) / s) ** 4))


def deannualize_sharpe(sharpe_annual: float, periods: int = 252) -> float:
    return float(sharpe_annual) / math.sqrt(periods)


# --------------------------------------------------------------------------- #
# PSR / DSR
# --------------------------------------------------------------------------- #
def probabilistic_sharpe_ratio(
    observed_sr: float,
    benchmark_sr: float,
    n_obs: int,
    skew: float = 0.0,
    kurt: float = 3.0,
) -> float:
    """PSR: P(true SR > benchmark_sr).

    ``observed_sr`` / ``benchmark_sr`` are *per-period* Sharpe ratios; ``n_obs``
    is the sample length. Returns a probability in [0, 1].
    """
    n = int(n_obs)
    if n < 2:
        return float("nan")
    denom = 1.0 - skew * observed_sr + ((kurt - 1.0) / 4.0) * observed_sr ** 2
    if denom <= 0:
        # Degenerate variance estimate; fall back to the Gaussian case.
        denom = max(1e-12, 1.0 + 0.5 * observed_sr ** 2)
    z = (observed_sr - benchmark_sr) * math.sqrt(n - 1) / math.sqrt(denom)
    return norm_cdf(z)


def expected_max_sharpe(n_trials: int, sr_std: float, sr_mean: float = 0.0) -> float:
    """Expected maximum of ``n_trials`` iid Sharpe estimates ~ N(sr_mean, sr_std^2).

    This is the deflation benchmark SR_0 (Bailey & López de Prado 2014). All
    quantities are *per-period* Sharpe units, consistent with ``sr_std``.
    """
    n = max(1, int(n_trials))
    if sr_std <= 0 or n == 1:
        return float(sr_mean)
    e = math.e
    term = (1.0 - EULER_MASCHERONI) * norm_ppf(1.0 - 1.0 / n) + \
        EULER_MASCHERONI * norm_ppf(1.0 - 1.0 / (n * e))
    return float(sr_mean + sr_std * term)


@dataclass(frozen=True)
class DSRResult:
    observed_sharpe_annual: float
    observed_sharpe_per_period: float
    benchmark_sharpe_per_period: float   # SR_0 (deflation threshold)
    n_obs: int
    n_trials: int
    skew: float
    kurt: float
    psr_vs_zero: float                    # P(true SR > 0)
    deflated_sharpe: float                # P(true SR > SR_0) == DSR
    passed: bool                          # DSR > threshold (default 0.95)

    def to_dict(self) -> dict:
        return asdict(self)


def deflated_sharpe_ratio(
    returns,
    n_trials: int,
    *,
    trial_sharpes=None,
    sr_variance: float | None = None,
    periods: int = 252,
    pass_threshold: float = 0.95,
) -> DSRResult:
    """Compute the Deflated Sharpe Ratio for a return series.

    Parameters
    ----------
    returns : array-like
        Per-period (e.g. daily) return series of the *selected* strategy.
    n_trials : int
        Number of independent strategy configurations that were tried. This is
        the multiplicity that DSR corrects for; ``N == 1`` reduces DSR to PSR.
    trial_sharpes : array-like, optional
        Per-period Sharpe estimates across the trials. If given, their variance
        estimates the cross-trial Sharpe variance used for the deflation
        benchmark (preferred). Annualized inputs are auto-detected and
        de-annualized when they are implausibly large for per-period values.
    sr_variance : float, optional
        Explicit per-period cross-trial Sharpe variance (overrides
        ``trial_sharpes``). When neither is provided, the analytic variance of a
        Sharpe estimate under the null, ``(1 + 0.5*SR^2)/(n-1)``, is used.
    """
    r = _clean(returns)
    n_obs = int(r.size)
    from common.validation.metrics import annualized_sharpe, per_period_sharpe

    sr_ann = annualized_sharpe(r, periods=periods)
    sr_pp = per_period_sharpe(r)
    sk = skewness(r)
    ku = kurtosis(r)

    # Cross-trial Sharpe standard deviation (per-period units).
    if sr_variance is not None:
        sr_std = math.sqrt(max(0.0, float(sr_variance)))
    elif trial_sharpes is not None and len(trial_sharpes) >= 2:
        ts = _clean(trial_sharpes)
        # Heuristic de-annualization: per-period daily Sharpe is ~O(0.1); if the
        # provided values look annualized (|.|>3 typical), scale them down.
        if ts.size and np.nanmedian(np.abs(ts)) > 3.0:
            ts = ts / math.sqrt(periods)
        sr_std = float(ts.std(ddof=1))
    else:
        # Analytic null variance of the Sharpe estimator (Lo, 2002).
        if n_obs > 1:
            sr_std = math.sqrt((1.0 + 0.5 * sr_pp ** 2) / (n_obs - 1))
        else:
            sr_std = 0.0

    sr0 = expected_max_sharpe(n_trials, sr_std, sr_mean=0.0)
    psr0 = probabilistic_sharpe_ratio(sr_pp, 0.0, n_obs, sk, ku)
    dsr = probabilistic_sharpe_ratio(sr_pp, sr0, n_obs, sk, ku)

    return DSRResult(
        observed_sharpe_annual=round(sr_ann, 6),
        observed_sharpe_per_period=round(sr_pp, 8),
        benchmark_sharpe_per_period=round(sr0, 8),
        n_obs=n_obs,
        n_trials=int(n_trials),
        skew=round(sk, 6),
        kurt=round(ku, 6),
        psr_vs_zero=round(psr0, 6),
        deflated_sharpe=round(dsr, 6),
        passed=bool(not math.isnan(dsr) and dsr > pass_threshold),
    )
