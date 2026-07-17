# System8 — SPY オーバーナイト FOMC プレドリフト移植記録 (2026-07-16)

## 1. 出所 (source of truth)

本戦略は **別リポジトリ** で研究・凍結・独立レビュー・封印(sealed)テスト済みの
検証済みエッジを、当リポジトリの System8 として移植したもの。**新規の探索・最適化は
一切していない**（凍結ルール v03 をそのまま実装）。

- 出所リポジトリ（ローカル参照）: `/home/user/mt5_Bundle-of-edges`
- 戦略 ID: `n0150_fomc_macro_event_drift_spy`
- 凍結ルール: `strategies/n0150_fomc_macro_event_drift_spy/rules_frozen.md`（v03, 唯一の正）
- 証跡チェーン: `strategies/n0150_fomc_macro_event_drift_spy/STATUS.md`
- FOMC カレンダー原本: `data/events/fomc.csv`（当リポジトリへ同一内容を複製）

### 出所リポジトリでのステータスと主要証跡（再検証不要・参考）

- ステータス: **GO_CANDIDATE**（2026-07-16 宣言, SEALED_PASS 後）
- フルヒストリ 2006-2025 の canonical `passes_oos`: 3 レジーム軸すべて PASS,
  **t = +3.280**, n = 159, MinTRL 35.3, **DSR(n_trials=3) = 0.9955**
- 独立 GO レビュー（レビュアー ≠ 最適化者, ゼロからの再実装）: **CONFIRM**
  （全ヘッドライン数値を完全再現）
- 封印 2025 単発テスト: **SEALED_PASS**（2025 セル n=8 mean +8.76bp, 5/8 positive）

## 2. 実装した凍結ルール（v03 そのまま）

- **銘柄**: SPY のみ。**ロングのみ**。同時保有は1ポジション。
- **イベント源**: `data/events/fomc.csv` の**予定された FOMC 声明日のみ**（年8回）。
  電話会議・臨時/緊急会合・議事録公表日は対象外。声明日が**非取引日**に落ちた場合は
  当該イベントを**丸ごと除外**（メイクアップ日なし）。
- **エントリー**: 声明日 T の前営業日 **T-1 の引け（MOC）** でロング。
- **エグジット**: 声明日 **T の寄り（MOO）**。1泊のオーバーナイト保有のみ。
  14:00 ET の発表は**絶対に持ち越さない**。
- **サイジング**: イベントごと等ノーショナル・無レバレッジ・ナンピン/マーチン禁止。
- **ストップ**: なし（1泊のイベント保有。リスクはサイジングで制御）。
- **コスト**: 往復 **2bp**（Alpaca 手数料 $0 + SPY スプレッド ~0.5-1bp/片道）。
  → 当リポジトリのライブ執行ブローカーも Alpaca のため前提はそのまま引き継ぎ。

### 明示的にスコープ外（出所リポジトリで却下/保留。実装しない）

- 日中 2pm→2pm ウィンドウ版
- QQQ 第2レッグ / CPI・NFP レッグ
- VIX/vol/レジームゲート、サプライズ符号条件付け

## 3. 当リポジトリでの実装（構造の違い）

System8 は System1-6 の「広いユニバースから top-N を選ぶ」パターンにも、System7 の
「指標セットアップ + ATR ストップ」にも当てはまらない**イベントカレンダー駆動**戦略。
「今日が予定 FOMC 声明日の前営業日 (T-1) か」だけが setup。

- `core/system8.py` — カレンダー読込 + setup 付与 + 候補生成
  - `load_fomc_event_dates()` / `prepare_data_vectorized_system8()`（`setup`/`fomc_event`/
    `fomc_event_date` 列を付与, 指標不要）/ `generate_candidates_system8()`
  - 前方（ライブ/当日）エッジ: 最終行の翌 NYSE 取引日が声明日なら setup。
    正準は `common.utils_spy.resolve_signal_entry_date`（System7 と同基準）、
    それが import 不能な環境では `pandas_market_calendars` に直接フォールバック。
- `strategies/system8_strategy.py` — `System8Strategy(AlpacaOrderMixin, StrategyBase)`
  - `get_trading_side()="long"`, 等ノーショナル `calculate_position_size`（ストップ非依存）、
    T-1 引け→T 寄りの 1 泊オーバーナイト `run_backtest`（往復2bp を控除）。
- `data/events/fomc.csv` — git 追跡の静的参照データ（`data_cache/` はキャッシュで gitignore の
  ため不可）。出所リポジトリと同一内容（2006-2027, 年8回）。

## 4. 配線したもの / あえて配線しなかったもの

### 配線した（バックテスト・診断・UI 可視化の範囲。ライブ発注ではない）

