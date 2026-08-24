# System3 / System5 / System6 の live 指値エントリーを documented spec に戻す (2026-08-22)

**対象ブランチ**: `claude/open-auto-run` (= live runner が読む worktree
`C:\tmp\qts-main-run`。Task Scheduler `QuantTrading_OpenAutoRun` の
`-File C:\tmp\qts-main-run\scripts\open_auto_run.ps1` / WorkingDirectory 同左)。
S2 fix (`7dfddca`) と **同じブランチ・同じ worktree**。
**paper 限定**。本作業自体は注文を 1 件も出していない (read-only の artifact 突合と
dry-run のみ)。

これは `docs/SYSTEM2_LIVE_SPEC_FIX_20260822.md` §2 が明示的に残した課題

> ### 既知の残課題 (今回は直さない)
> S3/S5/S6 の live entry_price も同じ `prev_close_fallback` でオフセットが無い。
> 本 task のスコープ (S2) 外なので手を付けていないが、**S2 と同種のバグが 3 本残っている**。
> `compute_entry_limit_price` を各 strategy に生やせば同じ配線で直る。

を閉じるもの。S2 の予告どおり **配線は 1 行も足さず、3 つの strategy に
`compute_entry_limit_price` を生やすだけ** で直った。

**本 doc の数値はすべて既存 spec からの引用で、新しい数字は 1 つも作っていない。**

---

## 0. 引用した spec 一覧 (この 3 修正で使った値のすべて)

3 系統それぞれについて、**4 つの独立した出所**が同じ ratio を指していることを確認した。
1 つでも欠けていれば「repo のどこにも書かれていない数字」なので STOP する取り決めだったが、
**不足していた spec 値はゼロ**。

### System3 — 前日終値 × **0.93** (long / 買い指値を前日終値の下に)

| 出所 | file:line | 原文 |
|---|---|---|
| canonical spec | `docs/systems/システム3.txt:19` | 「前日の終値の7%下に指値注文を入れる。」 |
| 機械可読 spec | `common/trade_management.py:224-226` | `entry_type=OrderType.LIMIT` / `entry_price_offset_pct=-7.0,  # 前日の終値の7%下に指値` / `entry_reference="close"` |
| ratio への変換式 | `common/trade_management.py:567` / `:599` | `offset_multiplier = 1.0 + (rules.entry_price_offset_pct / 100.0)` → `1.0 + (-7.0/100) = 0.93` |
| 運用 config | `config/config.yaml:64` | `entry_price_ratio_vs_prev_close: 0.93` |
| 注文種別 (コメント) | `common/alpaca_trading.py:40` | 「S3 = 前日終値-7% 指値買 (LIMIT)」 |
| 注文種別 (map) | `common/alpaca_trading.py:52` | `"system3": "limit",` |
| 損切り 2.5ATR10 | `docs/systems/システム3.txt:22` / `common/trade_management.py:227-228` / `config/config.yaml:65` | 「買値の下に過去 10日の 2.5ATR」 / `stop_atr_period=10` `stop_atr_multiplier=2.5` / `stop_atr_multiple: 2.5` |

### System5 — 前日終値 × **0.97** (long / 買い指値を前日終値の下に)

| 出所 | file:line | 原文 |
|---|---|---|
| canonical spec | `docs/systems/システム5.txt:20` | 「前日の終値の3%下に指値をして買う。」 |
| 機械可読 spec | `common/trade_management.py:250-252` | `entry_type=OrderType.LIMIT` / `entry_price_offset_pct=-3.0,  # 前日の終値の3%下に指値` / `entry_reference="close"` |
| ratio への変換式 | `common/trade_management.py:567` / `:599` | `1.0 + (-3.0/100) = 0.97` |
| 運用 config | `config/config.yaml:80` | `entry_price_ratio_vs_prev_close: 0.97` |
| 注文種別 (コメント) | `common/alpaca_trading.py:42` | 「S5 = 前日終値-3% 指値買 (LIMIT)」 |
| 注文種別 (map) | `common/alpaca_trading.py:54` | `"system5": "limit",` |
| 損切り 3ATR10 | `docs/systems/システム5.txt:23` / `common/trade_management.py:253-254` / `config/config.yaml:81` | 「買値の下に、過去10日の 3ATR」 / `stop_atr_period=10` `stop_atr_multiplier=3.0` / `stop_atr_multiple: 3.0` |

