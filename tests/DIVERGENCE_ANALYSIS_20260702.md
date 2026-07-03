# docs vs impl 乖離 深掘り分析 (2026-07-02)

Phase 2 audit (`tests/DOCS_IMPL_DIVERGENCE_REPORT.md`) で挙げられた 5 件の乖離を、
`docs/systems/システム1-6.txt` を single source of truth として精査した結果。

**この文書は判断材料であり、実装変更は含まない。**
各項目末尾の推奨は user 承認後に別 dispatch で実装する。

---

## 前提: 判断の 3 分類

- **docs 準拠 (impl fix)**: docs が正しい。impl を spec に寄せる。
- **impl 準拠 (docs update)**: impl が実運用で validate された refinement。docs を追記。
- **hybrid**: 一部は impl fix、一部は docs update を組み合わせる。
- **user 決定**: 事業判断 (投資フィロソフィー・backtest 結果) が必要で claude 側で決められない。

---

## Item 1: System1 二重 setup

### docs 側の記述

`docs/systems/システム1.txt:10-12`
```
セットアップ
	•	SPYの終値が100日SMA（単純移動平均線）を上回る。
	•	25日SMAの終値が50日SMAの終値を上回る。
```

`docs/systems/システム1.txt:14-15` (ランキング)
```
ランキング
ポジションサイジングが許容する以上のセットアップが発生したときは、銘柄を過去200日ROC（変化率）が高い順に選ぶ。
```

**docs 上の setup 条件**: SPY>SMA100 AND SMA25>SMA50。ROC200 は **ランキング用のみ** で setup gate ではない。

### impl 側の記述

#### 実装 A: batch DataFrame 経路

`core/system1.py:97-149` (`_apply_setup_conditions`)
```python
# L148
x["setup"] = _filter & (_close > _sma) & (_roc > MIN_ROC200)
```
条件: `filter (Close>=5 & DV20>50M) & Close > SMA200 & ROC200 > 0`

#### 実装 B: 行ベース経路

`core/system1.py:302-350` (`system1_row_passes_setup`)
```python
# L328, L332, L335
sma_trend_ok = ... sma25 > sma50
roc200_positive = ... roc200_val > 0
passes = sma_trend_ok and roc200_positive
```
条件: `SMA25 > SMA50 & ROC200 > 0` (Phase 2 filter は事前通過前提)

#### 実装 C: 統一 predicate

`common/system_setup_predicates.py:71-125` (`system1_setup_predicate`)
```python
# L100, L115
if not (close_v >= 5.0 and dv20_v >= 50_000_000): ...
ok: bool = (sma25_v > sma50_v) and (roc200_v > 0.0)
```
条件: `Close>=5 & DV20>=50M & SMA25 > SMA50 & ROC200 > 0`

#### SPY gate は orchestrator 委譲

`common/today_signals.py:980, 1320`
```python
spy_gate_bool = _make_spy_gate(spy_df, column="sma100")
column = "SMA100" if system_lower == "system1" else "SMA200"
```
SPY>SMA100 gate は core ではなく today_signals 側で適用される。

### 食い違いの中身

3 つの setup 判定 path が同居しており、A だけ **個別銘柄条件が SMA25>SMA50 ではなく Close>SMA200** で異質。

| 経路 | 個別銘柄 setup | 追加条件 |
|---|---|---|
| A (batch) | Close > SMA200 | ROC200>0 |
| B (row) | SMA25 > SMA50 | ROC200>0 |
| C (predicate) | SMA25 > SMA50 | ROC200>0 |
| docs (spec) | SMA25 > SMA50 | 無し |

- A と B/C は **異なる候補集合を返す**。A は「200 日 SMA を超えている銘柄」を絞り、B/C は「短期トレンド好転」を捉える。
- ROC200>0 gate は docs 未記載。全 path で追加されている pragmatic refinement。
- SPY>SMA100 gate は orchestrator で正しく適用されている (docs 通り)。

**subscriber 挙動への影響**: pipeline 呼び出し経路によって候補が変わる。特に「今日 SMA25>SMA50 だが Close<SMA200」の銘柄は A では落ち、B/C では通る。長期下落トレンド中の一時反発を A は除外、B/C は拾う。

### 推奨判断: **hybrid**

**理由**:
1. **A の個別銘柄条件は明確に spec と乖離** (SMA200 vs SMA25/50)。しかも B/C と齟齬。→ **A を SMA25>SMA50 に修正 (docs 準拠)**。これで 3 経路統一。
2. **ROC200>0 gate は 3 経路共通で存在** し、momentum strategy として理に叶う。→ **docs に追記 (impl 準拠)**。ranking 用 ROC200 を「positive momentum の setup gate + 降順 ranking」と明記。
3. SPY>SMA100 は orchestrator で正しく処理 (現状維持)。

