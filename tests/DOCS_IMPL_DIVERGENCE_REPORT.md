# docs/systems vs core 実装 乖離レポート (2026-07-02 Phase 1 audit 由来)

このレポートは Phase 2 の docs 準拠 test 実装中に判明した spec/実装乖離を、
**Phase 3 判断用に別 file に記録** したもの。実装変更禁止 (audit remediation 準拠済)
の制約下で、現状実装を authoritative として parametrize test を書いた際の副産物。

いずれの項目も既存 test でカバーされておらず、また `docs/systems/システムN.txt`
と `core/systemN.py` の実装が同一でない。判断は user がすべき (docs を single
source of truth とするか、実装現状に docs を寄せるか)。

---

## D1. System1 setup が二重実装で相互矛盾

**docs/systems/システム1.txt**:
- Filter: DollarVolume20 > 5000万$, 株価 ≥ 5$
- Setup: SPY終値 > SMA100 **かつ** SMA25 > SMA50

**core/system1.py**:
- `_apply_setup_conditions` (L97-149): `filter & Close>SMA200 & ROC200>0`
- `system1_row_passes_setup` (row-wise, 別 path): `filter & sma25>sma50 & roc200>0`
- SPY>SMA100 gate: core に存在せず、orchestrator (utils_spy/today_filters) に委譲

**問題:**
1. `_apply_setup_conditions` (Close>SMA200) と `system1_row_passes_setup` (SMA25>SMA50)
   が**別ロジックで両方 code path として存在** → 呼び出し経路によって setup 判定が異なる
2. どちらも spec の SPY>SMA100 gate を含まない
3. spec の SMA25>SMA50 は `_apply_setup_conditions` が使っていない
4. SMA200 gate は docs 未記載

**推奨判断:**
- (A) docs を single source of truth に寄せる → `_apply_setup_conditions` を
  spec (SPY>SMA100 & SMA25>SMA50) に統一
- (B) 実装現状を docs 化 → docs 側を「Close>SMA200 & ROC200>0 & SMA25>SMA50 (row 経路)」
  に書き換え

現在の Phase 2 test は (B) を仮定して `_apply_setup_conditions` 経路のみ固定。

---

## D2. System4 に spec に無い RSI4 追加除外

**docs/systems/システム4.txt**:
- Ranking: RSI4 昇順 (低い順に選ぶ = oversold トレンド追随)
- 具体的な RSI4 閾値による除外は spec 未記載

**core/system4.py**:
- `MAX_RSI4_THRESHOLD = 30.0` (L55): rsi4≥30 の銘柄は候補から除外

**問題:** docs に無い追加条件が実装に存在。既存 test で明示的に固定されていない。

**推奨判断:**
- (A) spec 準拠に revert (RSI4 除外を削除、単純に昇順 sort)
- (B) 30 閾値の根拠を docs に追加

Phase 2 test は現状を仮定 (parametrize では触れていない)。

---

## D3. System5 filter が spec と大幅乖離

**docs/systems/システム5.txt**:
- Filter: AvgVolume50 > 50万株 / DV50 > 250万$ / ATR > 4%

**core/system5.py**:
- `MIN_PRICE = 5.0` (docs に無い最低株価)
- `MIN_ADX = 55.0` (spec では **setup** 条件、実装では filter に前倒し)
- `DEFAULT_ATR_PCT_THRESHOLD = 0.025` (spec: 4%、実装: 2.5%)
- **AvgVolume50 / DV50 の 2 条件が実装に存在しない**

**問題:** filter の 3 条件が全部 spec と一致していない。
audit_remediation の test はこの「実装現状」を assert しており、docs を single
source of truth とするなら S3 の前例に倣った是正が必要。

**推奨判断:**
- (A) spec 準拠に revert (AvgVolume50/DV50 を導入、ATR 4% に上げる、ADX を setup へ戻す)
- (B) 実装現状を docs 化 (Close≥5, ADX>55, ATR>2.5% を docs に反映)

Phase 2 test は (B) を仮定して現状境界値を固定。

---

## D4. System6 filter に docs 未記載の HV50 bounds

**docs/systems/システム6.txt**:
- Filter: 株価 ≥ 5$ / DV50 > 1000万$
- HV bounds の記載無し

**core/system6.py**:
- `HV50_BOUNDS_PERCENT = (10.0, 40.0)` (L56)
- `HV50_BOUNDS_FRACTION = (0.10, 0.40)` (L57)
- Filter で hv50 の bounds check が加わっている

**問題:** docs に無い追加 filter が実装に存在。System4 と類似構造。

**推奨判断:**
- (A) HV bounds を削除 (spec 準拠)
- (B) HV bounds の根拠を docs に追加

---

## D5. SYSTEM_TRADE_RULES max_holding_days が全システムで 0

**docs/systems**:
- S2: 2 日で未達→翌日大引け exit
- S5: 6 日 time exit
- S3/S6: 3 日 time exit

**common/trade_management.py::SYSTEM_TRADE_RULES**:
- 全 system で `max_holding_days=0` (無効化 = time exit しない)

**問題:** spec で明示された time exit がすべて 0 = 無効化されている。バックテスト
経路の `max_hold_days` (constants.py 側) と TradeManager の `max_holding_days`
(trade_management.py 側) が別変数で管理されており、どちらが有効か unclear。

**推奨判断:** Phase 3 で TradeManager 側の time exit を docs 準拠 (S2=2, S3=3,
S5=6, S6=3) に設定するか、バックテスト側の `max_hold_days` に一元化するか判断。

Phase 2 test では `max_pct=0.10 / risk_pct=0.02` の共通条件のみ固定。

---

## Phase 3 で追加すべき test (乖離解消後)

| 判断 | 追加 test path | 内容 |
|---|---|---|
| D1 判断後 | tests/test_system1_setup_unified.py | 一本化された setup logic の spec 境界値 |
| D2 判断後 | tests/test_system4_rsi4_gate.py | RSI4 除外 (or 削除) の閾値 assert |
| D3 判断後 | tests/test_system5_filter_docs_compliance.py | AvgVolume50/DV50/ATR の docs 準拠境界値 |
| D4 判断後 | tests/test_system6_hv50_bounds.py | HV bounds (or 削除) の境界値 |
| D5 判断後 | tests/test_trade_manager_time_exit.py | S2=2 / S3=3 / S5=6 / S6=3 time exit 動作 |

---

## Phase 2 で押さえた invariant (現状固定)

Phase 3 判断が終わるまでの間、以下の behavior は Phase 2 test で **緑固定** されている:

- `tests/test_systems_filter_setup_spec_compliance.py`:
  - S1 filter: Close>=5, DV20>50M
  - S1 setup: Close>SMA200 & ROC200>0 (SMA25/SMA50 setup path は未 assert)
  - S2 filter/setup: docs 一致
  - S3 filter/setup: audit_remediation 準拠 (docs 一致)
  - S4 filter/setup: 現状実装 (RSI4 は未 assert)
  - S5 filter/setup: 現状実装 (Close>=5 / ADX>55 / ATR>2.5%)
  - S6 filter/setup: 現状実装 (HV50 bounds は未 assert)
  - SYSTEM_TRADE_RULES 数値: S1-S6 の stop period/multiplier/trailing 完全一致

Phase 3 で spec に寄せる場合、これらの Phase 2 test は正しく赤くなるはずで、
「意図的な仕様変更」を検知する trip-wire として機能する。
