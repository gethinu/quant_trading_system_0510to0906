---
title: Technical Specifications - Systems 1-7
description: Detailed technical documentation for each trading system
---

# Technical Specifications - Trading Systems 1-7

このドキュメントは、各トレーディングシステム（System1-7）の技術仕様を詳細に解説します。実装者、メンテナー、新規 contributor のための参照資料です。

## Table of Contents

1. [System1: Swing Mean-Reversion (Long)](#system1-swing-mean-reversion-long)
2. [System2: Daily Short (Short)](#system2-daily-short)
3. [System3: 3-Day Drop Mean-Reversion (Long)](#system3-3-day-drop-mean-reversion)
4. [System4: Setup Breakout (Long)](#system4-setup-breakout)
5. [System5: Trend Following (Long)](#system5-trend-following)
6. [System6: Enhanced Short (Short)](#system6-enhanced-short)
7. [System7: SPY Hedge (Short)](#system7-spy-hedge)
8. [Diagnostics Standard](#diagnostics-standard)
9. [Option-B Framework](#option-b-framework)

---

## System1: Swing Mean-Reversion (Long)

### Overview

**Strategy Type**: Long mean-reversion
**Signal**: 2-bar swing setup (RSI2 < 50)
**Holding Period**: 3-5 days typically
**Entry Condition**: RSI2 oversold + filter pass

### Entry Logic

```python
# Setup Predicate
filter_pass = (Close >= 5.0
               AND DollarVolume20 > 25M
               AND ATR_Ratio >= 0.05)
setup = filter_pass AND (RSI2 < 50)
```

### Candidate Generation

- **Source**: `core/system1.py::generate_candidates_system1()`
- **Ranking**: RSI2 ascending (most oversold first)
- **Top-N**: Default 20 per date
- **Diagnostics**:
  - `setup_predicate_count`: Filter + RSI2 conditions met
  - `ranked_top_n_count`: Final candidates after ranking
  - `ranking_source`: "latest_only" or "full_scan"

### Exit Logic

- **Exit Trigger**:
  - 3+ bar recovery (RSI2 > 70)
  - Stop loss: Entry - 5\*ATR10
  - Trailing stop: 0.25% from peak

### Files

- Core logic: `core/system1.py`
- Strategy wrapper: `strategies/system1_strategy.py`
- Tests: `tests/test_systems_controlled_all.py` (System1 section)

### Configuration

```python
# config/environment.py
SYSTEM1_ENABLED = True (default)
ENABLE_OPTION_B_SYSTEM1 = False (feature flag)
```

---

## System2: Daily Short

### Overview

**Strategy Type**: Short trend-following
**Signal**: Daily trend reversal
**Holding Period**: 1-3 days typically
**Entry Condition**: Trend down + volume spike

### Entry Logic

```python
# Daily trend identification
trend_down = (Close < SMA20 AND SMA20 < SMA50)
volume_spike = (Volume > AVG_VOLUME_20 * 1.5)
setup = trend_down AND volume_spike
```

### Candidate Generation

- **Source**: `core/system2.py::generate_candidates_system2()`
- **Ranking**: Decline % descending
- **Top-N**: Default 10 per date
- **Diagnostics**: Same schema as System1

### Exit Logic

- **Exit Trigger**:
  - Trend reversal (Close > SMA20)
  - Stop loss: Entry + 3\*ATR10
  - Time stop: 3 bars

### Files

- Core logic: `core/system2.py`
- Strategy wrapper: `strategies/system2_strategy.py`

---

## System3: 3-Day Drop Mean-Reversion

### Overview

**Strategy Type**: Long mean-reversion
**Signal**: 3-day consecutive drop
**Holding Period**: 2-4 days typically
**Entry Condition**: Drop3D >= 12.5% + filter pass

### Entry Logic

```python
# Filter conditions
filter_pass = (Close >= 5.0
               AND DollarVolume20 > 25M
               AND ATR_Ratio >= 0.05)

# Setup: 3-day drop >= 12.5%
setup = filter_pass AND (drop3d >= 0.125)
```

### Drop3D Calculation

```
drop3d = (Close[i-2] - Close[i]) / Close[i-2]
```

Measured over 3 consecutive trading days.

### Candidate Generation

- **Source**: `core/system3.py::generate_candidates_system3()`
- **Ranking**: Drop3D descending (steepest drop first)
- **Top-N**: Default 20 per date
- **Diagnostics**:
  - `ranking_input_counts`: Rows total, rows for label_date, lagged_rows
  - `ranking_stats`: drop3d min/max/mean/median/nan_count
  - `ranking_zero_reason`: Reason if no candidates (e.g., "no_prepared_data")
  - `thresholds`: Applied thresholds (drop3d, atr_ratio)
  - `exclude_reasons`: Breakdown of exclusion reasons per symbol

### Metadata Columns (Internal)

System3 latest_only mode now uses internal metadata columns for precise diagnostics:

```python
# Metadata columns (stripped before public return)
_setup_via: str  # "column" | "predicate" | "fallback"
_predicate_pass: bool  # True if predicate evaluation passed
_fallback_pass: bool  # True if fallback logic passed (test override)
```

**Purpose**: Track which evaluation path (setup column, predicate function, or test fallback) accepted each candidate. Enables mismatch detection and diagnostics transparency.

**Lifecycle**: Added during candidate collection, used for diagnostics recalculation, then stripped before returning public DataFrame.

### Diagnostics Recalculation

After trimming candidates by `label_date` and `top_n`, System3 recalculates diagnostics from metadata:

```python
# Count setup via metadata
via_series = df_all["_setup_via"].fillna("").astype(str)
diagnostics["setup_predicate_count"] = int((via_series != "").sum())

# Count predicate-only passes (column missing but predicate/fallback passed)
predicate_only_mask = (via_series != "column") & (predicate_series | fallback_series)
diagnostics["predicate_only_pass_count"] = int(predicate_only_mask.sum())
```

This ensures `setup_predicate_count` reflects the **final trimmed set**, not the pre-trim state.

### Zero-Candidate Diagnostics

When no candidates are produced in latest_only mode, System3 populates detailed reason fields:

```python
# Reason categorization
if filtered.empty:
    reason = "no_rows_for_label_date"
elif filtered["drop3d"].isna().all():
    reason = "all_drop3d_nan"
elif filtered["drop3d"].dropna().max() < threshold:
    reason = "all_below_drop3d_threshold"
else:
    reason = "unknown"

diagnostics["ranking_zero_reason"] = reason
```

**Filter-level breakdown** (when available):

```python
diagnostics["filter_counts"] = {
    "close_lt_5": count,           # Price filter failures
    "dvol_le_25m": count,          # Dollar volume filter failures
    "atr_ratio_lt_thr": count,     # ATR ratio filter failures
    "drop3d_nan": count            # Missing drop3d indicator
}
```

This helps distinguish between "no data" vs "data present but filtered out" scenarios.

### Top-Off Mechanism (Latest-Only Mode)

When filtered candidates fall short of `top_n`, System3 attempts to replenish from:

1. **Original pool** (`df_all_original`): Candidates with same label_date but excluded by threshold filters
2. **Lagged pool** (`lagged_rows`): Symbols with trading-day lag exceeding tolerance but valid drop3d

```python
# Calculate shortfall
missing = max(0, top_n - len(top_cut))

# Build extras pool (exclude already-selected symbols)
exists = set(top_cut["symbol"])
extras_pool = df_all_original[~df_all_original["symbol"].isin(exists)]
if lagged_rows:
    lag_df = pd.DataFrame(lagged_rows)
    extras_pool = pd.concat([extras_pool, lag_df[~lag_df["symbol"].isin(exists)]])

# Top-off: take highest drop3d from extras
extras_take = extras_pool.head(missing)
top_cut = pd.concat([top_cut, extras_take]).head(top_n)
```

**Diagnostics tracking**:

```python
diagnostics["ranking_breakdown"] = {
    "original_filtered": len(filtered),
    "top_cut_before_topoff": len(top_cut_before),
    "extras_added": extras_count,
    "final_count": len(df_public)
}
```

This ensures transparency when top-off logic activates.

### Diagnostics Integrity Check

System3 performs a consistency check after finalization to detect logic errors:

```python
# ranked_top_n_count should never exceed setup_predicate_count
if diagnostics["ranked_top_n_count"] > diagnostics["setup_predicate_count"]:
    log_callback(
        f"System3: WARNING - ranked_top_n ({ranked}) > "
        f"setup_predicate_count ({setup}). Breakdown: {breakdown}"
    )
```

**Common causes**:

- Duplicate symbol entries (should be deduplicated before ranking)
- Metadata tracking incomplete (missing `_setup_via` tags)
- Top-off logic adding candidates without updating setup count

**Resolution**: Check `ranking_breakdown` for `extras_added` to confirm if top-off triggered unexpectedly.

### Option-B Integration {#system3-optionb}

**Feature Flag**: `ENABLE_OPTION_B_SYSTEM3=1`

When enabled, uses enhanced `prepare_ranking_input()` + `apply_thresholds()`:

```python
# Option-B Phase A
input_counts = prepare_ranking_input(df, label_date, required_cols)
# Validates columns, filters by label_date, returns counts for diagnostics

# Option-B Phase B (future)
filtered_df, reason_counts, reason_symbols = apply_thresholds(df, rules)
# Applies threshold rules, tracks exclusion reasons for transparency

# Finalization
finalize_ranking_and_diagnostics(diag, ranked_df, "latest_only", extras=input_counts)
# Updates diagnostics dict consistently across all systems
```

### Files

- Core logic: `core/system3.py`
- Strategy wrapper: `strategies/system3_strategy.py`
- Tests:
  - `tests/test_system3_diagnostics.py` (diagnostics validation)
  - `tests/test_system3_option_b_parity.py` (Option-B parity)
  - `tests/diagnostics/test_diagnostics_minimal.py` (cross-system minimal checks)

### Exit Logic

**Exit Trigger**:

- Recovery threshold: Drop3D recovery to 0% (price returns to pre-drop level)
- Stop loss: Entry - 2\*ATR10 (tighter than System1 due to mean-reversion nature)
- Time stop: 5 bars (exit if no recovery within 5 days)

**Trailing Stop**:

- After 50% recovery: Trail at 1% from peak
- Full recovery: Exit immediately (target achieved)

### Configuration

```python
# config/environment.py
SYSTEM3_ENABLED = True (default)
ENABLE_OPTION_B_SYSTEM3 = False (feature flag, default OFF)
MIN_DROP3D_FOR_TEST = 0.10 (test mode override, optional)
MIN_ATR_RATIO_FOR_TEST = 0.03 (test mode override, optional)
```

---

## System4: Setup Breakout

### Overview

**Strategy Type**: Long breakout
**Signal**: Breakout after accumulation
**Holding Period**: 3-7 days typically

### Entry Logic (simplified)

```python
# Accumulation phase detection
accumulation = (ATR < SMA_ATR / 2)
# Breakout
breakout = (Close > High[i-20])
setup = accumulation AND breakout
```

### Files

- Core logic: `core/system4.py`
- Strategy wrapper: `strategies/system4_strategy.py`

### Latest-Only とゼロ候補時の診断

最新日のみ（latest_only）で候補がゼロになるケースでも、診断情報は必ず安定して埋まります。

- `ranking_source`: "latest_only" に固定して記録
- `ranked_top_n_count`: 0 に確定
- `setup_unique_symbols`: 0 に確定（ゼロ候補時の安定化）
- 仕上げ: `set_diagnostics_after_ranking()` で標準キーを再計算・確定

内部では最新日トリム後にメタデータ列（`_setup_via` 等）を用いて診断値を再計算し、公開前にメタデータ列は削除します。

---

## System5: Trend Following

### Overview

**Strategy Type**: Long trend-following
**Signal**: ADX trend strength + price momentum
**Holding Period**: 5-20 days typically
**Entry Condition**: ADX > 35 + price above SMA

### Entry Logic

```python
# Trend strength
strong_trend = (ADX7 >= 35.0)  # MIN_ADX = 35.0 (critical threshold)
# Price condition
price_condition = (Close > SMA20)
setup = strong_trend AND price_condition
```

### Constants

```python
MIN_ADX = 35.0  # ADX threshold for trend confirmation
DEFAULT_TOP_N = 20  # Default candidates per date
MIN_PRICE = 5.0  # Minimum closing price
```

### Latest-Only Diagnostics

System5 implements special handling for latest-only mode:

```python
# Alignment resilience: allows up to MAX_DATE_LAG_DAYS trading days lag
max_date_lag_days = 1  # Can be overridden
```

Edge cases handled:

- Zero candidates: `ranking_zero_reason` set appropriately
- Date lag: Lagged rows tracked separately for analysis
- Missing ADX: Filtered at setup level

#### Zero-Candidate Handling（ゼロ候補時の取り扱い）

latest_only で候補がゼロのときも診断の一貫性を保つように実装しています。

- `ranking_source` は必ず "latest_only" を設定（ログ有無に依存しない）
- `ranked_top_n_count` は 0、`setup_unique_symbols` は 0 に確定
- 最後に `set_diagnostics_after_ranking()` を通し、標準キーが欠けないように確定

### Files

- Core logic: `core/system5.py`
- Strategy wrapper: `strategies/system5_strategy.py`
- Tests: `tests/test_system5_latest_only.py` (latest-only edge cases)

### Configuration

```python
# config/environment.py
SYSTEM5_ENABLED = True (default)
ENABLE_OPTION_B_SYSTEM5 = False (feature flag)
MIN_ADX_FOR_TEST = 25.0 (test mode override, optional)
```

---

## System6: Enhanced Short

### Overview

**Strategy Type**: Short trend-following (enhanced)
**Signal**: Trend confirmation + enhanced filters
**Holding Period**: 2-5 days typically

### Entry Logic

```python
# Downtrend confirmation
downtrend = (Close < SMA20 AND SMA20 < SMA50)
# Enhanced filter
high_volume = (Volume > AVG_VOL_20 * 1.2)
setup = downtrend AND high_volume
```

### Files

- Core logic: `core/system6.py`
- Strategy wrapper: `strategies/system6_strategy.py`
- Backup: `core/system6_backup.py` (reference)

### Latest-Only とゼロ候補時の診断

System6 でも latest_only のゼロ候補時に診断が安定するように統一しています。

- `ranking_source`: "latest_only" に固定
- `ranked_top_n_count`: 0 に確定
- `setup_unique_symbols`: 0 に確定（ゼロ候補時の安定化）
- 仕上げ: `set_diagnostics_after_ranking()` またはラッパー（`finalize_*`）で標準キーを確定

必要に応じてメタデータ（`_setup_via`, `_predicate_pass`, `_fallback_pass` など）を内部保持し、トリム後に診断を再計算してから公開列のみ返します。

---

## System7: SPY Hedge

### Overview

**Strategy Type**: Short hedge (SPY-only)
**Purpose**: Portfolio downside protection
**Signal**: VIX elevation + SPY weakness
**Holding Period**: Variable (hedge duration)

### Critical Constraints

⚠️ **SPY FIXED**: System7 always trades **SPY only**. No symbol expansion.

### Entry Logic

```python
# SPY-only constraint (mandatory)
symbol_check = (symbol == "SPY")
# Hedge trigger
hedge_trigger = (VIX > 20 AND Close < SMA50)
setup = symbol_check AND hedge_trigger
```

### Files

- Core logic: `core/system7.py`
- Strategy wrapper: `strategies/system7_strategy.py`

⚠️ **Critical Rule**: Do NOT modify System7 to trade other symbols. Hedge efficacy depends on SPY-only focus.

---

## Diagnostics Standard

### Common Diagnostics Fields

All systems use standardized diagnostics dictionary:

```python
{
    "ranking_source": str,  # "latest_only" or "full_scan"
    "setup_predicate_count": int,  # Symbols passing setup predicate
    "ranked_top_n_count": int,  # Final candidates after ranking
    "predicate_only_pass_count": int,  # Predicate pass vs. setup column mismatch
    "mismatch_flag": int,  # 1 if predicate != setup column

    # Visualization-friendly extras
    "ranking_input_counts": {
        "rows_total": int,
        "rows_for_label_date": int,
        "lagged_rows": int,
    },
    "ranking_stats": {
        "drop3d_min": float | None,
        "drop3d_max": float | None,
        "drop3d_mean": float | None,
        "drop3d_median": float | None,
        "drop3d_nan_count": int,
    },
    "exclude_reasons": dict,  # reason -> symbol count
    "top_n": int | None,
    "label_date": pd.Timestamp | None,
}
```

### Utility: set_diagnostics_after_ranking()

```python
from common.system_candidates_utils import set_diagnostics_after_ranking

# After final ranking is complete:
set_diagnostics_after_ranking(
    diagnostics,
    final_df=ranked_df,
    ranking_source="latest_only",
    count_column="symbol"
)
# Updates: ranked_top_n_count, ranking_source, setup_predicate_count (if 0)
```

---

## Option-B Framework

### Overview

Option-B provides standardized candidate generation utilities to reduce code duplication and ensure consistent behavior across all systems.

### Three-Tier Architecture

**Tier 1: Input Validation**

```python
def prepare_ranking_input(
    df: pd.DataFrame,
    label_date: pd.Timestamp | None,
    required_cols: list[str],
) -> tuple[pd.DataFrame, dict[str, int]]:
    """Validates required columns, filters by label_date, returns counts."""
```

**Tier 2: Threshold Application**

```python
def apply_thresholds(
    df: pd.DataFrame,
    rules: dict[str, tuple[str, float]],  # {col: (operator, threshold)}
) -> tuple[pd.DataFrame, dict[str, int], dict[str, set[str]]]:
    """Applies threshold rules, tracks exclusion reasons."""
```

**Tier 3: Diagnostics Finalization**

```python
def finalize_ranking_and_diagnostics(
    diag: dict[str, Any],
    ranked_df: pd.DataFrame,
    ranking_source: str,
    extras: dict[str, Any] | None = None,
) -> None:
    """Wraps set_diagnostics_after_ranking, merges optional extras."""
```

### Activation

```python
# config/environment.py
ENABLE_OPTION_B_SYSTEM3 = 1  # Activates Option-B for System3
ENABLE_OPTION_B_SYSTEM5 = 1  # Activates Option-B for System5
ENABLE_OPTION_B_SYSTEM6 = 1  # Activates Option-B for System6
```

### Feature Flag Pattern

```python
# In system3.py (example)
try:
    env = get_env_config()
    use_option_b = env.enable_option_b_system3
except Exception:
    use_option_b = False

if use_option_b:
    # Use Option-B utilities
    from common.system_candidates_utils import prepare_ranking_input
    input_counts = prepare_ranking_input(df, label_date, required_cols)
    diag["ranking_input_counts"] = input_counts
else:
    # Legacy inline logic
    ...
```

---

## Troubleshooting

### Common Issues

| Issue               | Cause                      | Solution                                                |
| ------------------- | -------------------------- | ------------------------------------------------------- |
| No candidates       | Setup predicate too strict | Check filter conditions, adjust thresholds in env       |
| Misaligned dates    | Trading day lag            | Increase `max_date_lag_days` or use `latest_only=False` |
| Memory spike        | Large candidate batches    | Reduce `top_n` or enable `DataFrameCache`               |
| Diagnostics missing | Legacy system code         | Migrate to `set_diagnostics_after_ranking()`            |

---

## References

- `docs/README.md`: Project overview and navigation
- `common/system_candidates_utils.py`: Shared utilities (Phase-A)
- `config/environment.py`: Environment configuration
- `tests/test_system*_*.py`: System-specific tests

---

**Last Updated**: 2024-09-13
**Version**: 1.0
**Status**: Active (Systems 1-7 in production-ready state)