### System6 — 前日終値 × **1.05** (short / 売り指値を前日終値の上に)

| 出所 | file:line | 原文 |
|---|---|---|
| canonical spec | `docs/systems/システム6.txt:33` | 「前日の終値を5%上回る位置に指値を置いて売る。」 |
| 機械可読 spec | `common/trade_management.py:265-267` | `entry_type=OrderType.LIMIT` / `entry_price_offset_pct=5.0,  # 前日の終値を5%上回る位置に指値で売る` / `entry_reference="close"` |
| ratio への変換式 | `common/trade_management.py:567` / `:599` | `1.0 + (5.0/100) = 1.05` |
| 運用 config | `config/config.yaml:88` | `entry_price_ratio_vs_prev_close: 1.05` |
| 注文種別 (コメント) | `common/alpaca_trading.py:43` | 「S6 = 前日終値+5% 指値売 (LIMIT)」 |
| 注文種別 (map) | `common/alpaca_trading.py:55` | `"system6": "limit",` |
| 損切り 3ATR10 | `docs/systems/システム6.txt:36` / `common/trade_management.py:268-269` / `config/config.yaml:89` | 「売値の上に、過去10日の 3ATR」 / `stop_atr_period=10` `stop_atr_multiplier=3.0` / `stop_atr_multiple: 3.0` |

**side の向き**: S3/S5 は **long** なので買い指値を前日終値の **下** に (ratio < 1.0)、
S6 は **short** なので売り指値を前日終値の **上** に (ratio > 1.0) 置く。
`tests/test_system356_live_spec_20260822.py::test_side_direction_matches_the_spec` が
この向きを固定する (符号を反転させたら落ちる)。

---

## 1. 症状 (実測)

### 1a. 実 artifact — オフセットが完全に消えていた

`results_csv/today_signals_20260820.json` (S3/S5 の signal が両方出た直近の run)。
各 `entry_price` が data cache 内の **素の Close と 1 セント違わず一致** する
= オフセットゼロ。`limit_price` は全行 `null`。

```
--- sys3  spec ratio=0.93 ---   (8 signals すべて offset なし)
  MVIS    entry=   1.8000  == Close on 2026-08-18  -> ratio 1.0000 (素の終値)
  SGLY    entry=   3.4900  == Close on 2026-08-18  -> ratio 1.0000 (素の終値)
  HYFM    entry=   1.1700  == Close on 2026-08-18  -> ratio 1.0000 (素の終値)
  JZ      entry=   1.7300  == Close on 2026-08-18  -> ratio 1.0000 (素の終値)
  DBGI    entry=  10.9000  == Close on 2026-08-18  -> ratio 1.0000 (素の終値)
  FFAI    entry=   3.3100  == Close on 2026-08-18  -> ratio 1.0000 (素の終値)
  CANG    entry=   1.6300  == Close on 2026-08-18  -> ratio 1.0000 (素の終値)
  INHD    entry=   8.0000  == Close on 2026-08-18  -> ratio 1.0000 (素の終値)

--- sys5  spec ratio=0.97 ---   (10 signals すべて offset なし)
  EAT ZETA VOYG MRVI BWIN PAY PAVS GCT NEWP FIGS  — 全 10 件が ratio 1.0000
```

S6 は該当期間に signal が出ていない (spec の「6日で20%上昇」が厳しく候補ゼロは正常。
`docs/systems/システム6.txt:17-22`) が、コード経路は S3/S5 と完全に同一。

### 1b. 合成 df での経路実測 (BEFORE)

`prev_close=10.00` / `ATR10=0.30` / `entry_date` = df に無い翌営業日 (= live の形):

