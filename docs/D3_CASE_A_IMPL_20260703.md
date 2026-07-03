# D3 Case A 実装レポート — System5 流動性 filter + ATR 4% (docs 完全準拠)

**日付**: 2026-07-03
**対象**: `core/system5.py`, `common/system_setup_predicates.py`, `common/system_constants.py`, `common/today_signals.py`
**判断根拠**: `docs/D3_LIQUIDITY_FILTER_ATR_THRESHOLD_20260702.md` (Case A/B/C 3 case 比較) の Case A 採用
**ユーザ判断**: 「事業重視 = docs (原設計思想) 準拠で設計意図の透明性を最優先。data pipeline の filter 数が減っても OK」
**先行 dispatch**: `docs/DIVERGENCE_ANALYSIS_20260702.md` (Item 3 = D3 の深掘り)

---

## Executive Summary

- System5 の filter 3 条件 (docs/systems/システム5.txt:6-9) を実 gate として enforce。
- ATR 閾値 0.025 → 0.04 (spec 準拠に是正)。
- AvgVolume50 > 500k / DollarVolume50 > 2.5M を filter に新規追加 (実 gate 化)。
- Dead constant `SYSTEM5_MIN_DOLLAR_VOLUME = 25_000_000` を spec 準拠の `2_500_000` に是正。
- 予想影響: 5y proxy sim で top-20 候補 236 → 44、unique 銘柄 73 → 16。
- 他 system (1, 2, 3, 4, 6, 7) は既に docs 準拠。verify only の change log を追記。

---

## Before / After 閾値 table

### System 5 (D3 Case A)

| 対象 | 変数 | Before | After | Docs |
|------|------|--------|-------|------|
| ATR 閾値 | `core/system5.DEFAULT_ATR_PCT_THRESHOLD` | 0.025 | **0.04** | 4% |
| ATR 閾値 (predicate) | `common/system_setup_predicates.DEFAULT_ATR_PCT_THRESHOLD` | 0.025 | **0.04** | 4% |
| ATR 閾値 (定数) | `SYSTEM5_ATR_PCT_THRESHOLD` | 0.025 | **0.04** | 4% |
| AvgVolume50 | `core/system5.MIN_AVG_VOLUME_50` | (未定義) | **500_000** | > 500k 株 |
| AvgVolume50 (定数) | `SYSTEM5_MIN_AVG_VOLUME_50` | (未定義) | **500_000** | > 500k 株 |
| DollarVolume50 | `core/system5.MIN_DOLLAR_VOLUME_50` | (未定義) | **2_500_000** | > 2.5M $ |
| DollarVolume50 (dead) | `SYSTEM5_MIN_DOLLAR_VOLUME` | 25_000_000 (dead) | **2_500_000** (活きた gate) | > 2.5M $ |
| SYSTEM5_REQUIRED_INDICATORS | list | (`sma100`, `rsi3` は 2026-07-02 追加済) | + **`avgvolume50`, `dollarvolume50`** | — |

### 他 system (verify only)

| System | 検証結果 |
|--------|---------|
| System 1 | DV20>50M, Close>=5 → 既に docs 完全準拠。追加変更なし。 |
| System 2 | DV20>25M, Close>=5, ATR_Ratio>3% → 既に docs 完全準拠。追加変更なし。 |
| System 3 | Low>=1, AvgVol50>=1M, ATR_Ratio>=5% → 2026-07-02 spec revert 済み。追加変更なし。 |
| System 4 | DV50>100M, HV50 in [10, 40] → 既に docs 完全準拠。追加変更なし。 |
| System 6 | Low>=5, DV50>10M, HV50 in [10, 40] → 既に docs 準拠 (HV は 2026-07-02 docs 側追記済み)。追加変更なし。 |
| System 7 | フィルター無し (SPY 固定) → 追加変更なし。 |

---

## System5 filter chain — 変更点詳細

### Before (git HEAD 685bdb5)

