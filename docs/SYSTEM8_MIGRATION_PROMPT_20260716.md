# System8 移植作業 — 委譲時の元プロンプト (2026-07-16)

`core/system8.py` / `strategies/system8_strategy.py` 等一式を実装したエージェント
(opus, worktree 隔離)に与えた元の指示を、そのまま記録として残す。

**目的**: 保留2件(下記 (a)(b))を人間が直接進める際、実装エージェントが最初に
何を前提として作業したか(スコープ、禁止事項、参照ファイル)を再現できるようにする。
続きを別セッション/別エージェントに投げる場合は、このプロンプトをベースに
「(a) 資金配分ウェイトの決定」「(b) ライブ日次自動発注ループへの統合」の
2点だけを追加指示すればよい設計にしてある。

---

## 元プロンプト(委譲時、原文ママ)

```
You are working in the repo at /workspace/quant_trading_system_0510to0906 (a Streamlit-based systematic trading app with 7 existing strategies, "System1" through "System7", each implemented as a paired core/systemN.py + strategies/systemN_strategy.py module, wired into shared allocation/backtest/live-Alpaca-trading infrastructure). The user (the repo's owner) wants a validated new strategy — an overnight SPY drift around scheduled FOMC statements — ported in and registered as "System8", following this repo's existing conventions as closely as possible.

## Where the strategy comes from (a SEPARATE repo, already cloned locally at /home/user/mt5_Bundle-of-edges — you may read files there directly, e.g. via `cat` or a Read-equivalent tool, but do not modify anything in that repo)

The strategy was fully researched, frozen, independently reviewed, and sealed-tested in that other repo as `n0150_fomc_macro_event_drift_spy`. Full evidence chain:
- `strategies/n0150_fomc_macro_event_drift_spy/rules_frozen.md` — the frozen rule spec (read this file directly; it is the single source of truth for the exact mechanics)
- `strategies/n0150_fomc_macro_event_drift_spy/STATUS.md` — full evidence chain / gate history
- `data/events/fomc.csv` — the canonical FOMC scheduled-statement calendar (8 events/year, 2006-2027), which the strategy trades directly off of. You should copy/adapt this calendar into the quant repo as static, git-tracked reference data (NOT under `data_cache/` which is gitignored cache — check `.gitignore` and existing `data/` layout in the quant repo, or place it under `config/` if that fits this repo's convention better; use your judgment based on how this repo stores other static reference inputs, and document wherever you put it).

Exact frozen mechanics (also verify by reading rules_frozen.md yourself, this is a paraphrase):
- Instrument: SPY only, long only, one position at a time.
- Event source: scheduled FOMC statement days only (8/yr) — conference calls, unscheduled/emergency meetings, minutes-release days are NOT events. A statement falling on a non-trading day drops that event entirely (no makeup day).
- Entry: market-on-close (MOC) of the trading session T-1 (the session immediately before the FOMC statement day T).
- Exit: market-on-open (MOO) of the statement day T itself. This is a single-overnight hold — NEVER hold through the 14:00 ET announcement itself.
- Sizing: equal notional per event, no leverage, no averaging, no martingale.
- Stop: none (single-night event hold; risk is bounded by position sizing, not a stop-loss).
- Cost model used in research: 2bp round-trip (assumed $0 commission + ~0.5-1bp SPY spread per side — this matches Alpaca, which is also this repo's live broker, so the cost assumption should carry over cleanly).
- Explicitly OUT of scope (do not implement, these were explicitly rejected/deferred in the source repo): any intraday 2pm-to-2pm window variant, any second instrument leg (QQQ), any CPI/NFP event legs, any vol/regime gate, any signal-sign conditioning. Implement ONLY the exact frozen v03 rule above.

Evidence status (for your context/docs, not something you need to re-derive): full-history 2006-2025 canonical passes_oos (a statistical honesty-battery framework specific to the source repo) PASSED on all 3 regime axes, t=+3.280, DSR(n_trials=3)=0.9955; independent review (different reviewer than the original author) reproduced every number exactly; sealed 2025-only one-shot test also passed. Status in the source repo is `GO_CANDIDATE`. This is a well-validated, not speculative, strategy — implement it faithfully and exactly, do not add embellishments, filters, or "improvements" beyond the frozen spec.

## What "register as System8" means in THIS repo — study before you write code

Before writing anything, thoroughly study how System7 is built and wired, since it is the closest existing analog (SPY-only, single-fixed-symbol, NOT part of the "pick top-N candidates from a broad universe" pattern that Systems 1-6 use). Read in full:
- `strategies/base_strategy.py` (the abstract base all systems inherit)
- `strategies/system7_strategy.py` (the wrapper/orchestration layer)
- `core/system7.py` (the core signal logic — find and read it; system7_strategy.py imports from it)
- `common/alpaca_order.py` (AlpacaOrderMixin, used by System7Strategy — figure out if/how it should apply to System8 too)
- `common/system_constants.py` (System7's constants block, e.g. `SYSTEM7_SYMBOL`, `SYSTEM7_MIN_ROWS`, `SYSTEM7_REQUIRED_INDICATORS`, and the `SYSTEM_CONFIG`-style dict keyed by "system7")
- `common/system_groups.py` (`SYSTEM_SIDE_GROUPS`, `GROUP_DISPLAY_NAMES`, `SYSTEM_LABELS` — note System8 does NOT cleanly fit the existing "long"/"short" stock-picking-pool framing the same way System7 doesn't; use your judgment on whether it needs a new grouping concept, and clearly document your reasoning)
- `config/settings.py` — find where systems are enumerated, where system-specific params live (`get_system_params`), and where the live capital-allocation weights live (there are `long_allocations` / `short_allocations`-style dicts, e.g. system7 currently gets a 20% weight within some short-side allocation pool). **Do NOT add System8 into these live capital-allocation weight dicts** — that would silently divert real (paper-trading) capital away from the 7 systems that are already running live. This decision (how much capital, if any, to give System8, and whether it shares the existing systems' allocation pool or runs as a separate sleeve) is a business decision for the repo owner, not something you should decide. Just make sure System8 is otherwise fully wired/registered/testable, and flag the allocation-weight question explicitly and prominently in your final report.
- `tests/test_system7.py` (or the closest equivalent) for the testing pattern — determinism via `freezegun`/`monkeypatch`, no live network calls, uses cached/fixture data.
- grep for other files that reference "system7" (you can run `grep -rl "system7" --include="*.py" .` from the repo root) to find the FULL set of integration touch-points (things like `common/system_setup_predicates.py`, `common/today_signals.py`, `common/strategy_runner.py`, `common/symbol_universe.py`, `common/notifier.py`, `apps/systems/app_system7.py`, `common/ui_tabs.py`, etc.) — for each, decide whether System8 genuinely needs the same wiring (likely yes for things like signal generation, backtest running, diagnostics, UI tab so the owner can see/run it in the Streamlit app) versus things that would put it into the LIVE DAILY AUTOMATED TRADING LOOP with real order placement (e.g. `scripts/run_all_systems_today.py`, live scheduler registration scripts like `register_task_scheduler.ps1`/`start_scheduler.ps1`, or anything that would cause Alpaca orders to actually fire automatically). For the live-automation / actual-order-placement layer specifically: implement the code path so it CAN be wired in later (i.e. don't leave it broken or half-implemented), but do NOT flip it live yourself — i.e. don't add System8 to whatever list/config currently drives the actual daily automated order-placement job, and clearly flag in your report exactly what would need to change to go live and where.

## What to build

1. `core/system8.py` — the core signal/candidate-generation logic (event-calendar-driven, not indicator-crossing like the other systems — this is a structurally different kind of system, so don't force-fit indicator/setup patterns that don't apply; a "setup" here is simply "is today T-1 relative to a scheduled FOMC date").
2. `strategies/system8_strategy.py` — the wrapper, following the `StrategyBase` interface and System7Strategy's orchestration pattern (prepare_data / generate_candidates / run_backtest / position sizing — equal-notional sizing per the frozen spec, not ATR-risk sizing since there's no stop).
3. The FOMC calendar as git-tracked static data, sourced from the other repo's `data/events/fomc.csv`, in whatever location/format fits this repo's conventions.
4. Registration in `common/system_constants.py` and `common/system_groups.py` (with your documented reasoning on grouping) and `config/settings.py` (system-specific params — cost model 2bp RT, no stop, equal-notional sizing — but NOT the live capital-allocation weight, per above).
5. `tests/test_system8.py` following this repo's existing test conventions (deterministic, no network, use a small fixture SPY price series + a few known FOMC dates).
6. UI wiring so the owner can see/run System8 in the Streamlit app the same way they can for System7 (check `apps/systems/app_system7.py` and `common/ui_tabs.py` for the pattern) — backtest/diagnostics visibility, NOT live-order wiring.
7. A short migration doc under `docs/` (follow this repo's existing naming convention, e.g. `docs/SYSTEM8_FOMC_DRIFT_MIGRATION_20260716.md`) that records: where this strategy came from (source repo path, `n0150_fomc_macro_event_drift_spy`, GO_CANDIDATE status, the key evidence numbers above), the exact frozen rule, what you wired vs. explicitly did NOT wire (capital allocation weight, live daily automation), and a pointer back to the source repo's STATUS.md/rules_frozen.md for full audit trail.

## Process requirements

- Follow this repo's conventions closely: Japanese comments/docstrings (per `docs/internal/AGENTS.md`: "言語方針: 回答・コメントは日本語"), PEP8, type hints, `snake_case`/`PascalCase` naming, ruff/black/isort-clean.
- Run `ruff check`, `black --check`, `isort --check` (or the repo's actual lint commands — check `.pre-commit-config.yaml` / `Makefile` for the exact invocations) on your new/changed files and fix any issues.
- Run `pytest tests/test_system8.py -q` (and any other tests you touched) and make sure they pass.
- Do NOT run or modify anything that would place a live/paper Alpaca order, and do NOT touch the live scheduler registration scripts.
- Do NOT commit or push — just leave the changes in your isolated worktree. Report back the worktree path/branch, a summary of every file you created or changed and why, the lint/test results, and — most importantly — a clearly separated "PENDING DECISIONS FOR THE OWNER" section listing: (a) the capital-allocation-weight question, (b) exactly what would need to change to put System8 into the live daily automated trading loop, and (c) anything else you deliberately left unwired pending a human decision.

Keep your final report focused and skimmable — file list with one-line rationale each, lint/test pass/fail, then the pending-decisions section. Don't dump full file contents into your report; the reviewer will read the diff directly.
```

## 実行結果サマリ(このプロンプトを与えた結果)

- worktree: `system8-fomc-drift` ブランチ、12 ファイル変更(新規6 + 追記9、うち1件重複ノーカウント)。
- `pytest tests/test_system8.py`: 12 passed(セッション本体でも独立再検証済み)。
- `ruff check` / `black --check` / `isort --check`: 新規・変更ファイルすべて clean(独立再検証済み)。
- `data/events/fomc.csv`: 出所リポジトリとバイト完全一致を確認済み。
- 保留2件は本ドキュメントと同ディレクトリの
  `docs/SYSTEM8_FOMC_DRIFT_MIGRATION_20260716.md` §4-5 に詳細記載
  (資金配分ウェイト / ライブ日次自動発注ループへの統合)。
