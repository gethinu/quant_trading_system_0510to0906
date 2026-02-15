# Handoff: Polymarket CLOB Market Making (External)

Status: **External component** (not part of this repo's product runtime).

This repository (`c:\\Repos\\quant_trading_system`) is the **US equities** signal/backtest system described in:
- `docs/llm/SPEC.md`
- `docs/llm/ARCHITECTURE.md`
- `docs/llm/INTERFACES.md`
- `docs/llm/STATE.md`

Polymarket / Simmer automation is managed **separately** under:
- `C:\\Repos\\polymarket_mm\\...`

This document exists to reduce chat overhead and provide a durable pointer/handoff.

## Why Separate Management (Recommendation)

Keep Polymarket CLOB trading as a **separate repo/project** because:
- Different domain + risk model (CLOB microstructure vs US equities daily pipeline).
- Different dependencies (`py-clob-client`, on-chain keys, Discord webhooks).
- Different operational footprint (scheduled tasks, always-on process, hot wallet).
- Different security posture (private key handling, DPAPI secret files).

If you still want a mono-repo, treat Polymarket as an "external integration" with strict boundaries:
- no secrets in repo
- no coupling to `scripts/run_all_systems_today.py`
- separate runtime + logs + state

## What Was Implemented Externally (as of 2026-02-15)

External repo: `C:\\Repos\\polymarket_mm`

Core scripts:
- `C:\\Repos\\polymarket_mm\\scripts\\polymarket_clob_mm.py`
- `C:\\Repos\\polymarket_mm\\scripts\\polymarket_clob_arb_realtime.py`
- `C:\\Repos\\polymarket_mm\\scripts\\simmer_pingpong_mm.py` (Simmer $SIM demo)

Strategy: **Inventory ping-pong** market making:
- post-only maker quotes only (no crossing)
- no shorting
- no split/merge
- inventory-aware: only SELL when inventory > 0

Default behavior: **observe-only** (no live orders) unless explicitly enabled.

Key features implemented:
- Quiet-by-default logs/Discord:
  - Only start/stop/fill/halt + optional periodic summary
  - No quote spam unless explicitly enabled
- Universe:
  - Auto-select **3 tokens** from Gamma active markets using liquidity/volume/spread filters
  - State pruning to avoid token-state bloat
- Fills:
  - Incremental polling using `TradeParams(after=...)` (user-trades endpoint)
  - Note: for accounts with 0 user-trades, this remains empty; that is expected.
- Live order integrity:
  - Periodic reconcile via `get_orders(OpenOrderParams(asset_id=...))` (live mode only)
  - Clears stale order ids and re-quotes
- Risk:
  - Daily loss guard: halt if **(realized + unrealized, mark-to-mid) <= -$5** relative to a daily anchor
  - On halt: stop quoting; in live mode it cancels open orders best-effort; **does not auto-resume**
- Observation metrics:
  - JSONL metrics output (separate from event log) to analyze "is there enough spread/movement?"

## External Ops Cheat Sheet

Scheduled task (Windows):
- `PolymarketClobMM` (hidden via `pythonw.exe`, runs continuously)

External logs/state/metrics:
- Event log: `C:\\Repos\\polymarket_mm\\logs\\clob-mm.log`
- State: `C:\\Repos\\polymarket_mm\\logs\\clob_mm_state.json`
- Metrics JSONL: `C:\\Repos\\polymarket_mm\\logs\\clob-mm-metrics.jsonl`

Observation report:
- Script: `C:\\Repos\\polymarket_mm\\scripts\\report_clob_mm_observation.py`
- Example:
  - `python C:\\Repos\\polymarket_mm\\scripts\\report_clob_mm_observation.py --hours 24`
  - `python C:\\Repos\\polymarket_mm\\scripts\\report_clob_mm_observation.py --hours 24 --discord`

Live enable (danger):
- Requires explicit confirmation:
  - `CLOBMM_EXECUTE=1`
  - `CLOBMM_CONFIRM_LIVE=YES`

## SIM Demo Trading (Chosen)

Chosen approach:
- Simmer $SIM virtual trading via Simmer SDK
- Script: `C:\\Repos\\polymarket_mm\\scripts\\simmer_pingpong_mm.py`

Notes:
- This is **not** CLOB market making (Simmer LMSR/AMM behavior differs).
- It still supports observe-only and an explicit live toggle (`--execute --confirm-live YES`).

If a Polymarket CLOB "paper trading" simulator is still desired, implement it inside the external repo
as a separate strategy module (do not couple it to this US-equities repo runtime).
