universe=4655 symbols | capital=100,000 | CPCV n_groups=6 k_test=2 embargo=0.01 | n_boot=2000 seed=12345

### Fill realism (full-sample, single-system engine)

| system | limit entry | candidates | trades (pre-fix) | trades (fixed) | fill rate | win rate pre → fixed | mean trade return pre → fixed | cum. PnL % of capital pre → fixed |
|---|---|---:|---:|---:|---:|---|---|---|
| System1 | - | - | - | - | - | - | - | no candidates |
| System2 | no | 4,811 | 547 | 547 | 1.000 | 0.4022 → 0.4022 | -0.0495 → -0.0495 | -166.6% → -166.6% |
| System3 | yes | 3,604 | 1,293 | 999 | 0.773 | 0.4354 → 0.3223 | -0.0359 → -0.0692 | -135.2% → -133.3% |
| System4 | no | 3,158 | 83 | 83 | 1.000 | 0.1566 → 0.1566 | 0.0009 → 0.0009 | -1.3% → -1.3% |
| System5 | yes | 2,530 | 750 | 685 | 0.913 | 0.4747 → 0.4292 | -0.0351 → -0.0529 | -69.0% → -82.0% |
| System6 | yes | 165 | 112 | 54 | 0.482 | 0.6071 → 0.3148 | 0.0092 → -0.0213 | 10.1% → -10.9% |
| System7 | - | - | - | - | - | - | - | no candidates |

### Sharpe interpretability guard (equity crossing zero)

``common/validation/metrics.py`` builds equity as ``capital + cumsum(pnl)`` and takes ``pct_change()``. Once that series touches zero the returns flip sign and the Sharpe/DSR below stop being performance statements. Flagged here rather than silently reported.

| system | arm | min equity | final equity | equity crossed 0 | Sharpe interpretable |
|---|---|---:|---:|---|---|
| System2 | pre-fix | -66628 | -66628 | **yes** | **NO** |
| System2 | fixed | -66628 | -66628 | **yes** | **NO** |
| System3 | pre-fix | -37690 | -35219 | **yes** | **NO** |
| System3 | fixed | -34089 | -33266 | **yes** | **NO** |
| System4 | pre-fix | 74679 | 98660 | no | yes |
| System4 | fixed | 74679 | 98660 | no | yes |
| System5 | pre-fix | 30600 | 30978 | no | yes |
| System5 | fixed | 18002 | 18021 | no | yes |
| System6 | pre-fix | 97322 | 110057 | no | yes |
| System6 | fixed | 89072 | 89072 | no | yes |
| Integrated (7) | pre-fix | 71223 | 72727 | no | yes |
| Integrated (7) | fixed | 59133 | 61182 | no | yes |

### CPCV / bootstrap / Deflated Sharpe — before (pre-fix) vs after (fixed)

| system | arm | full-sample Sharpe | fold Sharpe mean ± std | fold min / max | folds > 0 | bootstrap 95% CI | P(SR≤0) | DSR (N) | verdict |
|---|---|---:|---|---|---:|---|---:|---:|---|
| System1 | - | - | - | - | - | - | - | - | no candidates |
| System2 | pre-fix | 0.465 | -2.713 ± 0.514 | -3.787 / -2.062 | 0.00 | [-0.686, 1.031] | 0.143 | 0.134 (N=15) | FAIL |
| System2 | **fixed** | 0.465 | -2.713 ± 0.514 | -3.787 / -2.062 | 0.00 | [-0.686, 1.031] | 0.143 | 0.134 (N=15) | FAIL |
| System3 | pre-fix | 0.619 | -2.491 ± 0.974 | -4.068 / -0.968 | 0.00 | [-1.186, 1.843] | 0.216 | 0.034 (N=15) | FAIL |
| System3 | **fixed** | 0.007 | -3.849 ± 1.481 | -5.958 / -1.392 | 0.00 | [-2.314, 1.232] | 0.527 | 0.000 (N=15) | FAIL |
| System4 | pre-fix | 0.077 | 0.070 ± 0.633 | -1.510 / 0.950 | 0.60 | [-4.409, 0.961] | 0.475 | 0.064 (N=15) | FAIL |
| System4 | **fixed** | 0.077 | 0.070 ± 0.633 | -1.510 / 0.950 | 0.60 | [-4.409, 0.961] | 0.475 | 0.064 (N=15) | FAIL |
| System5 | pre-fix | -2.067 | -2.002 ± 1.208 | -4.198 / 0.422 | 0.07 | [-3.603, -0.776] | 1.000 | 0.000 (N=15) | FAIL |
| System5 | **fixed** | -3.587 | -3.017 ± 1.437 | -6.383 / -0.927 | 0.00 | [-4.738, -2.351] | 1.000 | 0.000 (N=15) | FAIL |
| System6 | pre-fix | 1.072 | 0.702 ± 0.928 | -1.125 / 2.149 | 0.80 | [-0.174, 2.135] | 0.046 | 0.144 (N=15) | FAIL |
| System6 | **fixed** | -2.051 | -1.718 ± 0.628 | -3.289 / -0.897 | 0.00 | [-2.790, -1.197] | 1.000 | 0.000 (N=15) | FAIL |
| System7 | - | - | - | - | - | - | - | - | no candidates |
| **Integrated (7)** | pre-fix | -1.940 | -1.625 ± 0.716 | -2.797 / -0.370 | 0.00 | [-3.125, -0.773] | 1.000 | 0.000 (N=15) | FAIL |
| **Integrated (7)** | **fixed** | -2.977 | -2.311 ± 0.860 | -4.106 / -0.523 | 0.00 | [-4.241, -1.711] | 1.000 | 0.000 (N=15) | FAIL |
