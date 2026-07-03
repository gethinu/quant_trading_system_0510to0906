# Position Management / Capital Allocation / Risk Override — docs-alignment audit

**日付**: 2026-07-03
**対象 branch**: `claude/monitor-webapp` (base commit `29e25f5`)
**対象 files**:
- `common/alpaca_trading.py` (発注 layer)
- `common/trade_management.py::SYSTEM_TRADE_RULES` (per-system rule dict)
- `core/final_allocation.py` (capital / slot allocation)
- `scripts/paper_exit_check.py` (exit orchestration)
- `common/today_signals.py` (signals + ranking)
- `config/config.yaml` (allocation / risk defaults)

**方針**: docs (`docs/systems/*.txt`, `docs/systems/INDEX.md`,
`docs/today_signal_scan/6. 配分・最終リスト生成フェーズ.md`, `config/config.yaml`)
= single source of truth。実装が docs から drift したら是正、docs に無い機能は
report のみ (実装は user 決定後に別 dispatch)。

**先行 dispatch**:
- `docs/D3_CASE_A_IMPL_20260703.md` — filter/setup docs-alignment (System5)
- `docs/D5_SYSTEM_SPECIFIC_CONFIG_bug_20260702.md` — S1 backtest 3-day exit bug (defer 中)
- `docs/AUDIT_REMEDIATION_20260702.md` — SYSTEM_TRADE_RULES 収束 (Part4/System5/System3)

---

## Executive Summary

- **docs-driven な唯一の gap は `common/alpaca_trading.py::_DEFAULT_SYSTEM_ORDER_TYPE`** の
  3 件ミスマッチ (S3/S5=market → **limit**、S7=limit → **market**) を是正。
- SYSTEM_TRADE_RULES / DEFAULT_LONG/SHORT_ALLOCATIONS / risk / max_pct / max_positions /
  trailing / profit target / stop / holding は既に docs 準拠。**追加の docs-driven fix
  はゼロ**。
- 追加 regression test 20+ 件 (`tests/test_position_management_docs_alignment_20260703.py`)
  で per-system rule / allocation / order type / cross-system dedup 挙動を lock in。
- **docs に記述の無い portfolio-level 機能** (総 position cap / cross-system dedup 明文化 /
  drawdown flatten / sector cap) は **Phase 5 future consideration** に列挙。実装は
  user 判断待ち。
- **runtime signal 経路の holding period は spec 通り**。backtest 経路の S1 3-day 強制決済
  bug (D5 report) は本 dispatch でも defer (別 dispatch 起票済み)。

---

## 1. Phase 1: docs 抽出結果 (portfolio 管理 spec)

### 1.1 per-system rule (docs/systems/システム{N}.txt「ポジションサイジング」節)

| System | Side | risk | max_pct | max_pos | trailing | profit target | stop (period × mult) | time exit |
|---|---|---|---|---|---|---|---|---|
| S1 | long | 2% | 10% | **10** (明記) | 25% | 無 | 20 × 5 | 無 |
| S2 | short | 2% | 10% | **10** (明記) | 無 | 4% (%) | 10 × 3 | **2 日** |
| S3 | long | 2% | 10% | 明記なし | 無 | 4% (%) | 10 × 2.5 | **3 日** |
| S4 | long | 2% | 10% | 明記なし | 20% | 無 | 40 × 1.5 | 無 |
| S5 | long | 2% | 10% | 明記なし | 無 | 1×ATR10 | 10 × 3 | **6 日** |
| S6 | short | 2% | 10% | 明記なし | 無 | 5% (%) | 10 × 3 | **3 日** |
| S7 | short/hedge | 100% single (組合せ運用では 100% 使わない) | — | — | 無 (SPY 70日高値まで保有) | 無 | 50 × 3 | 無 |

### 1.2 portfolio-level allocation (docs/systems/INDEX.md)

- **long bucket 100%** = S1 25% + S3 25% + S4 25% + S5 25%
- **short bucket 100%** = S2 40% + S6 40% + S7 20%
- capital split: `default_long_ratio = 0.5` (long/short 半々)
- default_capital = 100,000 USD (`config/config.yaml::ui.default_capital`)

### 1.3 per-system 仕掛け rule (docs/systems/システム{N}.txt「仕掛け」節)

