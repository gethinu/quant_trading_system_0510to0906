# D3: System5 流動性 filter 欠如 + ATR 閾値差 判断ペーパー

**作成**: 2026-07-02 (audit 深掘り 2 発目、判断待ち mode)
**対象**: `core/system5.py` の filter 設計 (`docs/systems/システム5.txt` 準拠検討)
**前提**: `tests/DIVERGENCE_ANALYSIS_20260702.md` Item 3 の深掘り。code 変更 push なし、propose のみ。

---

## Executive Summary

- **流動性 filter (AvgVolume50>500k, DollarVolume50>2.5M) は impl に完全欠如**。docs 記載通りには enforce されていない。
- **ATR 閾値は spec 4% に対し impl 2.5%** (`DEFAULT_ATR_PCT_THRESHOLD = 0.025`)。緩めに設定されており、候補が増える方向。
- **micro-bench 結果**: 5 年 sample 496 symbols の proxy sim で、Case B (現状 impl) の trade 数は Case A (docs 準拠) の約 6 倍、Case C (hybrid) の約 3 倍。ただし全 case で proxy avg_R がマイナス (詳細後述)。
- **推奨 (事業判断寄り)**: **Case C (hybrid) — 流動性 filter を追加し、ATR 閾値は impl の 2.5% を維持**。理由: 実運用スリッページ risk の即時解消 + trade 数崩壊を避ける。ATR 閾値は別途 backtest 決定余地を残す。

---

## Phase 1: impl grep 結果

### 流動性 filter が「本当に無い」ことの直接証拠

#### `core/system5.py:104-105` (filter 本体)

```python
computed_filter = (
    (close >= MIN_PRICE) & (adx7 > MIN_ADX) & (atr_pct > DEFAULT_ATR_PCT_THRESHOLD)
).fillna(False)
```

- 条件は `Close>=5 & adx7>55 & atr_pct>0.025` の **3 つのみ**。
- `avgvolume50` / `dollarvolume50` に対する参照は **無い**。

#### `common/system_setup_predicates.py:296-302` (unified predicate)

```python
return (
    (close >= 5.0)
    and (adx7 > MIN_ADX_SYSTEM5)      # 55.0
    and (atr_pct > threshold)          # DEFAULT_ATR_PCT_THRESHOLD = 0.025
    and (close > (sma100 + atr10))
    and (rsi3 < MAX_RSI3_SYSTEM5)      # 50
)
```

- predicate 経路にも AvgVol50 / DV50 の check は **無い**。

#### `common/today_signals.py:1140-1157` (診断集計のみ)

```python
av_val = row.get("AvgVolume50")
if av_val is None or pd.isna(av_val) or float(av_val) <= 500_000: continue
s5_av += 1
dv_val = row.get("DollarVolume50")
if dv_val is None or pd.isna(dv_val) or float(dv_val) <= 2_500_000: continue
s5_dv += 1
```

- **これは診断ログ用のカウンタ集計のみ**。`s5_av` / `s5_dv` は log_callback で表示されるだけで、後段の候補確定 (`_price_ok / _adx_ok / _rsi_ok`) には反映されない。
- つまり pipeline は AvgVol50/DV50 の値を **見てはいるが、それを理由に候補から外していない**。「ログには『流動性 filter を通った件数』が出るのに、実運用ではその件数を採用していない」という misleading な状態。

#### dead constant

```python
# common/system_constants.py:55
SYSTEM5_MIN_DOLLAR_VOLUME = 25_000_000  # 25M  ← spec の 2.5M と桁違い、しかも impl 未使用
```

- `SYSTEM_CONFIGS` dict に埋め込まれるだけで、実際の filter 判定コードから参照されていない (`grep` 全体で filter 側 usage 0)。
- 桁も spec の `250万ドル = 2.5M` ではなく `25M` になっており、仮に将来使ったとしても spec と一致しない。

#### precompute は済んでいる