```
system       entry    ratio      stop  src
system1    10.0000   1.0000    8.5000  prev_close_fallback   (spec どおり成行)
system2    10.4000   1.0400   11.3000  spec_limit_price      (7dfddca で修正済)
system3    10.0000   1.0000    9.2500  prev_close_fallback   ← spec は 0.93
system4    10.0000   1.0000    9.5500  prev_close_fallback   (spec どおり成行)
system5    10.0000   1.0000    9.1000  prev_close_fallback   ← spec は 0.97
system6    10.0000   1.0000   10.9000  prev_close_fallback   ← spec は 1.05
```

---

## 2. 根因 — 「配線は S2 で通っている。emitter だけが無い」

S2 fix (`7dfddca`) が敷いた配線は **最初から system 非依存** だった。BEFORE の時点で
`limit_price` を手で載せた JSON を流すと、S3/S5/S6 も既に正しく指値として emit される:

```
=== BEFORE: signals_json_to_orders dry-run (limit_price を手で載せた場合) ===
sym   system    side  otype   tif     limit
AAA   system1   buy   market  day      None
BBB   system2   sell  limit   day     10.40
CCC   system3   buy   limit   day     18.60   ← 配線は既に通っていた
DDD   system4   buy   market  day      None
EEE   system5   buy   limit   day     38.80   ← 同上
FFF   system6   sell  limit   day     63.00   ← 同上
SPY   system7   sell  market  day      None
```

つまり欠けていたのは **1 箇所だけ**: 各 strategy が spec 指値を公開していないこと。

`common/today_signals.py:2712` の live 分岐は

```python
_limit_fn = getattr(strategy, "compute_entry_limit_price", None)
if callable(_limit_fn):
    ...
    _record_detail("entry_source", "spec_limit_price")
```

と **duck typing** なので、method を持たない S3/S5/S6 は素通りして
`prev_close_fallback` に落ちていた。`limit_price` が空のまま JSON に出るので、
`signals_json_to_orders` は「limit_price が row に無ければ market」という
既存の documented fallback (`common/alpaca_trading.py:47-48`) を正しく踏んで
成行を出していた。**誤発注ではなく、spec の指値が一度も存在しなかった**。

なぜ `compute_entry` が使えないか (S2 と同じ理由・別の失敗の仕方):

- S2 の `compute_entry` は **当日 Open** を必要とし、live には無いので None。
- S3/S5/S6 の `compute_entry` は `df.index.get_loc(candidate["entry_date"])` で
  **entry_date の bar 自体**を探す。当日シグナルは「翌日の注文」を今日作るので
  その bar はまだ df に無く、`get_loc` が失敗して None。

どちらも live では必ず None になり、generic fallback に落ちる。

---

## 3. 修正

| 変更 | 場所 |
|---|---|
| spec 指値 `前日終値 x 0.93` を strategy に公開 | `strategies/system3_strategy.py:218` `compute_entry_limit_price` |
| spec 指値 `前日終値 x 0.97` を strategy に公開 | `strategies/system5_strategy.py:240` `compute_entry_limit_price` |
| spec 指値 `前日終値 x 1.05` を strategy に公開 | `strategies/system6_strategy.py:235` `compute_entry_limit_price` |

**それだけ**。`common/today_signals.py` / `common/signal_export.py` /
`common/alpaca_trading.py` / `apps/app_today_signals.py` は **1 行も変更していない**
(S2 fix の配線をそのまま使う)。

実装は各 strategy 自身の `compute_entry` と同じ式・同じ config キー・同じ丸め:

```python
ratio = float(self.config.get("entry_price_ratio_vs_prev_close", <spec ratio>))
return round(pc * ratio, 2)
```

`pc <= 0` / `None` / 非数値は `None` を返し、下流は従来どおり成行にフォールバックする
(指値価格を確定できないのに limit を出す方が危険なため)。

