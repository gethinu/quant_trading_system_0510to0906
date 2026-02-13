# LLM Reference: State

State Locations
| Path | Owner | Purpose | Lifecycle |
| --- | --- | --- | --- |
| `docs/systems/` | Product Rules | Rule master documents per system | Versioned |
| `docs/llm/SPEC.md` | LLM Reference | Verbatim copy of `docs/systems/*.txt` for implementation/review | Versioned |
| `docs/llm/ARCHITECTURE.md` | LLM Reference | Runtime layer and ownership map | Versioned |
| `docs/llm/INTERFACES.md` | LLM Reference | CLI/API/function/output contracts | Versioned |
| `docs/llm/STATE.md` | LLM Reference | Persistent/ephemeral state inventory | Versioned |
| `rust/integrated_backtest_core/` | Rust Core | Integrated backtest allocation engine source | Versioned |
| `data/symbol_system_map.json` | Allocation | Symbol-to-system mapping and integration constraints | Versioned |
| `data_cache/full_backup/` | CacheManager | Raw long-term market data | Persistent source of truth |
| `data_cache/base/` | CacheManager | Indicator-enriched long-term data | Persistent, rebuilt from full_backup if needed |
| `data_cache/rolling/` | CacheManager | Recent N trading days for daily signals | Persistent, refreshed regularly |
| `settings.outputs.signals_dir/` | Pipeline Scripts | Daily signals (`system/final/exit/exit_plan`) | Append or per-run output |
| `results_csv/` | Pipeline Scripts | Allocation, metrics, reports, backtest outputs | Append or per-run output |
| `results_csv/daily_metrics.csv` | Pipeline Scripts | Daily per-system metrics (prefilter/setup/candidates/entries) | Append per run |
| `results_csv/daily_metrics_report.csv` | Pipeline Scripts | Daily metrics report with counts, deltas, totals | Per-run output |
| `results_csv/validation/validation_report_YYYY-MM-DD.json` | Pipeline Scripts | TRDlist validation report (summary + diagnostics) | Per-run output |
| `results_csv/single_system_backtests_sbi_YYYYMMDD_YYYYMMDD.json` | Backtest Tools | Per-system backtest summary with SBI-style cost assumptions | Per-run output |
| `results_csv/single_system_backtests_sbi_YYYYMMDD_YYYYMMDD_summary.csv` | Backtest Tools | Per-system aggregate metrics table | Per-run output |
| `results_csv/integrated_backtest_sbi_YYYYMMDD_YYYYMMDD.json` | Backtest Tools | Integrated 1-7 backtest summary with SBI-style costs | Per-run output |
| `results_csv/integrated_backtest_sbi_YYYYMMDD_YYYYMMDD_monthly.csv` | Backtest Tools | Monthly net P&L with breakeven flags | Per-run output |
| `results_csv/integrated_backtest_sbi_YYYYMMDD_YYYYMMDD_<TAG>.json` | Backtest Tools | Integrated summary variant when `--output-tag` is used | Per-run output |
| `results_csv/integrated_backtest_sbi_YYYYMMDD_YYYYMMDD_<TAG>_monthly.csv` | Backtest Tools | Monthly net P&L variant when `--output-tag` is used | Per-run output |
| `results_csv_test/` | Tests | Test-only outputs and diagnostics snapshots | Ephemeral, safe to clean |
| `logs/` | Pipeline/Backtest Scripts | Run logs and progress logs | Append per run |
| `logs/progress_today.jsonl` | Pipeline Scripts | Structured progress events | Append per run |
| `logs/single_system_backtests_YYYYMMDD_YYYYMMDD.log` | Backtest Tools | Single-system batch backtest log | Per-run output |
| `logs/integrated_backtest_YYYYMMDD_YYYYMMDD.log` | Backtest Tools | Integrated batch backtest log | Per-run output |
| `rust/integrated_backtest_core/target/` | Rust Toolchain | Cargo build artifacts and binary outputs | Ephemeral, rebuildable |
| `locks/` | RunLock | Run serialization locks | Ephemeral while running |
| `data/position_tracker.json` | Position Tracker | Open pseudo-position state used for exit signal generation | Persistent |
| `data/entry_confirmations_YYYY-MM-DD.csv` | Manual Ops | Manual entry confirmations for tracker apply | Manual input, consumed on apply |
| `data/exit_confirmations_YYYY-MM-DD.csv` | Manual Ops | Manual exit confirmations for tracker apply | Manual input, consumed on apply |
| `config/config.yaml` | Config | Primary YAML config | Versioned |
| `.env` | Config | Secrets and local overrides | Local, not versioned |
| `data/` | Data | Symbol lists and reference data | Versioned |

Position Tracker Field Contract
- Required per position:
  - `system`
  - `entry_date`
  - `entry_price`
- Optional per system-rule execution:
  - `side`
  - `qty`
  - `stop_price`
  - `profit_target_price`
  - `trailing_stop_pct`
  - `use_trailing_stop`
  - `max_holding_days`
  - `max_exit_date`
  - `atr10`, `atr20`, `atr40`
  - `last_update`

Runtime Ephemeral State (In-Memory)
- Per system:
  - prepared symbol DataFrames (indicator-enriched)
  - `filter`/`setup` boolean masks
  - candidates grouped by `entry_date`
- Integrated backtest:
  - `active_positions`
  - per-system used capital
  - long/short bucket used capital
  - SPY regime gate map by date (System1: `Close > SMA100`, System4: `Close > SMA200`)
  - realized P&L applied on exit dates

State Rules
- Never read/write under `data_cache/` directly; use CacheManager.
- Daily pipeline appends to `results_csv/daily_metrics.csv`.
- Progress events are emitted to `logs/progress_today.jsonl` when enabled.
- Locking uses `locks/<name>.lock` directories via `common/run_lock.py`.
- Per-system signal CSVs are written even when a system has 0 rows.
- Backtest candidate generation must apply the same SPY gate semantics as daily signals for System1 and System4.
- System7 state must remain SPY-only for both candidates and tracker updates.

State Sources Of Truth
- Rule master text: `docs/systems/`.
- LLM rule copy: `docs/llm/SPEC.md`.
- Operational contracts: `docs/llm/ARCHITECTURE.md`, `docs/llm/INTERFACES.md`, `docs/llm/STATE.md`.
- Market data: `data_cache/full_backup/`.
- Indicator-enriched data: `data_cache/base/`.
- Daily signal inputs: `data_cache/rolling/`.
- Persistent execution outputs: `results_csv/`, `logs/`, `data/position_tracker.json`.

Update Triggers
- Any update to `docs/systems/*.txt` requires copying into `docs/llm/SPEC.md`.
- New long-lived state file/directory.
- Changes to output naming or log/event formats.
- Changes to tracker field schema or lifecycle semantics.
