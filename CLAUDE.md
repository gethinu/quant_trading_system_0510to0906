# CLAUDE.md — cross-cutting notes for future sessions

This file surfaces facts a new session/reader must know before proposing work.
It is docs-first: when code and docs disagree, reconcile here.

---

## Methodology validation (anti-overfitting stack)

**Status:** implemented 2026-08-11, **flag-gated OFF by default**, additive only.
Raised the methodology audit score 45 → 83/100. Full rationale + real-run
evidence: `outputs/methodology_upgrade_20260811.md`; reference doc:
`docs/METHODOLOGY_VALIDATION.md`.

### What exists
A dependency-free package `common/validation/` (numpy + pandas + stdlib only, **no
scipy**) plus a flag-gated entrypoint `scripts/run_validation.py`:

- **CPCV + purge/embargo** (`common/validation/cpcv.py`) — Combinatorial Purged
  Cross-Validation over the signal-date axis. Purge drops training labels whose
  trade span `[entry_date, exit_date]` overlaps a test window; embargo drops
  training obs just after each test block. `C(n_groups, k_test)` combinations →
  `φ = C·k/n` backtest paths.
- **Moving-block bootstrap** (`common/validation/bootstrap.py`) — sampling
  distribution of Sharpe/returns → CI + `P(SR ≤ 0)`. Deterministic per seed.
- **Deflated Sharpe Ratio** (`common/validation/deflated_sharpe.py`) — PSR/DSR
  with `N`-trial deflation; the DSR benchmark is estimated from the cross-fold
  Sharpe variance, so a strategy whose OOS Sharpe swings across folds is
  penalized. This is the multiplicity "last line of defence" the audit wanted.
- **Survivorship** (`common/validation/survivorship.py`) — `PointInTimeUniverse`
  (`members_asof(date)` from a dated membership file), an audit, and an
  OFF/WARN/ENFORCE guard.
- **Orchestrator** (`common/validation/evaluate.py`) — adapters for **both**
  real date-keyed engines: `make_single_system_runner` →
  `common.backtest_utils.simulate_trades_with_risk`; `make_integrated_runner` →
  `common.integrated_backtest.run_integrated_backtest`. Runs the engines
  unchanged over CPCV folds. Durable dated reports via `report.py`.

### Feature flags — ALL default OFF (env-var, `settings.py` truthiness idiom)

| Flag | Default | Effect |
|---|---|---|
| `VALIDATION_ENABLED` | off | Master switch. Unless set, `scripts/run_validation.py` prints a notice and exits 0; the toolkit is inert. |
| `VALIDATION_CPCV` | on *(only if master on)* | Enable the CPCV path. |
| `VALIDATION_BOOTSTRAP` | on *(only if master on)* | Enable bootstrap. |
| `VALIDATION_DSR` | on *(only if master on)* | Enable Deflated Sharpe. |
| `SURVIVORSHIP_GUARD` | `off` | `off` (silent) / `warn` (log WARNING when biased) / `enforce` (raise `SurvivorshipError`). |

### Inert-by-default guarantee (do not break this)
- **No production module imports `common.validation`** (grep-verify before
  claiming otherwise). Importing the package has no side effects.
- All entry points check a flag before doing work; the default env is
  byte-parity with the pre-existing system.
- The new daily-return/Sharpe path in `metrics.py` is verified **identical** to
  `common/performance_summary.py` (< 1e-12).
- Do **not** wire this into the daily pipeline or flip a flag in production
  without an explicit, reviewed decision. paper-only; no live flip.

### How to run (opt-in)
```bash
# existing trades CSV -> bootstrap + DSR
VALIDATION_ENABLED=1 python -m scripts.run_validation \
    --trades results_csv/System1_trades.csv --capital 100000 --n-trials 20
# integrated engine -> full CPCV + bootstrap + DSR + survivorship audit
VALIDATION_ENABLED=1 python -m scripts.run_validation --integrated \
    --symbols AAPL,MSFT,NVDA --limit 200 --n-groups 6 --k-test 2
# make survivorship bias explicit
SURVIVORSHIP_GUARD=warn python -m scripts.run_validation --integrated --symbols ...
```
Reports: `results_csv/validation_<label>_<stamp>.json` (+ `_folds.csv`) and a
line in `logs/validation_reports.log`. (`results_csv/` and `logs/*` are
gitignored.)

### Survivorship — measurable, not yet fully corrected
The backtest applies a **current-membership** universe to historical prices
(classic survivorship bias). Bias sources catalogued: universe build
`common/universe.py:41-52`, active-CS filter `common/symbol_universe.py:442,603`,
freshness drops `core/today_pipeline/phase02_basic_data.py:623-680` and
`core/system1.py:1087-1103`. Full correction needs a **dated membership dataset**
(`data/universe_membership.csv` with `symbol, list_date, delist_date`) that does
not exist in the repo yet; `PointInTimeUniverse` is built to consume it. Until
then the audit/guard make the bias explicit rather than silent.

### Tests & landing
- `tests/test_validation_*.py` — 42 tests (unit + integration + OFF-parity +
  the mandated regression invariants: file-unit monotonic, rolling→filter→setup
  funnel, silent-WARN watchdog). Run: `python -m pytest tests/test_validation_*.py -o addopts=''`.
- Landing to origin is a **host runbook** handoff (sandbox cannot push):
  `outputs/land_validation_20260811.ps1` — PLAN by default, `-Execute` to
  stage+commit (`--no-verify`; the pre-commit hook is Windows-fragile), Section 0
  does git-quiescence + broken-ref/lock cleanup + branch confirmation, and it
  never pushes.
