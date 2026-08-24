# テストカバレッジ監査 — quant_trading_system 2026-08-22

- **as_of**: 2026-08-22
- **測定した木**: ローカル `claude/monitor-webapp` @ `f9d09c9`
  （= `origin/main` @ `1eaab5b`(system1 fix) + daily data commit。**指値約定判定 `960487c` を含む**）
- **種別**: READ-ONLY 監査。**コード変更ゼロ・テスト追加ゼロ・発注ゼロ・paper 含め注文一切なし。**
- 横断レポート（bundle 側の詳細を含む）:
  `C:\Repos\mt5_Bundle-of-edges\docs\test_coverage_audit_20260822.md`

> **未マージ branch の扱い**: 今日の修正の一部は main にも本測定木にも入っていない
> （`claude/open-auto-run` / `claude/monitor-safety-nets-20260712` / `claude/engine-gaps-20260821`）。
> 各所見には**どの branch で解決済みか**を明記した。解決済みを未解決として数えていない。

---

## 0. 一言でいうと

**テストは 2,504 本ある。全体 34.2%。問題は分布である。**

計測して出た形はこうである — **テストは「値を次の関数へ渡す配管」を検査し、
「値を決める計算」を検査していない。** 行レベルで並べると一目で分かる:

| 場所 | cov | 何をする行か |
|---|---:|---|
| `common/today_signals.py` L1778-1790 | **0.0%** | **指値を「直近終値」に差し替える行** ← バグの起点 |
| `common/candidates_schema.py` L84-140 | **0.0%** | `normalize_candidates_to_list`（S3/5/6/7 が依存） |
| `scripts/paper_exit_check.py` L172-205 | **3.3%** | ATR 取得。無いと**保護 stop が黙って作られない** |
| `common/integrated_backtest.py` L75-115 | 30.0% | `_compute_entry_exit` の黙示 `return None` |
| `common/signal_export.py` L150-175 | 85.7% | 値を次へ渡す配管 |
| `common/alpaca_trading.py` L409-421 | 81.8% | 値を注文に載せる配管 |
| `common/alpaca_trading.py` L1196-1302 | 91.4% | `_build_protection_orders`（純関数） |

`_build_protection_orders` が 91% でも「protective target が武装しない」が起きたのは、
**純関数ではなく入口（ATR 取得・OCO 昇格）が壊れていた**からである。
テストは手で作った入力を関数に渡しており、実データがその入力を組み立てる経路を通っていない。
今日の 7 件の bug は 1 件残らずこの「入口側」にあった。

---

## 1. 測定した数字

```
python -m coverage erase                       # ← 必須。飛ばすと最後に DataError
COVERAGE_RCFILE=<branch=false の rc> \
python -m pytest tests/ \
  --ignore=tests/test_app_imports.py \
  --ignore=tests/test_cache_manager_final.py \
  --ignore=tests/test_core_system4_enhanced.py \
  --ignore=tests/test_high_impact_modules.py \
  -o addopts= --cov --cov-report=term-missing -q

結果: 226 failed, 2380 passed, 58 skipped, 1 xfailed, 13 xpassed, 10 errors in 443.81s
```

| 指標 | 値 |
|---|---|
| **全体カバレッジ** | **34.2%**（19,993 / 58,452 statements） |
| 測定対象 | `core` `common` `strategies` `apps` `scripts` `tools` `config` `schedulers` = 333 モジュール |
| ゼロカバレッジ モジュール | **164 / 333** |
| テスト関数 | 2,504（270 ファイル） |

### 1.1 パッケージ別

| パッケージ | cov | covered / stmts | 備考 |
|---|---:|---|---|
| `config` | 87.8% | 541 / 616 | |
| `strategies` | 69.5% | 915 / 1,316 | strategy ラッパは厚い |
| `core` | 60.1% | 3,416 / 5,680 | system1-7 の本体 |
| `common` | 46.9% | 9,832 / 20,944 | **ライブ経路の本体がここ** |
| `scripts` | 27.8% | 4,419 / 15,905 | 日次パイプライン |
| `apps` | 13.3% | 779 / 5,837 | Streamlit / dashboard |
| `tools` | **1.2%** | 91 / 7,846 | 使い捨てスクリプト（重み低） |
| `schedulers` | **0.0%** | 0 / 308 | |

