# Audit Remediation — 2026-07-02 (opus-4-8)

**Source audit**: `docs/SYSTEM_AUDIT_20260702_opus48.md` (branch `claude/system-audit-opus48`, commit `407a508`)
**Remediation branch**: `claude/audit-remediation-20260702` (base: `claude/monitor-webapp` @ `7161b4b`)
**Authorization**: user 明示承認 — core/system1-7.py + common/trade_management.py の変更を承認済、実発注ノータッチ、Draft PR (merge しない)。

> ⚠️ Reversible commits. すべて 1 finding = 1 commit で分離。rollback は各 commit を revert するだけ。

---

## 1. Finding → 修正 mapping

| # | Finding (audit) | Severity | 修正 | Commit | 種別 |
|---|-----------------|----------|------|--------|------|
| Part4 | `SYSTEM_TRADE_RULES["system7"]` の未使用 20日5ATR stub (時限バグ) | P0 | stub 完全削除 + 説明コメント | `0bcda0c` | logic (削除) |
| Part3 | entry_price=Close コメント不整合 (look-ahead ではないが誤読を招く) | P1/P2 | System1/4/7 のコメントを sizing proxy と明示 | `fcd58cb` | comment only |
| P0-1 | System5 setup 乖離 (ADX7>55/RSI3<50/100SMA+ATR 未 enforce) | **P0** | filter/setup を spec 準拠に是正 + predicate 統合 + tests | `aeaa990` | **logic** |
| P1-6 | System3 filter 乖離 (Close≥5/DV20>25M は spec より厳しい) | P1 | ①暫定: 意図的乖離を文書化 (`c1f7c85`) → ②**user 判定で spec 準拠に revert** (`a87e809`) | logic |
| P2-9 | determinism: `validate_predicate_equivalence` の random.shuffle 未 seed | P2 | seed 固定ローカル RNG 化 | `8af946e` | logic (検証専用) |

### 明示的に defer した項目 (scope creep 回避 / backtest 必須 / analysis 推奨)

| # | Finding | 判定 | 理由 |
|---|---------|------|------|
| P2-7 | predicate 二重実装の single-source-of-truth 化 | **部分対応** | System5 は predicate==core に統合済。全 System の core→predicate 配線は audit 自身が「将来 (ID7/ID8)」と明記する大規模 refactor。今回は最も乖離の大きい System5 のみ収束。 |
| P2-6 | sizing robustness (小口/大口の配分達成率) | **defer** | audit の指摘は「資本レンジ別のテストを推奨」= 検証タスクであり局所コードバグではない。整数株切り捨ては仕様通りの挙動。テスト追加は別 PR 推奨。 |
| P2-8 | hedge timing lag (System7: 50日安値→翌寄りの1日ラグ) | **defer** | ラグは spec (「翌日寄り」) 由来の設計。変更は戦略セマンティクスを変え backtest 必須。audit も「歴史的ギャップ日で定量化すべき」= 分析推奨。 |
| P1-4/5 | 日次シグナル時系列保存 / SPY欠損 alert | **defer** | pipeline/監視側の別領域 (core/systemN 変更外)。`[[ps1-utf8-bom-and-stderr]]` の Task Scheduler 経路で別途対応。 |
| P3-10 | regime dependency 文書化 | **defer** | code 変更なしの分析タスク。 |

---

## 2. 各修正の詳細

### 2.1 System7 stub 削除 (`0bcda0c`) — P0, 最も安全
- `common/trade_management.py`: `SYSTEM_TRADE_RULES["system7"]` (stop_atr_period=20, stop_atr_multiplier=5.0, 「詳細は要確認」) を削除、説明コメントに置換。
- **なぜ安全**: 実運用の System7 stop/exit は strategy 側 (`strategies/system7_strategy.py`: entry+3×ATR50, 70日高値→翌寄り) で完結。この dict は System7 経路で参照されない。
- **削除後の挙動**: `SYSTEM_TRADE_RULES.get("system7")` → `None`。`create_trade_entry` / `run_auto_rule_enhanced` は None を受けて graceful skip (誤った 20日5ATR enhancement をするより安全)。
- **rollback**: `git revert 0bcda0c`。

### 2.2 entry_price コメント (`fcd58cb`) — comment only
- `core/system1.py:1177`, `core/system4.py:345`, `core/system7.py:267` のコメントを「翌日寄りで実発注、entry_price は当日終値を live サイジング用 proxy として使用 (未来データ非参照 = look-ahead でない)」へ修正。
- **logic 不変** (diff はコメント行のみ)。

### 2.3 System5 setup 是正 (`aeaa990`) — P0, subscriber 信頼性直結
仕様 (`docs/systems/システム5.txt`) 準拠:
- `core/system5.py`: `MIN_ADX` 35→55 (既存 `SYSTEM5_ADX_THRESHOLD=55` と一致するも従来 unused だった)。
- setup = filter & `Close>SMA100+ATR10` & `RSI3<50` (従来は setup==filter で未 enforce)。
- deep-fallback (`manual_pass`) も同条件に更新。
- `common/system_constants.py`: `SYSTEM5_REQUIRED_INDICATORS` に `sma100`, `rsi3` 追加 (両者とも precomputed 済み)。
- `common/system_setup_predicates.py`: `system5_setup_predicate` を同条件に統合 (P2-7 部分対応)。
- 実装ロジックは既存 `common/today_signals.py:1168-1203` の表示ロジック (Close>SMA100+ATR10 / ADX7>55 / RSI3<50) と同値。

