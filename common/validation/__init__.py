"""Anti-overfitting / methodology validation toolkit.

This package adds López de Prado-style backtest-methodology guardrails to the
quant trading system:

* :mod:`common.validation.cpcv` - Combinatorial Purged Cross-Validation with
  purge + embargo on trade label spans.
* :mod:`common.validation.bootstrap` - moving-block bootstrap for the
  sampling distribution of Sharpe / return statistics.
* :mod:`common.validation.deflated_sharpe` - Probabilistic and Deflated Sharpe
  Ratio (PSR / DSR) with multiplicity (number of trials ``N``) correction.
* :mod:`common.validation.survivorship` - point-in-time universe interface and
  a survivorship-bias audit / guard.
* :mod:`common.validation.evaluate` - orchestrates the above over the existing
  backtest engines and produces a durable :class:`ValidationReport`.

Design rules (see docs/methodology_upgrade):

1. **Everything here is opt-in.** Nothing in this package is imported or invoked
   by the production daily pipeline unless a caller explicitly does so. The
   feature flags in :mod:`common.validation.flags` all default to *disabled*,
   so importing this package has no side effects and does not change any
   existing behavior (OFF byte-parity is structural: no production module's
   logic is modified).
2. **Dependency-free.** Only numpy / pandas / the standard library are used, so
   the toolkit runs anywhere the base system runs (scipy is intentionally not a
   dependency; the required statistical functions are implemented locally).
"""

from __future__ import annotations

from common.validation.flags import (
    validation_enabled,
    cpcv_enabled,
    bootstrap_enabled,
    dsr_enabled,
    survivorship_guard_mode,
)

__all__ = [
    "validation_enabled",
    "cpcv_enabled",
    "bootstrap_enabled",
    "dsr_enabled",
    "survivorship_guard_mode",
]
