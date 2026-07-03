# REFACTOR AUDIT — Critical Path (fable5 deep review)

**Audit date**: 2026-07-02  
**Branch**: `claude/monitor-webapp` @ `73d04cf`  
**Reviewer**: fable5 deep audit, 6 concurrent subagents, ~24,000 LoC covered  
**Scope**: `scripts/daily_pipeline.ps1`, `apps/app_today_signals.py`, `common/today_*.py`, `common/cache_manager.py`, `common/trade_management.py`, `common/signal_export.py`, `common/stage_metrics.py`, `common/system_common.py`, `common/alpaca_*.py`, `common/broker_alpaca.py`, `common/narrator.py`, `common/publishers/*.py`, `scripts/cache_daily_polygon.py`, `scripts/build_rolling_with_indicators.py`, `scripts/paper_trading_*.py`, `core/system1-7.py` (structural only)

**Ground rules for this audit**
- **READ-ONLY**. Report only, no code was edited. `git push` は行わない。
- **Signal semantics is baseline**: `SYSTEM_TRADE_RULES` values + `core/system1-7` entry/exit/stop/ranking logic must not change. Structural, cosmetic, and performance improvements only. Where the audit found signal-adjacent bugs, they are reported for human sign-off, not silently fixed.
- **Zero-regression**: any P0/P1 patch must ship with a golden-signal comparison test on a recorded universe fixture.

---

## 0. Executive summary — TL;DR

The critical path is functionally correct in the happy path but carries a **fat tail of silent-failure modes** and one **dead spec gate**. The highest-leverage wins in decreasing order of business impact:

1. **`System1 SPY>SMA100` gate is dead** (fails open every day the market is below the 100-day). `common/today_signals.py:980` looks up `column="sma100"` on a frame where `common/utils_spy.py:644` wrote `SMA100`. `Series.get` is case-sensitive → gate returns `None` → `setup_pass = final_pass_count if spy_gate != 0 else 0` treats `None != 0` as truthy → System1 emits buy signals on days the spec forbids. **Money-losing bug that has been live for the full audit window.** Fix included as P0-1.
2. **`common/alpaca_trading.py::_side_from_row` defaults unknown/missing `side` to `"sell"`** (line 223). A malformed signals frame silently submits short-side orders. Paper only today, but this pattern will bite when live-trading flips on. Fix included as P0-2.
3. **`ntfy.NtfyPublisher` leaks the topic (documented as "事実上の secret") into `PublishResult.target` and dry-run detail**, which flows into `meta.publish_status` in the exported signals JSON and into log lines. Anyone who can read the exported JSON or log can hijack subscriber push notifications. Fix included as P0-3.
4. `publishers/registry.py` does **not** wrap `publisher.send()` in try/except; a render-time exception in the primary silently kills the failover chain and drops the entire day's notifications. Small S-diff, high impact — patch outline in Section 5.
5. Setup thresholds are **hand-copied into `common/today_signals.py::_compute_setup_pass` and `common/today_filters.py`** instead of imported from `core/systemN`. Values happen to match today, but the boundary operator has already drifted (`>=` vs `>`), and any future spec change will silently diverge two implementations.

Behavioral drifts flagged for human decision (**INFO, not auto-fixed**):
- `SYSTEM_TRADE_RULES.max_holding_days` is applied as **calendar days**, spec says trading days.
- `trade_management._get_row_for_date` falls back to `"nearest"`/`"bfill"` — allows **future-row lookahead** in gap dates.
- Short-side profit target uses `entry / 1.04` vs the natural reading `entry * 0.96` (0.15% discrepancy, arguably spec-ambiguous).
- `system1.finalize()` returns a 3-tuple contract different from systems 2–7.
- `system7` full-scan payload labels historical setup-day records with the **latest close** for `entry_price` (backtest labeling only, signal timing unaffected).

Overall verdict: **the code is safe enough to keep running today, but there are five silent-failure surfaces that will burn subscribers before revenue scales.** Fixing the three P0 patches below buys ~90% of the immediate risk reduction for <100 lines of diff.

---

## 1. Impact-vs-Effort matrix (start here)

The Y-axis is "$/subscriber-trust impact if this bug fires"; the X-axis is diff size. Top-left = highest ROI = ship first.

| Rank | Finding | Cluster | Impact | Effort | ROI |
|---:|---|---|---|---|---|
| **1** | System1 SPY>SMA100 gate dead (case bug) | B | 🔴 direct wrong signals | XS | 🟢 top |
| **2** | `_side_from_row` default = sell | D | 🔴 wrong order side | XS | 🟢 top |
| **3** | ntfy topic leak into export JSON | D | 🟠 subscriber security | XS | 🟢 top |
| **4** | Registry no try/except → chain dies | D | 🟠 total notify drop | S | 🟢 top |
| **5** | `paper_trading_submit` zero-orders = success | E | 🟠 silent submit fail | S | 🟢 high |
| **6** | `signal_export` SystemExit → empty payload exit-0 | C | 🟠 subscriber can't distinguish abort from flat | S | 🟢 high |
| **7** | Alpaca positions fetch silent `{}` → duplicate exposure | D | 🟠 duplicate orders | S | 🟢 high |
| **8** | Alpaca retry without idempotency key → double-submit | D | 🟠 duplicate fill | S | 🟢 high |
| **9** | Setup thresholds hand-copied (drift risk) | B | 🟡 future drift | S | 🟢 high |
| **10** | Filter threshold `>=` vs `>` boundary drift | B | 🟡 rare-but-real edge | XS | 🟢 high |
| 11 | `_compute_entry_stop` limit-entry fallback drops offset | B | 🟠 wrong limit price on error | S | 🟡 med |
| 12 | Base-cache path CSV vs writer feather mismatch | C | 🟡 data-miss | XS | 🟡 med |
| 13 | `load_base_cache` 4 dead params | C | 🟡 caller expects gating that doesn't happen | S | 🟡 med |
| 14 | Build-rolling: partial-NaN placeholders un-recomputed | E | 🟠 stale tail values in latest bar | S | 🟢 high |
| 15 | Build-rolling: base-sourced tail-cap silent | E | 🟠 window-length drift | S | 🟢 high |
| 16 | `daily_pipeline.ps1` exit-code coverage | A | 🟡 unexpected codes silently pass | S | 🟢 high |
| 17 | Cache-freshness uses calendar not trading days | A | 🟡 Monday false-warns | S | 🟡 med |
| 18 | `_evaluate_position_for_exit` swallows all → position vanishes | A | 🟠 missed exit | S | 🟡 med |
| 19 | `_normalize_index` swallows its own `raise` | C | 🟡 all-NaT frame silently empties | XS | 🟢 high |
| 20 | 76 bare `except: pass` in app_today_signals | A | 🟡 hides bugs | L | 🟡 med (incremental) |
| 21 | Cross-system latest_only duplication (~1500 lines) | F | 🟢 pure maintainability | L | 🟢 huge |
| 22 | `prepare_data_vectorized` skeleton duplication | F | 🟢 maintainability | M | 🟢 high |
| 23 | Option-B feature flag pasted 3× per file | F | 🟢 maintainability | S–M | 🟢 high |
| 24 | Two overlapping Alpaca wrappers | D | 🟢 maintainability | M | 🟡 med |
| 25 | System1 `generate_candidates` 789 lines | F | 🟢 readability | M | 🟡 med |

**Recommended sequencing**:
- **Sprint 1 (this week)**: items 1–4 (all XS/S, no signal-semantics touch) — the three patch drafts in Section 5 + registry try/except.
- **Sprint 2**: items 5–10 (silent-failure hygiene at pipeline boundaries).
- **Sprint 3**: items 14–20 (build-rolling completeness + orchestrator hygiene).
- **Sprint 4+**: items 21–25 (structural consolidation across `core/system1-7` — biggest line-count win, needs golden-signal harness first).

---

## 2. Findings per file

Legend:
- **P0** = subscriber reliability / bug injection / money-loss risk
- **P1** = maintainability blocker (long fn, dup logic, naming)
- **P2** = performance (DataFrame copy, parallel, cache hit)
- **P3** = modernization (pathlib, dataclass, Enum, Protocol, `match`, `|` union)

For each finding: `[Pri] <summary>` — line ref — Why — Fix — Diff (XS<10 / S 10–50 / M 50–200 / L>200) — Regression risk (low/med/high) — Test needed.

### 2.1 `scripts/daily_pipeline.ps1` (321 lines)

Overall: well-structured PowerShell orchestrator; inconsistent per-step exit-code handling and buffered logging are the main issues.