**追加 test**: `tests/test_system1_setup_unified.py` で A/B/C が同一 boolean を返すことを property-test。

---

## Item 2: System4 に spec に無い RSI4 追加除外

### docs 側の記述

`docs/systems/システム4.txt:14-15`
```
ランキング
4日RSIが小さい順に銘柄を選ぶ。
```

**docs 上の setup**: フィルター (`DV50 > 1億, HV 10-40%`) + `SPY>SMA200 & 銘柄>SMA200`。
RSI4 は **ランキング用のみ**、閾値による除外は spec 未記載。

### impl 側の記述

`core/system4.py:55`
```python
MAX_RSI4_THRESHOLD = 30.0  # RSI4 oversold threshold
```

`core/system4.py:531`
```python
if pd.isna(rsi4_val) or float(rsi4_val) >= MAX_RSI4_THRESHOLD:
    continue  # rsi4 が 30 以上の銘柄を候補から除外
```

Setup を通過した銘柄でも `rsi4 >= 30` なら除外し、その後 rsi4 昇順で top_n。

### 食い違いの中身

- **spec**: rsi4 昇順に top_n だけ取れば OK。閾値なし。
- **impl**: rsi4>=30 の銘柄はランキング以前に除外される追加 gate。
- 効果差: 低ボラ環境で市場全体 RSI4>30 の場合、spec なら top_n (例: 20) 銘柄取れるが、impl は **0 銘柄** になる。逆に強い oversold 環境では両者はほぼ同じ結果 (top_n が全部 rsi4<30 になるため)。

**subscriber 挙動への影響**: ランキング候補数が impl の方が常に少ない (RSI4<30 の銘柄しか候補にならないため)。trending market では System4 が「候補なし」になりやすい。

### 推奨判断: **impl 準拠 (docs update)** — ただし user 確認推奨

**理由**:
1. RSI<30 は mean-reversion / low-vol pullback 戦略の **教科書的 gate**。実装は業界標準に沿っている。
2. Setup + ranking だけだと「弱い oversold 環境で無理にポジション建てる」動作になり、実運用リスクが上がる。impl 側に運用上の validity がある。
3. ただし「候補が出ない日」を許容するか (impl 挙動) / 「常に top_n 埋める」を許容するか (spec 挙動) は **投資フィロソフィーの判断**。

**推奨**: docs に「RSI4>=30 の銘柄は除外 (over-sold 環境の担保)」と追記。閾値 30 の根拠 (業界標準 or backtest 検証) も併記。

**user 決定余地**: 閾値 30 → 別の値 (35, 40) にする議論はあり得る。

---

## Item 3: System5 filter が spec と大幅乖離

### docs 側の記述

`docs/systems/システム5.txt:6-9`
```
フィルター
	•	過去50日の平均出来高が50万株を上回る。
	•	過去 50日の平均売買代金が 250万ドルを上回る。
	•	ATRが4%を上回る。
```

`docs/systems/システム5.txt:11-14`
```
セットアップ
	•	 終値が「100日 SMA +過去10日の ATRを上回る。
	•	 7日 ADX が55を上回る。
	•	 3日 RSIが50を下回る。
```

### impl 側の記述

`core/system5.py:60, 65, 72`
```python
MIN_PRICE = 5.0           # docs 無し (最低株価は spec 未記載)
MIN_ADX = 55.0            # spec では setup 条件、impl では filter に前倒し
DEFAULT_ATR_PCT_THRESHOLD = 0.025  # spec: 4%、impl: 2.5%
```

`core/system5.py:104-105` (`_apply_filter_conditions`)
```python
computed_filter = (
    (close >= MIN_PRICE) & (adx7 > MIN_ADX) & (atr_pct > DEFAULT_ATR_PCT_THRESHOLD)
).fillna(False)
```

`core/system5.py:157-160` (`_apply_setup_conditions`)
```python
price_band_ok = (close > (sma100 + atr10)).fillna(False)
rsi_ok = (rsi3 < MAX_RSI3).fillna(False)  # MAX_RSI3 = 50 (spec 一致)
computed_setup = filter_ok & price_band_ok & rsi_ok
```

Setup の 3 条件 (Close>SMA100+ATR10, ADX7>55, RSI3<50) は spec 準拠だが、
filter 側の 3 条件は spec と大幅乖離。

### 食い違いの中身