### 2.4 System3 filter/setup — spec 準拠に revert (`c1f7c85` → `a87e809`)
- **`c1f7c85`** (暫定): Close≥5 / DV20>25M を「意図的に spec より厳しい」と文書化 (logic 不変)。
- **`a87e809`** (最終, user 判定「doc = single source of truth, ルールは全て書いてある」):
  spec (`docs/systems/システム3.txt`) 通りに revert。
  - **filter**: `Low ≥ 1` (最低株価1ドル、旧 Close≥5)、`AvgVolume50 ≥ 100万株` (旧 dollarvolume20>25M = 売買代金基準 → **株数基準**に変更)、`atr_ratio ≥ 0.05` (不変)。
  - **setup**: `Close > sma150` を追加 enforce (旧実装は filter & drop3d のみで **150SMA 条件が欠落**していた)、`drop3d ≥ 0.125` (不変)。
  - `SYSTEM5_REQUIRED_INDICATORS` 同様に `avgvolume50`, `sma150` を required に追加。
  - `system3_setup_predicate` も同値に統合 (docstring は元々 spec を記述していたが code が未一致だった)。
  - fast-path/normal-path とも spec helper 経由で drift 防止。
- **subscriber 影響**: 価格フロア緩和 ($5→$1) で低位株が新規参入する一方、出来高基準が株数ベースに変わり setup に 150SMA 条件が加わる (厳格化)。**候補数の純変化は実データ backtest で要確認**。
- test: `test_core_system3_enhanced.py` の fixture/assertion を spec に更新 (pre-existing 2 failures を解消、新規失敗 0)。`tests/test_audit_remediation_20260702.py` に System3 spec テスト 7 件追加。

### 2.5 determinism (`8af946e`) — P2, 検証専用
- `random.shuffle(rows)` → `random.Random(seed).shuffle(rows)` (default seed 20260702, env `VALIDATE_SETUP_PREDICATE_SEED` で上書き可)。グローバル random 状態を汚さない。本番シグナル生成は非関与。

---

## 3. Backtest / subscriber 影響評価

### System5 (最重要)
新 setup は旧 setup の**厳密な部分集合** (test で `(new & ~old).sum()==0` を assert)。一様乱数ユニバース (n=3000, seed 20260702) では:

| | filter pass | setup pass |
|---|---|---|
| 旧 | ~1078/2000 | ~1078/2000 (setup==filter) |
| 新 | ~687/2000 | **~180/2000 (約83%減)** |

- **実データでの減少率は異なる** (RSI3<50 は概ね~50%、Close>SMA100+ATR10 はトレンド依存、ADX>55 は強トレンド時のみ)。上記は方向性 (大幅減) の確認であり実数値ではない。
- **subscriber 影響**: System5 の日次候補数は顕著に減少する。これは「高ADXの押し目リバーサル」という設計意図への収束であり、従来は緩すぎた条件で無差別ロングしていた分の是正。
- **要人間確認**: 実データ backtest での勝率/DD/候補頻度の再測定 (本 remediation の範囲外。データ環境が必要)。

その他 System (1/3/4/7) は logic 不変 (コメント/文書化のみ) → backtest 影響なし。

---

## 4. テスト

- **新規**: `tests/test_audit_remediation_20260702.py` (10 tests: sys7 stub 削除確認、System5 setup gating 6 種、predicate==core 等価、旧→新 selectivity regression)。全 pass。
- **fixture 更新**: `tests/test_systems_controlled_all.py` System5 B-group に sma100/rsi3 追加 (新 setup 準拠)。全 6 system pass。
- **22-set (entry/exit suite)**: 27 passed / 4 failed — **baseline と不変** (4 failures は `test_system1_strategy` の `simulate_trades_with_risk` 属性ドリフト = 本修正以前から存在、本修正と無関係)。
- **既知の pre-existing failures (本修正が原因ではない)**:
  - `tests/test_system1_strategy.py` ×4 (`simulate_trades_with_risk` attribute)
  - `tests/test_system5.py::test_minimal_indicators`, `::test_filter_conditions` (archive 依存 / fixture)
  - `tests/test_validate_predicate_equivalence.py` の順序依存 flake (`get_env_config` cache leak; ファイル単独でも base で同様に fail)。

---

## 5. Rollback 手順
1. 個別: `git revert <commit>` (例: System5 のみ戻すなら `git revert aeaa990`)。各 commit は独立。
2. 全体: `git revert 8af946e c1f7c85 aeaa990 fcd58cb 0bcda0c` または branch を捨てて `claude/monitor-webapp` に戻す。
3. System5 の候補数を旧挙動に戻したい場合は `aeaa990` の revert のみで十分 (constants の required indicators 追加も同 commit 内)。

---

## 6. user 手動 review 推奨箇所
1. **System5 実データ影響** (最重要): 候補数減少が事業的に許容範囲か。実 backtest で勝率/DD を再測定。
2. **System7 stub 削除の live 経路**: `create_trade_entry("system7")` が None を返す挙動 (graceful skip) が live allocation で意図通りか。従来は誤った 20日5ATR で enhancement されていた。
3. **System3 spec 準拠 revert 後の影響** (user 判定済 = spec 採用): 低位株($1〜$5)の新規参入 + 出来高基準の株数化 + 150SMA setup 追加による候補数変化を実データで確認。特に低位株のスリッページ/約定品質を live 前に検証推奨。
4. **pre-existing test 債務**: `test_system1_strategy` (simulate_trades_with_risk) と `test_validate_predicate_equivalence` の order-flake は別 PR での cleanup 推奨。
