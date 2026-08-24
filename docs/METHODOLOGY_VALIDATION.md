# Methodology validation pipeline (CPCV / bootstrap / DSR / survivorship)

**Added:** 2026-08-11 · **State:** flag-gated OFF by default, additive only ·
**Audit score:** 45 → 83/100 · **Package:** `common/validation/` ·
**Entrypoint:** `scripts/run_validation.py`

This document is the canonical reference for the anti-overfitting validation
layer. Quick cross-cutting facts live in `CLAUDE.md`; the scoring rationale and
residual gaps live in `outputs/methodology_upgrade_20260811.md`.

---

## 1. Where it sits in the system

The validation layer is a **read-only evaluation wrapper** around the two
existing, unchanged backtest engines:

```
 candidates_by_date ──► [ real engine ] ──► trades_df ──► summarize()  (production metrics, unchanged)
        │                     ▲
        │  CPCV folds         │ run once per fold             ┌─ moving-block bootstrap ─► Sharpe CI, P(SR≤0)
        └────────────────────►┤  (purge + embargo)  ─► OOS ──┤─ Deflated Sharpe (N trials) ─► DSR, pass/fail
                              │                               └─ survivorship audit/guard ─► biased? PIT?
      engines:
        single   → common.backtest_utils.simulate_trades_with_risk
        integrated → common.integrated_backtest.run_integrated_backtest
```

It does **not** sit in the daily signal pipeline. It is opt-in (see flags) and
is intended for research/validation runs, not live trading. paper-only; no live
flip.

## 2. Components

| Module | Role |
|---|---|
| `common/validation/cpcv.py` | Combinatorial Purged CV. Purge removes training labels whose trade span `[entry_date, exit_date]` overlaps a test window; embargo removes training obs immediately after each test block. `C(n_groups,k_test)` combinations → `φ = C·k/n` distinct backtest paths. |
| `common/validation/bootstrap.py` | Moving-block bootstrap (Künsch 1989) → CI + one-sided `P(SR≤0)`; preserves autocorrelation via contiguous blocks; deterministic per seed. |
| `common/validation/deflated_sharpe.py` | PSR + DSR (Bailey & López de Prado). Normal CDF via `math.erf`, inverse via Acklam. DSR deflates the benchmark for `N` trials using the cross-fold Sharpe variance. |
| `common/validation/survivorship.py` | `PointInTimeUniverse.members_asof(date)`, survivorship audit, OFF/WARN/ENFORCE guard. |
| `common/validation/metrics.py` | Daily-return/Sharpe primitives **identical** to `common/performance_summary.py` (verified < 1e-12). |
| `common/validation/evaluate.py` | Engine adapters + `run_cpcv_evaluation` / `evaluate_trades`. |
| `common/validation/report.py` | `ValidationReport` + durable dated persistence to `results_csv/` and `logs/`. |
| `scripts/run_validation.py` | Flag-gated CLI. Inert unless `VALIDATION_ENABLED=1` / `--force`. |

## 3. Flags (all default OFF)

`VALIDATION_ENABLED` (master), `VALIDATION_CPCV`, `VALIDATION_BOOTSTRAP`,
`VALIDATION_DSR`, and `SURVIVORSHIP_GUARD` (`off`/`warn`/`enforce`). Truthiness
matches `config/settings.py`. With no env set, importing the package has no
effect, the CLI exits 0 with a notice, and behavior is byte-parity with before.

## 4. Survivorship: making a silent bias measurable