| System | docs 仕掛け | Order type |
|---|---|---|
| S1 | 翌日の寄り付きで成り行き | MARKET |
| S2 | 翌日、前日終値を4%以上上回る価格で売る | LIMIT (+4%) |
| S3 | 前日終値の7%下に指値注文 | LIMIT (-7%) |
| S4 | 寄り付きで成り行き (スリッページかかわらず) | MARKET |
| S5 | 前日終値の3%下に指値 | LIMIT (-3%) |
| S6 | 前日終値を5%上回る位置に指値 | LIMIT (+5%) |
| S7 | 翌日の寄り付きで成り行き | MARKET |

### 1.4 docs で明記されていない項目 (= 独自実装禁止)

- portfolio 全体の同時保有 position 数 cap (per-system max 10 の合算 implicit 上限のみ)
- cross-system で同一銘柄が候補になった時の tie-break / dedup rule
- portfolio drawdown / P&L 上限 override による全 flatten
- 相関 filter (sector cap / industry cap / beta cap)
- entry filter の重複 handling (S1+S3 で同銘柄出た時の優先順位)

---

## 2. Phase 2: 実装現状 audit (docs vs impl matrix)

| # | 論点 | docs | impl 該当 | 判定 | 対応 |
|---|---|---|---|---|---|
| 1 | per-system max_positions | S1/S2=10 明記、S3-S6 明記なし | `RiskConfig.max_positions=10` (global) + `config.max_positions` per-system override (`core/final_allocation.py::_resolve_max_positions`) | ✅ | regression test |
| 2 | per-system risk 2% | 全 S 明記 | `risk_pct=0.02` (global + per-system override) | ✅ | regression test |
| 3 | per-system max_pct 10% | 全 S 明記 (S7=100% single 例外) | `max_pct=0.10` global、S7=`0.20` (組合せ運用向け、`config.yaml` 明示) | ✅ | 変更なし |
| 4 | long/short bucket 配分 | INDEX.md 明記 (long: 25%×4、short: 40+40+20) | `DEFAULT_LONG/SHORT_ALLOCATIONS` (`core/final_allocation.py`) + `config.yaml::ui.long_allocations/short_allocations` 完全一致 | ✅ | regression test |
| 5 | bucket split (long/short) | phase6 doc: `default_long_ratio=0.5` | `finalize_allocation(default_long_ratio=0.5, default_capital=100_000)` | ✅ | 変更なし |
| 6 | holding period (time exit) | S2=2, S3=3, S5=6, S6=3, S1/S4/S7=無 | `SYSTEM_TRADE_RULES.max_holding_days`: S2=2, S3=3, S5=6, S6=3, S1/S4=0 (無), S7=None (rule 自体無し) | ✅ **runtime path** 一致。backtest 経路の S1 3-day bug は D5 report で defer 中 | regression test |
| 7 | trailing stop | S1=25%, S4=20%, その他無 | 一致 (`SYSTEM_TRADE_RULES.trailing_stop_pct`) | ✅ | regression test |
| 8 | profit target | S2 4%, S3 4%, S5 1×ATR10, S6 5% | 一致 (`SYSTEM_TRADE_RULES.profit_target_type/value/atr_period`) | ✅ | regression test |
| 9 | stop loss (ATR period × multiplier) | S1 20/5, S2 10/3, S3 10/2.5, S4 40/1.5, S5 10/3, S6 10/3, S7 50/3 (strategies 側) | 一致 (SYSTEM_TRADE_RULES + strategies/system7_strategy.py) | ✅ | regression test |
| 10 | **entry order type default map** | S1/S4/S7 market、S2/S3/S5/S6 limit | 旧 `_DEFAULT_SYSTEM_ORDER_TYPE`: **S3=market, S5=market, S7=limit** → docs 3 件不整合 | 🔴 **gap** | **本 dispatch で修正** |
| 11 | cross-system 同一銘柄 dedup | 未明記 | `chosen_symbols: set[str]` (final_allocation) + slot round-robin dedup (env `slot_dedup_enabled=1`) | ⚠️ | Phase 5 report のみ |
| 12 | portfolio 総 position 上限 | 未明記 | implicit ~70 (7 system × max 10) | ⚠️ | Phase 5 report のみ |
| 13 | portfolio drawdown / flatten | 未明記 | 未実装 | ⚠️ | Phase 5 report のみ |
| 14 | 相関 filter (sector / industry / beta cap) | 未明記 | 未実装 | ⚠️ | Phase 5 report のみ |

