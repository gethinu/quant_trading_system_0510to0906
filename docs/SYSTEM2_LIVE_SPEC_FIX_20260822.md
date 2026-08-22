# System2 の live 実行を documented spec に戻す (2026-08-22)

**対象ブランチ**: `claude/open-auto-run` (= live runner が読む worktree
`C:\tmp\qts-main-run`。Task Scheduler `QuantTrading_OpenAutoRun` の
`-File C:\tmp\qts-main-run\scripts\open_auto_run.ps1` / WorkingDirectory 同左)。
**paper 限定**。本作業自体は注文を 1 件も出していない (dry-run と read-only GET のみ)。

前提: System2 = Bensdorp「ショート RSI スラスト」。canonical spec は
`docs/systems/システム2.txt`、機械可読 spec は
`common/trade_management.py` の `SYSTEM_TRADE_RULES["system2"]` と
`config/config.yaml` の `strategies.system2`。
**本 doc の数値はすべてこの既存 spec からの引用で、新しい数字は 1 つも作っていない。**

---

## 0. 引用した spec 一覧 (この 3 修正で使った値のすべて)

| 値 | 出所 (このブランチの file:line) | 原文 |
|---|---|---|
| 指値エントリー +4% | `docs/systems/システム2.txt:19` | 「翌日、前日の終値を4%以上上回る価格で売る。」 |
| 同上 (機械可読) | `common/trade_management.py:210-212` | `entry_type=OrderType.LIMIT` / `entry_price_offset_pct=4.0,  # 前日の終値を4%以上上回る価格で売る` / `entry_reference="close"` |
| 同上 (config) | `config/config.yaml:53` | `entry_min_gap_pct: 0.04` |
| 同上 (既定値) | `strategies/constants.py:12` | `ENTRY_MIN_GAP_PCT_DEFAULT = 0.04  # 4% minimum gap for entry (system 2)` |
| 同上 (注文種別) | `common/alpaca_trading.py:38` | `#   S2 = 翌日 前日終値+4% 以上の指値売 (LIMIT)` |
| 同上 (map) | `common/alpaca_trading.py:51` | `"system2": "limit",` |
| 指値の計算式 | `common/trade_management.py:567` / `:599` | `offset_multiplier = 1.0 + (rules.entry_price_offset_pct / 100.0)` → `1.04` |
| 利確 +4% | `docs/systems/システム2.txt:31` | 「大引けで4%以上の利益が出ているときは翌日の大引けで成り行きで手仕舞う。」 |
| 同上 (機械可読) | `common/trade_management.py:216-217` | `profit_target_type="percentage"` / `profit_target_value=4.0,  # 4%の利益が出たら手仕舞う` |
| 同上 (config) | `config/config.yaml:54` | `profit_take_pct: 0.04` |
| 利確価格の式 (ショート) | `common/alpaca_trading.py` `_target_price_for` | `avg_entry_price / (1 + 4/100)` — **既存式。今回変更していない** |
| 損切り 3ATR10 | `docs/systems/システム2.txt:22` | 「売値の上に、過去 10日の3ATR の位置に損切り注文を置く。」 |
| 同上 (機械可読) | `common/trade_management.py:213-214` | `stop_atr_period=10` / `stop_atr_multiplier=3.0,  # 過去10日の3ATR` |
| 同上 (config) | `config/config.yaml:56` | `stop_atr_multiple: 3.0` |
| 最大保有 2 **立会日** | `docs/systems/システム2.txt:32` | 「2日後に利益目標に到達しないときは、翌日に大引け成り行き注文を入れる。」 |
| 同上 (単位が営業日であること) | `strategies/system2_strategy.py:207` | 「未達: **2営業日**待っても利確に届かない場合は3日目の大引けで決済」 |
| 同上 (bar 単位のループ) | `strategies/system2_strategy.py:215-216` | `for offset in range(max_hold_days): idx = entry_idx + offset` |
| 同上 (機械可読) | `common/trade_management.py:218` | `max_holding_days=2,  # 2日後に利益目標に到達しない場合は手仕舞い` |
| 同上 (config) | `config/config.yaml:55` | `max_hold_days: 2 # 書籍通り: 2日後に利益目標に到達しないときは翌日に手仕舞い` |
| 「limit_price 無しなら market」 | `common/alpaca_trading.py:47-48` | 「limit_price が row に無い場合の runtime fallback (`ot = "market"`) は現状維持」 |
| 立会日の数え方 (既存慣行) | `common/profit_protection.py:99` | `held = int(np.busday_count(entry_norm.date(), latest_norm.date()))` |