| 項目 | docs | impl |
|---|---|---|
| 最低株価 | 記載無し | Close ≥ 5 |
| AvgVolume50 | > 500,000 株 | **未実装** |
| DV50 | > 2,500,000 $ | **未実装** |
| ATR | > 4% | > **2.5%** |
| ADX7 | setup 側 (>55) | filter 側 (>55, 二重定義) |

- **完全欠如**: AvgVolume50 と DV50 の 2 条件が impl に存在しない。**流動性フィルターが機能していない**。
- **ATR 閾値**: spec 4% → impl 2.5%。閾値が緩い → 候補が **大幅に増える** (低変動銘柄も通す)。
- ADX>55 が filter に前倒し済み: setup 側でも redundantly enforce (`_apply_setup_conditions` は filter を継承)。実質同義で結果に差は出ないが code smell。
- Close≥5 は spec に無いが、penny stock 除外として妥当。

**subscriber 挙動への影響**:
1. 流動性の低い銘柄が候補に混入 (AvgVolume50/DV50 チェック無し) → 執行時のスリッページリスク。
2. ATR<4% の低変動銘柄が候補に混入 (2.5% で通す) → mean-reversion の効きが弱い銘柄まで対象。

### 推奨判断: **docs 準拠 (impl fix)** — ただし ATR 閾値のみ user 決定

**理由**:
1. AvgVolume50/DV50 は **流動性担保** の重要フィルターで、無いと実運用リスクが上がる。docs 記載通りに追加すべき。
2. ATR 閾値 4% vs 2.5% は **候補数と選別厳格性のトレードオフ**。backtest 結果次第。
   - 4% (spec): 候補少・高ボラ限定、mean-reversion の効きが強い環境のみ拾う
   - 2.5% (impl): 候補多・広めに拾う、期間平均利益は下がる可能性
   → **user 決定余地**。過去 backtest 結果があれば根拠にする。
3. Close≥5 は docs に追記して impl 維持で問題無し (penny stock 除外は業界標準)。
4. ADX>55 は setup 側に戻し、filter からは削除 (spec 通りの構造に)。

**推奨実装順**:
1. AvgVolume50>500K, DV50>2.5M を filter に追加 (docs 準拠)
2. ADX>55 を filter から削除、setup 側だけに残す (code cleanup)
3. Close≥5 を docs に追記 (現状維持)
4. ATR 閾値は user 判断 → 決まった値で固定

---

## Item 4: System6 filter に docs 未記載の HV50 bounds

### docs 側の記述

`docs/systems/システム6.txt:6-8`
```
フィルター
	•	 最低株価が5ドル以上。
	•	 過去 50 日の平均売買代金が 1000万ドルを上回る。
```

HV bounds の記載無し。System6 は 6-day surge 検出戦略で、ranking は return_6d 降順。

### impl 側の記述

`core/system6.py:54-57`
```python
MIN_PRICE = 5.0
MIN_DOLLAR_VOLUME_50 = 10_000_000
HV50_BOUNDS_PERCENT = (10.0, 40.0)
HV50_BOUNDS_FRACTION = (0.10, 0.40)
```

`core/system6.py:99-104`
```python
hv50_percent = hv50.between(*HV50_BOUNDS_PERCENT)
hv50_fraction = hv50.between(*HV50_BOUNDS_FRACTION)
hv50_condition = (hv50_percent | hv50_fraction).fillna(False)
computed_filter = (
    (low >= MIN_PRICE) & (dvol50 > MIN_DOLLAR_VOLUME_50) & hv50_condition
).fillna(False)
```

`low >= MIN_PRICE` は **Low 列** で比較。spec の「最低株価」を Close ではなく Low で解釈。

### 食い違いの中身

1. **HV50 bounds (10-40%)** が spec 無し。System4 の設定を borrowed した可能性。
2. **HV50 単位の二重解釈**: percent (10-40) と fraction (0.10-0.40) を OR で受ける defensive code。データ品質保証としては妥当だが code smell。
3. **株価 filter が Close ではなく Low**: spec の「株価≥5$」を厳格に解釈 (Low≥5 なら Close≥Low≥5)。System1/S2/S3 は Close で判定しており、S6 だけ違う。

**subscriber 挙動への影響**:
1. HV50<10% (超低ボラ) の銘柄は setup 通過しても候補にならない → 6-day 20% surge は元々高ボラ環境で発生するので影響は小さい可能性大。
2. HV50>40% の銘柄は「異常高ボラ (news-driven / broken stock)」として除外 → 実運用上は妥当な safety。
3. Low<5 の銘柄 (intraday で 5 割れ) が除外される → spec 準拠 (Close≥5) より厳しい。