### 1.2 重要モジュール（「壊れたときの損害」で並べた。%順ではない）

| モジュール | cov | stmts | 未到達 | 役割 |
|---|---:|---:|---:|---|
| `common/candidates_schema.py` | **0.0%** | 64 | 64 | **S3/5/6/7 が使う候補正規化。テストゼロ** |
| `apps/dashboards/app_alpaca_dashboard.py` | 13.1% | 1324 | 1150 | ポジション表示 |
| `apps/app_today_signals.py` | 18.2% | 2599 | 2127 | **既存建玉の exit 計画（entry 価格の第 2 実装）** |
| `common/integrated_backtest.py` | 26.5% | 272 | 200 | **もう一つのバックテストエンジン** |
| `common/trade_management.py` | 29.5% | 400 | 282 | **exit ルール本体**（max_holding_days / profit target） |
| `scripts/run_all_systems_today.py` | 30.9% | 3752 | 2591 | **日次パイプライン入口** |
| `common/today_signals.py` | 40.7% | 2105 | 1248 | **ライブ signal 生成** |
| `common/profit_protection.py` | 41.1% | 168 | 99 | 保有日数の第 3 実装 |
| `scripts/paper_exit_check.py` | 41.7% | 206 | 120 | **exit / 保護注文の実行者** |
| `common/broker_alpaca.py` | 49.8% | 245 | 123 | ブローカ SDK ラッパ |
| `common/alpaca_order.py` | **10.8%** | 186 | 166 | **もう一つの注文組み立て** |
| `core/final_allocation.py` | 64.2% | 1347 | 482 | 資金配分 |
| `common/backtest_utils.py` | 64.6% | 127 | 45 | エンジン |
| `strategies/base_strategy.py` | 74.4% | 262 | 67 | `_limit_entry_filled` を含む |
| `common/alpaca_trading.py` | **82.0%** | 699 | 126 | 注文/exit 提案（**厚い**） |
| `common/signal_export.py` | **82.6%** | 247 | 43 | signals JSON（**厚い**） |
| `common/exit_ledger.py` | **96.8%** | 380 | 12 | 実現損益台帳（最厚） |

---

## 2. ギャップの性質

### 2.1 **ゼロ**（重要度順）

| 対象 | stmts | なぜ怖いか |
|---|---:|---|
| `common/candidates_schema.py::normalize_candidates_to_list` | 34 | S3/S5/S6/S7 の `generate_candidates` が呼ぶ唯一の正規化。**「engine が候補スキーマ不一致で 0 トレード」の当事者**。同名の `tests/test_candidates_schema.py` は**このモジュールを一切 import しておらず**（`core.system6` / `core.system7` の出力形状を見ている）、名前から覆われていると誤読しやすい |
| `common/io_optimization_benchmark.py` | 340 | ベンチ（重み低） |
| `scripts/scheduled_daily_update.py` | 238 | **日次更新のスケジューラ本体** |
| `scripts/run_auto_rule_enhanced.py` | 228 | profit_target 分岐を持つ |
| `scripts/verify_bulk_accuracy.py` | 245 | データ検証 |
| `common/ai_dashboard.py` | 242 | 表示 |
| `schedulers/*` | 308 | 全部 |

### 2.2 **happy-path のみ**（今日の bug が隠れていた層）

| 場所 | cov | 未検査の分岐 |
|---|---:|---|
| `common/today_signals.py` L1778-1790 | **0%** | `entry_price` 不在時に **latest Close** を詰める分岐。ここが「S2/S3/S5/S6 のライブ指値オフセット消失」の起点 |
| `common/today_signals.py` L2737-2748 | 27% | `_compute_entry_stop` が上の hint を `candidate:entry_price` として採用する分岐 |
| `common/today_signals.py` L2762-2776 | 21% | それも無ければ Close/Open 系列の最終値を entry にする分岐 |
| `scripts/paper_exit_check.py::_load_atr_by_symbol` | 3.3% | rolling CSV 不在／ATR 列不在 → **その symbol は ATR なし → 保護 stop がそもそも提案されない**（例外もログもカウンタも無い） |
| `common/backtest_utils.py` L168-200 | 85% | 5 箇所の黙示 `continue`（df 空 / `get_loc` 失敗 / entry None / shares<=0 / 資金超過）に**計数が無く、全滅しても「0 トレード」としか出ない** |
| `common/integrated_backtest.py` L75-115 | 30% | 同じ黙示 `return None` |