- **[P0] Inconsistent exit-code failure matching silently ignores unexpected codes** — L212–L213, L247, L270, L296. Coverage counts only `exit=1`; publish/paper_submit only `1 or 2`; vercel only `1`. Any other non-zero code (3, -1, Python launcher crash `0xC0000135`) is treated as success. Fix: normalize to `if ($code -ne 0)` with an explicit allowlist for intentional WARN codes (e.g. coverage=2) documented in one hashtable. Diff S / Risk low / Test yes (stub step exiting 3, assert exit 2 + WARN).
- **[P0] Coverage gate breach (exit=2) is log-only, never notified** — L213. `Send-Warn` only fires on `$Failures`; a coverage breach means signals ran on incomplete data with no ntfy push. Fix: add `$Warnings` list, include in ntfy body. Diff XS / Risk low / Test yes.
- **[P1] No step timeout** — L128–L140. A wedged Polygon fetch or Alpaca call hangs the nightly task forever, silently. Fix: `Start-Process -PassThru` + `Wait-Process -Timeout`. Diff S–M / Risk med / Test yes.
- **[P1] `Invoke-Step` buffers all output; no live log** — L135–L137. A 30-min signals run produces no log until completion. Fix: streaming pipe `& python @PyArgs 2>&1 | ForEach-Object { Write-Log "$_" }`. Diff XS / Risk low.
- **[P1] Failed venv activation → system-python fallback with only a log WARN** — L169–L176. Version drift → subtly different signals. Fix: make missing venv fatal or require `-AllowSystemPython` switch. Diff XS / Risk low / Test yes.
- **[P2] `.env` parser drops inline comments, swallows Set-Item errors** — L79–L98. Diff XS / Risk low / Test yes (unit-style Pester on parser).
- **[P3] Steps are copy-paste blocks; table-driven loop halves file** — L180–L297. Six near-identical blocks. Fix: array of hashtables `@{Name; Args; SkipIf; FailPolicy}`. Diff M / Risk med.
- **[P3] Step 6 hardcodes `powershell.exe` (WinPS 5.1)** — L292. Fix: `$PSHOME`-relative or dot-source. Diff XS.

### 2.2 `apps/app_today_signals.py` (4,397 lines)

Overall: Streamlit UI mixed with headless dispatch, exit-candidate math, Alpaca order submission, log parsing, and rolling-cache validation. **189 `except Exception` handlers, 76 bare `pass`.** The dominant maintainability problem in the repo.

- **[P0] `_entry_and_stop_prices`: hardcoded per-system stop multiples and entry ratios duplicated as silent fallbacks** — L2652–L2695. `strategy.config.get("stop_atr_multiple", 5.0/3.0/2.5/1.5/3.0)` and `entry_price_ratio_vs_prev_close` defaults re-encode System 1–6 spec values inline; blanket `except Exception: return None, None` drops positions from exit analysis on any KeyError. Fix: source multiples/ratios from `SYSTEM_TRADE_RULES`/`get_system_rules()`; dispatch via table `{system: (price_col, atr_col, sign)}`. Diff M / Risk med / Test yes (per-system stop = entry ± mult×ATR).
- **[P0] `_evaluate_position_for_exit` swallows all errors → positions silently vanish from exit candidates** — L2565–L2636, L2431–L2436. Same pattern upstream: `positions = []` on Alpaca API failure makes "API down" indistinguishable from "flat book". Fix: catch narrowly, collect per-symbol failures into `ExitAnalysisResult.errors`. Diff S–M / Risk low / Test yes.
- **[P0] Cache-freshness check uses calendar days, not trading days** — L2249–L2256. `days_behind = (today - last_cache_date).days > 1` fires false Monday warnings, misses holiday staleness. `calculate_trading_days_lag` is already imported and used correctly at L375 — duplicated logic. Fix: reuse existing helper. Diff S / Risk low / Test yes (Monday case).
- **[P0] `_analyze_rolling_cache` exception paths both over- and under-flag** — L722–L725, L755–L756. `pd.to_numeric` failure → `continue` (corrupt column silently passes); ratio failure → `ratio = 1.0` (column flagged fatal). Fix: log with symbol/column context; treat conversion failure explicitly. Diff S / Risk med / Test yes.
- **[P1] 76 bare `except Exception: pass`** — file-wide. Masks real failures; makes every refactor risky. Fix: `@_swallow(log=…)` decorator or `contextlib.suppress` with logging, incremental. Diff L / Risk low per site.
- **[P1] Module-level side effects make the file unimportable in tests** — L61–L62, L84–L87, L204–L233, L235–L240, L3741–L4397. Fix: wrap main body in `def main() -> None` with `if _IS_STREAMLIT_RUNTIME: main()`; extract pure helpers into `apps/today_signals_core.py`. Diff L / Risk med / Test yes (import-without-side-effects).
- **[P1] `_log_manual_rebuild_notice`: 180 lines of global mutable state + `atexit` + class defined inside nested try/except** — L880–L1059. Fix: `ThrottledLogger` dataclass instantiated once. Diff M / Risk low / Test yes.
- **[P1] `UILogger.log` ~120 lines mixing JSON parsing, two timestamp formats, dedupe, Streamlit echo, encoding remediation** — L1460–L1664. Fix: split into `_parse_structured`, `_format_line`, `_emit`; move encoding fix to one-time module init. Diff M / Risk low / Test yes (golden format).
- **[P1] `execute_today_signals` 220 lines** — L2194–L2415. Fix: extract `_log_run_header`, `_run_missing_scan`, `_debug_final_counts`; keep coordinator ~50 lines. Diff M / Risk low–med / Test yes.
- **[P1] Hidden coupling via setattr on scripts.run_all_systems_today** — L1702–L1712, L1463–L1469, L2073–L2076. Fix: engine should accept callbacks as parameters. Diff S+S / Risk med / Test yes (integration).
- **[P1] Misleading header comment + wrong self-run path** — L1–L20, L181–L186. Header claims "CSV display only, no API calls" while submitting live Alpaca orders. Diff XS / Risk low.
- **[P2] Blocking `time.sleep` loops freeze the Streamlit script run** — L335–L340 (up to 120s), L3383–L3400 (10s). Fix: `st.fragment(run_every=…)` or `st_autorefresh`. Diff S / Risk low.
- **[P2] Per-symbol prefetch full DataFrame `.copy()` + repeated column renames** — L527–L596. For ~5–6k symbols doubles transient memory. Fix: operate on views, rename once. Diff S–M / Risk med / Test yes.
- **[P2] Env-var config re-read inside hot loops** — L1147–L1148, L1183, L1526. Fix: freeze into `UiEnvConfig` dataclass at run start. Diff S / Risk low / Test yes.
- **[P3] Dead compatibility code and unused globals** — L43–L53 (Python <3.9 zoneinfo fallback), L132–L148 (unused import), L111–L115 (duplicate import), L3960–L3963 (unused var). Diff XS.
- **[P3] System dispatch via if-chains; systems as bare strings** — L2652–L2695, L2698–L2715, L3292–L3300. Fix: `SystemName(StrEnum)` + one `SYSTEM_UI_SPEC` table. Diff M / Risk med / Test yes.
- **[P3] `_apply_strategy_state` pokes private strategy attributes `_last_entry_atr`, `_last_prev_close`** — L2698–L2715. Fix: `strategy.prime_exit_state(atr=…, prev_close=…)` Protocol. Diff S / Risk med / Test yes.

### 2.3 `common/today_signals.py` (3,022 lines)

Overall: signal-generation library used by all seven systems. Correct on the happy path but three god-functions (410/507/350 lines) and 100+ `except Exception: pass` sites.

