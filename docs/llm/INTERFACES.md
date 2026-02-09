# LLM Reference: Interfaces

CLI Entrypoints
| Command | Purpose | Notes |
| --- | --- | --- |
| `python scripts/run_all_systems_today.py` | Daily signal pipeline | Key flags: `--test-mode`, `--skip-external`, `--parallel`, `--save-csv`, `--benchmark` |
| `python scripts/daily_paper_trade.py` | Alpaca paper trading execution | Use `--dry-run` to validate without orders |
| `python scripts/cache_daily_data.py` | Cache refresh | Use `--bulk-today` for same-day bulk refresh |
| `python scripts/run_controlled_tests.py` | Deterministic controlled tests | Mirrors `tests/test_systems_controlled_all.py` |
| `python tools/precompute_shared_indicators.py` | Warm rolling cache with shared indicators | Delegates to `scripts/build_rolling_with_indicators.py` |
| `python tools/position_tracker_apply.py` | Apply manual entry/exit confirmations | Default: data/entry_confirmations_YYYY-MM-DD.csv + data/exit_confirmations_YYYY-MM-DD.csv |

UI Entrypoints
- `streamlit run apps/app_integrated.py`
- `Start-Dashboard.ps1` (runs FastAPI + Next.js)
- `python -m uvicorn apps.api.main:app --reload --port 8000`
- `npm run dev -- --port 3000` in `apps/dashboards/alpaca-next`

API Backend
- FastAPI app lives in `apps/api/main.py`. Run it with uvicorn and use `/docs` for the OpenAPI UI.

Python Contracts
- `core/systemX.py` exposes `generate_candidates_systemX` and data preparation helpers. Return shape is typically `candidates_by_date` plus optional merged DataFrame and diagnostics.
- `strategies/systemX_strategy.py` wraps core functions with `prepare_data` and `generate_candidates` for UI and pipeline usage.
- `core/final_allocation.py::finalize_allocation()` is a stable contract and must not change without updating callers.

Diagnostics Contract
- Required keys for every system: `ranking_source`, `setup_predicate_count`, `ranked_top_n_count`.
- Optional keys are allowed but must be documented when added.

Config And Env
- Precedence: JSON > YAML > .env (see `config/settings.py`).
- Access config via `get_settings()` and `get_env_config()`, not `os.environ.get()`.
- Environment variable catalog: `docs/technical/environment_variables.md`.

Outputs (Interface Level)
- Signals: `settings.outputs.signals_dir/signals_systemX_YYYY-MM-DD.csv`
- Signals (merged): `settings.outputs.signals_dir/signals_final_YYYY-MM-DD.csv`
- Signals (exits): `settings.outputs.signals_dir/signals_exit_YYYY-MM-DD.csv`
- Signals (exit plan): `settings.outputs.signals_dir/signals_exit_plan_YYYY-MM-DD.csv`
- Allocation: `results_csv/final_allocation_YYYYMMDD_HHMMSS.csv`
- Daily metrics: `results_csv/daily_metrics.csv`
- Daily metrics report: `results_csv/daily_metrics_report.csv`
- Validation report: `results_csv/validation/validation_report_YYYY-MM-DD.json`
- Progress log: `logs/progress_today.jsonl`

Notes
- `signals_systemX_YYYY-MM-DD.csv` is written even when a system has 0 signals (empty CSV).
- Signal notifications include the configured `stop_price_floor` for manual trade guidance.
- Signal notifications include a daily summary (total/long/short counts, per-system breakdown, and a shortlist based on `risk.max_positions` and `ui.default_long_ratio`), rendered as code-block tables.
- Signal notifications include a "本日のやること" block (1 line + 3 bullet lines) with entry/exit/hold counts and symbol lists. Holds include unrealized P&L computed from cached latest closes.
- Discord notifications can be routed by role via `DISCORD_WEBHOOK_URL_SUMMARY` / `DISCORD_WEBHOOK_URL_BACKTEST` / `DISCORD_WEBHOOK_URL_SYSTEM1..7` (or legacy `DISCORD_WEBHOOK_URL_SIGNALS` / `DISCORD_WEBHOOK_URL_EQUITY` / `DISCORD_WEBHOOK_URL_LOGS`), falling back to `DISCORD_WEBHOOK_URL` when unset.
- Position tracker auto-update can be disabled via `POSITION_TRACKER_AUTO_UPDATE=0` (useful for manual trading).
- When confirmation CSVs exist, the position tracker auto-applies them before notifications (`data/entry_confirmations_YYYY-MM-DD.csv` and `data/exit_confirmations_YYYY-MM-DD.csv`).

Update Triggers
- New command, flag, endpoint, or output file.
- Changes to diagnostics schema or allocation contract.