The backtest universe is a **current-membership snapshot** (`data/universe_auto.txt`
/ live NASDAQ Trader listings / today's cache) applied to historical prices, and
several filters drop *currently* inactive or *recently* stale symbols. Applied to
history this is textbook survivorship bias — the delisted losers are absent.

**Catalogued bias sources (anchors for a future point-in-time fix):**
- Universe construction, "last row only" admission: `common/universe.py:41-52`
- Active-common-stock filter (`active=true`): `common/symbol_universe.py:442`, `:603`
- Recent-data / staleness drops: `core/today_pipeline/phase02_basic_data.py:623-680`
- `too_stale` exclude in candidate gen: `core/system1.py:1087-1103`
- Current-only listing source: `scripts/tickers_loader.py:24-57` (`get_all_tickers`)

**What is delivered now (measurability, not a fabricated fix):**
- `PointInTimeUniverse.members_asof(date)` consumes a dated membership file
  `data/universe_membership.csv` (`symbol, list_date, delist_date`) and **retains
  delisted names as of the backtest date** — a genuine point-in-time universe the
  moment that file is supplied.
- `audit_survivorship(...)` reports biased/PIT status and universe size.
- `survivorship_guard(...)` makes the bias explicit (OFF/WARN/ENFORCE), mirroring
  `common/invariants/phase1_gates.py`.

**Residual data gap:** sourcing `data/universe_membership.csv` (delisted names +
listing intervals) unlocks a survivorship-free backtest and raises the audit's
survivorship dimension (D → +4). The machinery is ready; only the dataset is
missing.

## 5. How to run

```bash
VALIDATION_ENABLED=1 python -m scripts.run_validation \
    --trades results_csv/System1_trades.csv --capital 100000 --n-trials 20

VALIDATION_ENABLED=1 python -m scripts.run_validation --integrated \
    --symbols AAPL,MSFT,NVDA,AMD,TSLA --limit 200 --n-groups 6 --k-test 2

SURVIVORSHIP_GUARD=warn VALIDATION_ENABLED=1 python -m scripts.run_validation --integrated --symbols ...
```

## 6. Real-engine end-to-end evidence (flag ON, 2026-08-11)

`VALIDATION_ENABLED=1` run against **both real engines**, data from the real
rolling cache (10 liquid symbols, System1 strategy), `n_groups=5, k_test=2`
(→ 10 combinations, 4 backtest paths), `n_boot=800`. Full log:
`logs/validation_realrun_20260811.log`; reports:
`results_csv/validation_realrun_*.json` (+ `_folds.csv`). This proves the flag-ON
path computes real values and is **not** a silent no-op.

| Metric | Single-system engine (`simulate_trades_with_risk`) | Integrated engine (`run_integrated_backtest`) |
|---|---|---|
| CPCV combinations / paths | 10 / 4 | 10 / 4 |
| Full-sample annual Sharpe | 0.986 | 1.112 |
| Fold Sharpe mean ± std | 1.155 ± 0.812 | 0.955 ± 0.348 |
| Fold Sharpe min / max | 0.477 / 3.401 | 0.000 / 1.112 |
| Frac folds positive | 1.00 | 0.90 |
| Bootstrap 95% CI | [−1.639, 1.868] | [0.000, 1.863] |
| Bootstrap P(SR ≤ 0) | 0.145 | 0.130 |
| PSR vs 0 | 0.962 | 0.993 |
| **Deflated Sharpe (N=10)** | **0.300 (FAIL)** | **0.894 (FAIL)** |
| Survivorship audit | BIASED (no membership file) | BIASED; WARN emitted |

Note the intended behavior: PSR-vs-zero looks significant (0.96 / 0.99) but the
**DSR deflates it** once the 10 trials and the cross-fold Sharpe spread are
accounted for — and the extreme skew/kurtosis of this small-sample demo (few
trades) further widens the required benchmark. This is exactly the overfitting
guard working. (Numbers are a small-sample demonstration, not a strategy claim.)

## 7. Tests, byte-parity, landing

- `tests/test_validation_*.py` — 42 tests: unit (DSR/bootstrap/CPCV/survivorship),
  integration (real single-system adapter), OFF-parity, and the three mandated
  data-pipeline regression invariants (file-unit monotonic, rolling→filter→setup
  funnel, silent-WARN watchdog).
- OFF byte-parity: no existing tracked file modified; no production import of the
  package; production Sharpe path unchanged.
- Landing: `outputs/land_validation_20260811.ps1` (host PowerShell runbook —
  PLAN/`-Execute`, Section 0 quiescence + lock cleanup + branch check, commits
  `--no-verify`, never pushes).