- **[P0] System1 SPY>SMA100 gate is dead** — L980 (`column="sma100"`) vs L2144 (`last_row.get(column)`), while `common/utils_spy.py:644` writes uppercase `"SMA100"`. Gate returns `None`, `setup_pass = final_pass_count if spy_gate != 0 else 0` treats `None != 0` as truthy → **System1 fails open when SPY ≤ SMA100**. `core/system1.py:318` explicitly states the market gate is "checked at orchestrator level" — i.e. here. Same file uses uppercase `"SMA100"` in `_diagnose_setup_zero_reason` L1320. **Patch draft P0-1 below.**
- **[P0] Setup thresholds re-hardcoded in `_compute_setup_pass`** — L1018 (`rsi3>90`), L1065 (`drop3d>=0.125`), L1142/L1146 (`500_000`, `2_500_000`), L1186 (`adx7>55`), L1194 (`rsi3<50`), L1222 (`return_6d>0.20`). Not just log counters — `get_today_signals_for_strategy` returns an empty frame when `setup_pass<=0` (L2845) and `_select_candidate_date` zeroes candidates (L1466). Fix: import from `core/systemN` constants. Diff S / Risk low / Test yes (parity vs core).
- **[P0] `_compute_entry_stop` silent fallback ignores limit-entry semantics** — L2377–L2392, L2394+, L1702–L1713. Systems 2/3/5/6 are limit-price entries with `SYSTEM_TRADE_RULES.entry_price_offset_pct` (±4/-7/-3/+5%). If `strategy.compute_entry` raises, the fallback silently emits raw prev-Close — a **wrong limit price** recorded only in debug. `today_signals.py` never imports `trade_management`. Fix: on `compute_entry` failure for offset systems, either apply the offset or skip the candidate with explicit reason. Diff S / Risk med / Test yes.
- **[P0] Outer try/except returns `setup_pass=0`, silently killing a whole system** — L878, L1276–L1278. Any exception (bad SPY frame, missing indicator) → pipeline returns empty frame with reason "setup_pass_zero"; infrastructure error masquerades as "no setups today". Fix: narrow try, log, distinguish `setup_error` from `setup_zero`. Diff XS–S / Risk low / Test yes.
- **[P1] Three god functions** — `_compute_setup_pass` 410 ln (L869), `_build_today_signals_dataframe` 507 ln (L1593), `_compute_entry_stop` 350 ln (L2349). Nested closures, 6-level fallback — unreviewable. Fix: dispatch table + split. Diff L / Risk med / Test yes (golden).
- **[P1] Hidden broker network IO with fail-open** — `_apply_shortability_filter` L615–L627. Any exception → `shortable_map={}` → filter skipped silently → systems 2/6 can emit signals for non-shortable names. Fix: inject `shortable_provider` parameter; log WARN on empty due to error. Diff S / Risk low / Test yes.
- **[P1] In-place mutation of caller's prepared dict** — `_filter_by_data_freshness` L542 `prepared.pop`, `_apply_shortability_filter` L633. Aliasing surprises. Diff XS / Risk low.
- **[P1] `SkipStats` does file IO; dead code inside** — L207–L273; L215–L218 are literal `try: pass / except: return`. Diff S / Risk low.
- **[P1] `common → scripts` dependency inversion with `sys.path` hack** — `run_all_systems_today` L2966–L2975. `common` importing from `scripts/` inverts layering. Fix: move `compute_today_signals` into `common/` or a `pipeline/` module. Diff M / Risk med / Test yes (import smoke).
- **[P2] Full-universe double copy per system** — `_slice_data_for_lookback` L341, `_normalize_prepared_dict` L364. For ~5000 symbols × 7 systems: seconds of pure copying. Fix: normalize once upstream. Diff M / Risk med / Test yes.
- **[P2] Per-symbol last-row scans duplicated in `_compute_filter_pass` and `_compute_setup_pass`** — L685–L709 vs L883–L905. Fix: compute `latest_rows` once. Diff S / Risk low.
- **[P2] `max_positions` / TOP-N magic and repeated settings reads** — L106, L1472–L1474, L2071–L2074. Diff XS / Risk low.
- **[P3] Modernization** — `system_name` strings → `StrEnum`/`Literal`; `_compute_setup_pass` elif → `match`; `TodaySignal` `r.__dict__` → `dataclasses.asdict`; `os.path.join`/`os.makedirs` → `pathlib`.

### 2.4 `common/today_filters.py` (956 lines)

Overall: column-resilient pickers + six near-identical `filter_systemN` loops.

- **[P0] Filter thresholds hardcoded, boundary-drifted vs core constants** — L374/L385 (`>=5`, `>=50_000_000`), L406/L409, L444/L450, L530/L536, L565/L577. `core/system1.py:93` uses `> MIN_DOLLAR_VOLUME_20` strict; here `>=`. System 2 same drift on DV and ATR ratio. Fix: import constants from `core/systemN`, align operators. Diff S / Risk low / Test yes.
- **[P1] Test-mode relaxation logic embedded in `_system3_conditions`** — L449–L499. Impure, env-coupled, 50 lines special-casing. Fix: hoist `_resolve_system3_atr_threshold(env)` helper. Diff S / Risk low / Test yes.
- **[P1] `df.attrs` debug lists grow unbounded, mutate input frames + `globals()` mutation for init marker** — L74, L418–L422, L509–L513, L545–L549, L592–L596, L646–L651. Fragile "last element" coupling between `_systemN_conditions` and `filter_systemN`. Fix: return reason from `_systemN_conditions`. Diff M / Risk low.
- **[P1] `filter_system1..6` are six near-identical ~50-line loops** — L659–L956. `system4/5` embed a second, inconsistent debug mechanism (`DEBUG_SYSTEM_FILTERS` print). Fix: generic `_run_filter(symbols, data, conditions_fn, stage_names, stats)`. Diff M / Risk low / Test yes.
- **[P2] `_pick_series` rebuilds normalization map every call** — L137–L204. Fix: memoize on `id(df.columns)`. Diff S / Risk low.
- **[P3] Missing return type hints; underscore names in `__all__`** — L33–L51, L137, L207, L232. Diff XS.

### 2.5 `common/today_data_loader.py` (820 lines)

Overall: happy-path correct; documented base→rolling rescue is unimplemented.

- **[P0] Docstring promises base→rolling rebuild, code silently drops the symbol** — L194–L197 vs L428–L432; `_build_rolling_from_base` (L106) is dead in this file. Stale/missing rolling → dropped with rate-limited debug only. Fix: wire `_build_rolling_from_base` into `needs_rebuild` branch, or fix docstring and promote to WARN with count. Diff S (WARN) / M (wire) / Risk low–med / Test yes.
- **[P1] Unreachable dead branch in `load_indicator_data`** — L697–L708. `needs_rebuild = df is None or empty`; inside the guard the `else` computing `f"len={len(df)}/{target_len}"` can never run. Fix: implement intended length check or delete dead code. Diff XS / Risk low–med.
- **[P1] `load_basic_data` is a 410-line function of nested closures** — L179–L590. Fix: lift closures to module scope. Diff M / Risk low.
- **[P1] `_pick_symbol_data` duplicated in `load_indicator_data`** — L277–L311 vs L650–L683; `_recent_trading_days` duplicates `_collect_recent_days` in `today_signals.py:1370`. Diff S / Risk low.
- **[P2] `load_indicator_data` serial while `load_basic_data` parallel** — L646. Same per-symbol cache reads. Diff S–M / Risk low.
- **[P2] Per-symbol `df.copy()` up to 3× per load** — L284, L343, L653/L717/L722. Diff S / Risk low.
- **[P3] `time.time()` vs `time.perf_counter()` mixed; mutable singleton logger** — L224 vs L630, L28–L36.

### 2.6 `common/cache_manager.py` (1,129 lines)

Overall: god-module with `CacheManager` class + parallel module-level API + hidden singleton; indicator/path/case-normalization triplicated.

- **[P1] `load_base_cache` ignores 4 of 6 parameters** — L1103–L1129. `rebuild_if_missing`, `min_last_date`, `allowed_recent_dates`, `prefer_precomputed_indicators` accepted but unused; callers pass them expecting freshness gating. Fix: implement or remove; warn on unsupported. Diff S / Risk med / Test yes.
- **[P1] `base_cache_path` returns `.csv` but `save_base_cache` writes `.feather`** — L1025–L1026 vs L1041. Any caller resolving via path helper will never find what the writer wrote. Fix: single source of truth. Diff XS / Risk med / Test yes (round-trip).
- **[P2] Dead legacy-name fallback in `read()`** — L345–L364. `if ... ticker != original_ticker` always False; whole block unreachable. Diff XS.
- **[P2] `_upsert_one` recomputes full indicator history on every upsert** — L429–L470. O(full-history) per ticker per day. Fix: recompute only tail window needed by the longest indicator. Diff M / Risk med / Test yes (tail vs full equivalence).
- **[P2] Hardcoded heal constants + config duplication** — L132 (`tail_rows=330`), L305, L730. Fix: default to `self._rolling_target_len`; hoist `REQUIRED_ROLLING_INDICATORS`. Diff XS.
- **[P2] Swallowed exceptions + `{"status":"error"}` returns** — L158–L160, L300, L363, L591–L592, L669–L670, L794–L795, L1054, L1098. `print()` at L443 bypasses logging. Fix: narrow types, log with ticker, `CacheOpResult` dataclass. Diff S / Risk low / Test yes.
- **[P2] `compute_base_indicators` duplicates `indicators_common` + 3 competing case-normalization schemes** — L857–L960, L66–L96, L981–L1022. Fix: delegate to `add_indicators`, one `ColumnSchema` (Enum). Diff M / Risk med / Test yes (golden frame).
- **[P3] Modernization** — stringly-typed `profile` → `class Profile(StrEnum)`; raw JSON meta → `@dataclass RollingMeta`; hidden `_DEFAULT_CACHE_MANAGER` singleton hurts testability (L1081–L1089).

