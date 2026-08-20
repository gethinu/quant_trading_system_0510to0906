# Signal-score calibration probe — 2026-08-20

**Status: research probe. READ-ONLY. Nothing wired, no flag added, no pipeline run,
no orders. Verdict below is NO-GO for a confidence-based entry gate.**

Question asked: would temperature-scaling (or Platt / isotonic) calibration bolted
onto the existing per-signal `score` improve **entry quality** out-of-sample —
i.e. does the calibrated P(win) let us gate out low-confidence entries and raise
the realised hit rate / risk-adjusted PnL?

Short answer: **no.** Calibration works exactly as advertised — it collapses ECE
by ~85% — but it collapses it *to the base rate*, because the underlying score
carries essentially no discrimination for win/loss. Five of six systems have OOS
AUC statistically indistinguishable from, or below, 0.5. A gate on the calibrated
probability is therefore a gate on noise, and on System1 it is actively harmful:
it strips out precisely the trades that carry the system's expectancy.

Reproduce: `outputs/impl/signal_calibration_probe/` (scripts + raw result JSON).

---

## 1. Where the score comes from, and which systems emit a usable one

The allocator sorts each system's block by `score`
(`core/final_allocation.py:1543 _sort_final_frame`, ascending only for System4).
`score` is populated in `common/today_signals.py:2275 _score_from_candidate`,
which picks a per-system representative indicator. Verified against each
system's own `rank()` step in `core/systemN.py`, and cross-checked against the
one surviving production artifact that retained the field
(`data_cache/signals/signals_final_2026-07-02.csv`, 48 rows — its `score_key`
column matches the mapping below exactly).

| system | `score_key` | direction | continuous? | distinct values in probe (n) |
|---|---|---|---|---|
| System1 | `roc200` | desc | yes | 28,209 |
| System2 | `adx7` | desc | yes | 674 |
| System3 | `drop3d` | desc | yes, but **truncated** — setup requires `drop3d >= 0.125`, so 1%–99% range is only 0.13–0.51 | 68 |
| System4 | `rsi4` | **asc** (low RSI first) | yes | 9,260 |
| System5 | `adx7` | desc, but **truncated** — setup requires `adx7 > 55`, 1%–99% range 55.1–81.5 | yes | 1,442 |
| System6 | `return_6d` | desc | yes | 1,133 |
| System7 | — | — | **none.** `_score_from_candidate` returns `(None, None, False)` for system7 by design (SPY-only hedge). Excluded from this probe. | 0 |

So: six systems emit a genuinely continuous score; none is binary or rank-only.
System3 and System5 are continuous but heavily *bunched against their own setup
threshold*, which caps how much spread any calibrator can exploit.

## 2. The dataset — and why it could not come from the live record

**The live record cannot support this join, and that is a finding, not a
workaround.** Checked exhaustively:

* `results_csv/exit_ledger_20260819.json` has 1,211 closed trades spanning
  2025-11-03 → 2026-08-17 with system attribution (74.2% from `entry_order`
  ground truth), realised PnL and holding days — **but no score field at all**
  (`closed_trades[*]` keys: symbol, side, qty, system, entry/exit time+session+
  price, holding_days, realized_pl, realized_pl_pct, exit_reason, order ids,
  system_source, symbol_aliases).
* `results_csv/today_signals_*.json` exists for **8 days only** (2026-08-13 →
  08-20) and each signal carries `rank`, not `score`.
* `results_csv/paper_orders_*.json` (2026-07-01 → 08-20) carries no score.
* Exactly **one** score-bearing signal artifact survives anywhere in the repo:
  `data_cache/signals/signals_final_2026-07-02.csv`, 48 rows, 1 date.

One date of scores against 1,211 outcomes is not a dataset. The daily pipeline
writes `signals_final_<date>.csv` with `score_key,score,score_rank` but the file
is not retained. **Fixing that retention is the cheapest prerequisite for ever
revisiting this question on live data** — see §7.

So the probe reconstructs the dataset from the price cache instead. The score is
a deterministic function of prices up to the signal bar, so recomputing it is not
peeking.

