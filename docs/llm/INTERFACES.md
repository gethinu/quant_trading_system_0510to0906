# LLM Reference: Interfaces

Rule Contract Source
- System entry/exit/filter/setup/ranking rule text is defined in `docs/llm/SPEC.md`.
- `docs/llm/SPEC.md` is a direct copy of `docs/systems/*.txt`.
- If implementation conflicts with this, implementation is treated as bug.

CLI Entrypoints
| Command | Purpose | Notes |
| --- | --- | --- |
| `python scripts/run_all_systems_today.py` | Daily signal pipeline | Key flags: `--test-mode`, `--skip-external`, `--parallel`, `--save-csv`, `--benchmark` |
| `python scripts/daily_paper_trade.py` | Alpaca paper trading execution | Use `--dry-run` to validate without orders |
| `python scripts/cache_daily_data.py` | Cache refresh | Use `--bulk-today` for same-day bulk refresh |
| `python scripts/run_controlled_tests.py` | Deterministic controlled tests | Mirrors `tests/test_systems_controlled_all.py` |
| `python tools/precompute_shared_indicators.py` | Warm rolling cache with shared indicators | Delegates to `scripts/build_rolling_with_indicators.py` |
| `python tools/position_tracker_apply.py` | Apply manual entry/exit confirmations | Default: `data/entry_confirmations_YYYY-MM-DD.csv` + `data/exit_confirmations_YYYY-MM-DD.csv` |
| `python tools/build_rust_backtest_core.py` | Build Rust integrated-backtest core binary | Requires `rustc/cargo`; outputs under `rust/integrated_backtest_core/target/release/` |
| `python tools/run_single_system_backtests_sbi_2025_2026.py` | Per-system backtest runner (cost model included) | Writes JSON/CSV summaries under `results_csv/` |
| `python tools/run_integrated_backtest_sbi_2025_2026.py` | Integrated 1..7 backtest runner (cost model included) | Supports `--initial-capital-jpy`, `--systems`, `--daily-entry-cap`, `--min-hold-days`, `--engine`, `--output-tag`; writes JSON/CSV summaries under `results_csv/` |

UI Entrypoints
- `streamlit run apps/app_integrated.py`
- `Start-Dashboard.ps1`
- `python -m uvicorn apps.api.main:app --reload --port 8000`
- `npm run dev -- --port 3000` in `apps/dashboards/alpaca-next`

API Backend
- FastAPI app lives in `apps/api/main.py`.
- Use `/docs` for OpenAPI.

Python Contracts
- `core/systemX.py`:
  - exposes `prepare_data_vectorized_systemX(...)` and `generate_candidates_systemX(...)`
  - owns filter/setup/ranking logic implementation
- `strategies/systemX_strategy.py`:
  - wraps core methods via `prepare_data(...)` and `generate_candidates(...)`
  - owns executable trade semantics via:
    - `compute_entry(df, candidate, current_capital) -> tuple[entry_price, stop_price] | None`
    - `compute_exit(df, entry_idx, entry_price, stop_price) -> tuple[exit_price, exit_date]`
- `core/final_allocation.py::finalize_allocation()`:
  - stable allocation contract; breaking changes require coordinated caller updates

Candidate Payload Contract
- Required fields:
  - `symbol`
  - `date` (signal date)
  - `entry_date` (next NYSE trading day)
- Recommended fields:
  - ranking metric (`roc200`, `adx7`, `drop3d`, `rsi4`, `return_6d`, etc.)
  - indicator context for sizing/stops (`atr10`, `atr20`, `atr40`, etc.)

Diagnostics Contract
- Required keys for every system:
  - `ranking_source`
  - `setup_predicate_count`
  - `ranked_top_n_count`
- Optional keys are allowed but must be documented when added.
- System1/System4 candidate-generation diagnostics may include SPY gate fields:
  - `spy_gate_condition`
  - `spy_gate_total_candidates_before`
  - `spy_gate_total_candidates_after`
  - `spy_gate_dropped`
- For System1/System4, `ranked_top_n_count` in strategy-layer diagnostics reflects post-SPY-gate candidate count.

Config And Env
- Precedence: JSON > YAML > `.env` (see `config/settings.py`).
- Access config via `get_settings()` and `get_env_config()`, not `os.environ.get()`.
- Environment variable catalog: `docs/technical/environment_variables.md`.
- Integrated backtest engine selection:
  - CLI: `--engine python|rust|auto`
  - Env fallback: `INTEGRATED_BACKTEST_ENGINE=python|rust|auto`
  - Rust binary explicit path: `INTEGRATED_BACKTEST_RUST_BIN`
  - Rust payload contract keeps invalid candidates with `is_valid=false` so slot consumption matches Python (`cands[:slots]` parity).

Outputs (Interface Level)
- Signals:
  - `settings.outputs.signals_dir/signals_systemX_YYYY-MM-DD.csv`
  - `settings.outputs.signals_dir/signals_final_YYYY-MM-DD.csv`
  - `settings.outputs.signals_dir/signals_exit_YYYY-MM-DD.csv`
  - `settings.outputs.signals_dir/signals_exit_plan_YYYY-MM-DD.csv`
- Allocation:
  - `results_csv/final_allocation_YYYYMMDD_HHMMSS.csv`
- Metrics and validation:
  - `results_csv/daily_metrics.csv`
  - `results_csv/daily_metrics_report.csv`
  - `results_csv/validation/validation_report_YYYY-MM-DD.json`
- Backtest summaries:
  - `results_csv/single_system_backtests_sbi_YYYYMMDD_YYYYMMDD.json`
  - `results_csv/single_system_backtests_sbi_YYYYMMDD_YYYYMMDD_summary.csv`
  - `results_csv/integrated_backtest_sbi_YYYYMMDD_YYYYMMDD.json`
  - `results_csv/integrated_backtest_sbi_YYYYMMDD_YYYYMMDD_monthly.csv`
- Progress log:
  - `logs/progress_today.jsonl`

Notes
- `signals_systemX_YYYY-MM-DD.csv` is written even when a system has 0 signals.
- Position tracker auto-update can be disabled via `POSITION_TRACKER_AUTO_UPDATE=0`.
- If confirmation CSVs exist, tracker updates are auto-applied before notifications.
- System7 interface must stay SPY-only.

Update Triggers
- New command, flag, endpoint, or output file.
- Any change to function signature contract.
- Any change to diagnostics schema or allocation contract.
- Any rule update copied into `docs/llm/SPEC.md`.