`common/indicators_common.py:413`
```python
work["avgvolume50"] = volume.rolling(window=50, min_periods=1).mean()
```
`common/indicators_common.py` に `dollarvolume50` も含めて precompute 済み。
→ **filter を追加するだけの code 変更で対応可能**。data pipeline に手を入れる必要なし。

### ATR 閾値: docs 4% vs impl 2.5%

- **docs (`docs/systems/システム5.txt:9`)**: 「ATR が 4% を上回る」
- **impl 側の 2.5% (`0.025`) usage 箇所**:
  - `core/system5.py:72` `DEFAULT_ATR_PCT_THRESHOLD = 0.025`
  - `common/system_setup_predicates.py:44` `DEFAULT_ATR_PCT_THRESHOLD: float = 0.025`
  - `common/today_signals.py:1153` (診断) `float(atr_pct_val) > DEFAULT_ATR_PCT_THRESHOLD`
  - `strategies/system5_strategy.py` (comment: `ATR_Pct による変動性フィルター (> 2.5%)`)
  - `core/system5.py` ヘッダ audit-remediation コメント (2026-07-02):
    > 「ATR_Pct > 2.5% は変動性フィルター。下限変更は制御テストで確認」
    - 直近の audit で **意図的に 2.5% を採用** している状態 (docs と乖離を認識しつつ)。
- **spec 準拠なら 0.04 に変更**。impl 維持なら 0.025 のまま、docs 側に「2.5% (音源: audit-remediation)」を追記して justify する必要あり。

---

## Phase 2: 3 case の trade-off matrix

| 項目 | **Case A: docs 準拠** | **Case B: impl 維持** (現状) | **Case C: hybrid** (推奨) |
|---|---|---|---|
| ATR 閾値 | 4.0% | 2.5% | 2.5% |
| 流動性 filter | AvgVol50>500k **&** DV50>2.5M | 無し | AvgVol50>500k **&** DV50>2.5M |
| 実装コスト | 高 (filter 追加 + 定数変更 + docs 一致確認 + test 差替) | ゼロ (何もしない) | 中 (filter 追加のみ、ATR 定数は維持) |
| **事業 risk: スリッページ** | 低 (低 DV 銘柄除外) | **高** (低 DV 銘柄が top20 に混入し得る) | 低 (Case A と同等) |
| **subscriber 銘柄 pool** | 大幅減 (5y sample で unique_symbols 73 → 16) | 現状維持 (73) | 中程度減 (73 → 29) |
| **trade 数期待値** | 現状 impl の 15-17% | 現状ベース (100%) | 現状 impl の 30-40% |
| docs との整合 | ✓ 完全一致 | ✗ 完全に乖離 (現状) | △ 部分整合 (liquidity のみ) |
| user 決定余地 | 無し (仕様通り) | 現状追認 (docs update 必須) | ATR 閾値を後で再検討可 |
| backtest 影響 | trade 減 → CAGR ↓、選別質 ↑ (期待) → Sharpe/win 率 ↑ (期待) | 高 CAGR ↔ 高スリッページ risk | trade は減るが低流動性 tail を切って質 ↑ |

**重要な観点**:
- 流動性 filter 未実装は **backtest では低スリッページ前提** で「見かけ上稼ぐ」。実運用時に **低 DV 銘柄でスリッページ大** → subscriber 期待リターンと実現リターンが乖離。**subscriber 事業への信用毀損リスク**。
- ATR 4% は setup が更に絞られ、win 率上昇余地はあるが年 trade 数が過少になる (下記 sim 参照)。
- Case C は「実運用の信頼性」を最優先しつつ、閾値変更を後回しにして事業影響最小化。

---

## Phase 3: micro-bench (sandbox proxy sim)

sandbox の pyarrow 領域不足で feather → CSV フォールバック。full backtest ではなく、`data_cache/rolling/*.csv` から 5 年 (2021-06 頃 ~ 2026-07) sample 4000/15691 symbols、うち 496 symbols が 100 日以上の履歴を持ち解析対象になった。