**Construction** (`build_dataset.py`):
1. Universe = `data/universe_auto.txt` (4,655 symbols; 4,654 present in
   `data_cache/rolling/`).
2. Per symbol, per bar, evaluate each system's setup condition; the score is the
   system's ranking indicator on that bar.
3. Signal on bar *s* → entry on bar *s+1*, using each strategy's own
   `compute_entry` / `compute_exit` rules and its **live resolved config**
   (`config/config.yaml::strategies.*` merged by `StrategyBase.__init__`).
4. Every setup-passing candidate is evaluated — **not** just the top-10 that
   would have been traded. Restricting to the traded top-10 leaves almost no
   score variance to calibrate against; the top-10 subset is still reported
   separately in §6 because that is what a live gate would actually see.

**Fidelity check** (`verify_equivalence.py`, run before any numbers were taken):
the probe's numpy setup masks and entry/exit simulators were compared against
the repo's own `common/system_setup_predicates.py` and
`strategies/systemN_strategy.compute_entry/compute_exit` on 220 random symbols:

```
setup checks : 33000   mismatches: 0
trade checks : 1622   mismatches: 0
nan-stop rows dropped by probe (repo would emit NaN stop): 1
```

(The single divergence is deliberate: where ATR is NaN the repo's `compute_entry`
returns a NaN stop price; the probe drops that row rather than scoring a
degenerate trade. It occurred once in 1,622 checks.)

Two rounding bugs were found and fixed during this check, both in the probe, not
the repo: `np.float64.__round__` disagrees with Python's `round(float, 2)` on
half-way cases, which shifted System3/5/6 limit entry prices by one cent on ~1.3%
of rows.

**Dataset produced:** 261,741 candidate-trades, signal dates 2024-07-16 →
2026-08-17.

| system | rows | signal dates | first signal | usable from |
|---|---|---|---|---|
| System1 | 139,760 | 273 | 2025-04-21 | `roc200` needs 200 bars of warm-up |
| System2 | 627 | 232 | 2024-08-16 | |
| System3 | 7,289 | 324 | 2025-02-05 | `sma150` warm-up |
| System4 | 81,037 | 274 | 2025-04-17 | `sma200` warm-up |
| System5 | 2,268 | 352 | 2024-11-20 | `sma100` + `atr10` warm-up |
| System6 | 30,760 | 464 | 2024-07-16 | |

### Horizon cap — a correction that had to be made

The price cache is only ~2 years deep (2024-07-02 → 2026-08-18, 534 bars). The
first pass used each system's own unbounded exit rule. System1 and System4 use
trailing stops with **no time exit**, so 44% / 23% of their trades never resolved
inside the cache and were marked to market at the last close. That broke the
train/OOS label comparability outright:

| system | uncapped train base rate | uncapped OOS base rate | OOS rows still open |
|---|---|---|---|
| System1 | 0.157 | 0.489 | **64.8%** |
| System4 | 0.025 | 0.325 | 38.9% |

A calibrator fitted on a 15.7% win rate and tested against a 48.9% win rate
produces ECE ≈ 0.33 no matter how good the score is — that number would measure
the cache depth, not the signal. All headline results below therefore use a
**uniform 60-bar holding cap** with mark-to-market at bar `e+60`, and require 60
bars of forward data for every signal. This affects only System1/System4 (the
other four exit within ≤7 bars) and drops OOS censoring to ≤0.7% everywhere. The
uncapped run is retained as
`results/calib_uncapped_all_rows.json` for comparison.

## 3. Split — purge and embargo

Matched to the repo's own convention in `common/validation/cpcv.py`: **purge**
drops training rows whose label span `[entry_date, exit_date]` overlaps the test
window; **embargo** inserts a further gap after the training block
(`embargo_pct=0.01` of signal dates, the module default).

Forward split, train = first 60% of signal dates, no shuffling:

| system | signal dates | train ends | embargo (dates) | OOS starts | OOS ends | train rows purged | n train | n OOS |
|---|---|---|---|---|---|---|---|---|
| System1 | 273 | 2025-12-11 | 3 | 2025-12-17 | 2026-05-20 | 21,565 (28.0%) | 55,475 | 61,302 |
| System2 | 232 | 2025-10-01 | 2 | 2025-10-06 | 2026-05-18 | 3 | 345 | 263 |
| System3 | 324 | 2025-11-11 | 3 | 2025-11-17 | 2026-05-20 | 15 | 4,122 | 3,009 |
| System4 | 274 | 2025-12-10 | 3 | 2025-12-16 | 2026-05-20 | 7,506 (16.0%) | 39,379 | 33,205 |
| System5 | 352 | 2025-10-15 | 4 | 2025-10-22 | 2026-05-20 | 18 | 1,245 | 885 |
| System6 | 464 | 2025-08-22 | 5 | 2025-09-02 | 2026-05-20 | 1 | 17,608 | 12,796 |

The repo's `cpcv_date_folds(n_groups=6, k_test=2)` (15 folds) was additionally run
per system as a stability check on AUC; results in §4.

## 4. Discrimination — the question that decides everything

AUC of the oriented score against `win = ret > 0`, on the OOS split. 95% CI from
a **block bootstrap that resamples whole signal-dates**, so it respects the fact
that same-day candidates share a market shock. "Within-day AUC" pools only
same-day pairs, isolating whether the ranking key orders candidates *on the day
it is actually used*, free of regime drift.

| system | n OOS | AUC [95% CI] | within-day AUC | rank-IC(score, ret) | CPCV AUC mean ± sd (15 folds) | discrimination? |
|---|---|---|---|---|---|---|
| System1 | 61,302 | **0.480** [0.463, 0.495] | 0.485 | −0.049 | 0.477 ± 0.025 | inverse, small |
| System2 | 263 | **0.574** [0.492, 0.657] | 0.539 | +0.186 | 0.581 ± 0.014 | **CI straddles 0.5 — unresolved** |
| System3 | 3,009 | **0.424** [0.400, 0.448] | 0.426 | −0.035 | 0.446 ± 0.022 | inverse (but see §5) |
| System4 | 33,205 | **0.500** [0.479, 0.518] | 0.487 | −0.051 | 0.511 ± 0.016 | **none — dead flat** |
| System5 | 885 | **0.471** [0.430, 0.514] | 0.469 | −0.044 | 0.465 ± 0.023 | none |
| System6 | 12,796 | **0.467** [0.453, 0.481] | 0.464 | +0.070 | 0.463 ± 0.011 | inverse for hit rate |

Note the AUCs *below* 0.5 are not a sign error: the orientation was verified
against each `sort_values` call in `core/systemN.py` (only System4 is ascending)
and against the production `signals_final` artifact. A higher-ranked candidate
genuinely wins *less* often in System1/3/6.

**This is the finding the whole probe turns on.** With AUC at or below 0.5, a
calibrated probability is mathematically forced to be near-constant, and it is:

| system | fitted Platt slope `a` | OOS calibrated P(win) range |
|---|---|---|
| System1 | −0.088 | 0.4953 – 0.5394 |
| System2 | +0.590 | 0.4190 – 0.6996 |
| System3 | −0.302 | 0.6868 – 0.7859 |
| System4 | **−0.010** | **0.2373 – 0.2410** (a 0.37 pp spread) |
| System5 | −0.223 | 0.5939 – 0.6954 |
| System6 | −0.248 | 0.6937 – 0.7816 |

Five of six slopes are negative — the calibrator, fitted honestly on train data,
learned that *higher score means lower P(win)*. So "gate on high confidence"
means "keep the **low**-scoring candidates", which is the opposite of what an
operator would expect the knob to do.

### System1: the score is informative — about returns, not about hit rate

The most decision-relevant result in the probe. OOS deciles of `roc200`
(1 = lowest score, 10 = highest):

| decile | n | hit rate | mean ret | median ret |
|---|---|---|---|---|
| 1 | 6,131 | 0.4905 | +0.0125 | −0.0037 |
| 5 | 6,130 | 0.4638 | +0.0146 | −0.0174 |
| 8 | 6,130 | 0.4721 | +0.0400 | −0.0210 |
| 9 | 6,130 | 0.4458 | +0.0411 | −0.0420 |
| 10 | 6,131 | **0.4317** | **+0.0610** | −0.0551 |