### 推奨判断: **hybrid** — user 確認推奨

**理由**:
1. HV50 bounds は System4 (spec: HV 10-40%) から borrowed した operational refinement。**docs 追記で正当化 (impl 準拠)** が妥当。
   - 削除案 (A) だと HV>40% の異常銘柄が拾われるリスク。
2. HV50 の percent/fraction 二重判定は data pipeline の quirk。docs に追記する必要はないが、code コメントで由来を明記推奨。
3. **株価 filter (Low vs Close) は別途 finding**。docs には「最低株価」としか書かれておらず、Close/Low どちらとも解釈可能。S1-S3 の Close 判定と整合させるか S6 独自の Low 判定を維持するかは **user 決定**。

**推奨実装順**:
1. docs に HV50 10-40% 範囲の filter を追加 (impl 準拠、operational safety の justification 付き)
2. 株価 filter の Low vs Close は user 判断待ち
3. HV50 の percent/fraction 二重解釈は code comment 追加のみ (振る舞い変更なし)

---

## Item 5: TradeManager max_holding_days が全システムで 0

### 元 report の記述の再検証

元 report は「全 system で `max_holding_days=0`」と主張。**これは事実誤認 (outdated)**。
現時点で 2 つの config surface が併存しており、どちらも部分的に spec-compliant / divergent。

### docs 側の記述

| System | spec time exit | 出典 |
|---|---|---|
| S1 | 記載無し (trailing stop で管理) | `システム1.txt:26-30` |
| S2 | 2 日で目標未達→翌日大引け | `システム2.txt:30-32` |
| S3 | 3 日で目標未達→翌日大引け | `システム3.txt:30-32` |
| S4 | 記載無し (trailing stop で管理) | `システム4.txt:26-30` |
| S5 | 6 日で目標未達→翌日寄付 | `システム5.txt:31-33` |
| S6 | 3 日→大引け | `システム6.txt:41-43` |
| S7 | SPY hedge (time exit なし) | `システム7.txt` |

### impl 側の記述: **2 つの並行 config**

#### Config A: `common/trade_management.py::SYSTEM_TRADE_RULES` (L194-287)

| System | max_holding_days | spec との一致 |
|---|---|---|
| S1 | 未設定 (default 0) | ✓ (spec: no time exit) |
| S2 | **2** | ✓ |
| S3 | **3** | ✓ |
| S4 | 未設定 (default 0) | ✓ |
| S5 | **6** | ✓ |
| S6 | **3** | ✓ |
| S7 | N/A (rules 削除済) | ✓ |

→ **Config A は spec-compliant**。元 report の「全 0」は誤り。

#### Config B: `strategies/constants.py::SYSTEM_SPECIFIC_CONFIG` (L25-58)

`MAX_HOLD_DAYS_DEFAULT = 3` (L15)

| System | max_hold_days | spec との一致 |
|---|---|---|
| S1 | **3** | **✗** (spec: no time exit) |
| S2 | 3 (`MAX_HOLD_DAYS_DEFAULT`) | **✗** (spec: 2) |
| S3 | 3 | ✓ |
| S4 | 3 | **✗** (spec: no time exit) |
| S5 | fallback_exit_days=**6** (別 key) | ✓ (別変数だが一致) |
| S6 | **未設定** | **✗** (spec: 3, 抜けている) |
| S7 | 3 | **✗** (spec: no time exit) |

→ **Config B は 4-5 件 divergent**。特に S1/S4 は 3 日 time exit で「trend-following なのに 3 日で強制決済」 → **strategy の根幹を破壊するバグ**。

`strategies/system1_strategy.py:216-229` で実際に enforce:
```python
max_hold_days = int(self.config.get("max_hold_days", MAX_HOLD_DAYS_DEFAULT))
...
exit_idx = min(entry_idx + max_hold_days, n - 1)
return float(df.iloc[exit_idx]["Close"]), pd.Timestamp(...)
```

### 食い違いの中身

**根本問題**: time exit の source of truth が 2 箇所に分裂。
- Config A (TradeManager) は今 signal enrichment / execution layer で使われる
- Config B (SYSTEM_SPECIFIC_CONFIG) は strategy classes の `_simulate_trade` で使われる (backtest 経路)

どちらが「実際の time exit を決めているか」は backtest の launcher / broker の実装次第。両方が同時に効いた場合、strict 側 (Config B の 3 日) が勝つ。

**subscriber 挙動への影響**:
- **Live signal 経路** (Config A 使用時): spec 準拠、期待通り。
- **Backtest 経路** (Config B 使用時): S1/S4 が 3 日で強制決済 → trend momentum が機能せず、大幅に劣化した backtest 結果を出す。S6 は time exit 抜けで、ダラダラ持ち続けるバグ。

