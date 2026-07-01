# System Reliability Audit — sys1-7 (2026-07-01)

**Scope**: read-only static audit. `core/system1-7.py` / `CacheManager` は **不変**。実発注・broker API ノータッチ。
**目的**: 事業運用開始 (subscriber tier 化・実売買) 前の信頼性 baseline 確立。
**Auditor**: Claude Code (branch `claude/system-audit`)

> ⚠️ **データ制約**: 旧 repo `C:\Repos\quant_trading_system\results_csv\` に
> `signals_final_YYYY-MM-DD.csv` は**存在しない**。日次 signal 時系列は未保存で、
> 統計 audit (Part 2) は `skip_details_system*.csv` / `system3_final_alloc_*.csv` /
> `integrated_backtest_sbi_*.json` / `daily_metrics.csv` から**導出可能な範囲**に限定される。
> 実 live signals は 2026-07-02 06:00 Task Scheduler tick 後に初出。

---

## Part 1: sys1-7 戦略サマリ & code review (static)

Source: `core/system1.py`–`core/system7.py`, `core/final_allocation.py`。
Exit rule (profit target / trailing / max-hold) は **どの system の core file にも存在しない** —
exit は別レイヤ (`final_allocation` の stop、及び未読の exit_planner) 管轄。

### System 1 — Long ROC200 Momentum
- **戦略概要**: trend-following long。上昇トレンド銘柄の中で momentum 最上位を買い、翌日寄付エントリ・ATR stop。
- **filter chain**: `Close ≥ 5.0` → `DollarVolume20 > 50,000,000` (`system1.py:53–54,93`) → setup `Close > SMA200 & ROC200 > 0` (`:148`)。市場ゲート SPY>SMA100 は外部。
- **entry rules**: setup pass → **ROC200 降順**でランク、top_n=20 (`:1332–1335`)。
- **exit rules**: stop = entry − `STOP_ATR_MULTIPLE_SYSTEM1`×ATR20 (`:1179–1183`)。target/time stop は core になし。
- **position sizing**: core になし → `final_allocation` (risk% / max_pct)。
- **hedge**: N/A。

### System 2 — Short RSI3 Spike (mean-reversion)
- **戦略概要**: 2 日連続上昇 + overbought の short mean-reversion。
- **filter chain**: `Close ≥ 5.0` → `DollarVolume20 > 25,000,000` → `ATR_Ratio > 0.03` (`system2.py:51–53,74–79`) → setup `RSI3 > 90 & twodayup` (`:105–108`)。
- **entry rules**: setup pass → **ADX7 降順**ランク、top_n=10 (`:388`)。
- **exit rules**: core になし。 **sizing**: 外部。 **hedge**: N/A。

### System 3 — Long Mean-Reversion (3-day drop)
- **戦略概要**: 急落 3 日後のリバウンド狙い long。
- **filter chain**: `Close ≥ 5.0` → `DollarVolume20 > 25,000,000` → `atr_ratio ≥ 0.05` (`system3.py:61–64,294–297`) → setup `drop3d ≥ 0.125` (12.5%) (`:300`)。
- **entry rules**: setup pass → **drop3d 降順**ランク、top_n=20 (`:1175`)。
- **exit rules**: core になし。 **sizing**: 外部。 **hedge**: N/A。

### System 4 — Long Trend Low-Vol Pullback
- **戦略概要**: 低ボラ上昇トレンド内の押し目買い long。
- **filter chain**: `DollarVolume50 > 100,000,000` → `HV50 ∈ [10,40]` (`system4.py:52–54,70`) → setup `Close > SMA200` (`:87`) → full-scan gate `RSI4 < 30.0` (`:526`)。
- **entry rules**: setup pass → **RSI4 昇順** (最安値優先)、top_n=20 (`:430`)。
- **exit rules**: `stop_price = High` (or `Close×1.015`) (`:350`)。target/time stop は core になし。
- **position sizing**: 外部 (atr40)。 **hedge**: N/A。

### System 5 — Long High-ADX Mean-Reversion
- **戦略概要**: 強トレンド・高ボラ regime の mean-reversion long。
- **filter chain**: `Close ≥ 5.0` → `ADX7 > 35.0` → `atr_pct > 0.025` (2.5%) (`system5.py:56–63,95–96`)。setup == filter (`:123`)。
  ⚠️ docstring は AvgVol50>500k / DV50>2.5M / Close>SMA100+ATR10 / RSI3<50 も列挙 (`:27–28`) だが **この file の filter には未実装** (乖離)。
- **entry rules**: setup pass → **ADX7 降順**ランク、top_n=20 (`:497`)。full-scan gate `ADX7 > 35` (`:642`)。
- **exit rules**: core になし。 **sizing**: 外部。 **hedge**: N/A。

### System 6 — Short 6-Day Surge Burst (mean-reversion)
- **戦略概要**: 高ボラ銘柄の 6 日急騰後 short。
- **filter chain**: `Low ≥ 5.0` → `DollarVolume50 > 10,000,000` → `HV50 ∈ (10,40) OR (0.10,0.40)` (`system6.py:54–57,99–104`) → setup `return_6d > 0.20 & UpTwoDays` (`:76,134–135`)。
- **entry rules**: setup pass → **return_6d 降順**ランク、top_n=10 (`:679`)。
- **exit rules**: core になし。 **sizing**: 外部 (atr10)。 **hedge**: N/A。

### System 7 — SPY Catastrophe Hedge (short)
- **戦略概要**: SPY 専用の下落ヘッジ。50 日安値ブレイクで SPY を空売り、short 資本の ~20%。利益狙いでなく損失緩和。
- **filter chain**: SPY のみ。atr50 / min_50 / max_70 precompute 必須 (欠損時 `IMMEDIATE_STOP`) (`system7.py:74–128`)。
- **entry rules**: setup = `Low ≤ min_50` (50 日安値) (`:130`)、翌営業日エントリ (`:264`)。
- **exit rules**: core になし。 **sizing**: 外部 (atr50)。
- **hedge trigger**: short 配分 `system2=0.40 / system6=0.40 / system7=0.20` (`final_allocation.py:100–104`)。
  SPY が新 50 日安値を付けるたびに発火。**他 system 用の独立 SPY-regime ゲートは core file になし**。

### 潜在 bug 候補 (推測 — 断言せず、file:line 引用付き)

| # | 疑い | 引用 | 深刻度 |
|---|------|------|--------|
| B1 | **entry_price に同足 Close 使用** (全 system)。comment は「翌日寄付」だが格納値は当日 Close。downstream が約定値扱いなら look-ahead / fill bias | `system1.py:1178`, `system4.py:349`, `system6.py:574`, `system7.py:269` | 高 |
| B2 | **full-path で全履歴行に「最終足」Close を付与**。過去 setup record が signal 足でなく最新足の close を持つ → backtest data leak 疑い | `system6.py:868–869,921`, `system7.py:376–378,388` | 高 |
| B3 | **System4 stop fallback `close×1.015`** — long なのに close の 1.5%「上」の stop で方向が逆に見える | `system4.py:350` | 中 |
| B4 | **System6 HV50 の dual-unit テスト** `between(10,40) | between(0.10,0.40)` — %表現と分数表現の両方を通過。上流の単位不整合があると mis-scaled 値が filter を抜ける | `system6.py:99–101` | 中 |
| B5 | **ranking key の NaN 混入**。latest_only path で roc200 NaN 行が `math.nan` のまま sort され top_n に漏れる可能性 | `system1.py:1172` | 中 |
| B6 | **`dropna` による silent frame 空化**。indicator 列が全 NaN だと全行 drop → 銘柄 silent loss (空時のみ raise) | `system6.py:271–273` | 中 |
| B7 | **System7 cache merge が stale `max_70` 優先** — 再計算値を cached 値で上書き、stale indicator 残存疑い | `system7.py:147` | 低 |
| B8 | **final_allocation stop が固定 2×ATR 両側 + entry=Close fallback** — B1 と合成で fill bias が sizing (shares) に波及 | `final_allocation.py:1253,1277–1279` | 中 |

> 注: B1/B2 は「comment=翌日寄付 vs 格納=当日Close」という記述と実装の乖離であり、
> **実約定パイプラインが entry_price を約定値として再取得しているか**を確認すれば white/black が確定する。
> core 不変制約下では確認のみ推奨、修正は別 branch。

---

## Part 2: 過去 backtest history の統計 audit

Source: `C:\Repos\quant_trading_system\results_csv\`。
**newest data = 2026-03-07** (recheck backtest)。`daily_metrics.csv` は **単日 (2026-02-06)** のみ、
`diagnostics_test/*.json` は 381 日分の triage (2025-10-05 → 2026-01-18)。
`signals_final_*.csv` は不在のため日次 signal 時系列は triage の `setup_count`/`final_count` で代替。

### 2.1 年次 signal 数 (walk-forward backtest, 2021–2025)

`integrated_backtest_sbi_*_wf_all_cap2_300k_y*.json` の `signals_per_system` を 5 年集約 (**年次合計**)。

| System | min | median | mean | max | 備考 |
|--------|-----|--------|------|-----|------|
| System1 | 124 | 399 | 385 | 498 | 2022 に 124 へ急減 (regime 依存) |
| System2 | 464 | 493 | 489 | 501 | 安定 |
| System3 | 345 | 476 | 454 | 499 | |
| **System4** | **76** | 432 | 394 | 504 | 最も regime 感応 (2022=76 vs 2024=504) |
| System5 | 327 | 454 | 434 | 484 | |
| System6 | 437 | 484 | 475 | 495 | |
| **System7** | **2** | **16** | **16** | **43** | 準休眠 hedge。年 2–43 発火のみ |

### 2.2 日次 funnel (単日 2026-02-06, `daily_metrics.csv`)

| System | prefilter | setup | candidate | entry | setup/prefilter | cand/setup |
|--------|-----------|-------|-----------|-------|-----------------|------------|
| system1 | 1352 | 928 | 10 | 10 | 0.686 | 0.011 |
| system2 | 1150 | 109 | 10 | 10 | 0.095 | 0.092 |
| system3 | 735 | 7 | 7 | 7 | 0.010 | 1.000 |
| system4 | 623 | 416 | 10 | 10 | 0.668 | 0.024 |
| system5 | 1840 | 10 | 10 | 10 | 0.005 | 1.000 |
| system6 | 1432 | 0 | 0 | 0 | 0.000 | — |
| system7 | 1 | 0 | 0 | 0 | 0.000 | — |

**binding constraint = 10-slot candidate cap**、setup 枯渇ではない (sys1: 928 setup → 10 = 1.1% capture)。

### 2.3 setup → final 生存率 (triage 381 日, 2025-10 → 2026-01)

| System | n days | setup (min/med/max) | final (min/med/max) | Σsetup | Σfinal | agg final/setup |
|--------|--------|---------------------|---------------------|--------|--------|-----------------|
| system1 | 360 | 0/2/465 | 0/2/10 | 13,139 | 1,040 | 0.079 |
| system2 | 350 | 0/0/430 | 0/0/50 | 1,147 | 821 | 0.716 |
| system3 | 375 | 0/0/36 | 0/0/10 | 559 | 755 | **1.35** ⚠ |
| system4 | 377 | 0/2/7002 | 0/2/49 | 12,857 | 1,375 | 0.107 |
| system5 | 377 | 0/1/5552 | 0/1/50 | 17,704 | 1,217 | 0.069 |
| system7 | 340 | 0/0/1 | 0/0/1 | 4 | 6 | **1.50** ⚠ |

⚠ **データ整合性異常**: system3/7 は `final > setup` の日が存在 (agg ratio >1.0)。
全 system-day の **16.3% (411/2523) で `setup ≠ final`**。setup-vs-final reconciliation の
バグ疑い — owner へ要 escalate (edge ではなくデータ整合性 note)。

### 2.4 weight 分布 (system3_final_alloc, 5 file 全て同一・6 銘柄)

named weight 列なし → `position_value / Σposition_value` で導出 (Σ=$7,552.7 / $25k budget = 30% 展開)。

| Metric | position_value ($) | derived weight |
|--------|--------------------|----------------|
| min | 554.4 | 0.073 |
| median | 1,123.5 | 0.149 |
| max | 2,160.0 | **0.286** |
| n symbols | 6 | typical 0.07–0.29 |

**system-level 固定配分** (`allocation_summary_*.json`): long sys1/3/4/5 = 各 0.25、short sys2/6 = 0.40、sys7 = 0.20。budget: long $25k / short $40k / sys7 $20k。

### 2.5 主要数字サマリ (要求 sys1/4/7)

- **sys1**: 年次 signal median 399 (range 124–498)。日次 setup→final 生存率 7.9% (agg)。
- **sys4**: 年次 signal median 432 (range **76–504**, 最大 regime 感応)。生存率 10.7%。weight は system 固定 0.25。
- **sys7**: 年次 signal median **16** (range 2–43, 準休眠)。SPY 専用 hedge。

### 2.6 entry price sanity

日次 signal snapshot が未保存のため **entry_price vs close 乖離は算出不可** (Part 3 R3 は将来 hook)。
現存の alloc file は entry_price 列を持たず、backtest json は集計値のみ。→ **infra gap** (Part 5 R2)。

---

## Part 3: 異常検知 rule set + `evaluate_survival` 統合案

`scripts/daily_polygon_monitor.py::evaluate_survival` (L253–323) は現状
**全 US universe に対する gate 生存率 (`ratio = n_pass / n_total`)** のみを算出し、
`ratio < warn_ratio` で `warn` を立てるだけ。以下 5 rule を追加提案する。

| Rule | 判定 | データ源 |
|------|------|----------|
| **R1** | sys1 日次 signal 数が過去 **p05–p95 range 外** | 日次 signal 時系列 (要保存) |
| **R2** | sys4 weight max が過去 **p99 超** | `system*_final_alloc` weight |
| **R3** | entry price が対応 Close の **±5% 超** 乖離 | signal entry_price vs grouped Close |
| **R4** | 候補生存率 (`ratio`) が過去 **p05 未満** — feed 障害 / logic 変更疑い | `evaluate_survival` の `ratio` |
| **R5** | skip 理由 top1 が**前日から急変** (順位入替 or share +20pt) | `skip_details_system*.csv` |

### 提案パッチ (diff 案 — **実装せず**)

現 `evaluate_survival` は universe survival ratio しか持たないため、R1/R2/R3 は
signal/alloc レイヤの入力を要する。以下は **R4 (ratio ベース異常) を最小侵襲で組込む** 案。
R1/R2/R3/R5 は日次 signal・alloc・skip の時系列保存を前提とする後続 iteration とする。

```diff
--- a/scripts/daily_polygon_monitor.py
+++ b/scripts/daily_polygon_monitor.py
@@ SystemSurvival dataclass に anomaly フィールド追加
 @dataclass
 class SystemSurvival:
     system: str
     n_pass: int = 0
     n_total: int = 0
     ratio: float = 0.0
     warn_threshold: float = 0.0
     status: str = "pending"
+    anomaly_flags: list[str] = field(default_factory=list)  # R1..R5 hit rule id
     survived_tickers: list[str] = field(default_factory=list)
     rejected_tickers: list[str] = field(default_factory=list)

     def as_dict(self) -> dict[str, Any]:
         return {
             "n_pass": self.n_pass,
             "n_total": self.n_total,
             "ratio": round(self.ratio, 4),
             "warn_threshold": self.warn_threshold,
             "status": self.status,
+            "anomaly_flags": self.anomaly_flags,
         }

@@ 過去分位の baseline (Part 2 統計から算出、外部 JSON で更新可能に)
+# survival ratio の過去 p05 (feed 障害 / logic 変更検知用)。
+# Part 2 統計が未整備のため暫定値。日次 report 蓄積後に自動再フィットする。
+SURVIVAL_RATIO_P05: dict[str, float] = {
+    "sys1": 0.010, "sys2": 0.020, "sys3": 0.020,
+    "sys4": 0.005, "sys5": 0.030, "sys6": 0.040, "sys7": 1.0,
+}

@@ evaluate_survival() 内、status 判定の直後 (L319 付近)
         s.status = "warn" if s.ratio < s.warn_threshold else "ok"
+        # --- R4: 候補生存率が過去 p05 未満 → feed 障害 / logic 変更疑い ---
+        p05 = SURVIVAL_RATIO_P05.get(sysname)
+        if p05 is not None and s.n_total > 0 and s.ratio < p05:
+            s.anomaly_flags.append("R4_survival_below_p05")
+            s.status = "fail"  # p05 割れは warn より重い
         s.survived_tickers = survived
```

R1/R2/R3/R5 の hook (別 module で実装推奨、`evaluate_survival` の外):

```python
# scripts/anomaly_rules.py (新規案 — 未実装)
def check_signal_anomalies(signals_df, alloc_df, skip_today, skip_prev, baseline):
    flags = []
    # R1: sys1 日次 signal 数 p05-p95 外
    n1 = len(signals_df.query("system == 'system1'"))
    if not (baseline["sys1_p05"] <= n1 <= baseline["sys1_p95"]):
        flags.append(("R1", f"sys1 signals={n1} out of [{baseline['sys1_p05']},{baseline['sys1_p95']}]"))
    # R2: sys4 weight max > 過去 p99
    w4 = alloc_df.query("system == 'system4'")["weight"].max()
    if w4 > baseline["sys4_weight_p99"]:
        flags.append(("R2", f"sys4 wmax={w4:.4f} > p99={baseline['sys4_weight_p99']:.4f}"))
    # R3: entry price vs Close 乖離 > 5%
    dev = (signals_df["entry_price"] / signals_df["close"] - 1).abs()
    if (dev > 0.05).any():
        flags.append(("R3", f"{int((dev>0.05).sum())} signals >5% entry/close deviation"))
    # R5: skip 理由 top1 が前日から急変
    if skip_today and skip_prev:
        t_top = max(skip_today, key=skip_today.get)
        p_top = max(skip_prev, key=skip_prev.get)
        if t_top != p_top:
            flags.append(("R5", f"skip top1 shifted {p_top} -> {t_top}"))
    return flags
```

> **R5 の限界**: 現 `skip_details_system*.csv` は system あたり **単一 reason code** しか記録せず
> (sys2=`stale_over_month`×1150、sys6=`not_shortable`×17 等)、top-5 多様性が無い。
> R5 を機能させるには skip logging を **filter-gate 別カウント**に拡張する必要がある。
>
> **前提**: R1/R2/R3/R5 は日次 signal・alloc・skip snapshot の**永続化が未整備**。
> 現状 `results_csv` は上書き型で時系列が残らない (Part 2 のデータ制約の根因)。
> **最優先 infra タスク = 日次 signal/alloc/skip の date-stamped 保存** (Part 5 R2 参照)。

---

## Part 4: 既知の技術的負債・懸念事項 (docs grep)

| # | 項目 | 出典 | 種別 | 状態 |
|---|------|------|------|------|
| TD1 | **CI auto-trigger 全停止** — `daily-signals.yml` の cron を含む push/PR/schedule を無効化。日次 signal は手動 dispatch のみ | `docs/CI_PAUSED.md` | 運用 | 未復旧 |
| TD2 | **SPY 不在時に早期停止しない** — SPY 依存の sys1/4/7 が空/ゼロ data で最後まで継続、警告 log のみ | `docs/internal/signal_system_improvement_analysis.md §9`, `scripts/run_all_systems_today.py:3223–3261,2942–3021` | 品質 | 未修正 |
| TD3 | **system6 実行時間過剰** — cache 再利用時も 50 日分を全再計算 (ATR/rolling) | 同 §2, `core/system6.py:145–399` | 性能 | 未修正 |
| TD4 | `recover_spy_cache.py` エラー解析 — SPY cache 復旧経路の error handling 不備 | 同 §3 | 運用 | 未修正 |
| TD5 | エラーメッセージ体系 / CLI-UI ログ同期 / 進捗率乖離 | 同 §1,4,5,6,8 | UX/運用 | 部分 |
| TD6 | Exit 関連テスト未整備 (`test_exit_planner` / `test_trade_history` 推奨) | `docs/CODE_REVIEW_2025-11-03.md` | 品質 | 未着手 |

**技術的負債 top 3**: TD1 (CI/cron 停止 → 自動配信の生命線が手動)、TD2 (SPY 欠損の無警告継続 → hedge/regime 誤判定)、TD3 (system6 性能 → 日次バッチ遅延リスク)。

---

## Part 5: 事業運用開始前に解消すべきリスク top 5

| 順位 | リスク | 影響度 | 発火頻度 | 対策 (工数 / downtime) |
|------|--------|--------|----------|------------------------|
| **1** | **CI/cron 停止で日次 signal が手動 dispatch 依存** (TD1)。人が忘れると当日配信ゼロ = subscriber SLA 違反 | システム停止 / 顧客信頼 | 常時 (毎営業日) | `daily-signals.yml` cron 復旧 + 失敗時 ntfy/email alert。**0.5–1 日 / downtime ~0** |
| **2** | **日次 signal/alloc/skip の時系列が未保存** (上書き型)。異常検知 (Part 3 R1–R3,R5) も事後監査も不可能 | 顧客損失検知不能 / 法務(監査証跡) | 常時 | date-stamped snapshot 保存 + 30 日 retention。**1–2 日 / downtime ~0** |
| **3** | **entry_price = 同足 Close の look-ahead 疑い** (B1/B2)。backtest 実績が実約定より楽観的なら誇大広告・顧客損失 | 顧客損失 / 法務(誇大表示) | 常時 (全 signal) | 実約定 vs entry_price の突合監査。leak 確定なら next-open 化。**2–3 日 (core 別branch) / downtime ~0** |
| **4** | **SPY 欠損時の無警告継続** (TD2)。hedge (sys7) と regime gate が誤動作 → 過大 short / 無ヘッジ | 顧客損失 / システム | 稀 (feed 障害時) | フェーズ0で SPY 鮮度検証、欠損は FAIL 停止 + 復旧導線。**1 日 / downtime = 障害時のみ** |
| **5** | **Exit ロジックが core 外 + 未テスト** (TD6)。stop/target の regression が無検知で本番混入 | 顧客損失 | 月次(改修時) | `test_exit_planner`/`test_trade_history` 追加、entry-exit E2E。**2 日 / downtime ~0** |

---

## Appendix: audit メタ

- data 制約により Part 2 は導出可能範囲に限定 (日次 signal 時系列は未保存)。
- Part 3 patch は **doc 内 diff 案のみ、未実装**。`core/system1-7` / `CacheManager` は不変。
- 実 live signals は 2026-07-02 06:00 tick 後に初出 → 本 baseline で初日を照合すること。