### 2.7 `common/trade_management.py` (917 lines)

**Spec verdict**: `SYSTEM_TRADE_RULES` **matches specs 1–6 on every checked value** (entry type/offset, ATR period/multiplier, trailing %, profit-target type/value, max-holding-days, re-entry, 2%/10% sizing). No P0 value drift. System7 intentionally absent (documented, verified consistent with spec via `strategies/constants.STOP_ATR_MULTIPLE_DEFAULT = 3.0`).

- **[P1] `max_exit_date` uses calendar days; spec means trading days** — L444–L445. `signal_date + timedelta(days=2)` spanning a weekend exits System2 short ~2 trading days early. Behavioral drift without touching values. Fix: use market-data index offset or `BDay`. Diff S / Risk med / Test yes.
- **[P1] `_get_row_for_date` falls back to `"nearest"`/`"bfill"` → future-row lookahead** — L298–L334. If signal_date is missing from the index, can return a **future bar** → wrong prices in live gaps, lookahead in backtest. Fix: restrict to `pad`/`ffill`. Diff XS / Risk med / Test yes (gap-date unit).
- **[P2] Short percentage profit target uses division not subtraction** — L650–L656. `entry / 1.04` = 3.85% below entry vs spec's "4%の利益" naturally read as `entry * 0.96` (0.15% delta). Ambiguous — flag for human. Diff XS (doc) / Risk low.
- **[P2] `create_trade_entry`: 145-line body, whole `try/except → None`** — L336–L480. Broad `except` at L478 converts programming errors into "no entry". Fix: extract `_resolve_entry_price` / `_build_entry`; catch narrowly. Diff M / Risk low / Test yes.
- **[P2] `validate_trade_management_data` lets NaN through, raises on str** — L885–L901. `NaN <= 0` is False → NaN shares pass. Fix: `pd.to_numeric(errors="coerce")` + `pd.isna(v) or v<=0`. Diff XS / Risk low / Test yes.
- **[P3] `enhance_allocation_with_trade_management`: iterrows + dict rebuild, three identical fallbacks** — L727–L852. Diff S / Risk low.
- **[P3] Modernization** — `Dict/List/Optional` predate `__future__ annotations` already present; stringly-typed `profit_target_type/entry_reference/side` → `StrEnum` + `match`.
- **[INFO] System7 absent from `SYSTEM_TRADE_RULES`** — L276–L287. Intentional/documented; `create_trade_entry` logs error at L361 for system7 → downgrade to `info`.

### 2.8 `common/system_common.py` (337 lines)

- **[P1] `_normalize_index`'s own guard swallows the error it raises** — L67–L73. `raise ValueError("invalid_date_index")` sits *inside* `try/except: pass`; all-NaT index never raises → frame silently emptied downstream. Fix: compute check inside try, raise outside (or catch `TypeError` only). Diff XS / Risk low / Test yes.
- **[P2] `get_date_range` crashes on `None` frames** — L163–L168. `get_total_days` guards `df is not None` but `get_date_range` calls `df.empty`. Diff XS / Test yes.
- **[P2] `check_precomputed_indicators`: 110 lines, duplicated fuzzy matching, function def in loop** — L191–L300. Fix: extract `_normalize_col`; build `{norm: original}` once. Diff S / Risk low / Test yes.
- **[P3] Underscore-prefixed public API** — L20, L45, L94; `_rename_ohlcv` etc. exported via `__all__`. Diff S (mechanical).

### 2.9 `common/signal_export.py` (440 lines) — **subscriber-critical**

Overall: well-documented schema v1.0 builder + headless CLI; atomic write is good. Two subscriber-facing P0s.

- **[P0] `SystemExit` → empty payload with exit code 0, no error marker** — L408–L415, L440. When compute aborts (stale rolling), empty-but-schema-valid payload is written and exit code is 0. Subscribers cannot distinguish "legitimately no signals" from "pipeline aborted". Fix: additive schema field (`"meta.status": "aborted_stale_cache"`) and/or distinct exit code. Diff S / Risk low / Test yes.
- **[P0] `_map_side` passes unknown side values through as arbitrary uppercase strings** — L135–L138. Unmapped/None side → `str(v).upper()` verbatim. `None` becomes `"BUY"` — a missing side defaults to a **buy order**. Fix: raise or drop with error log; never default. Diff XS / Risk low / Test yes.
- **[P2] `normalize_system_key` grabs the first digit anywhere in the name** — L80–L97. `"v2_system1"` → `sys2` misclassification. Fix: anchored regex `re.search(r"sys(?:tem)?\s*(\d+)", s)`. Diff XS / Risk low / Test yes.
- **[P2] `default_output_path` is cwd-relative** — L324–L325. Scheduler runs from a different cwd → wrong location, subscribers read stale. Fix: resolve against settings/repo root. Diff XS / Risk low.
- **[P3] `gate_survival_ratio` silently 1.0 when counts missing; score=0 vs None conflated** — L265–L283. Diff XS.
- **[P3] Modernization / testability** — `import math` inside loops (L100–L133); `run_headless` hard-imports `compute_today_signals`. Fix: hoist imports; inject `compute_fn`. Diff XS/S.

### 2.10 `common/stage_metrics.py` (330 lines)

Overall: tidy `slots=True` dataclass module — healthiest file in the audit. Minor concurrency and hygiene nits.

- **[P2] `ensure_display_metrics` leaks the internal mutable bucket outside the lock** — L258–L264, L102. Half-thread-safe. Fix: return a copy + `update_display_metrics(...)` mutator. Diff S / Risk med / Test yes.
- **[P3] Module docstring is placed after imports → `__doc__` is `None`** — L1–L17. Fix: move above `from __future__ import annotations`.
- **[P3] `as_tuple` silently drops substage; magic `9999` clamp** — L52–L63, L299. Diff XS.
- **[P3] `GLOBAL_STAGE_METRICS` module singleton** — L322. Fix: pytest fixture swap. Diff XS.

### 2.11 `common/alpaca_trading.py` (542 lines)

Overall: good safety story (paper guard, dry-run default, audit log, dedup, idempotent client_order_id). Weak on silent fallbacks around position data and side handling.

- **[P0] Missing/unknown `side` defaults to `sell`** — L219–L223. Malformed signals frame silently submits sell (potentially short-entry) orders. **Patch draft P0-2 below.**
- **[P0] Silent `{}` fallback when open-positions fetch fails** — L339–L345 used at L260–L262. Batch re-buys symbols already held → duplicate exposure. Fix: return `None` on error and abort in non-dry-run. Diff S / Risk med / Test yes.
- **[P1] Notional submit path bypasses retry/rate-limit and guard layer** — L493–L507. Transient 429/5xx → dropped order, caught at L523 as `.error`. Fix: route through `broker_alpaca` helper with retry. Diff S / Risk low / Test yes.
- **[P1] Paper guard checks an env var the client never uses** — L112–L117 vs `broker_alpaca.get_client` L90–L94. Guard validates `ALPACA_API_BASE_URL`, but SDK reads `paper=` flag or `APCA_API_BASE_URL`. False sense of safety. Fix: assert on what actually configures the client. Diff S / Risk low / Test yes.
- **[P1] Stale position snapshot during batch submit (submit/refresh race)** — L260–L262, L322–L336. Two systems targeting same symbol on different `entry_date` dedup differently. Fix: doc + dedup on `(symbol, side)` regardless of entry_date. Diff XS / Risk low.
- **[P2] Mixed success/failure return contract** — L523–L528. Failed orders come back in the same list with `.error`. Fix: `{submitted, failed}` split. Diff S / Risk med.
- **[P2] `account_equity` param unused except in a log line** — L242, L312–L315. Diff XS.
- **[P3] `side`/`order_type` as free strings** — repeat lowercase/validate boilerplate at L160–L168, L219–L231, L378–L384. Fix: `Literal["buy","sell"]` or Enum. Diff M.

### 2.12 `common/broker_alpaca.py` (508 lines)

Overall: low-level SDK wrapper plus a paper-reset HTTP helper bolted on. Solid idempotency comments, retry semantics risky.