### 推奨判断: **docs 準拠 (impl fix)** — 統一 + Config B の削除 or spec-align

**理由**:
1. Config A は既に spec 準拠。維持。
2. Config B は spec 違反かつ Config A と重複。**削除して Config A に一元化** すべき。
   - もし backtest 経路が Config B に依存しているなら、Config B の値を spec に合わせて修正:
     - S1: max_hold_days → 削除 (時間 exit なし)
     - S2: max_hold_days → 2
     - S4: max_hold_days → 削除 (時間 exit なし)
     - S6: max_hold_days → 3 を追加
     - S7: max_hold_days → 削除
3. 更に望ましいのは Config B を廃止し、strategy classes が Config A (`SYSTEM_TRADE_RULES`) を参照するように refactor。

**追加 test**: `tests/test_trade_manager_time_exit.py` で Config A/B の値と、実際の `_simulate_trade` の exit date が spec 通りかを end-to-end 検証。

---

## Summary Table (5 件の推奨判断)

| Item | 分類 | Impl 変更 | Docs 変更 | user 決定余地 |
|---|---|---|---|---|
| D1: S1 二重 setup | **hybrid** | `_apply_setup_conditions` を SMA25>SMA50 に修正 | ROC200>0 gate を追記 | 無し (統一が急務) |
| D2: S4 rsi4<30 | **impl 準拠** (docs update) | 変更無し | RSI4>=30 除外を追記 | 閾値 30 の値 (30/35/40?) |
| D3: S5 filter | **docs 準拠** (impl fix) + 一部 user | AvgVol50/DV50 追加、ADX を setup へ移動 | Close≥5 追記 | **ATR 4% vs 2.5%** (backtest 結果次第) |
| D4: S6 hv50 | **hybrid** | 変更無し (bounds 維持) | HV 10-40% を追記 | 株価 filter を Low vs Close どちらにするか |
| D5: TradeMgr | **docs 準拠** (impl fix) | Config B (SYSTEM_SPECIFIC_CONFIG) を Config A に一元化 or spec 値に修正 | 変更無し | 無し (S1/S4 3-day exit は bug) |

---

## Priority (実装 dispatch 順の推奨)

事業重視の観点で優先順位を付けると:

1. **D5 (highest)** — S1/S4 backtest が **3 日強制決済で strategy が機能不全**。これが本番影響してれば long-only モメンタム戦略の想定リターンが根本から狂う。即修正推奨。
2. **D3** — 流動性 filter 欠如で **実運用でスリッページ被弾リスク**。AvgVol50/DV50 追加は急務。
3. **D1** — 三重実装は maintainability の危機。setup 判定が経路依存で異なるとテストで catch できないバグを生む。中期的に統一必須。
4. **D2** — 現状の impl 挙動は業界標準通り。docs 追記のみで OK。優先度低。
5. **D4** — 現状の impl 挙動は operational safety として妥当。docs 追記のみで OK。優先度低。

---

## Phase 2 test への影響

Phase 2 は Config A (SYSTEM_TRADE_RULES) の数値と現状 impl の setup を「invariant として緑固定」している。以下の順で赤くなる想定:

- D5 修正 (Config B 統一) → 影響最小 (Config A 使ってる test は緑継続)
- D3 修正 (S5 filter 追加) → `test_systems_filter_setup_spec_compliance.py` の S5 filter assertion が **意図的に赤く** なる → docs 準拠 test に差し替え
- D1 修正 (`_apply_setup_conditions` を SMA25/50 に) → S1 setup assertion が赤くなる → docs 準拠 test に差し替え
- D2/D4 修正 (docs のみ) → Phase 2 test は緑継続

赤くなる test は「意図的な仕様変更を検知した」ことを意味し、docs 準拠版 test に置き換える。

---

## Next Action (user 選択待ち)

上記の Priority 順で 1 件ずつ実装 dispatch する場合、以下のどれから始めるか user が指示:

- **A. D5 から** (backtest 破壊バグの修正、事業影響最大)
- **B. D3 から** (流動性 filter 追加、実運用リスク軽減)
- **C. D1 から** (三重実装の統一、maintainability)
- **D. 全部 batch で** (D5→D3→D1→D2→D4 を 1 dispatch で連続実装)
- **E. D2/D4 の docs update のみ先行** (低リスクな doc 変更だけ先に片付ける)

user が上記 5 件それぞれの推奨判断について合意 or 修正指示すれば、次 dispatch で該当 item を実装 + test 追加。