`apps/app_today_signals.py::_entry_and_stop_prices` は **元から** S3/S5/S6 を
ratio 方式で復元していた (S2 だけが当日 Open を使う例外で、それは `7dfddca` で是正済)
ので、こちらも無変更で spec と整合している。

---

## 4. 修正後の実測 (AFTER)

### 4a. live 経路 — spec ratio ちょうど

```
system       entry    ratio      stop  src
system1    10.0000   1.0000    8.5000  prev_close_fallback   (不変)
system2    10.4000   1.0400   11.3000  spec_limit_price      (不変 = S2 fix 保持)
system3     9.3000   0.9300    8.5500  spec_limit_price      ← 修正
system4    10.0000   1.0000    9.5500  prev_close_fallback   (不変)
system5     9.7000   0.9700    8.8000  spec_limit_price      ← 修正
system6    10.5000   1.0500   11.4000  spec_limit_price      ← 修正
```

損切りも spec どおり side 別に付く (`_compute_entry_stop` の既存ロジック):

| system | entry | stop 式 | stop |
|---|---|---|---|
| S3 (long) | 9.30 | 買値 − 2.5 × ATR10(0.30) | 8.55 |
| S5 (long) | 9.70 | 買値 − 3.0 × ATR10(0.30) | 8.80 |
| S6 (short) | 10.50 | 売値 + 3.0 × ATR10(0.30) | 11.40 |

### 4b. 注文の emit (dry-run、**発注ゼロ**)

| sym | system | side | order type | tif | limit | 判定 |
|---|---|---|---|---|---|---|
| AAA | system1 | buy | market | day | None | 不変 |
| BBB | system2 | sell | **limit** | day | 10.40 | 不変 (S2 fix 保持) |
| CCC | system3 | buy | **limit** | day | 18.60 | **修正** |
| DDD | system4 | buy | market | day | None | 不変 |
| EEE | system5 | buy | **limit** | day | 38.80 | **修正** |
| FFF | system6 | sell | **limit** | day | 63.00 | **修正** |
| SPY | system7 | sell | market | day | None | 不変 |

`time_in_force="day"`: S2 と同じ理由。指値が翌セッションに残って寝たまま約定すると、
その日のシグナルでない建玉を持つことになるので GTC にしない。

### 4c. 他系統が変わらないことの確認

- **S1 / S4 / S7**: `_DEFAULT_SYSTEM_ORDER_TYPE` 上も spec 上も `market`。
  live entry は `prev_close_fallback` のまま、注文も成行のまま (上表)。
- **S2**: `spec_limit_price` / ratio 1.0400 / `limit` / tif=day のまま。
  `tests/test_system2_live_spec_20260822.py` 全 24 件 + 本 fix 用の
  `test_system2_limit_entry_is_untouched` / `test_system2_still_emits_its_limit_order`
  で二重に固定。
- **バックテスト**: `compute_entry` に一切触れていない。
  `common/today_signals._compute_entry_stop` / `get_today_signals_for_strategy` は
  live 専用経路 (`strategies/base_strategy.py:320` からのみ到達)。さらに
  `TestBacktestPathUntouched::test_live_limit_equals_the_backtest_limit` が
  「live 指値 == 同じ df に対する `compute_entry` の指値」を 3 系統で固定する
  (= 新しい式を持ち込んでいないことの証明)。

---

## 5. ★ 副作用 (ON にする前に読むこと)

### 5a. S3/S5 の long は notional 経路から整数株経路へ移る

S3/S5 は **long**。成行だった頃は fractionable 銘柄で
`plan_order_execution` の `EXEC_NOTIONAL` (= `MarketOrderRequest(notional=...)`) に
乗っていた。指値になった今は `signals_json_to_orders` の

```python
prefer_fractional=prefer_fractional and po.order_type != "limit"
```

(`common/alpaca_trading.py:1501`、S2 fix で導入済) により **必ず `EXEC_QTY` (整数株)** へ
寄る。Alpaca の notional 注文は成行専用なので、このガードが無いと long の指値が
黙って成行に化ける。

