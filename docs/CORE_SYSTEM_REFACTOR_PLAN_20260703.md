# CORE / system1-7 refactor plan

**Author**: fable5 refactor prep dispatch (Claude, 2026-07-03)
**Branch baseline**: `claude/monitor-webapp @ b7ffad1`
**Audit reference**: `docs/REFACTOR_AUDIT_20260702_fable5.md` (items 21–25, Cluster F)
**Regression harness**: `scripts/golden_signal_harness.py` + `tests/golden_signals/20260701.json` + `tests/system/test_golden_signals_match.py` + `tests/system/test_core_system_public_api_stability.py`

> **Status**: **plan only — no code refactor performed**. This document is the
> pre-work for the big-ticket "Cluster F" refactor identified by the fable5
> audit. Actual refactor execution requires user sign-off and lands in a
> separate dispatch, one system per PR, each PR gated on golden-signal
> parity.

---

## 0. TL;DR — the business case

The fable5 audit measured 25 findings against ROI. The three P0 patches
(items 1–3) are direct money-losing bugs; those ship first in a separate
dispatch. **This plan targets items 21–23** (the maintainability trio in
Cluster F), which together carry ~2,300–2,800 removable lines out of ~7,029
in `core/system1-7.py`.

Why this matters commercially:

1. Every future spec change (docs/systems/システム1-6.txt) currently has to
   be re-implemented **six times** — the four setup/filter thresholds are
   already hardcoded in `common/today_filters.py` and `common/today_signals.py`
   in addition to `core/systemN`, and the boundary operator `>=` vs `>` has
   already drifted (audit finding B, items #9–10). Every duplication is a
   silent-drift surface that will burn subscribers once we're at scale.
2. The `latest_only` fast path (which is what the daily pipeline actually
   runs; full-scan is only used in nightly backtests) is duplicated in six
   systems with ~1,500 lines of near-identical scaffolding. A single bug in
   ranking-tie handling has to be discovered and re-patched six times.
3. Adding an 8th system (or splitting an existing system into two
   sub-strategies for ETF vs equity) currently means copying an 800+ line
   `generate_candidates_systemN` skeleton. With the utility extraction below
   the delta is under 200 lines.

**Estimated reduction (measured, see §5)**: **2,150–2,500 lines** across
`core/system1-7.py`. Below the audit's stated 2,800 upper bound but firmly
inside its 2,300 lower bound and above the "worth the effort" threshold
(any refactor removing <1,000 lines from a critical-path module doesn't
pay back the review cost).

---

## 1. Measured code map

Function inventory taken from `core/*.py` at `b7ffad1`. Long tail:

| System | Total | prep_data_vectorized | generate_candidates | latest_only block(s) | filter+setup helpers |
|--------|------:|---------------------:|--------------------:|---------------------:|---------------------:|
| system1 | 1,711 | 354 | 789 | 704 | 94 |
| system2 |   596 | 100 | 325 | 148 | 53 |
| system3 | 1,595 | 115 | 1,202 | 949 | 80 |
| system4 |   611 |  98 | 365 | 213 | 28 |
| system5 |   878 |  97 | 510 | 279 | 94 |
| system6 | 1,164 |  53 | 811 | 380 | 61 |
| system7 |   474 | 145 | 260 | 112 | 0 |
| **total** | **7,029** | **962** | **4,262** | **2,785** | **410** |

`prepare_data_vectorized_*` alone = 962 lines. `generate_candidates_*` = 4,262
lines. That's 74 % of `core/` in two function skeletons, both largely
duplicated boilerplate around ~200 lines of actual signal logic per system.

**Working-tree caveat**: at the time of writing, `core/system5.py` in the
working copy is truncated to 791 lines (HEAD is 878 lines). This appears to
be a Linux-side mount artefact and does NOT affect the file that will be
committed from Windows. Numbers above use the git-HEAD version.

---

## 2. Cross-system duplication matrix

Compiled from the audit's Cluster-F findings, cross-checked against direct
`ast` inspection of each file.

Legend: ▲ high dup (near-identical), ▽ partial dup (semantics diverge),
◇ dup with divergent operator, — not applicable.

| Duplication site | s1 | s2 | s3 | s4 | s5 | s6 | s7 | Total est. |
|---|:-:|:-:|:-:|:-:|:-:|:-:|:-:|:-:|
| `prepare_data_vectorized_systemN` skeleton (check_precomputed_indicators → per-sym copy → apply filter/setup → process_symbols_batch) | ▲ | ▲ | ▲ | ▲ | ▲ | ▲ | ▽ | **~350 lines removable** |
| `latest_only` fast-path skeleton (validate → last-row scan → ranking → payload assembly → diagnostics finalize) | ▲ | ▲ | ▲ | ▲ | ▲ | ▲ | ▽ | **~1,500 lines removable** |
| Option-B feature flag + `finalize_ranking_and_diagnostics` pasted 3× per file (system3/5/6) | — | — | ▲ | — | ▲ | ▲ | — | ~300 lines removable |
| Setup-source resolution block (column vs predicate vs manual fallback) | — | — | ▲ | ▲ | ▲ | ▲ | — | ~200 lines removable |
| "0 candidates DEBUG sample" logging block | — | ▲ | ▲ | ▲ | ▲ | ▲ | ▲ | ~180 lines removable |
| Meta-column diagnostics recompute (`setup_predicate_count` split) | — | — | ▲ | ▲ | ▲ | ▲ | — | ~120 lines removable |
| Full-scan per-date ranking loop | ▲ | ▲ | ▲ | ▲ | ▲ | — | — | ~250 lines removable |
| Diagnostics dict initializer | ▲ | ▲ | ▲ | ▲ | ▲ | ▲ | ▲ | ~70 lines removable |
| `_col_numeric_ci` case-insensitive lookup (divergent fill semantics) | — | — | ◇ | — | ◇ | — | — | ~30 lines removable |
| `_rename_ohlcv` / `_normalize_index` reimplementation of `common/system_common` helpers | ▲ | — | — | — | — | ▲ | — | ~40 lines removable |
| `get_total_days_systemN` local reimplementation (systems 6, 7) | — | — | — | — | — | ▽ | ▽ | ~20 lines removable |
| **Estimated total removable** | | | | | | | | **~3,060 line gross → ~2,150–2,500 net** after utility fn body added |

Net calculation: gross duplication removal offset by the ~500–900 lines added
in `common/system_common.py` (shared utilities). Golden signal parity gated
per system.

---

## 3. Extraction targets — recommended design

**Recommendation**: **utility functions in `common/system_common.py` + one
spec dataclass**, not a base class or mixin hierarchy.

Reasoning:

- The seven systems have divergent enough return-type contracts (system1
  returns 3-tuple mode; system6 full-scan returns `df=None`; system3 uses
  `list[dict]` payload vs others' `dict[sym, payload]`) that a common base
  class would need `Any` return types and defeat static analysis.
- A dataclass `SystemSpec` (rank column, direction, top-n default, payload
  fields, `latest_only` semantics enum) cleanly captures the *actual* data
  variance between systems.
- Utility functions are trivially testable in isolation with fixtures
  identical to those already in the golden harness.
- No metaclass magic; git blame remains readable per-system.

Proposed new module surface (in `common/system_common.py` unless noted):

```python
# --- Data preparation ---
def prepare_data_vectorized(
    system_name: str,
    raw_data_dict: dict[str, pd.DataFrame],
    required_indicators: Sequence[str],
    apply_filter_fn: Callable[[pd.DataFrame], pd.DataFrame],
    apply_setup_fn: Callable[[pd.DataFrame], pd.DataFrame],
    *,
    reuse_indicators: bool = False,
    symbols: list[str] | None = None,
    batch_size: int | None = None,
    use_process_pool: bool = False,
    max_workers: int | None = None,
    progress_callback: Callable[[str], None] | None = None,
    log_callback: Callable[[str], None] | None = None,
    skip_callback: Callable[[str, str], None] | None = None,
) -> dict[str, pd.DataFrame]: ...

# --- latest_only fast-path ---
@dataclass(slots=True, frozen=True)
class LatestOnlySpec:
    system_name: str
    rank_col: str
    rank_direction: Literal["asc", "desc"]
    top_n_default: int
    exclusion_predicate: Callable[[pd.Series], bool] | None = None  # e.g. rsi4<30
    payload_fields: tuple[str, ...] = ("close",)                    # extra cols
    entry_price_fn: Callable[[pd.Series], float] | None = None
    stop_price_fn: Callable[[pd.Series], float] | None = None

def generate_latest_only_candidates(
    prepared_dict: dict[str, pd.DataFrame],
    spec: LatestOnlySpec,
    *,
    top_n: int | None = None,
    diagnostics: dict | None = None,
) -> tuple[dict, pd.DataFrame | None, dict]: ...

# --- Setup source resolution ---
@dataclass(slots=True)
class SetupResolution:
    passed: bool
    source: Literal["setup_column", "predicate", "manual", "fallback"]
    reason: str | None = None

def resolve_setup_state(
    row: pd.Series,
    predicate: Callable[[pd.Series], bool],
    manual_fn: Callable[[pd.Series], bool] | None,
) -> SetupResolution: ...

# --- Option-B finalize ---
def resolve_option_b_flag(kwargs: Mapping[str, Any], system_id: int) -> bool: ...
def finalize_diagnostics(diagnostics: dict, *, mode: str, top_n: int) -> dict: ...

# --- Zero-candidate diagnostics ---
def log_zero_candidate_samples(
    prepared_dict: dict[str, pd.DataFrame],
    metric_col: str,
    system_name: str,
    log_callback: Callable[[str], None] | None,
) -> None: ...

# --- Meta-column summarizer ---
def summarize_setup_sources(
    df_all: pd.DataFrame, meta_cols: Sequence[str]
) -> tuple[pd.DataFrame, dict[str, int]]: ...

# --- Numeric case-insensitive column access ---
def col_numeric_ci(
    df: pd.DataFrame, name: str, default: float = float("nan")
) -> pd.Series: ...
```

Each system's `generate_candidates_systemN` becomes ~150–250 lines of
thin dispatch:

```python
_SPEC = LatestOnlySpec(
    system_name="system1",
    rank_col="roc200",
    rank_direction="desc",
    top_n_default=DEFAULT_TOP_N,
    payload_fields=("close", "sma200", "roc200"),
)

def generate_candidates_system1(prepared_dict, *, top_n=None, latest_only=False, ...):
    if latest_only:
        return generate_latest_only_candidates(prepared_dict, _SPEC, top_n=top_n, ...)
    return _generate_full_scan_system1(prepared_dict, top_n, ...)  # residual logic
```

---

## 4. Phased migration plan

Each phase is one PR. Every PR ends with `pytest tests/system/test_golden_signals_match.py` **green** — that's the release gate. Golden signal harness runs deterministically on synthetic fixtures (no market data required) so it works in any CI environment.

### Phase A — infrastructure (this dispatch; already done)

- ✅ Golden signal regression harness (`scripts/golden_signal_harness.py`)
- ✅ Fixed-date golden JSON (`tests/golden_signals/20260701.json`)
- ✅ pytest gate (`tests/system/test_golden_signals_match.py`)
- ✅ Public API stability gate (`tests/system/test_core_system_public_api_stability.py`)
- ✅ Function inventory (§1 above) + duplication matrix (§2)

### Phase B — zero-risk log/diagnostics extraction (PR 1)

**Target: -300 lines gross, +80 lines in common. Risk: LOW.**

- Extract `log_zero_candidate_samples` from system2/3/4/5/6/7 (audit item ~2.3.5)
- Extract `resolve_option_b_flag` + `finalize_diagnostics` from system3/5/6
- Extract diagnostics-dict initializer helper
- Update `test_core_system_public_api_stability.py::EXPECTED` if any private
  helper name changes (rare — most extractions are body-only)

No signal semantics touched. Golden harness gate is a pure regression check.

### Phase C — data-prep skeleton consolidation (PR 2)

**Target: -350 lines gross, +150 lines in common. Risk: LOW-MED.**

- Introduce `prepare_data_vectorized(system_name, required_indicators, apply_fn_pair, ...)` in `common/system_common.py`
- Migrate systems 4 → 2 → 5 → 1 → 3 → 6 in that order (least to most complex)
- system7 stays independent — SPY-only path has different semantics
- Delete `_rename_ohlcv` / `_normalize_index` duplicates in system1/system6; use `common/system_common` counterparts
- Update `test_core_system_public_api_stability.py::EXPECTED` — expected param list becomes `raw_data_dict`, `apply_filter_fn`, `apply_setup_fn`, plus the shared kwargs

Golden harness will exercise `prepared_dict` equality via `generate_candidates_*` (which consumes the output of `prepare_data_vectorized_*`).

### Phase D — setup source resolution + meta-column summarizer (PR 3)

**Target: -320 lines gross, +150 in common. Risk: MED.**

- Extract `resolve_setup_state(row, predicate, manual_fn)` used by system3/4/5/6
- Extract `summarize_setup_sources(df_all, meta_cols)` used by system3/4/5/6
- Fix audit finding I-6 (system3 `filter_counts` diagnostic mismatch) — INFO-flagged, needs user sign-off. **Do not fix silently in this phase.**
- Extract `col_numeric_ci` from system3 and system5 — **preserve divergent fill semantics** by passing `default=` explicitly per callsite (system3 wants filled default, system5 wants NaN).

### Phase E — `latest_only` fast-path consolidation (PRs 4–9, one per system)

**Target: -1,500 lines gross, +500 in common. Risk: HIGH (this is where signal parity is most vulnerable). Golden gate is MANDATORY per PR.**

Sequencing (ascending complexity):

1. **PR 4 — system2** (short mean-reversion, ADX7-desc ranking, 148 latest_only lines) — smallest and most linear
2. **PR 5 — system4** (long trend, RSI4-asc ranking with rsi4<30 gate, 213 lines)
3. **PR 6 — system5** (long high-ADX, ADX7-desc, 279 lines, Option-B tangles)
4. **PR 7 — system6** (short breakout, return_6d-desc, 380 lines, `latest_mode_date` param) — **special care**: fixture-observed diagnostics inconsistency (see harness note)
5. **PR 8 — system3** (long pullback, drop3d-desc, 949 lines — largest; note payload shape difference `list[dict]` vs `dict[sym, payload]`)
6. **PR 9 — system1** (long trend, ROC200-desc, 704 lines, `diagnostics` kwarg reuse)
7. **PR 10 — system7** (SPY-only 50-day break, no ranking) — trivial after the pattern is set

Each PR is atomic: introduce spec dataclass, wire up shared helper, delete
old inline block, verify golden. If golden diverges on a system, revert
that single PR — the rest of the branch ships.

### Phase F — full-scan consolidation (PR 11)

**Target: -250 lines gross, +100 in common. Risk: MED.**

- Extract per-date ranking loop used by system1/2/3/4/5
- Backtest-critical (nightly full-scan runs); golden covers full_scan mode already

### Phase G — cleanup + drift prevention (PR 12)

- Delete `core/system6_backup.py` (audit-flagged deletion candidate)
- Wire threshold constants from `core/systemN` into `common/today_filters.py` and `common/today_signals.py._compute_setup_pass` (fixes audit finding B item #9)
- Extend public API stability test to cover the new `common/system_common.py` public surface

---

## 5. Risk assessment

| # | Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|---|
| R-1 | latest_only refactor changes a symbol's inclusion boundary in production | MED | HIGH — subscribers see different daily signals | Golden harness gate; one-system-per-PR; each PR reviewed against `docs/systems/システム1-6.txt`; abort-and-revert on any diff |
| R-2 | Full-scan refactor changes historical backtest labeling | LOW | MED — backtest metrics shift | Golden harness covers full_scan mode explicitly; regenerate golden only after user sign-off |
| R-3 | Option-B flag consolidation changes which internal path executes | MED | LOW — diagnostics-only, candidates unchanged | Golden harness intentionally excludes diagnostic-only counters from hash (see `_hashable` docstring in harness) |
| R-4 | `col_numeric_ci` fill-default drift breaks system3 or system5 | MED | HIGH — silent wrong-price rows | Preserve per-callsite `default=` explicitly during migration; add per-system unit test asserting fill behavior |
| R-5 | Public API signature change breaks `apps/`, `scripts/`, `strategies/`, other tests | HIGH if uncontrolled | HIGH | `tests/system/test_core_system_public_api_stability.py` fails-fast on any signature drift; update `EXPECTED` in the same PR |
| R-6 | Working-tree corruption of `core/system5.py` (currently truncated to 791/878 lines in the sandbox mount) delays PR-6 | LOW | LOW | Refactor executes on Windows-side clean checkout; sandbox truncation is display-only and does not affect commits |
| R-7 | Refactored code accidentally changes `SYSTEM_TRADE_RULES` semantics via strategy imports | LOW | HIGH | `common/trade_management.py::SYSTEM_TRADE_RULES` is in audit's freeze list; no touch permitted; enforce via CODEOWNERS review |

---

## 6. Line-count evidence backing audit's 2,300–2,800 range

Direct measurement (this dispatch):

| Duplication cluster | Gross removable | Utility fn body added | Net saved |
|---|--:|--:|--:|
| `prepare_data_vectorized` skeleton | 350 | 150 | 200 |
| `latest_only` fast-path | 1,500 | 500 | 1,000 |
| Option-B feature flag + finalize | 300 | 60 | 240 |
| Setup-source resolution | 200 | 80 | 120 |
| Zero-candidate DEBUG logger | 180 | 40 | 140 |
| Meta-column summarizer | 120 | 50 | 70 |
| Full-scan per-date ranking | 250 | 100 | 150 |
| Diagnostics dict init | 70 | 20 | 50 |
| `_col_numeric_ci` / helpers | 100 | 40 | 60 |
| `_rename_ohlcv` / `_normalize_index` / `get_total_days` | 60 | 20 | 40 |
| **Total** | **3,130** | **1,060** | **2,070** |

Net savings 2,070 lines, sits in the lower end of the audit's stated
2,300–2,800 range. The audit's upper bound assumes some additional wins
from cross-file dedup (`common/today_filters.py` mirrors) that are
tracked in Phase G but not counted here to keep scope tight.

---

## 7. Not-in-scope (explicit non-goals for the refactor)

To keep review load bounded and PRs revertible, the following are
**explicitly out of scope** for the Cluster-F refactor and belong to
separate dispatches:

- Any change to `SYSTEM_TRADE_RULES` values (audit freeze list §4)
- Any signal-logic edit to `_apply_filter_conditions` / `_apply_setup_conditions`
- Any fix to the 8 behavioral spec drifts in §3 of the audit (INFO items I-1…I-8) — those are product decisions
- Fixing the 3 P0 audit patches (SPY gate case bug, `_side_from_row` default-sell, ntfy topic leak) — those ship first in a separate dispatch
- `apps/app_today_signals.py` split (audit's biggest maintainability lever after Cluster F) — needs its own harness

---

## 8. Acceptance criteria for each PR in Phases B–G

Every PR must:

1. Pass `pytest tests/system/test_golden_signals_match.py` — 8 tests, all green
2. Pass `pytest tests/system/test_core_system_public_api_stability.py` — 14 tests, all green (update `EXPECTED` if intentional)
3. Show a **negative net line delta** on `wc -l core/system*.py` — this is
   the point of the refactor; a PR that adds lines needs a separate
   justification
4. Not modify `common/trade_management.py::SYSTEM_TRADE_RULES`
5. Not modify `docs/systems/システム*.txt`
6. Include a per-PR summary of which system was migrated, gross lines
   removed, and any signal-adjacent decision points that came up

If the golden diverges intentionally (e.g. Phase D user-signed off fix of
system3 diagnostics finding I-6), regenerate the golden with:

```
python scripts/golden_signal_harness.py --regenerate
```

and commit the JSON delta together with the code delta in the same PR.
The `content_sha256` field in the JSON provides a git-blame-friendly
tripwire.

---

## 9. Handoff — start-of-refactor checklist

Before opening PR 1 (Phase B):

- [ ] All 3 P0 audit patches (SPY case, side default, ntfy leak) have shipped
- [ ] User has read this plan and signed off on the utility-function
      design in §3 (vs. base-class alternative)
- [ ] Golden harness runs green in CI (both under `pytest` and standalone
      `python scripts/golden_signal_harness.py --verify`)
- [ ] `docs/systems/システム1-7.txt` reviewed once more to confirm no
      spec changes in the last 30 days
- [ ] Working-tree integrity confirmed on Windows-side clean checkout
      (specifically `core/system5.py` at 878 lines matching git HEAD)

Once those check, migration proceeds Phase B → G, one PR at a time,
golden-gated.

---

*Prepared by refactor-prep dispatch (2026-07-03). No `core/systemN` code
was modified; only harness + tests + this document were added.*