```python
# core/system5.py:104-105
computed_filter = (
    (close >= MIN_PRICE) & (adx7 > MIN_ADX) & (atr_pct > DEFAULT_ATR_PCT_THRESHOLD)
).fillna(False)
# DEFAULT_ATR_PCT_THRESHOLD = 0.025
# AvgVolume50 / DollarVolume50 の gate は無し
```

`common/today_signals.py` の該当箇所は診断カウンタ (`s5_av`, `s5_dv`) を数えるだけで、`_price_ok / _adx_ok / _rsi_ok` に反映されない「見せかけの流動性 filter」だった。

### After (working tree, this dispatch)

```python
# core/system5.py:_apply_filter_conditions
close   = pd.to_numeric(result["Close"], errors="coerce")
adx7    = pd.to_numeric(result["adx7"], errors="coerce")
atr_pct = pd.to_numeric(result["atr_pct"], errors="coerce")
avgvol50 = pd.to_numeric(result["avgvolume50"], errors="coerce")
dv50     = pd.to_numeric(result["dollarvolume50"], errors="coerce")

computed_filter = (
    (close >= MIN_PRICE)
    & (adx7 > MIN_ADX)
    & (atr_pct > DEFAULT_ATR_PCT_THRESHOLD)   # 0.04 = 4%
    & (avgvol50 > MIN_AVG_VOLUME_50)          # 500_000
    & (dv50 > MIN_DOLLAR_VOLUME_50)           # 2_500_000
).fillna(False)
```

`common/system_setup_predicates.system5_setup_predicate` にも同一の 2 条件を同期。`common/today_signals.py` の diagnostic ブロックには「実 gate は core 側に格上げ済み」旨のコメントを追加。

---

## 変更ファイル一覧 (Windows 側 working tree)

### code
- `core/system5.py` — 定数追加、filter 拡張、header/docstring 更新
- `common/system_setup_predicates.py` — `DEFAULT_ATR_PCT_THRESHOLD` 0.025→0.04、`MIN_AVG_VOLUME_50_SYSTEM5` / `MIN_DOLLAR_VOLUME_50_SYSTEM5` 新設、`system5_setup_predicate` に 2 条件同期
- `common/system_constants.py` — `SYSTEM5_MIN_DOLLAR_VOLUME` 25M→2.5M、`SYSTEM5_ATR_PCT_THRESHOLD` 0.025→0.04、`SYSTEM5_MIN_AVG_VOLUME_50` (500k) 追加、`SYSTEM5_REQUIRED_INDICATORS` に 2 列追加、`SYSTEM_CONFIGS["system5"]` に配線
- `common/today_signals.py` — diagnostic block (旧 1140-1157) に Case A 実 gate 格上げ済みコメント追記

### test
- `tests/test_systems_filter_setup_spec_compliance.py` — System5 の閾値 assertion を spec 値 (4%, 500k, 2.5M) に update
- `tests/test_d3_case_a_liquidity_filter.py` — 新規 (System5 boundary + predicate 同期 + Case A ⊂ Case B property test)
- `tests/test_d3_docs_alignment.py` — 新規 (7 system 横断 alignment matrix test)

### docs
- `docs/systems/システム5.txt` — 主要 change log (D3 Case A の全変更を記録)
- `docs/systems/システム1.txt` / `システム2.txt` / `システム3.txt` / `システム4.txt` / `システム6.txt` / `システム7.txt` — verify only change log 追記
- `docs/D3_CASE_A_IMPL_20260703.md` — 本ファイル

---

## 予想影響 (subscriber / backtest)

Micro-bench proxy sim (`docs/D3_LIQUIDITY_FILTER_ATR_THRESHOLD_20260702.md` Phase 3、5y sample 496 symbols) の相対比較:

| 指標 | Case B (旧 impl) | Case A (docs, this dispatch) | 比率 |
|------|-----------------|------------------------------|------|
| filter 通過 row-days | 2,685 | 435 | 16.2% |
| setup 通過 row-days | 236 | 44 | 18.6% |
| top-20/day 候補総数 | 236 | 44 | 18.6% |
| signal 発生日数 (500 日弱) | 161 | 40 | 24.8% |
| **unique 銘柄数** | **73** | **16** | **21.9%** |

### 実運用への効果 (事業 lens)

- **subscriber スリッページ risk 解消**: 低 DV 銘柄 (DV50 < 2.5M) が top-20 候補から除外される。旧 impl では低 DV 銘柄が混入して backtest と実運用リターンが乖離する リスクがあったが、Case A で塞がる。
- **配信頻度の低下トレードオフ**: System5 の subscriber 配信は「日次 1 銘柄前後」から「週数銘柄」に減る想定。ポートフォリオ内の System5 寄与比が低下するため、他 sub-system (S1/S3/S4) との allocation バランスは要再検証。
- **設計思想の透明性優先**: 「原設計者が示した spec 通り」であることが、subscriber 説明資料上のシンプルさと将来のメンテ容易性を担保する。

### 絶対値 R multiple の解釈 (limitations)

proxy sim の avg_R は全 case でマイナス (position sizing / slippage 未反映のノイズ) のため、絶対値ではなく **相対 trade 数** のみを判断根拠に使用。full backtest は Windows 側で `scripts/run_all_systems_today --backtest` を走らせる想定。

---

## Windows 側での明日 06:00 tick 後の filter 数変化確認方法

```powershell
# 1. daily_polygon_monitor 出力 (pipeline_YYYYMMDD.json) の system5 FILpass / STUpass 数を確認
Get-Content C:\Repos\quant_trading_system_0510to0906\reports\pipeline_20260704.json | ConvertFrom-Json |
    Select-Object -ExpandProperty pipeline_by_system |
    Where-Object { $_.system -eq "sys5" } |
    Select-Object system, @{n="FILpass";e={($_.phases | Where-Object name -eq "FILpass").count}}, @{n="STUpass";e={($_.phases | Where-Object name -eq "STUpass").count}}, @{n="TRDlist";e={($_.phases | Where-Object name -eq "TRDlist").count}}

# 2. 前日 (Case B 時代) との比較
$today  = Get-Content C:\Repos\quant_trading_system_0510to0906\reports\pipeline_20260704.json | ConvertFrom-Json
$prev   = Get-Content C:\Repos\quant_trading_system_0510to0906\reports\pipeline_20260703.json | ConvertFrom-Json
Write-Host "FILpass sys5: prev=$($prev.pipeline_by_system | Where-Object system -eq 'sys5' | ForEach-Object { ($_.phases | Where-Object name -eq 'FILpass').count }), today=$($today.pipeline_by_system | Where-Object system -eq 'sys5' | ForEach-Object { ($_.phases | Where-Object name -eq 'FILpass').count })"

# 3. today_signals.py 実行時に log_callback で "🧪 system5集計" line を検索
Select-String -Path C:\Repos\quant_trading_system_0510to0906\logs\today_signals_20260704.log -Pattern "system5集計|system5セットアップ集計"

# 期待値 (proxy sim 相対): FILpass 数が Case B 比 ~15-20% レベル、TRDlist は同水準 ~19%。
```

---

## 参考

- `docs/D3_LIQUIDITY_FILTER_ATR_THRESHOLD_20260702.md` — Case A/B/C 3 case 判断ペーパー (micro-bench 数値付き)
- `tests/DIVERGENCE_ANALYSIS_20260702.md` — 5 件 docs 乖離 (Item 3 = D3) の深掘り
- `docs/systems/システム5.txt` — spec 原文 + 2026-07-03 change log
- `tests/test_d3_case_a_liquidity_filter.py` — System5 特化 boundary/property test
- `tests/test_d3_docs_alignment.py` — 全 7 system 横断 alignment matrix test
- `tests/test_systems_filter_setup_spec_compliance.py` — spec-compliance boundary test (Case A 反映済み)