**構造的な重複（どれもテストで突き合わされていない）**:

- 保有日数の実装が **3 つ**: `alpaca_trading.compute_holding_days`（暦日）/
  `exit_ledger.holding_days`（立会日ラベル間の暦日差）/
  `profit_protection.calculate_business_holding_days`（NYSE 営業日）。
  バックテスト側は `compute_exit` の `entry_idx + offset` = **bar（＝立会日）**。
- 候補正規化の実装が **2 つ**: `candidates_schema.normalize_candidates_to_list` と
  `backtest_utils` L170-181 のインライン変換。
  **後者は `**payload` を後置するため payload の `entry_date` が日付キーを上書きする**が、
  前者は明示的に正規化する。両者の一致を主張するテストは無い。
- 正規化を通す system が **非対称**: S3/S5/S6/S7 は通す、**S1/S2/S4 は通さない**（grep 実測 0 件）。
- entry 価格の実装が **2 つ**: `common/today_signals._compute_entry_stop`（**ratio を掛けない**）と
  `apps/app_today_signals._entry_and_stop_prices`（**掛ける**）。前者が発注経路、後者が exit 計画。

### 2.3 スイートが走らない / 赤が常態

| 症状 | 詳細 |
|---|---|
| **フルスイートが収集できない** | `tests/test_app_imports.py` は import 時に実行されるスクリプトで、`except ImportError: sys.exit(1)` を持つ。pytest の収集中に `SystemExit` が飛び **INTERNALERROR でスイート全体が停止**する |
| **import 不能テスト 3 本** | `test_cache_manager_final.py`（`common.cache_manager_old` が無い）/ `test_core_system4_enhanced.py`（`core.system4._compute_indicators` が無い）/ `test_high_impact_modules.py`（`common.system_common.format_dataframes_for_display` が無い） |
| **226 failed** | 上位原因は API 腐り（下表） |
| **CI も赤** | `gh run list` 実測: **CI Unified は直近 3 回すべて failure**（08-20 / 08-20 / 08-21）。`Documentation Check` のみ緑 |
| **カバレッジ計測自体が壊れている** | `pyproject.toml` `[tool.coverage.run] parallel = true` + `branch = true` と、テストが起動する subprocess 側の非 branch データが combine で衝突し `DataError: Can't combine statement coverage data with branch data` → **pytest-cov が INTERNALERROR で落ち、レポートも JSON も出ない** |
| **強制される回避策** | 直近 2 つの修正コミットは安全確認に「修正前後で**失敗集合が完全一致**」を使っている（`960487c` / `7dfddca` の 234 件）。赤を常態として受け入れた運用であり、本物の regression はこの中に埋もれる |

**226 件の原因内訳（上位）**:

| 原因 | 件数 | 性質 |
|---|---:|---|
| `ModuleNotFoundError: indicators_common` | 28 | 消えたモジュールを import |
| `TypeError: ...() takes 1 positional argument but N were given` | 16+ | **API が変わりテストが追随していない** |
| `AssertionError: assert False` | 15 | 要調査（本監査では未分類） |
| `RuntimeError: system_precomputed_indicators_missing` | 12 | フィクスチャ不足 |
| `AttributeError: module 'core.systemN' has no attribute ...` | 11+ | API 腐り |
| `FrozenInstanceError` / `CacheRollingConfig has no attribute 'days'` | 9 | 設定オブジェクトの変更に追随せず |

失敗ファイルは 50 本。上位は `test_indicators_precompute.py`(28) / `test_strategies_optimization.py`(19) /
`test_system2_partial.py`(16) / `test_core_system5_enhanced.py`(16)。

---

## 3. **バグを正解として固定していたテスト**（tests that pin wrong behavior）

**7 件確認。内訳: 4 件は main まで解決済み / 2 件は未マージ branch にのみ fix があり main は誤りのまま / 1 件はどこにも fix が無い。**