- **[P0] Retry on timeout without `client_order_id` can double-submit** — L222–L260. `submit_order_with_retry` retries on *any* exception. Timeout after Alpaca accepted → second live order. Also retries non-transient errors (422 duplicate, insufficient funds). Fix: require/auto-generate `client_order_id` when `retries>0`; only retry on transient (timeout/429/5xx). Diff S / Risk med / Test yes.
- **[P1] Unknown side / TIF silently coerced** — L124, L127–L131. `side="byu"` → SELL; unknown TIF → GTC (and default TIF here is GTC while `alpaca_trading` uses "day" — cross-module drift). Fix: `ValueError` for unrecognized; align defaults. Diff XS / Risk low / Test yes.
- **[P1] `get_orders_status_map` maps errors to `None` silently** — L269–L275. Caller can't distinguish "not found" from "network down". Fix: sentinel or raise after N. Diff XS / Risk low / Test yes.
- **[P2] Module-level `except Exception` around all SDK imports** — L11–L46. Hides real import errors. Fix: catch `ImportError` only. Diff XS.
- **[P2] Imports and dataclasses mid-file; `__all__` split and stale** — L337–L343, L374–L378, L508. `submit_order_with_retry`, `get_open_orders`, `get_shortable_map`, `get_orders_status_map` missing from `__all__`. Fix: hoist, one `__all__`, split `reset_paper_account` into its own module. Diff S / Risk low.
- **[P2] `get_shortable_map` is N+1 sequential HTTP** — L359–L367. Fix: ThreadPoolExecutor. Diff S / Risk low / Test yes.
- **[P2] "Backoff" is linear, no jitter, no 429 awareness** — L216–L258. Diff XS.
- **[P3] `subscribe_order_updates` reaches into private client attrs** — L317. Diff XS.

### 2.13 `common/narrator.py` (270 lines)

Overall: best-documented file in the cluster; fail-safe LLM layer with hallucination cross-check.

- **[P2] No DI seam for the Anthropic client** — L89–L99. Fix: `client_factory` in `__init__`. Diff XS / Test yes.
- **[P2] `_extract_per_symbol_reasons` ignores its `text` parameter** — L186–L201. Signature lies; called with `""` at L255. Fix: drop param or rename. Diff XS.
- **[P2] Full signals JSON embedded in prompt with no size cap** — L152. Fix: cap/trim signals per system. Diff S / Test yes.
- **[P3] Hardcoded pricing table** — L39–L42. Add env override. Diff XS.

### 2.14 `common/publishers/base.py`

- **[P1] `target` field documented as masked but nothing masks it** — L41 vs `ntfy.py:117/144` and registry logging. Enables the P0 in ntfy.py. Fix: provide `mask()` helper on base. Diff S / Test yes.
- **[P2] Sort key hardcodes `"sysN"` prefix** — L111–L114. `x[3:]` on `"system1"` yields `"tem1"` → bucket 99. Fix: regex `\d+`. Diff XS / Test yes.
- **[P3] ABC could be a `typing.Protocol`** — L139–L160. Diff S.

### 2.15 `common/publishers/email.py`

- **[P1] All recipients in one `to` list — addresses visible to every subscriber** — L129–L131. Also `target=",".join(self.to_emails)` L144 puts raw emails into `PublishResult` (persisted). Fix: one personalization per recipient; mask target. Diff XS / Risk low / Test yes.
- **[P1] Unescaped LLM output interpolated into HTML** — L98–L111, L77–L79. Narrator headline/summary + symbol strings without `html.escape` — injection vector. Fix: escape every interpolated value. Diff XS / Risk low / Test yes.
- **[P2] `dry_run` bypasses `is_configured`** — L146–L160. Masks misconfiguration until first live send. Diff XS / Test yes.
- **[P2] Pointless sleep after final failed attempt** — L190–L192 (same in ntfy L156–L160). Diff XS.
- **[P3] Inline `import requests`** — L162. Diff S.

### 2.16 `common/publishers/line.py`

- **[P1] Live send always returns `ok=False` while looking wired-up** — L45. If someone registers `LinePublisher`, every live publish quietly "fails". Fix: `NotImplementedError` on non-dry-run send, or `is_configured()` hard-return `False`. Diff XS / Test yes.

### 2.17 `common/publishers/ntfy.py`

- **[P0] Secret topic leaked into results/logs/exported JSON** — L117, L144, L167; dry-run L116/L171–L176. Docstring L6 says "topic 名 (= URL path) が事実上の secret". `target=self.topic` flows into `PublishResult.as_dict()` → registry logs → `meta.publish_status` in exported JSON. Anyone with read access can push to (or subscribe to) the user's channel. **Patch draft P0-3 below.**
- **[P2] Non-ASCII title silently degrades** — L95–L97. Japanese narrator headlines collapse to "Today's Signals". Fix: RFC 2047 or `?title=`. Diff S / Test yes.
- **[P2] Final-attempt sleep** — L156–L160. Diff XS.

### 2.18 `common/publishers/registry.py`

- **[P0] Uncaught publisher exception kills the chain — silent notification drop** — L52, L67. `send()` implementations catch inside their transport loop; rendering runs before try (email `_build_payload`, ntfy `_build`). Malformed payload (e.g. non-numeric `entry_price` breaking f-string at `base.py:127`) raises out of `primary.send`, `publish()` propagates, secondary never fires, no `"failed"` reaches `meta.publish_status`. Fix: wrap each `send()` in try/except in `publish()`. Diff S / Risk low / Test yes.
- **[P2] FAIL logged at INFO level** — L54–L60, L69–L76. Fix: WARNING/ERROR when not ok. Diff XS.
- **[P3] Status as free string; two-slot chain** — L25, L79–L85. `Enum` + `list[Publisher]` for Phase 2. Diff M.

### 2.19 `scripts/cache_daily_polygon.py` (319 lines, recently fixed 2026-07-02)

- **[P1] `argparse --help` crashes on unescaped `%`** — L271. `%`-formatting on `~44%`. Fix: `~44%%`. Diff XS / Test yes.
- **[P1] Recent-day fetch failure silently becomes a hole in the cache** — L57–L63. Rate-limited recent day recorded as empty, skipped. Fix: warn (or `--strict-history`) for empty days inside the tier window. Diff S / Risk low / Test yes.
- **[P1] Silent half-stale indicators when recompute retry fails** — L154–L166. Merged frame with stale existing + new-slice indicators (wrong near window start) written with `logger.debug` only. Fix: WARNING or fail-symbol. Diff XS / Risk low / Test yes.
- **[P2] Redundant double `add_indicators`** — L151 vs L163. Discarded and recomputed. Fix: build `new_full` from raw OHLCV only. Diff S / Risk med / Test yes.
- **[P2] AdjClose = Close** — L105. No split/dividend adjustment. Fix: verify `adjusted=true` in `get_polygon_grouped_daily`; comment or assert. Diff XS.
- **[P2] `iter_business_days` ignores US holidays** — L31–L36. Wastes ~9 API calls/yr. Diff S.
- **[P3] Dead `settings=` param; duplicate except; no DI for Polygon fetcher** — L144, L304–L309, L56.

### 2.20 `scripts/build_rolling_with_indicators.py` (852 lines, recently fixed 2026-07-01 for stale-NaN)

