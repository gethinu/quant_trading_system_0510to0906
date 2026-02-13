# LLM Reference: Architecture

Overview
The system is a layered Python application with UI frontends, an API backend, and a daily signal pipeline.
Trading-rule text is maintained in `docs/systems/*.txt` and copied verbatim into `docs/llm/SPEC.md`.

Rule Authority Chain
1. `docs/systems/*.txt` (rule master text)
2. `docs/llm/SPEC.md` (verbatim copy for LLM/workflow usage)
3. `core/systemX.py` and `strategies/systemX_strategy.py` (implementation)
4. `docs/llm/INTERFACES.md` and `docs/llm/STATE.md` (operational contracts)

Diagram
[Streamlit UI] -> [scripts/run_all_systems_today.py] -> [strategies/systemX_strategy.py] -> [core/systemX.py] -> [core/final_allocation.py] -> [results_csv/]
[Next.js UI] -> [FastAPI apps/api/main.py] -> [scripts/run_all_systems_today.py]
[scripts/*] -> [common/cache_manager.py] -> [data_cache/]
[scripts/*] -> [common/exit_signals.py + common/position_tracker.py] -> [signals_exit*.csv]
[scripts/*] -> [notifications] -> Slack/Discord

Layers And Responsibilities
- Presentation:
  - `apps/app_integrated.py`
  - `apps/dashboards/alpaca-next/`
- API:
  - `apps/api/main.py`
- Orchestration:
  - `scripts/run_all_systems_today.py`
  - `scripts/daily_paper_trade.py`
  - schedulers under `schedulers/`
- Strategy wrappers (execution semantics):
  - `strategies/systemX_strategy.py`
  - own `compute_entry` and `compute_exit`
- Core domain (candidate semantics):
  - `core/systemX.py`
  - own filter/setup/rank/candidate generation
- Allocation:
  - `core/final_allocation.py`
- Common services:
  - cache, indicators, diagnostics, performance, notification, tracker in `common/`
- Config:
  - `config/settings.py`, `config/environment.py`

Daily Signal Pipeline
1. Build symbol universe.
2. Load market data via CacheManager (rolling -> base -> full_backup).
3. Ensure indicators required by each system rule.
4. Run filter/setup/ranking and generate candidates.
5. Convert candidates to executable entries/exits.
6. Merge and allocate signals.
7. Build exit signals from tracker and market data.
8. Save outputs and send notifications.

Backtest Pipeline
1. Prepare per-system data and candidates.
2. Use each strategy's `compute_entry` and `compute_exit`.
3. Enforce allocation and capital constraints.
   - Default engine: Python (`common/integrated_backtest.py`)
   - Optional engine: Rust core via bridge (`common/integrated_backtest_rust_bridge.py` -> `rust/integrated_backtest_core`)
4. Apply cost model (slippage, commission, borrow/interest) when enabled.
5. Persist per-system and integrated summaries.

Cross-Cutting Rules
- Cache I/O only via CacheManager.
- System7 must remain SPY-only.
- Config access via settings/environment helpers only.
- Entry-date semantics are next NYSE trading day after signal date.

Update Triggers
- Any rule edit under `docs/systems/`.
- Any sync/copy update to `docs/llm/SPEC.md`.
- Any implementation change in `core/systemX.py` or `strategies/systemX_strategy.py`.
- New pipeline stage, integration, or service boundary.