**運用上の帰結**: S3/S5 の signal で `notional_usd // limit_price < 1` になるものは
`skip:prefer_qty:...` として **発注されず skip される** (従来は端株で建っていた)。
skip は silent ではなく `skip_reason` 付きで artifact / サマリに残る。
S3 は低位株 (上表の MVIS $1.80 / HYFM $1.17 等) が多いので、
`notional` が小さい run では影響が出る。**初回の run では
`results_csv/` の entry 側 skip 内訳を確認すること**。

S6 は short なので元から `EXEC_QTY`、S2 も short。よってこの副作用は **S3/S5 のみ**。

### 5b. `entry_filled < entry_submitted` が **正常** になる (観測の読み替えが要る)

`scripts/open_auto_run.py:777-806` の fill 再突合は、entry 注文が全件
**終端**するまで `--poll-timeout` (既定 300s、`:1180`) 待つ。DAY の指値は
**指値に届かない限りセッション中ずっと `new`/`accepted` (= `is_working`)** なので、
届かなかった注文は終端せず TIMEOUT 分岐に落ち、`entry_filled` に数えられない。

```
[fills] TIMEOUT (300s) 未終端 N/M -> 現状で記録
[fills] entry fill 再突合: filled=K/M 件      (K < M)
```

これは **不具合ではなく spec どおり**。2026-08-20 の実測で
`entry_submitted=47 / broker 実測 47/47 filled` と 100% だったのは、
全部が成行だったから。S2/S3/S5/S6 が指値になった以上、fill 率が 100% を
割るのが正常な状態になる (バックテスト実測の到達率は §6 参照)。

**朝の self-monitor / recon で `entry_filled < entry_submitted` を CRIT 扱いしない**
こと。指値が届かなかったのか、本当に発注が失敗したのかは
`results_csv/paper_orders*.json` の各行 `status` で区別する
(未約定の指値は `new`/`accepted`、失敗は `rejected`/`canceled`)。
run が 300s 余計に伸びる点も併せて想定しておく。

---

## 6. 未検証で残る点 (正直な限界)

- **broker が指値を実際に受理して常駐させるか**は、注文を出さずには検証できない
  (S2 fix と同じ限界)。本作業で確認したのは
  `PreparedOrder(order_type="limit", limit_price=..., time_in_force="day")` が
  正しく組み立てられるところまで。
- **約定率**: spec 指値は「届かなければ約定しない」。`docs/BACKTEST_LIMIT_FILL_FIX_20260820.md`
  のバックテスト実測では S3 32.0% / S5 52.5% / S6 40.3% の到達率だった。
  つまり修正後は **entry 件数が減るのが正常**。「signals は出たのに建玉が増えない」を
  障害と誤読しないこと。従来 (成行) が spec 外に 100% 約定していた方が異常だった。
- 本 fix は **entry のみ**。exit/保護注文側は無変更。

## 7. ロールバック

配線ではなく method 追加なので、3 つの `compute_entry_limit_price` を消せば
そのまま従来挙動 (`prev_close_fallback` + 成行) に戻る。flag は増やしていない
(S2 fix の指値部分も flag 無しの無条件修正だったので、それに揃えた)。

---

## 8. テスト

新規: `tests/test_system356_live_spec_20260822.py` (34 件)。

- `test_spec_values_exist_in_repo` (×3) — **invented number の検出器**。
  各系統について `trade_management` の `entry_price_offset_pct` → ratio 変換、
  `config/config.yaml` の `entry_price_ratio_vs_prev_close`、
  `docs/systems/システムN.txt` の仕掛け原文 (文字列一致)、
  `_DEFAULT_SYSTEM_ORDER_TYPE` の 4 つを突き合わせる。どれか 1 つでも
  欠けたら落ちる。
- `test_side_direction_matches_the_spec` (×3) — long は前日終値の下、short は上。
- `test_strategy_exposes_the_documented_limit_price` (×3) / 不正入力は None
- `test_live_signal_uses_the_spec_ratio` (×3) — `_compute_entry_stop` 経由で
  entry・stop・`entry_source=spec_limit_price` を固定