**不足していた spec 値はゼロ**。3 修正のどれも、値の捏造なしに実装できた。

---

## 1. バグ A — +4% 利確が live で一度も常駐していなかった

### 症状 (実測)
`PROTECT_USE_OCO` 未設定の paper dry-run (2026-08-22, positions=49):

```
targets that will REST: 1 / 18   (S2 は 0/10)
stops   that will REST: 2 / 19
```

System2 の 10 建玉 (ARGX ATRC BHC DINO ETON MTDR OCUL ORKA PSKY WEAV) はすべて
`protect_target` が `skip_reason=qty_reserved:stop_order_already_open` で **未発注**。
broker 側の resting protective order を数えると `stop 17 / trail 5 / target 0 / oco 0`。
つまり **利確指値は 1 本も broker に載っていなかった**。

### 根因 (2 段)
1. Alpaca は 1 注文が建玉 qty を **全量予約** する (`held_for_orders`) ので、
   同じ建玉に stop と limit(target) を同時常駐できない。既存の
   `_PROTECT_KIND_PRIORITY=("trailing","stop","target")` は stop を優先し、
   target を `skip_reason` 付きの非発注提案にする (2026-08-19 の設計)。
2. 同時常駐の唯一の手段である OCO 分岐 (`PROTECT_USE_OCO=1`) は、
   **単発 stop が既に resting だと到達しない**。`already_open` 判定が先に
   短絡して `return` するため。
   → 実測で `PROTECT_USE_OCO=1` にしても **提案 25 件が 1 行も変わらなかった**
   (flag だけでは既存建玉の利確は永久に張られない)。

### 修正 (すべて `PROTECT_USE_OCO` の内側。既定 OFF は不変)
| 変更 | 場所 |
|---|---|
| resting OCO を自分の coid で dedup (毎日 422 duplicate を作らない) | `common/alpaca_trading.py:2276` |
| 単発 stop → OCO **昇格**。同 stop 価格・同 qty の OCO に差し替え、外すべき stop の coid を `cancel_client_order_ids` に載せる | `common/alpaca_trading.py:2293-` |
| `PreparedExit.cancel_client_order_ids` | `common/alpaca_trading.py:1713` |
| coid 指定の限定 cancel (同一銘柄の他注文には触らない) | `common/broker_alpaca.py:552` |
| 昇格前 cancel を実発注 pass にだけ挿入 (dry-run では絶対に通らない) | `scripts/paper_exit_check.py:662-` |
| **昇格 OCO が拒否されたら stop を必ず張り直す** (無保護を作らない) | `common/alpaca_trading.py:2415` `build_stop_rearm_after_failed_oco` / `scripts/paper_exit_check.py:685-` |
| 張り直し済 (`-protect-stop-rearm`) は「stop resting」かつ「昇格失敗済」と解釈し、昇格を再試行しない | `common/alpaca_trading.py:1589`, `:2276-2300` |

`_PROTECT_KIND_PRIORITY` は**変えていない**。priority を target 優先に倒すと
stop が落ちて建玉が無保護になり、今より悪くなるため。

### paper before/after (2026-08-22, dry-run, **発注ゼロ**)
`scripts/paper_exit_check.py --output-json <scratch>` を `--confirm` なしで 2 回。

| | BEFORE (flag OFF) | AFTER (flag ON + 本修正) |
|---|---|---|
| 提案行数 | 25 | 25 |
| 常駐する profit target | **1** | **18** |
| 常駐する stop | **2** | **19** |
| S2 の target 常駐 | **0 / 10** | **10 / 10** |
| S1 trailing 建玉 (AMIX IPST PFSA SLS WETO) | 変化なし | 変化なし |
| orphan stop (CDTX FOLD) | 変化なし | 変化なし |
| 端株 synthetic (MRVI) | 変化なし | 変化なし |

**stop を動かしていないことの実測**: 昇格対象 17 件すべてについて、
新 OCO の `stop_price` と broker に resting 中の stop の `stop_price` が
**完全一致 (delta 0.0000)**、qty も一致。昇格は「stop 据え置き + target 追加」であり、
損切り水準は 1 セントも動かない。

```
sym     resting stop   oco stop    delta   rest qty  oco qty
ARGX       1146.6600  1146.6600   0.0000          1        1
ATRC         53.1200    53.1200   0.0000         53       53
...  (17 件すべて delta=0.0000 / qty 一致)
mismatches: 0
```