Hit rate falls monotonically with score; **mean return rises monotonically**, and
the gap is real:

```
mean_ret(D10) − mean_ret(D1) = +0.0485
block-bootstrap 95% CI [+0.0249, +0.0715]   (n_boot=500, resampled by signal-date)
```

That is the classic trend-following payoff — the highest-momentum names win less
often and win much bigger. The negative rank-IC (−0.049) and the positive mean
gap are both true and not in conflict: the median return falls with score while
the mean rises, because the right tail fattens.

**A P(win) gate on System1 would systematically discard the highest-expectancy
trades.** Hit rate is simply the wrong objective for this system.

System6 shows the same shape more weakly: hit rate 0.752 → 0.688 across deciles
while median return rises 0.061 → 0.102.

System4 shows nothing at all — hit rate wanders between 0.263 and 0.291 with no
ordering, mean return between +0.003 and +0.009 with no ordering.

## 5. Calibration quality — ECE before vs after

OOS, 10 equal-count bins. Four predictors compared:

* **raw** — the naive reading of the score as a confidence: its percentile within
  the train distribution. This is the "before".
* **temperature** — `p = σ(z/T)`, no bias term, `T` fit by NLL on train.
* **Platt** — `p = σ(a·z + b)`, i.e. logistic regression on the single feature.
* **isotonic** — PAVA monotone fit on train.
* **const** — a single constant equal to the train base rate. **This is the honest
  null**: it is what "no information" looks like.

| system | ECE raw | ECE temp | ECE Platt | ECE isotonic | **ECE const** | Brier Platt | Brier const |
|---|---|---|---|---|---|---|---|
| System1 | 0.285 | 0.023 | 0.040 | 0.045 | **0.044** | 0.2506 | 0.2512 |
| System2 | 0.205 | 0.112 | 0.124 | 0.112 | **0.034** | 0.2480 | 0.2503 |
| System3 | 0.336 | 0.285 | 0.052 | 0.153 | **0.044** | 0.1682 | 0.1701 |
| System4 | 0.317 | 0.226 | 0.035 | 0.035 | **0.035** | 0.2002 | 0.2002 |
| System5 | 0.292 | 0.155 | 0.042 | 0.010 | **0.010** | 0.2255 | 0.2260 |
| System6 | 0.300 | 0.222 | 0.020 | 0.019 | **0.019** | 0.2006 | 0.2011 |

Read this correctly. Calibration *does* cut ECE by 85–93% versus the raw score —
that headline number is real and it is also meaningless, because the raw score
percentile is uniform on [0,1] by construction and was never a probability. The
comparison that matters is **calibrated vs constant base rate**, and there the
calibrated model never wins by more than noise:

* System1, System4, System5, System6: calibrated ECE **equals** the constant
  predictor to within 0.001–0.005. Brier is identical to 3–4 decimals.
* System2 and System3: the constant predictor is **better** than the calibrated
  one (0.034 vs 0.124; 0.044 vs 0.052) — calibration fitted on train actively
  transferred worse than doing nothing.
* Temperature-only scaling is the worst of the three wherever the base rate is
  far from 50% (System3 0.285, System4 0.226, System6 0.222), which is expected:
  with no bias term it cannot move the intercept to the base rate.

Reliability diagram, System4 (Platt), 10 bins — predicted spans 0.34 pp while
observed wanders over 2.8 pp with no ordering:

| bin | n | mean predicted | observed rate |
|---|---|---|---|
| 1 | 3,322 | 0.2374 | 0.2790 |
| 2 | 3,320 | 0.2378 | 0.2696 |
| 4 | 3,319 | 0.2385 | 0.2630 |
| 6 | 3,321 | 0.2393 | 0.2743 |
| 8 | 3,322 | 0.2401 | 0.2908 |
| 10 | 3,319 | 0.2408 | 0.2639 |

System1 (Platt): predicted 0.4968 → 0.5356 across bins, observed 0.4318, 0.4458,
0.4720, 0.4771, 0.4801, 0.4634, 0.4763, 0.4855, 0.5076, 0.4903 — no monotone
relationship. The full reliability tables for every system are in
`results/calib_h60_all_rows.json` under `calibration.*.reliability`.