---

## 3. Phase 3: gap fill 実装 (docs-driven fix)

### 3.1 `common/alpaca_trading.py::_DEFAULT_SYSTEM_ORDER_TYPE` の docs-alignment

#### Before (git HEAD `29e25f5`)

```python
_DEFAULT_SYSTEM_ORDER_TYPE = {
    "system1": "market",
    "system3": "market",   # ❌ docs = limit (-7%)
    "system4": "market",
    "system5": "market",   # ❌ docs = limit (-3%)
    "system2": "limit",
    "system6": "limit",
    "system7": "limit",    # ❌ docs = market (翌日寄り成行)
}
```

#### After (this dispatch)

```python
_DEFAULT_SYSTEM_ORDER_TYPE = {
    "system1": "market",
    "system2": "limit",
    "system3": "limit",   # ✅ docs 準拠
    "system4": "market",
    "system5": "limit",   # ✅ docs 準拠
    "system6": "limit",
    "system7": "market",  # ✅ docs 準拠
}
```

**runtime 挙動への影響**:
- `signals_to_orders` は order_type=limit で `row.get("entry_price")` が無い場合、
  `ot = "market"` に fallback する既存ロジックがある (safety guard 維持)。
- 従って `entry_price` (final_allocation で算出) が row にあれば docs 通り limit で発注、
  無ければ market fallback = 誤発注を招かない conservative 挙動。
- S7 (SPY hedge) は本来 `entry_price` を row に載せない設計だが、旧 map では limit と
  なっており、`limit_price=None` fallback で結果 market になっていた「偶然 docs
  通り」の状態。新 map では最初から market なので **意図が code で明示される**。

**なぜ安全**:
1. runtime fallback ロジックが変わらない (limit→market on missing limit_price)。
2. `SYSTEM_TRADE_RULES` の entry_type / entry_price_offset_pct は既に docs 準拠なので、
   trade_management.py 経由の entry_price 計算 (`_calculate_limit_order_price`) と
   一貫性が取れる。
3. `signals_json_to_orders` (tier notional 経路) は `order_type="market"` 固定 (462-527
   行) なのでこの map を参照せず影響外。

---

## 4. Phase 4: regression test 追加

### 4.1 新規 test file: `tests/test_position_management_docs_alignment_20260703.py`

以下 5 cluster で 21 test:

| Cluster | 対象 | test 数 |
|---|---|---|
| A. capital allocation docs alignment | `DEFAULT_LONG/SHORT_ALLOCATIONS` | 2 |
| B. per-system trade rules docs alignment | `SYSTEM_TRADE_RULES` (S1-S6) + S7 absence | 7 |
| C. entry order type map docs alignment | `_DEFAULT_SYSTEM_ORDER_TYPE` (S1-S7 各 assert) | 7 |
| D. end-to-end signals_to_orders docs alignment | S3/S5 limit / S7 market の統合検証 | 3 |
| E. cross-system dedup behavior (impl lock-in) | (docs 未明記の) 現行仕様を固定化 | 2 |

**cluster A/B/C/D は docs alignment**。docs から drift すると FAIL する。
**cluster E は impl 独自挙動の lock-in**。future dispatch で dedup 仕様を変更する時に
「意図した変更」であることを test 修正で明示する。

### 4.2 既存 test の更新

`tests/test_signals_to_orders.py`:
- S3/S5 の docs-alignment 新 assertion 追加
- S7 の `order_type == "limit"` を `"market"` に修正 (docs 準拠)
- fixture に S3 (AMD), S5 (NVDA) の signals row を追加

---

## 5. Phase 5: docs update + future consideration

### 5.1 docs 側の追記 (per-system change log)

各 `docs/systems/システム{N}.txt` の【変更履歴】末尾に 2026-07-03 alignment log を
追記予定 (docs は既に 2026-07-03 D3 audit の log を持つため、その隣に置く)。
本 dispatch の変更は S3/S5/S7 の default order type map のみで、docs 側 spec は
変更なし → **各 docs は verify only log** (docs と impl が最初から一致していた
旨を明記)。

### 5.2 future consideration (docs 未明記の項目、実装は user 判断待ち)