- `test_ratio_comes_from_config_not_a_hardcoded_constant` (×3) — config を
  差し替えたら指値も追随する (= 数字を焼き付けていない)
- `test_market_systems_live_entry_is_unchanged` (S1/S4) /
  `test_system2_limit_entry_is_untouched`
- `TestBacktestPathUntouched::test_live_limit_equals_the_backtest_limit` (×3)
- `TestOrderEmission` — flattener が `limit_price` を保持 / 3 系統が `limit`+tif=day+
  正しい side / S2 が不変 / S1・S4・S7 が `market` のまま /
  `limit_price` 無しは market へフォールバック (×3) /
  long の指値が notional 経路に落ちない
- `test_end_to_end_todaysignal_to_order` (×3) — `TodaySignal` →
  `build_signals_json` → flattener → `PreparedOrder` を通しで走らせ、
  `limit_price` が 1 度も落ちずに注文まで届くことを固定

更新: `tests/test_system2_live_spec_20260822.py` 2 件。
`7dfddca` の時点で **修正前の (バグっていた) S3/S5/S6 挙動を固定していた** テストなので、
本 fix の期待値へ移した:

- `TestLimitEntry::test_other_systems_live_entry_is_unchanged` の parametrize から
  system3/system5/system6 を外し、成行 spec の **S1/S4 のみ**に絞る
  (3 系統の新しい期待値は新ファイル側)。
- `TestOrderEmission::test_other_systems_still_emit_market_orders` は
  **落ちない**ので assertion は変えず、docstring で
  「この JSON は sys2 にしか `limit_price` を載せていないので、S3/S5/S6 は
  documented fallback (market) に落ちる — それをここで固定している」ことを明示。

### 回帰

`tests/test_*.py` (230 ファイル) を 1 ファイルずつ 180s timeout で回し、
FAILED/ERROR の ID 集合を before / after で `comm` 突合した。

**before は必ず「同じ worktree の HEAD 状態」で取ること。** 最初 detached worktree
(`git worktree add`) で baseline を取ったが、そこには `.env` が無く `data_cache` も
空 (rolling feather 0 件 vs 本番 15,850 件) だったため、多数のテストが別経路を通り
**比較が成立しなかった** (env 起因の差 3 件が「修正された」ように見えた。うち 2 件は
実際には本番 worktree だと 180s timeout に達して行が出なかっただけ)。
そこで本番 worktree `C:\tmp\qts-main-run` 側で変更ファイルを一旦退避して HEAD 状態に
戻し、同一 env / 同一 data_cache / 同一 cwd で before を取り直した。

| | ID 数 |
|---|---|
| before (同 worktree の HEAD) | **242** |
| after (本 fix 適用) | **241** |
| **new failures (after ∖ before)** | **0** |
| gone (before ∖ after) | 1 |

**新規失敗ゼロ**。gone の 1 件
`tests/test_ui_components_integration.py::TestErrorHandling::test_show_results_with_invalid_data`
は本 fix と無関係な UI テストで、単独実行では HEAD でも 3/3 pass する順序依存の
flake (本 fix が直したものではない)。

---

## 9. 変更ファイル一覧

| file | 内容 |
|---|---|
| `strategies/system3_strategy.py` | `compute_entry_limit_price` (前日終値 × 0.93) |
| `strategies/system5_strategy.py` | `compute_entry_limit_price` (前日終値 × 0.97) |
| `strategies/system6_strategy.py` | `compute_entry_limit_price` (前日終値 × 1.05) |
| `tests/test_system356_live_spec_20260822.py` | 新規 34 件 |
| `tests/test_system2_live_spec_20260822.py` | 旧挙動を固定していた 2 件を更新 |
| `docs/SYSTEM356_LIVE_SPEC_FIX_20260822.md` | 本 doc |

production コード (`common/` / `apps/` / `scripts/`) は **1 行も変更していない**。