### Fill realism — a separate finding that changes System3/5/6

System3, System5 and System6 enter with a **limit** priced away from the prior
close (×0.93, ×0.97, ×1.05). The repo's backtest assumes that limit fills
unconditionally. It often would not have:

| system | fraction of candidate bars where the limit was actually touched |
|---|---|
| System3 | 32.0% |
| System5 | 52.5% |
| System6 | 40.3% |

Re-running with only genuinely fillable entries (`--filled-only`) removes the
apparent inverse discrimination *and* most of the apparent edge:

| system | base hit (all rows) | base hit (fillable only) | AUC (all rows) | AUC (fillable only) [CI] |
|---|---|---|---|---|
| System3 | 0.786 | **0.529** | 0.424 | **0.470** [0.425, 0.510] |
| System5 | 0.655 | **0.498** | 0.471 | **0.502** [0.441, 0.553] |
| System6 | 0.722 | **0.539** | 0.467 | **0.497** [0.483, 0.514] |

Under realistic fills all three sit exactly on AUC 0.5 — no discrimination in
either direction — and their mean OOS return per trade is ≈0 or negative
(System3 −0.0008, System5 −0.0141, System6 −0.0039). The sub-0.5 AUCs in the
main table were partly an artifact of the unconditional-fill assumption.

This is out of scope for the calibration question but it is a material fidelity
issue in the repo's own backtest, and it is flagged rather than acted on.

## 6. The decision-relevant test: does gating actually help OOS?

Gate = keep OOS signals whose calibrated Platt P(win) ≥ τ, for τ at deciles of
the OOS probability distribution. Floors used: a system needs **≥200 OOS rows**
for a verdict, a threshold bucket needs **≥50 rows** to be reported. Rationale:
at n = 200 the standard error of a hit rate near 0.5 is ≈3.5 pp, so anything
under a ~10 pp move is unresolvable; below n = 50 the SE exceeds 7 pp and the
bucket is noise. All six systems clear the system-level floor; buckets that do
not clear the bucket floor are marked.

**System1** (n = 61,302, base hit 0.4730, base mean ret +0.0274):

| kept | n | hit rate | mean ret | mean/sd |
|---|---|---|---|---|
| 100% | 61,302 | 0.4730 | **+0.0274** | 0.108 |
| 50% | 30,653 | 0.4846 | +0.0143 | 0.078 |
| 20% | 12,263 | 0.4989 | +0.0133 | 0.081 |
| 10% | 6,135 | 0.4905 | +0.0125 | 0.074 |

The gate does what it says — hit rate rises from 0.473 to 0.499 — and **halves
mean return per trade (−51%) and cuts return-per-unit-risk from 0.108 to 0.081**.
This is the §4 finding cashed out: buying a 2.6 pp hit-rate improvement by
discarding the fat right tail is a bad trade.

**System4** (n = 33,205, base hit 0.2741): completely inert, as expected from a
0.37 pp probability spread. Hit rate 0.2741 → 0.2641 at 10% kept; mean return
+0.0067 → +0.0032. The gate makes it slightly worse.

**System6** (n = 12,796, base hit 0.7220): hit 0.7220 → 0.7537 at 15% kept, mean
return +0.0401 → +0.0417, mean/sd 0.061 → 0.133 — but under realistic fills
(§5) the same gate moves hit rate only 0.5391 → 0.5487 while mean return goes
from −0.0039 to −0.0155. The apparent improvement does not survive fill realism.

**System3** (n = 3,009): hit 0.786 → 0.825 at 15% kept, mean return flat
(+0.0845 → +0.0892). Under realistic fills (n = 903): hit 0.529 → 0.532 at 12%
kept, mean −0.0008 → +0.0079, non-monotone across thresholds. Noise.

**System5** (n = 885): hit 0.655 → 0.775 at 10% kept looks strong, but that
bucket is n = 89 and the AUC CI straddles 0.5. Under realistic fills (n = 454)
the 10% bucket is n = 46 — **below the reporting floor** — and every threshold
above 20% kept has negative mean return.