| # | テスト | 何を固定していたか | 状態 |
|---|---|---|---|
| 1-4 | `tests/test_system6.py` / `test_system5_old.py` / `test_entry_exit_integration.py` / `test_monthly_roll_forward.py` の各エントリー日 bar | **「指値は必ず約定する」を仕様として固定**（例: System6 の売り指値 105.0 に対しエントリー日の高値が 101 のまま「約定」を期待） | **解決済み** `960487c`（origin/main に到達済み）。修正コミット自身が「テストが仕様としてバグを固定していたため長期間検出されなかった」と記録 |
| 5 | `tests/test_alpaca_exit_orders.py::TestHoldingDays::test_basic`<br>`assert compute_holding_days("2026-07-01","2026-07-04") == 3` | **暦日**。repo 自身の NYSE カレンダーで `(07-01, 07-04]` は **1 立会日**（07-03 は 7/4 が土曜のため休場、07-04 は土曜） | **fix は `origin/claude/open-auto-run` に存在。`origin/main` には未到達**（実測: main の `compute_holding_days` に「立会日」記述 0 件） |
| 6 | `tests/test_alpaca_exit_orders.py::test_system5_time_based_at_6_days`<br>entry 2026-06-26(金) / today 2026-07-02(木) で `max_holding_days=6` の time exit 発火を期待 | **暦日 6 = 立会 4**。spec（`system5_strategy.compute_exit`「6営業日」）より **2 立会日早い手仕舞い**を正解として固定 | 同上（同 branch で「6 立会日後は 2026-07-07(火)」に修正済み、main 未到達） |
| 7 | `tests/test_signals_to_orders.py::test_limit_without_price_falls_back_to_market` | **ライブ発注経路の黙示降格を仕様として固定**。limit system の行に `entry_price` が無いと `order_type` が **market** に落ち、`limit_price is None` になることを「正しい」と主張 | **現存**（`origin/main`・`open-auto-run` の両方にあり） |

**#7 について（反対意見も併記する）**: 「価格の無い limit 注文は送れない以上、何かはしなければならない」
は妥当な反論である。しかし本 repo は同じファイルの中に**正しい先例**を持っている —
`_side_from_row` は不正行を **skip + `_audit_log` + `logger.error`** で落とし、バッチは生かす（L387-403）。
`ot = "market"` はその先例を使わず、**S3 の「−7% まで下がったら買う」を「今の値段で成行買い」に
無言で置換する**。少なくとも「黙って降格した」ことが計数・通知されるべきであり、
現状のテストはその不在を仕様として固定している。

---

## 4. 今日の bug それぞれの「テスト側の穴」対応表

| 今日の bug | 該当コード | 測定した cov | 現状 |
|---|---|---|---|
| 指値が必ず約定する（backtest） | `strategies/system{3,5,6}_strategy.compute_entry` | 71-82% | **解決済** `960487c` + `tests/test_limit_entry_fill_realism.py`(12 件)。旧フィクスチャ 4 本も修正済 |
| **S2/S3/S5/S6 ライブ指値オフセット消失** | `common/today_signals.py` L1778-1790 → L2737-2776 | **0% / 21-27%** | **S2 のみ** `open-auto-run` で解決（`compute_entry_limit_price` 新設 + 27 件のテスト）。**S3(−7%) / S5(−3%) / S6(+5%) は未解決**（同コミット本文が「S3/S5/S6 は不変」と明記） |
| protective target が武装しない | `alpaca_trading._build_protection_orders`(91%) の**入口**: OCO 昇格 / `paper_exit_check._load_atr_by_symbol`(3.3%) | — | OCO 昇格は `open-auto-run` で解決。**ATR 不在→無保護の黙示経路は未解決** |
| 保有日数が暦日 | `alpaca_trading.compute_holding_days` | 66.7% | `open-auto-run` で解決（main 未到達）。**実装は依然 3 系統ある** |
| self_monitor が別 run を読む | `scripts/self_monitor_check.py` | 測定対象外 | `claude/monitor-safety-nets-20260712` で解決。**この木にはファイル自体が無い**（worktree のみ） |
| engine が候補スキーマ不一致で 0 トレード | `common/candidates_schema.py` / `backtest_utils` L168-200 | **0% / 85%** | `claude/engine-gaps-20260821` で対応中。**正規化 2 実装の一致テストは無い** |

---

## 5. 推奨（P0 / P1 / P2）

テストを書くのは本監査の範囲外。**何を・どこに・何を主張するか**まで確定させる。

### P0（今日の bug と同型を止める。6 本）

