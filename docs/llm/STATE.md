# LLM Reference: State

State Locations
| Path | Owner | Purpose | Lifecycle |
| --- | --- | --- | --- |
| `data_cache/full_backup/` | CacheManager | Raw long-term market data | Persistent source of truth |
| `data_cache/base/` | CacheManager | Indicator-enriched long-term data | Persistent, rebuilt from full_backup if needed |
| `data_cache/rolling/` | CacheManager | Recent N trading days for daily signals | Persistent, refreshed regularly |
| `settings.outputs.signals_dir/` | Pipeline scripts | Daily signals (system/final/exit CSVs) | Append or per-run output |
| `results_csv/` | Pipeline scripts | Signals, allocation, paper trade logs, daily metrics | Append or per-run output |
| `results_csv_test/` | Pipeline tests | Test-only outputs and diagnostics snapshots | Ephemeral, safe to clean |
| `logs/` | Pipeline scripts | Run logs, progress JSONL, exclusions | Append per run |
| `locks/` | RunLock | Run serialization locks | Ephemeral while running |
| `snapshots/` | tools/ | UI snapshot diffs and reports | Ephemeral, manual retention |
| `results_images/` | tools/ | Captured UI screenshots | Ephemeral unless copied into docs |
| `screenshots/` | tools/ | Ad hoc UI screenshots | Ephemeral |
| `data/position_tracker.json` | Position Tracker | Pseudo-trade positions for exit signals/auto rules | Persistent, updated after signal notifications |
| `config/config.yaml` | Config | Primary YAML config | Versioned |
| `.env` | Config | Secrets and local overrides | Local, not versioned |
| `data/` | Data | Symbol lists, maps, reference data | Versioned |

State Rules
- Never read or write under `data_cache/` directly. Use CacheManager.
- Daily pipeline appends to `results_csv/daily_metrics.csv`.
- Progress events go to `logs/progress_today.jsonl` when enabled.
- Locking uses `locks/<name>.lock` directories created by `common/run_lock.py`.

State Sources Of Truth
- Market data: `data_cache/full_backup/`.
- Indicator-enriched data: `data_cache/base/`.
- Daily signal inputs: `data_cache/rolling/`.
- Output artifacts: `results_csv/` and `logs/`.

Update Triggers
- New cache layer or output directory.
- Changes to output naming or log formats.
- New long-lived state that should be documented.