**System2** (n = 263) — **the only system where the gate genuinely improves
everything at once**:

| kept | n | hit rate | mean ret | mean/sd |
|---|---|---|---|---|
| 100% | 263 | 0.5285 | +0.0123 | 0.070 |
| 50% | 133 | 0.5940 | +0.0372 | 0.166 |
| 30% | 79 | 0.6962 | +0.0734 | 0.298 |
| 20% | 55 | 0.6727 | +0.0856 | 0.322 |
| 10% | 27 | 0.7407 | +0.1086 | 0.352 | *(below bucket floor)* |

Monotone in hit rate, mean return and risk-adjusted return. But: 263 OOS rows
total, AUC CI [0.492, 0.657] straddles 0.5, and System2 fires roughly 0.35 times
per day. This is a hint, not a result — and System2 is exactly the system the
project has already flagged for other reasons.

### Restricted to the actually-traded top-10/day

A live gate would only ever see the top-10 by score per system per day. On that
subset the results are non-monotone in every system — the signature of noise:

| system | n OOS | AUC | all kept, hit / mean | ~50% kept, hit / mean | ~25% kept, hit / mean |
|---|---|---|---|---|---|
| System1 | 1,060 | 0.509 | 0.438 / +0.0511 | 0.449 / −0.0063 | 0.485 / +0.0461 |
| System2 | 259 | 0.575 | 0.529 / +0.0122 | 0.603 / +0.0379 | 0.708 / +0.0949 |
| System3 | 1,208 | 0.457 | 0.712 / +0.0768 | 0.736 / +0.0746 | 0.726 / +0.0792 |
| System4 | 1,070 | 0.537 | 0.251 / −0.0031 | 0.221 / −0.0074 | 0.216 / −0.0123 |
| System5 | 749 | 0.484 | 0.638 / +0.0179 | 0.637 / +0.0192 | 0.667 / +0.0254 |
| System6 | 1,810 | 0.524 | 0.686 / +0.0335 | 0.663 / +0.0541 | 0.675 / +0.0594 |

System1's mean return going +0.051 → −0.006 → +0.046 as the gate tightens is not
a signal; it is a 1,060-row sample being sliced too thin. System2 is again the
only monotone column.

## 7. Honest caveats

These bound how far the verdict travels. None of them rescue the result.

1. **The absolute levels do not match live performance, in either direction.**
   Live paper realised win rates from `exit_ledger_20260819.json` (1,211 closed
   trades, 74.2% attributed from `entry_order` ground truth) against the probe's
   whole-dataset win rates:

   | system | live win rate (n) | probe win rate | probe − live |
   |---|---|---|---|
   | System1 | 0.193 (88) | 0.526 | +0.333 |
   | System2 | 0.388 (577) | 0.544 | +0.156 |
   | System3 | 0.383 (193) | 0.491 (fillable) | +0.108 |
   | System4 | 0.467 (107) | 0.297 | **−0.170** |
   | System5 | 0.412 (165) | 0.468 (fillable) | +0.056 |
   | System6 | 1.000 (2) | 0.559 (fillable) | n too small |

   Mostly the probe is optimistic — live trades bear real fills, slippage,
   position caps and the exit-management defects logged elsewhere in this repo —
   but System4 runs the other way, and System1's 33 pp gap is far too large to be
   explained by costs alone. The probe is valid only for the **relative**
   question — does the score rank outcomes within a system — and its absolute
   hit rates should not be read as live expectations.

2. **Survivorship bias.** The universe is `data/universe_auto.txt`, current
   membership applied to historical prices. This is the bias
   `docs/METHODOLOGY_VALIDATION.md` already catalogues; the dated membership file
   `data/universe_membership.csv` still does not exist, so
   `PointInTimeUniverse` cannot correct it here either. The bias inflates
   absolute returns. It has no obvious reason to create or destroy *rank*
   information, which is what is being measured.

3. **Two years of price history.** The cache is 2024-07-02 → 2026-08-18, so
   System1 and System4 (200-bar warm-up) have ~14 months of usable signal dates
   and a single OOS regime (2025-12 → 2026-05). A 0.5 AUC over one regime is not
   proof of 0.5 AUC forever.