| # | 対象 | 新規テスト | 主張する性質 |
|---|---|---|---|
| **Q-P0-1** | `common/today_signals.py::_build_today_signals_dataframe` + `_compute_entry_stop` | `tests/test_live_limit_offset_end_to_end.py` | **S3/S5/S6** の当日シグナルにつき、`entry_price`（または `limit_price`）が `round(prev_close * ratio, 2)` と**一致**すること（ratio は `config` 由来、テストにハードコードしない）。`compute_entry` が None を返す条件下（＝ entry_date が価格データに無い＝ライブそのもの）で成立すること。**`ratio == 1.0000` になったら FAIL**（08-21 artifact で S2 が実際にこれだった） |
| **Q-P0-2** | `common/today_signals` → `signal_export` → `alpaca_trading.signals_to_orders` | 同上ファイルに追加 | prev_close を 1 つ与えて、**提出される `PreparedOrder.limit_price` まで**の 3 段を通し、S3 で `limit == prev_close*0.93` を主張。現状は各段が個別に厚く、**端から端まで通すテストが 1 本も無い** |
| **Q-P0-3** | `common/alpaca_trading.py::signals_to_orders`（`ot = "market"` 分岐 L419-420） | `tests/test_limit_downgrade_is_loud.py`（§3 #7 の置換） | limit system の行に有効な `entry_price` が無いとき、(a) 注文を**生成しない**か、(b) 生成するなら `_audit_log` に `event="limit_downgraded_to_market"` が 1 行残り、戻り値から**降格件数が数えられる**こと。**「黙って market」を許さない** |
| **Q-P0-4** | `scripts/paper_exit_check.py::_load_atr_by_symbol` + `build_exit_orders_from_positions` | `tests/test_protection_arming_inputs.py` | (a) rolling CSV 不在 / ATR 列不在 / ATR<=0 の各ケースで、**「保護提案 0 件」が黙って返らない**（`unprotected` 理由が症状別に出る）。(b) profit target を持つ全 system（S2/S3/S5/S6）で **long / short 両側**に PROTECT_TARGET が 1 件出る。(c) 建玉があるのに保護提案が 0 件なら**非ゼロ終了 or 明示アラート** |
| **Q-P0-5** | `common/alpaca_trading.compute_holding_days` × `strategies/system{2,3,5,6}_strategy.compute_exit` | `tests/test_holding_days_unit_parity.py` | **同一の entry/exit 日付**に対し、ライブの `compute_holding_days` とバックテストの bar offset が**同じ整数**になること。金曜エントリー（週末跨ぎ）と NYSE 祝日跨ぎを必ず含める。ついでに 3 実装（`alpaca_trading` / `exit_ledger` / `profit_protection`）が同一入力で一致することを主張し、**片方だけ直る事態を構造的に止める** |
| **Q-P0-6** | `common/candidates_schema.normalize_candidates_to_list` × `backtest_utils` インライン変換 | `tests/test_candidate_schema_parity.py`（**既存 `test_candidates_schema.py` は別物。改名も検討**） | (a) 両正規化器が同じ入力に同じ出力を返す（特に payload に `entry_date` がある場合の優先順位）。(b) `dict[sym→payload]` / `list[dict]` / 文字列日付 / Timestamp 日付の 4 形状すべてが通る。(c) **全候補が落ちた場合、`simulate_trades_with_risk` は「0 トレード」ではなく理由別の drop 件数を返す**（`skipped_no_bar` / `skipped_no_entry` / `skipped_capital` …）。これが無い限り「シグナルが無い」と「配管が壊れた」は区別できない |

### P1（スイートを信号に戻す＋残りの計算層）