### ロールバック (1 行)
```
PRIMARY .env から PROTECT_USE_OCO を削除 (または PROTECT_USE_OCO=0) → 次 run で従来挙動
```
runner は PRIMARY `.env` (`C:\Repos\quant_trading_system_0510to0906\.env`) を読む
(`scripts/open_auto_run.ps1`)。flag が無い状態のコードパスは従来と同一
(テスト `test_flag_off_keeps_old_behaviour` が固定)。

### ★ book 全体への波及 (S2 だけの話ではない)
この修正は **profit target を持つ全 system の保護注文の張り方**を変える。
2026-08-22 の実測で、S2 の 10 件に加えて **S3 が 7 件 (CANG DBGI HYFM INHD MVIS
SGLY) と S5 が 1 件 (PAVS)** も stop-only → OCO(stop+target) に変わる。
S1/S4 (trailing 保持) と S7、端株、orphan は対象外で不変。
「S2 のためだけの flag」ではないことを理解した上で ON にすること。

### 未検証で残る点 (正直な限界)
broker が OCO を **実際に受理して常駐させるか** は、注文を出さずには検証できない。
本作業では以下までを確認した:
- SDK が short クローズ (side=buy) の OCO request を構築できる (offline 実測)
- builder が正しい 2 leg (stop=売値+3ATR10 / target=売値/1.04) を作る (unit test)
- 昇格が stop 価格・qty を変えない (broker 実測突合)
- resting OCO を再送しない (unit test)

受理されなかった場合に備えて **stop 張り直し** を入れてあるので、最悪でも
「従来どおり stop だけ resting」に戻る (無保護にはならない)。
**初回 ON の run は `logs/` と `results_csv/exit_orders_*.json` を必ず確認すること**
(`[exit_check] !! OCO 昇格失敗` / `!! CRITICAL` の行が出ていないか)。

---

## 2. バグ B — +4% の指値エントリーが live 経路に存在しなかった

### 症状 (実測)
`results_csv/today_signals_20260821.json` の sys2 全 10 件で
**`entry_price / prev_close = 1.0000`**。spec の +4% オフセットがどこにも無い。
さらに発注は成行。

```
HTFL  SELL entry_price=45.53  prev_close=45.53  ratio=1.0000
PSKY  SELL entry_price=10.60  prev_close=10.60  ratio=1.0000
NIQ   SELL entry_price=18.31  prev_close=18.31  ratio=1.0000
AMR   SELL entry_price=194.42 prev_close=194.42 ratio=1.0000
```

### 根因 (3 箇所)
1. `common/alpaca_trading.py` `signals_json_to_orders` が
   `order_type="market"` **固定**。同 module が持つ
   `_DEFAULT_SYSTEM_ORDER_TYPE["system2"] = "limit"` を参照していなかった。
2. `common/today_signals.py::_compute_entry_stop` の live 経路。当日シグナルは
   「翌日の注文」を作るので entry_date の bar は df にまだ無く、
   `System2Strategy.compute_entry` (当日 Open が要る) は必ず None を返す。
   結果 generic fallback の `prev_close_fallback` = **オフセットなしの前日終値**が
   entry_price になっていた。
3. `apps/app_today_signals.py::_entry_and_stop_prices` の system2 分岐が
   当日 Open を約定価格として復元していた (保有中建玉の exit 判定用)。
   同じ指値系の system6 は ratio で復元しており、**system2 だけが例外**だった。

### 修正
| 変更 | 場所 |
|---|---|
| spec 指値 `前日終値 x (1 + entry_min_gap_pct)` を strategy に公開 | `strategies/system2_strategy.py:197` `compute_entry_limit_price` |
| live 経路でそれを使う (`entry_source=spec_limit_price`) | `common/today_signals.py:2712-` |
| `TodaySignal.limit_price` を新設し JSON まで運ぶ | `common/today_signals.py:102,124,1818,2118` / `common/signal_export.py:168,173,334` |
| JSON flattener が `limit_price` を保持 | `common/alpaca_trading.py:868` |
| 注文種別を `_DEFAULT_SYSTEM_ORDER_TYPE` に従わせる。**`limit_price` が無ければ market へフォールバック** | `common/alpaca_trading.py:1329-` |
| 指値は notional (成行専用) 経路に落とさない | `common/alpaca_trading.py:1501` |
| 整数株 submit に `limit_price` を渡す (渡し忘れていた) | `common/alpaca_trading.py:1536` |
| exit 側の entry 復元を system6 と同じ ratio 方式へ | `apps/app_today_signals.py:2665-` |

