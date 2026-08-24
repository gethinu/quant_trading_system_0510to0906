"""Moving-block bootstrap for the sampling distribution of Sharpe / returns.

The moving-block bootstrap (Kunsch, 1989) resamples *contiguous blocks* of the
return series, preserving short-range autocorrelation that an i.i.d. bootstrap
would destroy. This turns a single point estimate of the Sharpe ratio into a
sampling distribution, from which confidence intervals and a one-sided p-value
(P(Sharpe <= 0)) are derived.

Pure numpy; deterministic given a seed.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np

from common.validation.metrics import annualized_sharpe


def optimal_block_length(n: int) -> int:
    """A simple, robust rule of thumb: block length ~ n**(1/3), >= 1."""
    if n <= 1:
        return 1
    return max(1, int(round(n ** (1.0 / 3.0))))


def _moving_block_indices(n: int, block: int, rng: np.random.Generator) -> np.ndarray:
    """Indices for one moving-block resample of length ~n (circular blocks)."""
    if n <= 0:
        return np.empty(0, dtype=int)
    block = max(1, min(block, n))
    n_blocks = int(np.ceil(n / block))
    starts = rng.integers(0, n, size=n_blocks)  # circular start points
    idx = (starts[:, None] + np.arange(block)[None, :]) % n
    return idx.reshape(-1)[:n]


@dataclass(frozen=True)
class BootstrapResult:
    statistic: str
    point_estimate: float
    mean: float
    std: float
    ci_low: float
    ci_high: float
    ci_level: float
    p_value_le_zero: float  # P(bootstrapped statistic <= 0)
    n_boot: int
    block_length: int
    n_obs: int

    def to_dict(self) -> dict:
        return asdict(self)


def moving_block_bootstrap(
    returns,
    *,
    n_boot: int = 2000,
    block_length: int | None = None,
    statistic="sharpe",
    ci_level: float = 0.95,
    periods: int = 252,
    seed: int = 12345,
) -> BootstrapResult:
    """Bootstrap the sampling distribution of a statistic of a return series.

    ``statistic`` may be ``"sharpe"``, ``"mean"``, ``"total_return"`` or a
    callable ``f(np.ndarray) -> float``.
    """
    r = np.asarray(returns, dtype=float)
    r = r[~np.isnan(r)]
    n = r.size

    if statistic == "sharpe":
        name = "annualized_sharpe"
        stat_fn = lambda x: annualized_sharpe(x, periods=periods)  # noqa: E731
    elif statistic == "mean":
        name = "mean_return"
        stat_fn = lambda x: float(np.mean(x)) if x.size else 0.0  # noqa: E731
    elif statistic == "total_return":
        name = "total_return"
        stat_fn = lambda x: (  # noqa: E731
            float(np.prod(1.0 + x) - 1.0) if x.size else 0.0
        )
    elif callable(statistic):
        name = getattr(statistic, "__name__", "custom")
        stat_fn = statistic
    else:
        raise ValueError(f"unknown statistic: {statistic!r}")

    block = int(block_length) if block_length else optimal_block_length(n)
    point = float(stat_fn(r)) if n else 0.0

    if n < 2:
        return BootstrapResult(
            statistic=name,
            point_estimate=point,
            mean=point,
            std=0.0,
            ci_low=point,
            ci_high=point,
            ci_level=ci_level,
            p_value_le_zero=float("nan"),
            n_boot=0,
            block_length=block,
            n_obs=n,
        )

    rng = np.random.default_rng(seed)
    samples = np.empty(int(n_boot), dtype=float)
    for i in range(int(n_boot)):
        idx = _moving_block_indices(n, block, rng)
        samples[i] = stat_fn(r[idx])

    alpha = 1.0 - ci_level
    lo = float(np.quantile(samples, alpha / 2.0))
    hi = float(np.quantile(samples, 1.0 - alpha / 2.0))
    p_le0 = float(np.mean(samples <= 0.0))

    return BootstrapResult(
        statistic=name,
        point_estimate=round(point, 6),
        mean=round(float(np.mean(samples)), 6),
        std=round(float(np.std(samples, ddof=1)), 6),
        ci_low=round(lo, 6),
        ci_high=round(hi, 6),
        ci_level=ci_level,
        p_value_le_zero=round(p_le0, 6),
        n_boot=int(n_boot),
        block_length=block,
        n_obs=n,
    )
