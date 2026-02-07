# LLM Reference: Interfaces

CLI Entrypoints
| Command | Purpose | Notes |
| --- | --- | --- |
| `python scripts/run_all_systems_today.py` | Daily signal pipeline | Key flags: `--test-mode`, `--skip-external`, `--parallel`, `--save-csv`, `--benchmark` |
| `python scripts/daily_paper_trade.py` | Alpaca paper trading execution | Use `--dry-run` to validate without orders |
| `python scripts/cache_daily_data.py` | Cache refresh | Use `--bulk-today` for same-day bulk refresh |
| `python scripts/run_controlled_tests.py` | Deterministic controlled tests | Mirrors `tests/test_systems_controlled_all.py` |

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
- Allocation: `results_csv/final_allocation_YYYYMMDD_HHMMSS.csv`
- Daily metrics: `results_csv/daily_metrics.csv`
- Progress log: `logs/progress_today.jsonl`

Update Triggers
- New command, flag, endpoint, or output file.
- Changes to diagnostics schema or allocation contract.