| 箇所 | 内容 |
|---|---|
| `strategies/__init__.py` | `get_strategy("system8")` ファクトリ登録 |
| `common/system_constants.py` | `SYSTEM8_*` 定数 + `SYSTEM_CONFIGS["system8"]`（指標不要） |
| `common/system_groups.py` | side グルーピング（long 側）+ ラベル |
| `common/system_setup_predicates.py` | `system8_setup_predicate` + レジストリ |
| `common/notifier.py` | `SYSTEM_POSITION["system8"]="long"` |
| `common/alpaca_order.py` | 既定オーダータイプ `system8:"market"`（MOC/MOO） |
| `config/config.yaml` | `strategies.system8`（cost 2bp / position_pct / top_n）。**配分ウェイトではない** |
| `apps/systems/app_system8.py` + `apps/app_integrated.py` + `common/ui_components.py` | Streamlit の System8 タブ（SPY 単一バックテスト/診断表示） |
| `tests/test_system8.py` | 決定的テスト（ネットワーク無し, 12 件 PASS） |

**side グルーピングの判断**: System8 は建玉方向としては long だが、System1/3/5 のような
「普通株プールから top-N を選ぶ」ストックピッキングではなく、単一銘柄のイベント駆動
スリーブ。新しいグループ概念は導入せず `SYSTEM_SIDE_GROUPS["long"]` に追加したが、
これは**表示上の side 分類**に過ぎず、**資金配分プール（long_allocations /
short_allocations）への参加を意味しない**（下記参照）。

### あえて配線しなかった（人間の判断待ち）

- **実資金の配分ウェイト**（`config/settings.py` の `long_allocations` /
  `short_allocations`、`config/config.yaml` の同名セクション、
  `core/final_allocation.py` の `DEFAULT_LONG/SHORT_ALLOCATIONS`）。
  → System8 を加えると既存 7 システムからペーパー資金が黙って移動するため未登録。
- **ライブ日次自動発注ループ**（`scripts/run_all_systems_today.py` /
  `scripts/build_execution_recon.py` / スケジューラ `.ps1`）。これらは `range(1, 8)` で
  システムを列挙しており、**あえて `range(1, 9)` に上げていない**。
- 統合バックテスト/配分ビューの列挙ループ（`core/final_allocation.py:1064` order,
  `common/integrated_backtest.py`, `common/stage_metrics.py` 等）も未変更のため、
  System8 は**自分専用の System8 タブ**でのみ実行・可視化される（System7 と同様）。

## 5. ライブに載せるとき（将来）に変える箇所

コードパスは実装済みで壊れていない。ライブ化する際は以下を**人間の判断で**行う:

1. **配分ウェイト付与**: `config/config.yaml` の `short_allocations`/`long_allocations`
   （および `config/settings.py`・`core/final_allocation.py` の既定）を、合計 1.0 を
   保ったまま再配分して `system8: <weight>` を追加。System8 を**別スリーブ**として
   既存プールから独立させるか、既存プール内で薄めるかも経営判断。
2. **ライブ列挙ループ**: `scripts/run_all_systems_today.py` の `range(1, 8)` 群
   （L1259 / L3145 / L4084 / L5003 / L5362 / L5597）と `order_1_7`、
   `scripts/build_execution_recon.py:38` を System8 込みに拡張。ただし System8 は
   MOC/MOO 執行（当日引け発注 + 翌寄り決済）で他システム（翌寄り成行）と執行タイミングが
   異なるため、`common/today_signals.py` の per-system 分岐と exit スケジューリング
   （`schedulers/next_day_exits.py`）に System8 用の MOC/MOO 経路を追加する必要がある。
3. **今日シグナル配線**: `common/today_signals.py` の `LONG_SYSTEMS` / `STOP_ATR_MULTIPLE`
   / SPY 単一分岐に System8 を追加（当日 latest_only 経路は core 側で実装済み）。
4. **フォワード監視**: 出所リポジトリの forward kill trigger（N=16 events で cum mean<0 →
   demote, < −10bp/event → HARD_KILL）と、各イベントが実際に約定したかの発注ログ監査を
   移植（Alpaca 版の attach 検証）。

## 6. 検証結果

- `pytest tests/test_system8.py`: **12 passed**（ネットワーク無し・決定的）。
- `ruff check` / `black --check` / `isort --check`: 新規・変更ファイルすべて clean。
- ライブ/ペーパー Alpaca 発注は一切実行していない。スケジューラ登録 `.ps1` も未変更。

## 7. 監査証跡へのポインタ

- 凍結ルール: 出所リポジトリ `strategies/n0150_fomc_macro_event_drift_spy/rules_frozen.md`
- 証跡チェーン: 出所リポジトリ `strategies/n0150_fomc_macro_event_drift_spy/STATUS.md`
- 独立レビュー: 出所リポジトリ
  `reports/go_reviews/fomc_macro_event_drift_spy_n0150_go_review_20260716.md`