`time_in_force="day"`: 売り指値が翌セッションに残って寝たまま約定すると、
その日のシグナルでない建玉を持つことになるため GTC にしない。

### S1/S3/S4/S5/S6/S7 が変わらないことの実測
`_compute_entry_stop` を 5 system で同一 df (prev_close=10.00) に当てた結果:

```
system1   entry=10.0  ratio=1.0000  src=prev_close_fallback   (不変)
system2   entry=10.4  ratio=1.0400  src=spec_limit_price      (修正)
system3   entry=10.0  ratio=1.0000  src=prev_close_fallback   (不変)
system5   entry=10.0  ratio=1.0000  src=prev_close_fallback   (不変)
system6   entry=10.0  ratio=1.0000  src=prev_close_fallback   (不変)
```

`signals_json_to_orders` の dry-run:

```
sym   system   side otype   tif   limit
AAA   system1  buy  market  day   None    (不変)
BBB   system2  sell limit   day   10.4    (修正)
CCC   system3  buy  market  day   None    (不変)
DDD   system4  buy  market  day   None    (不変)
EEE   system5  buy  market  day   None    (不変)
FFF   system6  sell market  day   None    (不変)
SPY   system7  sell market  day   None    (不変)
```

S3/S5/S6 も `_DEFAULT_SYSTEM_ORDER_TYPE` 上は `limit` だが、emitter が
`limit_price` を出していないので **既存の documented fallback (`market`)** に
落ちる。挙動は変わらない。将来 S3/S5/S6 の emitter が spec 指値を出すように
なれば、そこだけ直せば自動的に指値になる。

**バックテストは無変更**: `compute_entry` に一切触っていない。
`_compute_entry_stop` / `get_today_signals_for_strategy` は live 専用経路
(`strategies/base_strategy.py:320` からのみ到達)。

### 既知の残課題 (今回は直さない)
S3/S5/S6 の live entry_price も同じ `prev_close_fallback` でオフセットが無い。
本 task のスコープ (S2) 外なので手を付けていないが、**S2 と同種のバグが 3 本残っている**。
`compute_entry_limit_price` を各 strategy に生やせば同じ配線で直る。

---

## 3. バグ C — 保有日数が暦日で数えられていた + run が飛ぶと exit も飛ぶ

### 3a. 単位の食い違い
`compute_holding_days` は暦日 (`(d1 - d0).days`) を返していたのに、
突き合わせ先の `max_holding_days` は **立会日** ベースの spec だった
(`strategies/system2_strategy.py:207` 「2営業日」、同 `:215-216` の bar ループ)。

暦日で数えると金曜エントリーの System2 は土日を 2 日と数え、**月曜 (=立会 1 日)**
に time exit が発火する。spec より 1 立会日早い手仕舞い。祝日でも同じことが起きる。

**修正**: `common/alpaca_trading.py:1918` `compute_holding_days` を立会日換算に。
NYSE カレンダー (`pandas_market_calendars`。既に `common/utils_spy.py` が使う依存)
で祝日も除外し、カレンダーが使えなければ `np.busday_count`
(= `common/profit_protection.py:99` の既存慣行) へフォールバックする。

| 期間 | 旧 (暦日) | 新 (立会日) |
|---|---|---|
| 金 08-21 → 月 08-24 | 3 → **早期 exit** | 1 → exit しない |
| 金 08-21 → 火 08-25 | 4 | **2 → spec どおり exit** |
| 月 08-17 → 水 08-19 | 2 | 2 (不変) |
| 水 11-25 → 月 11-30 (Thanksgiving 挟み) | 5 → **3 立会日ぶん早い** | 2 |

**波及**: `compute_holding_days` は S2 だけでなく **time exit を持つ全 system
(S2 max=2 / S3 max=3 / S5 max=6 / S6 max=3)** の判定に使われる。どの system も
spec は立会日 (`strategies/system5_strategy.py:251`「時間退出: **6営業日**経過後も…」、
`docs/systems/システム5.txt:33`、`docs/systems/システム6.txt:46`) なので、
これは S2 限定ではなく **全 time exit の単位を spec に揃える修正**。
表示側 (`scripts/export_alpaca_snapshot.py:700`,
`scripts/paper_trading_status.py:165`) の「保有日数」も立会日になり、
`apps/dashboards/app_alpaca_dashboard.py:194` の
`calculate_business_holding_days` と単位が一致する (今までは食い違っていた)。

