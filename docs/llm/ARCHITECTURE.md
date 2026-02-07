# LLM Reference: Architecture

Overview
The system is a layered Python application with UI frontends, an API backend, and a daily signal pipeline. Core trading logic lives in `core/` and is wrapped by `strategies/`.

Diagram
[Streamlit UI] --> [scripts/run_all_systems_today.py] --> [strategies/*] --> [core/systemX.py] --> [core/final_allocation.py] --> [results_csv/]
[Next.js UI] --> [FastAPI apps/api/main.py] --> [scripts/run_all_systems_today.py]
[scripts/*] --> [common/*] --> [CacheManager] --> [data_cache/]
[scripts/*] --> [notifications] --> Slack/Discord

Layers And Responsibilities
- Presentation: `apps/app_integrated.py`, `apps/dashboards/alpaca-next/`.
- API: `apps/api/main.py` (FastAPI, used by Next.js dashboard).
- Orchestration: `scripts/run_all_systems_today.py`, `scripts/daily_paper_trade.py`, schedulers in `schedulers/`.
- Strategy wrappers: `strategies/systemX_strategy.py` integrate UI, settings, and optional broker actions.
- Core domain logic: `core/systemX.py`, `core/final_allocation.py`.
- Common services: cache, indicators, diagnostics, performance, alerts in `common/`.
- Config: `config/settings.py`, `config/environment.py`.

Pipeline (Daily Signals)
1. Build symbol universe.
2. Load data via CacheManager (rolling -> base -> full_backup).
3. Add or reuse indicators.
4. Apply per-system filters, setup predicates, and ranking.
5. Merge signals and allocate positions.
6. Save CSV or JSON outputs and emit notifications.

Concurrency
- Parallel execution is optional and controlled by environment flags and `--parallel`.

External Integrations
- Market data: EOD Historical Data (EODHD).
- Broker: Alpaca (paper trading by default).
- Notifications: Slack and Discord.

Cross-Cutting Rules
- Cache I/O only via CacheManager.
- System7 must remain SPY-only.
- Config access via settings or environment helpers.

Update Triggers
- New layer, service, or pipeline stage.
- Changes to data flow or output boundaries.
- New external integration.