以下は「docs に無い機能を独自に追加するのは絶対 NG」の user 方針に従い、
本 dispatch では **提案のみ**。user が判断したら別 dispatch で spec 化 + 実装。

#### (a) cross-system 同一銘柄 dedup の明文化

**現状 impl**:
- `core/final_allocation.py::_allocate_by_capital` の `chosen_symbols: set[str]` で、
  system1〜system7 の順に 1 symbol 1 slot を割り当てる。
- 同 symbol が S1 と S3 両方に上がった場合、system1 が先に走るため **S1 が勝つ**
  (system 番号順の first-come-first-serve)。
- slot mode では `slot_dedup_enabled` env で round-robin dedup を有効化可能
  (default OFF)。

**論点**:
- docs は「S1+S3 で同銘柄」を明記しない。system 番号順に依存する現状は透明性が低い。
- 事業 lens: subscriber 説明時に「同銘柄は system 番号が若い方が優先」と説明できる
  か、または backtest スコア (risk-adjusted return) の高い system を選ぶべきか。

**提案 option**:
- A: docs に「system 番号順 first-come」と明記して現状を追認
- B: docs に「score 順で 1 系統だけ通す」と明記して impl 変更
- C: docs に「同銘柄を許可、両系統で保有」と明記して impl から dedup 撤去
- 判断は subscriber 側の期待 (ポートフォリオ多様化 vs 保有集中) 次第

#### (b) portfolio 総 position 数 cap

**現状 impl**:
- 各 system の `max_positions` を config で個別に指定 (default 10)
- 全 system 合計で implicit 70 個 (7 × 10)
- 全体を強制的に減らす hard cap は無い

**論点**:
- 100,000 USD equity で 70 position は 1 position あたり平均 $1,428 = 手数料負けの
  可能性 (Alpaca commission 0 なので現状影響なし、ただし将来別 broker の場合)
- docs に「portfolio 全体 max N positions」を書くべきか

**提案 option**:
- A: `config.yaml::risk.max_total_positions: 20` を追加して implicit hard cap
- B: 現状維持 (per-system cap のみ)

#### (c) portfolio drawdown / P&L override

**現状**: 未実装。日次 P&L や cumulative drawdown が閾値を超えても pipeline は
新規 entry を続ける。

**論点**:
- 事業 lens: catastrophe hedge (S7) と別に、system 全体の防衛機構が欲しい局面がある
- subscriber 説明時「システムが自動で全 flatten するのはどんな条件?」を規定できる

**提案 option**:
- A: `-30% peak drawdown` を検出したら翌営業日 open で全 close (S7 の役割を
  補完)
- B: 日次 -5% を超えたら当日以降の新規 entry を停止 (既存 position は継続)
- C: 現状維持 (subscriber 各自の判断に委ねる)

#### (d) 相関 / sector cap

**現状**: 未実装。同 sector に S1+S3+S4+S5 が全部集中しても止まらない。

**論点**:
- risk management の教科書標準 (sector 20% cap 等) は事業説明として強い
- しかし docs に無い機能なので原設計者の思想と乖離するかも

**提案 option**:
- A: sector metadata を Polygon API から取得し、sector 20% cap で allocation 縮小
- B: 現状維持

#### (e) backtest 経路 S1 3-day exit bug (D5 defer)

**現状**: `docs/D5_SYSTEM_SPECIFIC_CONFIG_bug_20260702.md` で Case 3 hybrid 案が
提案済みだが実装は defer。user 承認待ち。

**論点**:
- runtime signal 経路 (`SYSTEM_TRADE_RULES`) は spec 通りなので **live 影響なし**
- backtest 経路の勝率/CAGR 表示が spec と乖離する = subscriber 説明時に困る

**推奨**: D5 の Case 3 (hybrid) を別 dispatch で実装。本 dispatch は scope 外。

---

## 6. Windows 側 明日 (07-04) tick 後の verify 方法