既存テスト 2 件が旧 (暦日) 挙動を固定していたので spec 側へ更新した:
`tests/test_alpaca_exit_orders.py::TestHoldingDays::test_basic` と
`::TestBuildExitOrders::test_system5_time_based_at_6_days`。

### 3b. run が飛ぶと exit も飛ぶ
`scripts/open_auto_run.py` の `main()` は `gate()` → `signals()` → `exit_stage()`
の順。`gate()` は Alpaca clock を **単発** で取り、1 回でも例外が出ると
`is_open=False` → ABORT する。ABORT は `exit_stage()` より前なので、
**市場が実際に開いていても API の一過性エラーだけでその日の time exit が丸ごと飛ぶ**。
max_holding_days=2 の System2 では 1 立会日の遅延が致命的になる。

**修正**: `scripts/open_auto_run.py:88` `_CLOCK_FETCH_ATTEMPTS = 3` を追加し、
`gate()` で 3 回リトライ (線形バックオフ 2s) してから「閉場」と結論する
(`:337-361`)。本当に閉場ならリトライしても閉場なので安全側は崩れない。
さらに「閉場だった」と「clock が読めなかった」を `record` 上で分離し
(`abort=clock_unavailable` / `record["clock_unavailable"]`)、ntfy 本文にも
「**exit も飛ぶ**ので保有の期限超過を確認すること」を出すようにした。
取り違えると「exit が飛んだ日」を「休場だから正常」と誤読するため。

薄シグナル gate は既に entry 専用 (`0c44623`) なので exit を止めない。
`signals()` が False を返すのは明示 opt-in の `--thin-aborts-run` 時だけ。

### 3c. 直していない既知のズレ
spec は「大引け成り行き」(`docs/systems/システム2.txt:31,32`) だが、runner は
22:35/23:35 JST = 米国寄り付き近辺に成行 close を出す。これは S2 固有ではなく
全 system 共通の運用設計 (open 実行) なので今回は触れていない。

---

## 4. テスト

新規: `tests/test_system2_live_spec_20260822.py` (27 件)。
spec 値が repo に実在することの固定 (`test_spec_values_exist_in_repo`) を含む。

- target が常駐する / 昇格する / resting OCO を再送しない / flag OFF は従来どおり /
  昇格失敗時に stop を張り直す / 一度失敗したら再試行しない / trailing 建玉は対象外
- S2 が prev_close x 1.04 の指値を出す / 損切りが売値+3ATR10 /
  S1/S3/S4/S5/S6 の live entry が不変 / S2 が `limit` + `tif=day` を発注 /
  他 6 system が `market` のまま / `limit_price` 無しは market へフォールバック
- 保有日数が立会日 / 祝日を数えない / 金曜エントリーが月曜に早期 exit しない /
  火曜に spec どおり exit する

更新: `tests/test_alpaca_exit_orders.py` の暦日前提 2 件 (上記 3a)。

回帰比較は `tests/test_*.py` を 1 ファイルずつ 180s timeout で回し、
FAILED/ERROR の ID 集合を before (HEAD 素) と after で突合した (結果は §5)。

---

## 5. 変更ファイル一覧

| file | 内容 |
|---|---|
| `common/alpaca_trading.py` | OCO dedup / stop→OCO 昇格 / 張り直し builder / `cancel_client_order_ids` / 立会日 holding days / `_DEFAULT_SYSTEM_ORDER_TYPE` 順守 + `limit_price` 配線 |
| `common/broker_alpaca.py` | `cancel_open_orders_by_client_order_ids` (coid 指定の限定 cancel) |
| `common/today_signals.py` | live 経路の spec 指値 / `TodaySignal.limit_price` |
| `common/signal_export.py` | `limit_price` を JSON に載せる |
| `apps/app_today_signals.py` | `_entry_and_stop_prices` の system2 分岐を ratio 方式へ |
| `strategies/system2_strategy.py` | `compute_entry_limit_price` |
| `scripts/paper_exit_check.py` | 昇格前 cancel / 昇格失敗時の stop 張り直し |
| `scripts/open_auto_run.py` | clock リトライ / `clock_unavailable` の分離 |
| `tests/test_system2_live_spec_20260822.py` | 新規 27 件 |
| `tests/test_alpaca_exit_orders.py` | 暦日前提 2 件を立会日へ |
