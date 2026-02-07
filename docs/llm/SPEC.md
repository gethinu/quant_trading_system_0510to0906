# LLM Reference: Product Spec

Purpose
This document captures the stable product intent, scope, and invariants for the Quant Trading System. It is the first reference for changes and should be kept in sync with code.

Scope
- Educational and paper-trading oriented system for US equities.
- Daily signal generation, backtests, and dashboards.
- Seven systems: long systems 1,3,4,5 and short systems 2,6,7.

Core Workflows
1. Build the symbol universe from settings and data files.
2. Load cached market data via CacheManager.
3. Ensure indicators exist (precomputed or cache-managed).
4. For each system: filter, setup, rank, and generate candidates.
5. Merge signals and allocate capital or slots via finalize_allocation.
6. Persist outputs and diagnostics; send notifications when configured.

System Set
- System1, System3, System4, System5: long systems.
- System2, System6: short systems.
- System7: SPY-only hedge, short side.
- Allocation weights and system descriptions live in `docs/systems/INDEX.md` and `data/symbol_system_map.json`. Core logic in `core/systemX.py` is the source of truth.

Critical Invariants
- System7 trades SPY only. Expanding symbols is forbidden.
- All cache I/O must go through `common/cache_manager.py::CacheManager`.
- Configuration access must go through `config/settings.py::get_settings()` and `config/environment.py::get_env_config()`.
- Tests must not call external APIs. Use cached data or fixtures.
- Diagnostics must include `ranking_source`, `setup_predicate_count`, and `ranked_top_n_count` for every system.

Out of Scope
- Real-money performance guarantees.
- Direct manipulation of files under `data_cache/` outside CacheManager.
- Changing the allocation contract in `core/final_allocation.py`.

Update Triggers
- Any change to system logic, candidate selection, or diagnostics schema.
- New UI, API, or CLI entry point.
- Changes to cache structure, output naming, or allocation contract.

References
- `docs/README.md`
- `docs/systems/INDEX.md`
- `docs/TECHNICAL_SPECS.md`
- `docs/technical/environment_variables.md`
- `.github/copilot-instructions.md`
- `.agent/workflows/project-reference.md`