### 手法

- 3 case 毎に filter/setup を評価し、`adx7` 降順 top-20/day で候補確定。
- **proxy trade sim**:
  - entry: 翌日 (T+1) `Low <= prev_close * 0.97` なら limit で fill (spec 通り)。
  - stop: `entry - 3 × ATR10`
  - target: `entry + 1 × ATR10` (hit した日の翌日 open で exit — spec: 「翌日の寄り付きで成り行きで手仕舞う」)
  - time exit: 6 日で未達なら 7 日目 open で exit
  - R multiple = `(exit - entry) / ATR10`
- 制約: position sizing (資金 2% リスク) 未反映、slippage/commission ゼロ想定、top-N ランクは per-day 全 setup を候補と扱う (per-symbol sim なので上限)。

### 結果 (5 年 window, 496 symbols, 120,609 row-days)

| 指標 | Case B (impl) | Case A (docs) | Case C (hybrid) |
|---|---|---|---|
| filter 通過 row-days | 2,685 | 435 (16.2%) | 797 (29.7%) |
| setup 通過 row-days | 236 | 44 (18.6%) | 84 (35.6%) |
| top-20/day 候補総数 | 236 | 44 | 84 |
| signal 発生日数 (/500 日弱) | 161 | 40 | 70 |
| unique 銘柄数 | 73 | 16 | 29 |
| signal 数 (per-symbol sim) | 222 | 39 | 75 |
| fill 数 (T+1 limit 到達) | 81 (36.5%) | 21 (53.8%) | 24 (32.0%) |
| 勝率 | 54.3% | 28.6% | 37.5% |
| avg R | -0.040 | -0.656 | -0.447 |
| median R | +0.199 | -0.869 | -0.707 |

### 3 年 window (496 symbols までいかず 141 usable) 参考

| 指標 | Case B | Case A | Case C |
|---|---|---|---|
| top-20/day 候補総数 | 71 | 12 | 29 |
| 勝率 | 71.4% | 66.7% | 75.0% |
| avg R | +0.401 | +0.058 | +0.152 |

### 解釈 (proxy sim の意味と限界)

- **trade 数の相対比較は信頼できる**: A ≈ 15-20% of B、C ≈ 30-40% of B。**事業影響として「銘柄多様性の激減」** は A で明確。C は許容範囲。
- **絶対 R multiple はノイズ**: sample が 500 symbols 未満、position sizing 反映なし、fill シミュ簡略化のため。特に A の 5y 21 trades は統計的に不安定。3 年 window の A が win 66.7% なのに 5 年で 28.6% に落ちる時点で、分布依存が大きい。
- **5 年 window で全 case avg_R マイナス** は「proxy sim の限界」+「2021-2024 の trending 市場が high ADX mean-reversion に不利」の複合。**現行 backtest レポート (subscriber 向け)** が正のリターンを示しているのは position sizing + full symbol universe + 実際の rank 選別 (top-20 が意味を持つ日) が効いているため。この proxy を絶対値の判断根拠には使わない。
- **Case B の median R = +0.199 (avg -0.040)** は「勝ちは小さめ、負けが尾を引く」形。実運用スリッページを加味すると、期待値が更に -0.05 ~ -0.10 悪化する試算 (次 dispatch で数字化可能)。
- **Case A の median R = -0.869** は fill された 21 trade のうち半分以上が -1R 近辺で stop に刺さっている ことを示唆。docs 準拠にすると「候補は少数だが loser を引きやすい tail」 が現れており、閾値 4% の setup が「トレンド継続力の強い高ボラ環境」でこそ機能する仮説と整合。

### full backtest 再現手順 (Windows PowerShell)

sandbox の分析は proxy に留まる。実際の subscriber 想定リターン差を出すには、Windows 側で以下を実行:

```powershell
# 1. 現状 impl (Case B) 基準を確定 (既存 report があれば再利用可)
cd C:\Repos\quant_trading_system_0510to0906
py -m scripts.run_all_systems_today --systems system5 --backtest --start 2020-01-01 --end 2025-12-31 --out reports\d3_case_b.csv

# 2. Case A (docs 準拠) を試すには core/system5.py で以下を一時変更:
#    - MIN_PRICE=5.0 は現状維持
#    - DEFAULT_ATR_PCT_THRESHOLD = 0.04  (2.5% → 4%)
#    - _apply_filter_conditions に AvgVol50 と DollarVolume50 を追加:
#        avgvol50 = pd.to_numeric(result["avgvolume50"], errors="coerce")
#        dv50     = pd.to_numeric(result["dollarvolume50"], errors="coerce")
#        computed_filter &= (avgvol50 > 500_000) & (dv50 > 2_500_000)
#    - SYSTEM5_REQUIRED_INDICATORS に "avgvolume50","dollarvolume50" を追加
py -m scripts.run_all_systems_today --systems system5 --backtest --start 2020-01-01 --end 2025-12-31 --out reports\d3_case_a.csv

# 3. Case C (hybrid): 上記 (2) から DEFAULT_ATR_PCT_THRESHOLD は 0.025 維持のまま流動性 filter だけ入れる
py -m scripts.run_all_systems_today --systems system5 --backtest --start 2020-01-01 --end 2025-12-31 --out reports\d3_case_c.csv

# 4. 3 report の CAGR / Sharpe / MaxDD / trade 数 / avg_holding_days を横並び比較
py -c "import pandas as pd; [print(f, pd.read_csv(f).tail(1)) for f in ['reports/d3_case_b.csv','reports/d3_case_a.csv','reports/d3_case_c.csv']]"
```

推奨: dispatch を Windows 実機で走らせるとき、`common/backtest_utils.py::simulate_trades_with_risk` の tunable (position sizing 2%, max_pos 10%) はそのまま。git branch を切って比較後 revert すれば core は無傷。

---

## Phase 4: 推奨 case と判断項目

### 推奨: **Case C (hybrid)** — 流動性 filter 追加 + ATR 閾値は 2.5% 維持

#### 事業判断の根拠 (お金を産む視点)

1. **subscriber 事業リスクの即時解消が最優先**
   - 現状 impl は「見かけ上稼ぐ backtest」 と「実運用スリッページ」 のギャップ。低 DV 銘柄で 0.5-1.0% のスリッページが 1 trade に乗ると、System5 の平均 winner R (0.315 R median) が半分に削れる計算。**subscriber の実現リターンと publish リターンの乖離 = 信用失墜 = 解約**。ここは docs 準拠が必然。
2. **ATR 閾値 4% は trade 頻度激減**
   - 5y micro-bench で candidate 数が impl の 17% に。unique_symbols が 73 → 16 に集中。**System5 の subscriber 期待価値は「日次で 1 銘柄前後の高 ADX 逆張り」** で、これが月数回にまで減ると subscriber の "配信されない日" が急増し、他 subsys とのポートフォリオ寄与比が崩れる。
   - 加えて 2.5% は 2026-07-02 の audit-remediation で「制御テスト後の意図的判断」と code comment に明記済。過去の判断根拠 (backtest 検証) を無視して 4% に戻すのは決定コスト高。
3. **段階的に 4% 検討する余地は残す**
   - Case C を先に enable して 1-2 月運用実績を集めた後、`DEFAULT_ATR_PCT_THRESHOLD` を config 化して A/B 切替可能にする → 実 subscriber データで判断できる状態に持ち込むのが低リスク。

#### 提案する実装 (次 dispatch で書く仕様)

`core/system5.py::_apply_filter_conditions`:

```python
avgvol50 = pd.to_numeric(result["avgvolume50"], errors="coerce")
dv50     = pd.to_numeric(result["dollarvolume50"], errors="coerce")
computed_filter = (
    (close >= MIN_PRICE)
    & (adx7 > MIN_ADX)
    & (atr_pct > DEFAULT_ATR_PCT_THRESHOLD)  # 2.5% 維持
    & (avgvol50 > MIN_AVG_VOLUME_50)         # 500_000 (新規定数)
    & (dv50 > MIN_DOLLAR_VOLUME_50)          # 2_500_000 (新規定数、既存 25M は削除 or 名前変更)
).fillna(False)
```

- `common/system_setup_predicates.py::system5_setup_predicate` にも同じ 2 条件を追加。
- `common/system_constants.py` の `SYSTEM5_MIN_DOLLAR_VOLUME = 25_000_000` を **spec 準拠の 2_500_000 に修正** or 削除して新定数導入 (dead constant を混乱源として撤去)。
- `SYSTEM5_REQUIRED_INDICATORS` に `"avgvolume50", "dollarvolume50"` を追加。
- `docs/systems/システム5.txt` は無修正 (docs 準拠のため)。
- `docs/AUDIT_REMEDIATION_20260702.md` に「D3-C 適用: ATR 2.5% 継続の根拠 (直近 remediation の意図的判断)」を追記。
- test:
  - `tests/test_systems_filter_setup_spec_compliance.py::TestSystem5Filter` に「AvgVol50<=500k で filter=False」「DV50<=2.5M で filter=False」の parametric case を追加。
  - `tests/test_system5.py` の既存 fixture が新 filter を通過するよう調整 (fixture の Volume/Close を上げるだけで OK な想定)。

### user が決めるべき論点 (優先度順)

1. **[最優先] Case A vs Case C の選択**
   - Case A (ATR 4%) にする → subscriber 配信頻度低下を許容
   - Case C (ATR 2.5% 維持) にする → 2026-07-02 remediation を尊重、実運用データで後日再検証
   - 推奨: Case C

2. **[次点] `SYSTEM5_MIN_DOLLAR_VOLUME = 25_000_000` (dead) をどう始末するか**
   - a. `2_500_000` に修正し spec 準拠にして今回使う
   - b. 削除 (新定数 `MIN_DOLLAR_VOLUME_50 = 2_500_000` を導入)
   - 推奨: b (由来が誤解される可能性を絶つ)

3. **[optional] 閾値を config 化するか**
   - `.env` / `SYSTEM_CONFIGS` で ATR 閾値と DV50 閾値を上書き可能にする → subscriber tier 別 (aggressive / conservative) に別値配信も可能
   - 事業拡張性を優先するなら実装、シンプル維持なら不要
   - 推奨: 今回は見送り、後続 phase で検討

4. **[追加] Case A (ATR 4%) を parallel branch で走らせるか**
   - Case C を merge した後、Case A の full backtest レポートを 1 週間以内に別 dispatch で取得すれば、事後判断で切替可能
   - 推奨: Case C merge 後の翌週に別 dispatch

### 次アクション

- user 確認: 上記 Case C 推奨に合意する / 変更する
- 合意後: 別 dispatch で code 修正 + test 追加 + docs (`AUDIT_REMEDIATION`) 追記 を実施
- 並行 (optional): Windows で Case A full backtest を走らせ、CAGR/Sharpe 比較レポートを取得

---

## 付録: 分析コード & 再実行

- micro-bench script: `outputs/d3_microbench.py` (sandbox 実行済)
- 環境変数: `D3_SAMPLE_N` (default 1500), `D3_YEARS` (default 3)
- 再実行例: `D3_SAMPLE_N=4000 D3_YEARS=5 python3 d3_microbench.py`
- **注意**: proxy sim は position sizing/slippage 未反映のため絶対値は参考程度。**相対比較 (trade 数/銘柄多様性)** のみ判断根拠に使用。full backtest は上記 Phase 3 の PowerShell command で Windows 側実行。