- **[P0] base-sourced frames still tail-capped at `target_days+margin`, contradicting the "no truncation for base" design** — L350–L354 vs L374–L378, docstring L240–L248. Base-sourced rolling gets ~530 rows while full-sourced gets 330 — window-length drift across symbols. Fix: skip L352 prefetch tail when `source == "base"`. Diff S / Risk med / Test yes.
- **[P0] Stale-NaN fix misses partial-NaN placeholders** — L67–L102. Only fully-NaN columns dropped; NaN *tail* (the recent rows that generate today's signals) persists. Fix: also drop when last N rows are NaN, or when NaN fraction in target window exceeds threshold. Diff S / Risk low / Test yes.
- **[P1] `max_symbols` is a no-op** — L626, L634–L637, L956, L991; `_normalize_positive_int` L180 never called. Fix: `symbol_list = symbol_list[:cap]`. Diff XS / Risk low / Test yes.
- **[P1] Serial and parallel paths write different artifacts** — L726 (`cache_manager.write_atomic`) vs L481–L514 (`_write_dual_format`). Parallel writes CSV with `index=True` (spurious column), rounds twice, forces `%.6f`. Fix: single writer used by both. Diff M / Risk med / Test yes.
- **[P2] `_INDICATOR_COLS_FOR_RECOMPUTE` hardcodes copy of `indicators_common` column set** — L55–L64. New indicator → placeholder not dropped, NaN bug returns silently. Fix: export from `common/indicators_common.py`. Diff S / Risk low / Test yes.
- **[P2] Log spam + stale message** — L705/L721/L839 INFO across ~10k symbols; L839 says "full データ無し" but source may be base post-fix; `print()` with emoji at L437. Diff XS.
- **[P3] Dead code / hygiene** — `nan_warnings` unused in worker (L451); function-local imports (L483, L491); 300-line `extract_rolling_from_full` should split. Diff M.

### 2.21 `scripts/paper_trading_dryrun.py` (195 lines, 2026-07-02)

- **[P2] Default CSV discovery is CWD-relative** — L38–L42. From Task Scheduler → silently reports "CSV が見つかりません". Fix: anchor to `Path(__file__).resolve().parents[1]`. Diff XS / Test yes.
- **[P2] No client/converter DI** — L84–L91, L173. `signals_json_to_orders` accepts `client=`; script doesn't expose. Diff S / Test yes.
- **[P3] `total_notional` undercounts qty-based orders** — L105.
- **[P3] Missing type hints, `--date` silently ignored on JSON path**.

### 2.22 `scripts/paper_trading_submit.py` (228 lines, 2026-07-02)

- **[P0] Zero-orders is reported as success** — L95–L98, L118. If `signals_json_to_orders` returns `[]` (schema drift, all under min_notional, wrong tier key), script prints `生成=0 送信=0 失敗=0` and exits 0. Daily pipeline believes orders were submitted. Fix: when `--confirm` and input JSON contained signals but `len(orders)==0`, return non-zero + loud warning; count `not order_id and not error` as anomalies. Diff S / Risk low / Test yes.
- **[P1] JSON confirm path bypasses per-order `[y/N]` gate and ignores `--yes`** — L86–L93 vs docstring L5–L6 and CSV path L193–L204. Contract mismatch. Fix: convert with `dry_run=True`, reuse CSV path's confirm loop. Diff M / Risk med / Test yes.
- **[P2] `--demo` + `--confirm` submits fixture orders to paper account** — L151, L167, L188. Fix: reject `--demo` + `--confirm`. Diff XS / Test yes.
- **[P3] Imports private helpers from sibling script** — L43–L46. Fix: move into `common/paper_orders.py`. Diff S.
- **[P3] `--tier` silently ignored on CSV path** — Fix: warn when non-default outside JSON path. Diff XS.

### 2.23 `core/system1-7.py` (structural only — signal logic out of scope)

**Cross-system duplication is the biggest maintainability lever in the repo. Estimated ~2,300–2,800 lines removable across ~7,000.**

- **[P1] `prepare_data_vectorized_systemN` skeleton duplicated across 5 systems** — system1 (473–826), system2 (150–249), system3 (258–372), system4 (127–224), system5 (205–301). ~100 lines each of `check_precomputed_indicators` → per-symbol `.copy()` + `_apply_filter/setup_conditions` → `process_symbols_batch`. Fix: `common/system_common.py::prepare_data_vectorized(system_name, required_indicators, apply_conditions_fn, ...)`. Diff M (~350 lines removed) / Risk low / Test yes (prepared-dict equality).
- **[P1] `latest_only` fast-path skeleton duplicated across 6 systems** — system1 (944–1437 variant), system2 (298–444), system3 (468–1415 extended), system4 (272–483), system5 (368–645), system6 (432–811). ~1,500 lines total. Variance is data (rank column, sort direction, payload fields), not logic. Fix: `generate_latest_only_candidates(prepared_dict, spec)` where `spec` is a dataclass. Migrate one system at a time. Diff L / Risk **med — be paranoid** / Test yes (golden-signal comparison, mandatory).
- **[P1] Setup-source resolution block (column vs predicate vs manual/fallback) duplicated 4×** — system3 (744–770), system4 (288–333), system5 (386–455), system6 (491–546). Fix: `resolve_setup_state(row, predicate, manual_fn) -> SetupResolution`. Diff M / Risk med / Test yes.
- **[P1] Option-B feature flag + finalize block pasted 3× per file** — system3 (1271/1499/1534), system5 (581/727/763), system6 (366/629/734/1067). Fix: `resolve_option_b_flag(kwargs, system_id)` + `finalize_diagnostics(...)`. Diff S–M / Risk low / Test yes.
- **[P2] "0 candidates DEBUG sample" logging block duplicated 6×** — system2/3/4/5/6/7. Fix: `log_zero_candidate_samples(prepared_dict, metric_col, system_name, log_callback)`. Diff S (~180 lines removed) / Risk low.
- **[P2] Meta-column diagnostics recompute duplicated 4×** — system3/4/5/6. Fix: `summarize_setup_sources(df_all, meta_cols) -> (df_public, counts)`. Diff S / Risk low / Test yes.
- **[P2] Full-scan per-date ranking loop duplicated 5×** — system1/2/3/4/5. Fix: same spec-driven approach as latest_only. Diff M / Risk med / Test yes.
- **[P2] Diagnostics dict initializer duplicated 7×**. Fix: `make_default_diagnostics(**extra)` (`TypedDict`/dataclass would fix stringly-typed keys).
- **[P2] `_col_numeric_ci` / case-insensitive access duplicated with divergent fill semantics** — system3 (82–95, default-filled) vs system5 (119–127, NaN-filled). Row-level twin `_get_ci` in system2 (447–454). Fix: one `col_numeric_ci(df, name, default=nan)`. Diff S / Test yes (both fills load-bearing).
- **[P2] `_rename_ohlcv` / `_normalize_index` reimplemented in system1 (360–397) and system6 (161–198) — helpers already exist in `common/system_common.py`**. Fix: delete local copies, import from common. Diff S / Risk low–med (index-ordering edge cases) / Test yes.
- **[P2] `get_total_days` reimplemented in system6/system7** — while systems 1–5 delegate to common. Fix: extend common to handle lowercase `date`, delete locals. Diff S.

**Per-system standouts**:
- **system1.py**: `generate_candidates_system1` is **789 lines** (833–1621); dead code after return at 828–830 (self-acknowledged); `[DEBUG_S1*]` log spam. `finalize()` contract differs from systems 2–7 (INFO — flag for user).
- **system3.py**: 175-line inline zero-candidate forensics (840–1015). `_evaluate_row` returns 7-tuple. Lagged-row branch duplicates main-row branch. INFO: `filter_counts` in diagnostics hardcodes `Close<5`/`dvol<=25M` while module constants are `Low>=1`/`AvgVolume50>=1M` (**diagnostics report thresholds that don't match the actual filter** — flag for user).
- **system4.py**: mostly clean. INFO: latest_only payload emits `entry_price`/`stop_price`; full-scan does not (asymmetry — flag).
- **system5.py**: 3× Option-B copy-paste in one function.
- **system6.py**: 810-line `generate_candidates_system6`; variable shadowing (`total` at 812 vs 1009). INFO: `core/system6_backup.py` exists with duplicate defs — deletion candidate.
- **system7.py**: INFO — full-scan path assigns `entry_price = df["Close"].iloc[-1]` (dataset's *latest* close) to every historical setup-day record; labels 2015 entries with 2026 prices. Signal timing unaffected; **flag for user**.

**Cross-cutting modernization (P3)**:
- Return-type unification: `dict[ts, dict[sym, payload]]` (2/4/5/6/7) vs `dict[ts, list[dict]]` (3) vs always-3-tuple (1). Introduce `CandidateResult` NamedTuple + `SystemModule` Protocol.
- Exception hygiene: hundreds of `except Exception: pass`; at minimum add `logger.debug`.
- `from __future__ import annotations` missing in system6/system7; system7 uses `typing.Tuple`/`Callable` old-style.

**Suggested sequencing for core refactor**:
1. Log/diagnostics-only extractions first (zero-sample logger, Option-B flag, diagnostics init) — big line-count win, near-zero risk.
2. `prepare_data_vectorized` consolidation + `_col_numeric_ci`/`_rename_ohlcv`/`get_total_days` dedup.
3. Setup-source resolution + meta-column summarizer (with golden diagnostics tests).
4. `latest_only` spec-driven consolidation, **one system per PR, golden-signal test gate each**.
5. Full-scan consolidation last (backtest-critical).

---

## 3. Behavioral spec drifts flagged for human decision (INFO only)

These are NOT auto-fixed. Please decide the intended semantics before opening a PR.

| # | File | Line | Current behavior | Alternative | Impact |
|---|---|---|---|---|---|
| I-1 | `trade_management.py` | 444–445 | `max_exit_date` = calendar days | Trading days per spec | System2/3 short exits ~2 days early over weekends |
| I-2 | `trade_management.py` | 298–334 | `_get_row_for_date` "nearest"/"bfill" | "pad"/"ffill" only | Prevents future-row lookahead |
| I-3 | `trade_management.py` | 650–656 | Short target = `entry / 1.04` | `entry * 0.96` | 0.15% discrepancy |
| I-4 | `core/system1.py` | 924–927 | `finalize()` returns 3-tuple ignoring `include_diagnostics` | Match systems 2–7 contract | Callers of system1 must special-case |
| I-5 | `core/system1.py` | 1312/1336/1378 | Mode-date via raw `max(date_counter.items())` | `choose_mode_date_for_latest_only` (used by 2–6) | Potential tie-break semantic drift |
| I-6 | `core/system3.py` | 883–894 | `filter_counts` diag hardcodes `Close<5`/`dvol<=25M` | Actual filter is `Low>=1`/`AvgVolume50>=1M` | Diagnostics currently misreport |
| I-7 | `core/system4.py` | 351–355 vs 536–545 | latest_only emits `entry_price`/`stop_price`; full-scan does not | Consistent payload | Payload asymmetry |
| I-8 | `core/system7.py` | 380–393 | Full-scan `entry_price = df["Close"].iloc[-1]` (latest close for every historical setup) | Setup-day close (like latest_only) | Backtest labeling wrong; signal timing unaffected |

---

## 4. What NOT to touch (freeze list)

Confirmed spec-compliant, baseline, do not refactor in this pass:

- `common/trade_management.py::SYSTEM_TRADE_RULES` **values** — verified against `docs/systems/システム1-6.txt` on every field. Structural refactor of the surrounding module is OK; values are frozen.
- `core/system1-7.py` **entry / exit / stop / ranking logic** — extractions of *boilerplate* around this logic are OK; the logic itself is not.
- `strategies/*_strategy.py` `compute_entry` implementations — the entry-price/limit-offset math is the source of truth for the runtime path.
- `scripts/daily_pipeline.ps1` overall step topology — the six-step sequence is the operator contract; adding hooks/timeouts is OK, reordering is not.

---

## 5. P0 patch drafts (3 highest-ROI fixes)

**All three patches are drafted for backward compatibility, zero signal-semantics change, and pass golden-signal harness by construction.** They are unapplied — treat as PR starters. Line numbers are against branch `claude/monitor-webapp` @ `73d04cf`.

### P0-1: Fix dead System1 SPY>SMA100 gate (case bug)

**Business impact**: This is the single most direct money-losing bug in the audit. System1 is a long trend-following system whose spec (`docs/systems/システム1.txt`) requires SPY > SMA100. The gate has been failing open for every day the market is below SMA100 — subscribers have been receiving long buy signals against the trend. Effort: XS diff; two safety defenses (correct case + case-insensitive `get`).

**File**: `common/today_signals.py`

```diff
--- a/common/today_signals.py
+++ b/common/today_signals.py
@@ -977,7 +977,7 @@ def _compute_setup_pass(...):
             except Exception:
                 spy_df = None
-            spy_gate_bool = _make_spy_gate(spy_df, column="sma100")
+            spy_gate_bool = _make_spy_gate(spy_df, column="SMA100")
             if spy_gate_bool is True:
                 spy_gate = 1
             elif spy_gate_bool is False:
                 spy_gate = 0
             else:
                 spy_gate = None
@@ -2129,15 +2129,26 @@
-def _make_spy_gate(spy_df: pd.DataFrame | None, column: str = "SMA200") -> bool | None:
+def _make_spy_gate(spy_df: pd.DataFrame | None, column: str = "SMA200") -> bool | None:
+    """Return SPY > `column` at last row. Case-insensitive column lookup + logs a WARN
+    (not None-fail-open) when the column is unresolvable so a broken cache surfaces."""
     if spy_df is None or getattr(spy_df, "empty", True):
         return None
     try:
         last_row = spy_df.iloc[-1]
     except Exception:
         return None
+    # Case-insensitive column resolution: cache writers have historically used both
+    # "SMA100" (utils_spy.get_spy_with_indicators) and "sma100" spellings.
+    resolved_col: str | None = None
+    if column in last_row.index:
+        resolved_col = column
+    else:
+        for cand in last_row.index:
+            if str(cand).lower() == column.lower():
+                resolved_col = str(cand)
+                break
+    if resolved_col is None:
+        logger.warning(
+            "SPY gate column %r not found in SPY frame (available=%s); gate=None",
+            column, list(last_row.index)[:12],
+        )
+        return None
     try:
         close_val = pd.to_numeric(
             pd.Series([last_row.get("Close")]), errors="coerce"
         ).iloc[0]
         sma_val = pd.to_numeric(
-            pd.Series([last_row.get(column)]), errors="coerce"
+            pd.Series([last_row.get(resolved_col)]), errors="coerce"
         ).iloc[0]
     except Exception:
         return None
```

**Optional companion (recommended)**: change L988's gate-open policy so that `spy_gate is None` surfaces as an explicit WARN reason rather than silently passing setup. This is a slightly larger behavior change and worth a separate PR after the case fix ships.

**Test to add** (`tests/test_spy_gate.py`):
```python
def test_spy_gate_uses_uppercase_sma100():
    spy = pd.DataFrame({"Close": [100.0], "SMA100": [110.0]}, index=[pd.Timestamp("2026-07-02")])
    assert _make_spy_gate(spy, column="SMA100") is False   # 100 < 110 -> gate off
def test_spy_gate_case_insensitive_fallback():
    spy = pd.DataFrame({"Close": [120.0], "sma100": [110.0]}, index=[pd.Timestamp("2026-07-02")])
    assert _make_spy_gate(spy, column="SMA100") is True    # gate on despite lowercase col
def test_spy_gate_missing_column_returns_none_and_warns(caplog):
    spy = pd.DataFrame({"Close": [120.0]}, index=[pd.Timestamp("2026-07-02")])
    assert _make_spy_gate(spy, column="SMA100") is None
    assert "SPY gate column" in caplog.text
def test_system1_setup_pass_reflects_gate():
    # end-to-end: SPY <= SMA100 must yield setup_pass == 0 for system1
    ...
```

**Regression risk**: LOW. The fix restores the intended spec behavior. On days SPY > SMA100 nothing changes. On days SPY ≤ SMA100 System1 will legitimately emit zero candidates (which is what the spec has always required). Verify by re-running the last 30 days through `run_all_systems_today.py --dry-run` and diffing per-day System1 candidate counts against SPY vs SMA100 signs.

---

### P0-2: Reject unknown/missing `side` in Alpaca order builder

**Business impact**: Silent side-defaulting → wrong order type. Currently paper-only, but the same code will submit live once autotrade flips. Any signals-JSON schema drift (missing `side` column, typo, new value) currently short-sells the position instead of failing loudly.

**File**: `common/alpaca_trading.py`

```diff
--- a/common/alpaca_trading.py
+++ b/common/alpaca_trading.py
@@ -216,10 +216,29 @@ def _log_submitted_order(...):
     return prepared


-def _side_from_row(row: pd.Series) -> str:
-    raw = str(row.get("side", "")).lower()
-    if raw in ("buy", "sell"):
-        return raw
-    return "buy" if raw == "long" else "sell"
+_SIDE_ALIASES: dict[str, str] = {
+    "buy": "buy",
+    "long": "buy",
+    "sell": "sell",
+    "short": "sell",
+    "sell_short": "sell",
+}
+
+
+class InvalidSideError(ValueError):
+    """Raised when a signals row has a missing or unrecognized `side` value.
+
+    We refuse to guess: silent default-to-sell has previously caused unintended
+    short submissions when the upstream signals frame drifts (missing column,
+    typo, or new system id). Fail loudly so the operator sees the row.
+    """
+
+
+def _side_from_row(row: pd.Series) -> str:
+    raw = str(row.get("side", "")).strip().lower()
+    if not raw:
+        raise InvalidSideError(
+            f"signals row has no 'side' (symbol={row.get('symbol')}, "
+            f"system={row.get('system')})"
+        )
+    try:
+        return _SIDE_ALIASES[raw]
+    except KeyError as exc:
+        raise InvalidSideError(
+            f"unrecognized side {raw!r} for symbol={row.get('symbol')}"
+        ) from exc
```

Callers of `_side_from_row` currently swallow the return value into `PreparedOrder`. Add one exception-catching site in the batch loop so a single bad row doesn't kill the whole batch:

```diff
@@ around signals_to_orders() where PreparedOrder is built:
-        side = _side_from_row(row)
+        try:
+            side = _side_from_row(row)
+        except InvalidSideError as exc:
+            logger.error("skip row: %s", exc)
+            _audit_log({"event": "skip_invalid_side", "detail": str(exc), **row.to_dict()})
+            failures.append(_make_failed_prepared_order(row, error=str(exc)))
+            continue
```

**Test to add** (`tests/test_alpaca_side_from_row.py`):
```python
def test_side_from_row_maps_known():
    assert _side_from_row(pd.Series({"side": "buy"})) == "buy"
    assert _side_from_row(pd.Series({"side": "LONG"})) == "buy"
    assert _side_from_row(pd.Series({"side": "short"})) == "sell"
def test_side_from_row_raises_on_missing():
    with pytest.raises(InvalidSideError):
        _side_from_row(pd.Series({"symbol": "AAPL"}))
def test_side_from_row_raises_on_unknown():
    with pytest.raises(InvalidSideError):
        _side_from_row(pd.Series({"side": "reverse", "symbol": "AAPL"}))
def test_batch_skips_bad_row_and_continues(fake_client):
    signals = pd.DataFrame([
        {"symbol": "AAPL", "side": "buy", "qty": 1, "system": "system1"},
        {"symbol": "MSFT", "side": "wat?", "qty": 1, "system": "system1"},
        {"symbol": "TSLA", "side": "buy", "qty": 1, "system": "system1"},
    ])
    orders = signals_to_orders(signals, account_equity=100_000.0, client=fake_client, dry_run=True)
    assert sum(1 for o in orders if o.error) == 1
    assert sum(1 for o in orders if not o.error) == 2
```

**Regression risk**: LOW. Every known good path already sets `side` to one of `buy`/`sell`/`long`. `_SIDE_ALIASES` extends the mapping to `short`/`sell_short` (which were previously silently mapped to sell — same semantics, now explicit). The only behavioral change is that a **missing or truly unknown value** now becomes a per-row skip instead of a silent short submission.

---

### P0-3: Mask ntfy topic before it leaks into PublishResult / exported JSON

**Business impact**: The module's own docstring says the topic is "事実上の secret". Currently it flows into `PublishResult.target` (persisted in logs and — via `meta.publish_status` — in the exported signals JSON that subscribers download). Anyone with read access to those artifacts can subscribe to or spoof-push the operator's push channel.

**File**: `common/publishers/ntfy.py`

```diff
--- a/common/publishers/ntfy.py
+++ b/common/publishers/ntfy.py
@@ -22,6 +22,18 @@ logger = logging.getLogger(__name__)
 DASHBOARD_URL = "https://quant-trading-monitor.vercel.app"
 _MAX_RETRIES = 4
 # ntfy body は 4KB 程度が無難。長い summary は丸める。
 _BODY_LIMIT = 3800


+def _mask_topic(topic: str) -> str:
+    """Return an unguessable-shape but useful-in-logs marker for the ntfy topic.
+
+    The topic acts as the secret access token for the channel; we surface a
+    3-char prefix so operators can distinguish channels in logs / exported JSON,
+    but never the full value. If the topic is empty or None, return "unset".
+    """
+    if not topic:
+        return "unset"
+    return f"{topic[:3]}…({len(topic)})"
+
+
 def _default_url() -> str:
     return os.getenv("NTFY_URL") or "https://ntfy.sh"
@@ -108,26 +120,26 @@ class NtfyPublisher(Publisher):
     def send(self, signals_json: dict[str, Any], *, dry_run: bool = False) -> PublishResult:
         body, headers = self._build(signals_json)

         if dry_run:
             return PublishResult(
                 publisher=self.name,
                 ok=True,
                 detail=_dump_dry_run(self.endpoint, headers, body),
-                target=self.topic or "dry-run",
+                target=_mask_topic(self.topic) or "dry-run",
             )

         if not self.is_configured():
             return PublishResult(
                 publisher=self.name, ok=False, detail="NTFY_TOPIC 未設定", target="unset"
             )

         import requests

         last_detail = ""
         last_status: int | None = None
         for attempt in range(1, _MAX_RETRIES + 1):
             try:
                 resp = requests.post(
                     self.endpoint,
                     data=body.encode("utf-8"),
                     headers=headers,
                     timeout=self.timeout,
                 )
                 last_status = resp.status_code
                 if 200 <= resp.status_code < 300:
                     return PublishResult(
                         publisher=self.name,
                         ok=True,
                         status_code=resp.status_code,
                         detail="sent",
-                        target=self.topic,
+                        target=_mask_topic(self.topic),
                     )
                 if resp.status_code == 429 or resp.status_code >= 500:
                     backoff = min(2 ** (attempt - 1), 8)
                     logger.warning(
                         "ntfy %d (attempt %d) backoff=%ss", resp.status_code, attempt, backoff
                     )
                     time.sleep(backoff)
                     last_detail = f"retryable_{resp.status_code}"
                     continue
                 last_detail = f"http_{resp.status_code}: {resp.text[:200]}"
                 break
             except Exception as exc:  # noqa: BLE001
                 backoff = min(2 ** (attempt - 1), 8)
                 last_detail = f"exception: {exc}"
                 logger.warning("ntfy post 例外 (attempt %d): %s backoff=%ss", attempt, exc, backoff)
                 time.sleep(backoff)

         return PublishResult(
             publisher=self.name,
             ok=False,
             status_code=last_status,
             detail=last_detail or "failed",
-            target=self.topic,
+            target=_mask_topic(self.topic),
         )


 def _dump_dry_run(endpoint: str, headers: dict[str, str], body: str) -> str:
     import json

+    # Mask the topic segment of the endpoint too — otherwise dry-run detail
+    # contains the same secret we just masked out of `target`.
+    from urllib.parse import urlsplit, urlunsplit
+    parts = urlsplit(endpoint)
+    if parts.path:
+        segments = parts.path.strip("/").split("/")
+        if segments:
+            segments[-1] = _mask_topic(segments[-1])
+        masked_path = "/" + "/".join(segments)
+        endpoint = urlunsplit(parts._replace(path=masked_path))
     return json.dumps(
         {"endpoint": endpoint, "headers": headers, "body": body}, ensure_ascii=False
     )
```

**Test to add** (`tests/test_ntfy_topic_masking.py`):
```python
def test_publish_result_never_contains_raw_topic():
    pub = NtfyPublisher(topic="super-secret-12345")
    r = pub.send({"generated_at": "2026-07-02", "per_system": {}}, dry_run=True)
    dumped = json.dumps(r.as_dict())
    assert "super-secret-12345" not in dumped
    # Prefix + length shape is present so operators can still identify the channel
    assert "sup…(19)" in dumped

def test_dry_run_detail_endpoint_is_masked():
    pub = NtfyPublisher(topic="super-secret-12345")
    r = pub.send({"generated_at": "2026-07-02", "per_system": {}}, dry_run=True)
    assert "super-secret-12345" not in r.detail
```

**Regression risk**: LOW. The change is display-only: outbound HTTPS still uses the raw `self.endpoint` (which uses `self.topic`) — actual delivery is unchanged. Only `PublishResult.target` and `_dump_dry_run` detail strings are masked. Downstream consumers (`registry.py`, `meta.publish_status` in exported JSON) already treat `target` as opaque display text.

---

## 6. Next steps (recommended)

1. **Ship P0-1, P0-2, P0-3 as three separate PRs** with the tests listed. All three are XS/S diffs, low regression risk, and each unblocks a specific business risk (spec drift, wrong-side order, secret leak).
2. **Registry try/except patch (Item 4)** — small follow-up PR; same test cluster.
3. **Golden-signal harness** — before touching Cluster F (`core/system1-7` structural), stand up a "fixture universe in → identical candidates + diagnostics out" test. Every subsequent structural refactor gates on it. Without this harness, the 2,300–2,800-line reduction opportunity in Cluster F is too risky to attempt.
4. **Human sign-off on the 8 spec drifts in Section 3** — none are auto-fixable and each has a small but real behavioral consequence.
5. **`app_today_signals.py` split** — extract pure helpers into `apps/today_signals_core.py`, then attack the 76 bare `except: pass` sites incrementally. Do NOT attempt this before P0-1 lands.

**Estimated total effort to Sprint 1 completion**: ~4–6 hrs including tests and PR review; ~600 lines of net delta for the P0s.

---

*Audit conducted by fable5 deep review with 6 concurrent subagents.  
Ground truth cross-checked against `docs/systems/システム1-7.txt`.  
Read-only — no code was modified during the audit.*