4. **The 60-bar horizon cap changes System1/4's label** from "the system's own
   trailing exit" to "trailing exit, capped at 60 bars". This was necessary (§2)
   and it makes the label comparable across the split, but it is not identical to
   what the live system realises.

5. **System3/System5 scores are bunched against their own setup thresholds**
   (`drop3d ≥ 0.125`, `adx7 > 55`), which structurally limits the spread any
   calibrator could exploit. Their near-0.5 AUC is partly by construction.

6. **No transaction costs anywhere** in the probe. Adding them would lower every
   absolute number and would hurt the gated (fewer, and in System1's case
   smaller) buckets at least as much as the ungated ones.

## 8. Verdict

**NO-GO on building a flag-gated calibration/confidence-gate feature.**

* No system carries usable discrimination for P(win). Four are statistically
  indistinguishable from AUC 0.5 once fill realism is applied (System4 0.500,
  System5 0.502, System6 0.497, System3 0.470); System1 is mildly *inverse*
  (0.480, CI excludes 0.5).
* Calibration works and is correctly implemented — ECE drops 85–93% versus the
  raw score — but it converges on the base rate. Calibrated ECE never beats a
  constant base-rate predictor, and on System2/System3 it is worse. That is the
  right behaviour for a score with no discrimination, and it is the answer to the
  question, not a failure of the method.
* On System1 a P(win) gate is **actively harmful**: it raises hit rate 2.6 pp
  while halving mean return per trade, because ROC200 predicts return magnitude
  (D10−D1 = +4.85 pp, CI [+2.49, +7.15]) in the opposite direction to hit rate.
  Any confidence gate built on hit rate would degrade the system it is meant to
  protect.
* System2 is the sole system where gating improves hit rate, mean return and
  risk-adjusted return monotonically — but n = 263 OOS, the AUC CI straddles
  0.5, and it fires ~0.35 times/day. Not decidable on present data.

### If this is ever revisited

The blocker is data, not method. In order of value:

1. **Retain `signals_final_<date>.csv`.** The pipeline already writes it with
   `score_key,score,score_rank,score_rank_total`
   (`scripts/run_all_systems_today.py:2845`); exactly one file survives. Keeping
   it, and stamping `score` into `exit_ledger` `closed_trades`, would make the
   live join possible within a year of accumulation — and would let the question
   be answered on real fills instead of a simulation.
2. **Deepen the price cache beyond 2 years**, so System1/System4 get more than
   one OOS regime and the horizon cap becomes unnecessary.
3. **Re-ask the question against expected return, not P(win).** System1's decile
   table says the ranking key is informative about return magnitude. A
   calibrated *expected-return* model is the version of this idea that the data
   actually supports; a calibrated *win-probability* model is not.
4. Only then, and only if AUC clears 0.55 OOS with a CI excluding 0.5 on a
   system with enough daily signals to matter, is a flag-gated implementation
   worth writing.

---

### Artifacts

| path | contents |
|---|---|
| `outputs/impl/signal_calibration_probe/build_dataset.py` | candidate + outcome dataset builder (read-only; `--max-hold` cap) |
| `outputs/impl/signal_calibration_probe/verify_equivalence.py` | equivalence check vs `common/system_setup_predicates.py` and `strategies/*` |
| `outputs/impl/signal_calibration_probe/analyze.py` | purged split, temperature/Platt/isotonic, ECE, AUC, gating sim |
| `.../results/calib_h60_all_rows.json` | headline run (60-bar cap) |
| `.../results/calib_h60_filled_only.json` | fill-realism sensitivity |
| `.../results/calib_uncapped_all_rows.json` | uncapped run, retained to show the censoring artifact |

Nothing in `outputs/impl/signal_calibration_probe/` is imported by any runtime
module. To reproduce:

```bash
python outputs/impl/signal_calibration_probe/verify_equivalence.py
python outputs/impl/signal_calibration_probe/build_dataset.py --out <dir> --max-hold 60
python outputs/impl/signal_calibration_probe/analyze.py --data <dir>/candidates.parquet --out <dir>/calib.json
```