```powershell
cd C:\Repos\quant_trading_system_0510to0906

# 1. 修正後の default order type map を確認
python -c "from common.alpaca_trading import _DEFAULT_SYSTEM_ORDER_TYPE; import json; print(json.dumps(_DEFAULT_SYSTEM_ORDER_TYPE, indent=2))"
# 期待: system1=market, system2=limit, system3=limit, system4=market,
#       system5=limit, system6=limit, system7=market

# 2. 新規 regression test を走らせる
python -m pytest tests\test_position_management_docs_alignment_20260703.py -v --tb=short

# 3. 既存の関連 test も併走 (regression 0 確認)
python -m pytest tests\test_signals_to_orders.py tests\test_final_allocation.py tests\test_audit_remediation_20260702.py tests\test_d3_docs_alignment.py -v --tb=short

# 4. daily paper_trading_dryrun で 実際の entry order type を確認
python scripts\paper_trading_dryrun.py --tier small 2>&1 | Select-String "system3|system5|system7"
# 期待: S3/S5 は entry_price 付きなら limit、S7 は market

# 5. 07-04 06:00 tick 後、Vercel dashboard で当日 signal を確認 (subscriber 影響)
# https://quant-trading-monitor.vercel.app
```

---

## 7. 変更 files 一覧 (Windows 側 working tree)

### code (2 files)
- `common/alpaca_trading.py` — `_DEFAULT_SYSTEM_ORDER_TYPE` の S3/S5/S7 是正 + comment 追加

### test (2 files)
- `tests/test_position_management_docs_alignment_20260703.py` — **新規** (21 test, 5 cluster)
- `tests/test_signals_to_orders.py` — S3/S5/S7 の docs-alignment assertion 更新

### docs (1 file)
- `docs/POSITION_MANAGEMENT_AUDIT_20260703.md` — **新規** (本文書)

---

## 8. Rollback 手順

```powershell
# 個別 revert
git revert <commit-hash>

# または branch 全体を戻す
git reset --hard 29e25f5
```

- 本 dispatch の変更は「default order type map の 3 key 修正 + test + docs 追加」
  のみ。他 module に副作用なし。
- test を revert しないと map 修正だけ入れた状態で test が fail する。

---

## 9. user 手動 review 推奨箇所

1. **S3/S5 の live 挙動**: `paper_trading_dryrun` で実際に limit 注文が発行されるか確認。
   `entry_price` が row にない edge case で market fallback するか確認。
2. **S7 (SPY) の live 挙動**: catastrophe hedge が「翌日寄り成行」で発火するか。
   SPY は tick 差が広いので limit だと不利だった懸念を解消。
3. **Phase 5 future consideration の 5 項目**: user 判断待ち。特に (a) dedup 明文化
   と (c) portfolio drawdown flatten は事業 lens で重要。

---

## Appendix A: 参考 file

- `docs/systems/システム1.txt` 〜 `システム7.txt`
- `docs/systems/INDEX.md`
- `docs/today_signal_scan/6. 配分・最終リスト生成フェーズ.md`
- `docs/D3_CASE_A_IMPL_20260703.md` (先行 dispatch)
- `docs/D5_SYSTEM_SPECIFIC_CONFIG_bug_20260702.md` (defer 中)
- `docs/AUDIT_REMEDIATION_20260702.md`
- `config/config.yaml` (`risk`, `strategies`, `ui.long_allocations/short_allocations`)
- `core/final_allocation.py::DEFAULT_LONG/SHORT_ALLOCATIONS`
- `common/trade_management.py::SYSTEM_TRADE_RULES`
- `common/alpaca_trading.py::_DEFAULT_SYSTEM_ORDER_TYPE`

---

## Appendix B: docs 未変更を明示する change log 案 (各 systems/*.txt 追記候補)

以下を各 docs の【変更履歴】末尾に 1 blockずつ追記予定 (Phase 6 の別 sub-step で
バッチ処理予定、本 audit dispatch では report のみ)。

```
2026-07-03 position management alignment audit:
	•	 System{N} の docs spec (仕掛け / ポジションサイジング / 損切 / 利食い /
	     利益保護 / 保有期間) は既に impl (common/trade_management.py::SYSTEM_TRADE_RULES,
	     common/alpaca_trading.py::_DEFAULT_SYSTEM_ORDER_TYPE, core/final_allocation.py)
	     と完全整合していた (verify only)。
	•	 本 dispatch で S3/S5 = LIMIT / S7 = MARKET の default order type map ミスマッチ
	     を是正 (docs は無変更、impl 側が docs 準拠に is正)。
	•	 参考: docs/POSITION_MANAGEMENT_AUDIT_20260703.md
```