| # | 対象 | 内容 |
|---|---|---|
| Q-P1-1 | `tests/test_app_imports.py` | **`sys.exit(1)` を撤去**して普通の `def test_...` にする。1 ファイルがスイート全体の収集を殺している状態を解消 |
| Q-P1-2 | 腐った 3 ファイル（`test_cache_manager_final` / `test_core_system4_enhanced` / `test_high_impact_modules`） | 削除するか、現行 API に合わせて書き直す。放置は収集エラーのまま |
| Q-P1-3 | `pyproject.toml` `[tool.coverage.run]` | `parallel = true` と branch データの衝突を解消（subprocess 側も branch を有効にするか parallel を切る）。**現状カバレッジを一度も取れない** |
| Q-P1-4 | 226 failed の棚卸し | 原因別に「腐り（削除/更新）」「実バグ」に仕分け、**赤ゼロを 1 度作る**。それまで CI は信号として機能しない（`gh run list` 実測で CI Unified は直近 3 回すべて failure） |
| Q-P1-5 | `common/trade_management.py`（29.5%） | `SYSTEM_TRADE_RULES` の各 system につき `max_holding_days` / `profit_target_type` / `profit_target_value` / `stop_atr_multiplier` が **docs/systems/システムN.txt の値と一致**することを主張（`open-auto-run` の「spec 値が repo に実在することの固定」と同型を全 system へ） |
| Q-P1-6 | `common/integrated_backtest._compute_entry_exit`（30%） | `backtest_utils` と同じ入力で**同じトレードを返す**こと（エンジン 2 実装の一致） |
| Q-P1-7 | `common/alpaca_order.py`（10.8%） | `alpaca_trading` 側と order 種別・limit_price の決定が一致すること |
| Q-P1-8 | `scripts/run_all_systems_today.py`（30.9% / 3752 stmts） | 少なくとも「全 system が 0 シグナルのとき」と「一部 system が例外を投げたとき」に**黙って部分結果を返さない**ことを主張 |

### P2

- Q-P2-1: `apps/app_today_signals._entry_and_stop_prices`（18.2%）— `common` 側の実装との
  **ratio 一致テスト**。実装が 2 本ある事実自体を pin する。
- Q-P2-2: `core/final_allocation.py`（64.2% / 1347 stmts）— 配分の境界（枠不足・同一 symbol 競合）。
- Q-P2-3: `schedulers/`（0% / 308）— 最低限の smoke。
- Q-P2-4: `common/profit_protection.calculate_business_holding_days` — `np.busday_count` は
  **NYSE 祝日を除外しない**（Mon-Fri 換算）。祝日入力での挙動を明示的に pin するか、
  カレンダー版へ寄せる。
- Q-P2-5: `tools/`（1.2% / 7846）— 使い捨てが多く優先度は低いが、
  `tools/check_candidates.py` / `trace_unknown_candidates.py` は診断に使われるので smoke だけ。

---

## 6. 本監査で実際に検証したこと（自己レビュー）

1. **最初の測定値を捨てた**: 初回集計（全体 7.3% / `alpaca_trading.py` 13.2%）は**誤り**だった。
   単一ファイル実行で 36% が出たため矛盾を追い、`parallel = true` × branch 不一致で combine が失敗して
   **JSON が書かれていなかった**ことを突き止め、`coverage erase` + branch 無効 rc で再測定した。
   本書の数値（34.2%）は再測定後のもの。**この不整合自体が Q-P1-3 の根拠**である。
2. **暦日 vs 立会日を repo 自身のカレンダーで検算した**: `pandas_market_calendars` の NYSE スケジュールで
   2026-07-03 が休場、`(2026-06-26, 2026-07-02]` が 4 立会日、`(2026-07-01, 2026-07-04]` が 1 立会日
   であることを確認した上で §3 #5/#6 を判定した。
3. **未マージ branch を確認してから「未解決」と書いた**: `git fetch --all` 後に
   `open-auto-run` / `monitor-safety-nets` / `engine-gaps` の各 branch を読み、
   **解決済みのものを未解決として数えていない**。S3/S5/S6 のライブ指値が未解決である根拠は、
   `7dfddca` のコミット本文中の「**S3/S5/S6 は不変**」という著者自身の記述と、
   同 branch の `today_signals.py` に `entry_price_ratio_vs_prev_close` が **0 件**である実測である。
4. **名前に騙されなかった**: `tests/test_candidates_schema.py` は `common/candidates_schema.py` を
   **import していない**（`core.system6` / `core.system7` を見ている）ことを確認した上で
   当該モジュールを「テストゼロ」と判定した。
5. **CI の実状を推測せず照会した**: `gh run list` で CI Unified が直近 3 回すべて failure、
   `Documentation Check` のみ緑であることを確認した。
6. **数えた**: 333 モジュール / 164 がゼロ / 226 failed の原因内訳は、coverage JSON と
   pytest 出力からの機械集計であり目視ではない。
