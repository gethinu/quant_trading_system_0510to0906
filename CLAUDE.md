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

---

## Backtest fidelity — System3/5/6 の指値は「必ず約定」ではない

**Status:** fixed 2026-08-20。フラグ無しの無条件修正（バックテストのみ。ライブ発注は無変更）。
詳細・再測定手順・影響棚卸し: `docs/BACKTEST_LIMIT_FILL_FIX_20260820.md`。

System3 (`prev_close×0.93`) / System5 (`×0.97`) / System6 (`×1.05`) は前日終値から
離した**指値**で仕掛ける。2026-08-20 以前の `compute_entry` は指値を計算するだけで
**当日バーが到達したかを確認していなかった**ため、実際には約定しなかった候補まで
建玉として計上し、勝率を押し上げていた。

| system | 約定率 (実測) | 勝率 修正前 → 後 | 平均リターン 前 → 後 |
|---|---|---|---|
| System3 | 32.0% | 0.757 → **0.491** | +0.076 → **−0.005** |
| System5 | 52.5% | 0.636 → **0.468** | +0.030 → **−0.015** |
| System6 | 40.3% | 0.734 → **0.559** | +0.046 → **−0.002** |

約定判定は `StrategyBase._limit_entry_filled()`（exit 側の stop/target 到達判定と同一規約:
long は `Low <= limit`、short は `High >= limit`、約定値は指値、NaN は fail-closed）。

**読む前に知っておくこと**: 2026-08-20 より前に出力された **System3/5/6 のバックテスト
実績（およびそれらを含む統合バックテスト・`common/validation/` の CPCV/DSR 出力）は
すべて過大**。継続可否の判断は再測定後の数字で行うこと。
