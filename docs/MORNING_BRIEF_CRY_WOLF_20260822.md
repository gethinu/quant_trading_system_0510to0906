# モーニングブリーフの「オオカミ少年」2 件 — 診断と修正 (2026-08-22)

**Status:** 修正済み。診断は READ-ONLY で完了、修正は表示/判定ロジックのみ。
発注系は一切触っていない (paper のまま、ライブ発注なし)。

08:00 JST のモーニングブリーフが毎朝出していた 2 つのアラートは、**どちらも実害の無い
正常状態を異常として報告していた**。オオカミ少年になったアラートは「無視するもの」に
なり、本物が来たときに効かなくなる。以下、それぞれの真因と、**本物だけが鳴るように
した**修正を記録する。

| | アラート | 発火頻度 (実測) | 真因 | 着地ブランチ |
|---|---|---|---|---|
| 1 | `ダッシュボードの publish が取りこぼされています` | **14/14 朝** (08-09〜08-22) | 検知が **self-heal より前**に鳴っていた | `claude/monitor-webapp` |
| 2 | `[quant] WARN open_run: <date>: 実 run だが entry_submitted=0` | 履歴上 **6/6 の entry=0 の夜** | cap 満杯で全件 skip = 正常なのに無条件 WARN | `claude/monitor-safety-nets-20260712` |

---

## 0. どのブランチ / どのチェックアウトが実際に走っているか

修正を「動いている場所」に置くため、Task Scheduler の実体を先に確定した。

| タスク | 実行スクリプト | チェックアウト | ブランチ / HEAD |
|---|---|---|---|
| `QuantTrading_MorningBrief` (08:00) | `scripts\morning_brief.ps1` | `C:\Repos\quant_trading_system_0510to0906` (PRIMARY) | `claude/monitor-webapp` @ `7e11d4b` |
| `QuantTrading_SelfMonitor` (07:15) | `scripts\self_monitor_check.ps1` | `C:\tmp\qts-safety-nets` (PRIMARY の linked worktree) | `claude/monitor-safety-nets-20260712` @ `289701d` |
| `QuantTrading_OpenAutoRun` (22:35) | `scripts\open_auto_run.ps1` | `C:\tmp\qts-main-run` | `claude/open-auto-run` @ `738834b` |

- `scripts/self_monitor_check.py` は **`claude/monitor-safety-nets-20260712` にしか存在しない**
  (`monitor-webapp` / `main` には無い)。
- `scripts/morning_brief.py` / `morning_brief.ps1` / `check_dashboard_freshness.py` は
  **`claude/monitor-webapp` と `main`** にあり、safety-nets には無い。
- したがって 2 件の修正は**別ブランチに着地する**。同じブランチにまとめると、どちらかは
  走っていないツリーに置くことになる。

### 289701d (open_run dir 選択の修正) のデプロイ状況 → **デプロイ済み**

2026-08-21 の `289701d`「open_run は canonical な nightly dir だけを見る」は、
スケジュールタスクが読むツリーに**入っている**。

```
C:\tmp\qts-safety-nets  ->  claude/monitor-safety-nets-20260712 @ 289701d  (worktree clean)
scripts/self_monitor_check.py:319  _CANONICAL_OPEN_RUN_RE = re.compile(r"^open_run_(\d{8})$")
```

実行結果でも裏が取れている: `logs/self_monitor_20260822.json` (07:15:09 の定例実行) は
`"dir": "open_run_20260821"` を選んでおり、同居している sidecar
`open_run_20260820_oneshot_flatten` を掴んでいない。**デプロイギャップは無い。**
`289701d` は `origin/claude/monitor-safety-nets-20260712` にも載っている。

→ よって ALERT 2 の原因は (a) ではない。

---

## 1. ALERT 1 — `ダッシュボードの publish が取りこぼされています`

### 1.1 何が起きていたか

`morning_brief.ps1` の並びが**検知 → 通知 → 治療**になっていた。

```
1. check_dashboard_freshness.py --notify --check-served   <- stale なら即 ntfy
2. publish_data_to_vercel.ps1 -AutoLatest                 <- self-heal (数秒で解消)
```

つまり **「次の行が直そうとしている状態」に対して通知していた**。

しかも 08:00 時点で gap があるのは事故ではなく **通常状態**である:

- 06:00 のデイリー (`daily_main_follow.ps1`) は `results_csv/` に当日分を生成する。
- publish (`git commit-tree` で `origin/claude/monitor-webapp` へ直接 push) は
  そこでは走らない。
- よって 08:00 の時点で **origin の `data/` は必ず前日のまま** = 定義上 stale。
- 08:00:08 に self-heal が走り、10 秒足らずで当日分が published になる。

### 1.2 実測 (`logs/morning_brief/launch_*.log`)

2026-08-22 の 1 分間:

```
08:00:06  [dashboard_freshness] status=stale generated=2026-08-22 served=2026-08-21
08:00:07  [ntfy] 送信 ok=True                      <- ここでオオカミ少年
08:00:08  [dashboard_selfheal] -AutoLatest 開始
08:00:17  [publish_data] verify OK: served date=20260822, exact bundle blobs match
08:00:17  [dashboard_selfheal] exit=0
```

08-09〜08-22 の 14 日すべてが同じ形 (`status=stale` → 通知 → `verify OK` / `exit=0`)。
**14/14 が誤報**。

唯一の例外が **08-20**: self-heal が

```
ERROR dashboard bundle rejected: execution inputs are not bound to the current
      signals run: exit_orders=unverified, paper_orders=unverified
[dashboard_selfheal] exit=1
```

で落ち、その日は**本当に publish が取りこぼされていた**。この日の通知だけが正しかった。

### 1.3 修正 — 通知権を self-heal の後ろへ移す (無効化ではない)

`morning_brief.ps1` を **2 パス構成**にした:

```
1. detect    check_dashboard_freshness.py --notify --check-served --defer-stale-notify
             -> 検出・ログ・exit code は従来どおり。stale の ntfy だけ送らない。
2. self-heal publish_data_to_vercel.ps1 -AutoLatest
3. re-check  check_dashboard_freshness.py --notify --post-heal
             -> ここで **まだ** stale なら通知する。fresh なら誰も鳴らない。
```

`scripts/check_dashboard_freshness.py` に 2 つのフラグを追加:

- `--defer-stale-notify` — この pass では stale の ntfy を送らない (診断出力と exit=2 は残す)。
- `--post-heal` — 通知本文を「self-heal は実行済みだが解消しなかった」に変える。
  タイトルも `Dashboard STALE (self-heal 後も): ...` になるので、通知履歴で新旧を
  取り違えない。

設計上の注意 (壊さないこと):

- **`--check-served` (deploy watchdog) は 1 パス目に残す。** これは *過去に* publish 済みの
  run が本番 HTML に出ているかを見る検査で、self-heal とは無関係。self-heal の後に
  動かすと、見る manifest が push 直後の age 0 分になり **必ず grace 内 = 検査が空振り**に
  なる。deploy_missing の通知は `--defer-stale-notify` でも抑止されない。
- **self-heal が存在しない構成では defer しない。** `publish_data_to_vercel.ps1` が
  無ければ 1 パス目が従来どおり即通知する (アラートを消したわけではない)。
- 先送りは「pending キューに積む」ではない。積むと翌朝に遅れて鳴ってしまう。

### 1.4 修正後に鳴る条件

| 状況 | 修正前 | 修正後 |
|---|---|---|
| 通常の朝 (self-heal が 10 秒で解消) | **通知** (誤報) | 静か |
| self-heal が失敗 (08-20 の bundle preflight FAIL) | 通知 | **通知** (文面で self-heal 済みと分かる) |
| Vercel build 不達 (deploy_missing) | 通知 | **通知** (1 パス目のまま) |
| ntfy 送信失敗 | pending へ退避し次回再送 | 変わらず |

---

## 2. ALERT 2 — `[quant] WARN open_run: <date>: 実 run だが entry_submitted=0`

### 2.1 3 つの候補原因の切り分け

| 候補 | 判定 | 根拠 |
|---|---|---|
| (a) `289701d` の dir 選択修正が走行ツリーに未デプロイ | **該当しない** | §0 参照。`C:\tmp\qts-safety-nets` は `289701d` で clean、`self_monitor_20260822.json` は canonical dir を選んでいる |
| (b) cap 飽和で全シグナルが skip され entry_submitted=0 は**正当** | **これが真因** | 下記 §2.2 |
| (c) enum status の表示バグが `entry_submitted` に波及 | **該当しない** | 下記 §2.3 |

### 2.2 (b) — 真因: book が満杯で全件 pre-submit skip

`logs/open_run_20260821/entry.log` (2026-08-21 の実データ):

```
完了: 入力 signals=11 生成=11 送信=0 失敗=0 skip=11 (--confirm=True)
[skip] pre-submit で 11 件スキップ (内訳: {'already_held': 4, 'standing_cap': 7}):
    - system1 PFSA buy: already_held:buy_qty=41
    - system2 HTFL sell: standing_cap:system2_held=10+batch=0>=cap=10
    - system2 PSKY sell: already_held:sell_qty=-261
    ... (system2 の残り全部が standing_cap または already_held)
```

`completion_recon.json`: `entry_submitted=0 / entry_skipped=11 / entry_failed=0 /
entry_status="no_orders_submitted"`、`final_positions={total:49, long:39, short:10}`。

もう一つの発火日 2026-08-11 も**完全に同じ形**: 生成 17 件がすべて
`already_held=13 + standing_cap=4` で skip、`entry_failed=0`。

つまり **1 件も失敗していない**。per-system cap (=10, `risk.max_positions`) に system2 が
到達し、残りは既保有だっただけ。これは設計どおりの正常終了であって run の失敗ではない。
**WARN ルールの側が誤っていた。**

### 2.3 (c) — enum status の混入は無い

`entry_submitted` は `paper_orders.json` の `meta.submitted`、その実体は
`scripts/paper_trading_submit.py` で submit 成功のたびに `submitted += 1` する
**整数カウンタ**であり、`OrderStatus` に一切触れていない。
2026-08-20 に直した `str(OrderStatus.FILLED)` → `'OrderStatus.FILLED'` の artifact は
**fill 判定 (`entry_filled`) 専用**で、`entry_submitted` には波及しない。

### 2.4 修正 — skip 理由で「枠が無い」と「壊れている」を分ける

`scripts/self_monitor_check.py` に `classify_zero_entry()` を追加し、
`entry_submitted<=0` を無条件 WARN するのをやめた。判定は
`logs/open_run_<date>/paper_orders.json` の **per-order `skip_reason`** で行う
(取れない場合だけ同じ日付の `results_csv/paper_orders_<YYYYMMDD>.json` へフォールバック。
別日の残骸は使わない — 土曜には翌営業日ぶんが残っていることがあるため)。

capacity 由来 = 良性とみなす skip 種別:

| kind | 意味 |
|---|---|
| `standing_cap` | per-system / portfolio の建玉上限に到達 |
| `already_held` | 同一 symbol/side を既に保有 |
| `already_open` | 同一 `client_order_id` の注文が既に生存 (重複抑止) |
| `qty_reserved` | 別注文 (保護 / exit) が qty を予約済み |

**WARN のままにするもの** (本物の異常。1 つも黙らせていない):

- `entry_failed > 0` — 送信が失敗している。
- `entry_status == "no_orders_generated"` — signals はあるのに order が 0 件 (schema drift)。
- `entry_status == "all_submit_failed"`。
- `skip_reason` の無い order がある — 送信も skip もされず消えた = silent drop。
- capacity 以外の skip (`untradable` など) が混じっている。
- **`paper_orders` 成果物が読めない — fail-closed。** 「証明できない」を「正常」に
  倒すと、本物の沈黙を silent success に変えてしまう。

### 2.5 過去の全 open_run に新ルールを当て直した結果 (read-only 検証)

```
entry_submitted=0 だった run: 6 件 (旧ルールでは 6/6 が WARN)
  07-31  OK   全 16 件が枠不足/既保有で skip (already_held=9,  standing_cap=7)
  08-06  OK   全 18 件が枠不足/既保有で skip (already_held=7,  standing_cap=11)
  08-11  OK   全 17 件が枠不足/既保有で skip (already_held=13, standing_cap=4)
  08-21  OK   全 11 件が枠不足/既保有で skip (already_held=4,  standing_cap=7)
  07-27  WARN paper_orders 成果物が無く skip 理由を確認できない   <- fail-closed で残る
  08-03  (mode=dry_run。従来から info 扱いでこの判定に到達しない)
新ルールで残る WARN: 1 / 6
```

修正後の実データ出力:

```
[OK] open_run: 2026-08-21: entry_submitted=0 は正常
     — 全 11 件が枠不足/既保有で skip (already_held=4, standing_cap=7)
```

`data` には `skip_kinds` / `orders_without_skip_reason` / `input_signals` /
`zero_entry_verdict` を残すので、OK でも中身は後から追える。

---

## 3. 直していない本丸 — cap 飽和そのもの (別途追跡)

**ALERT 2 の裏にある構造的な問題は本ドキュメントでは直していない。**

- standing cap は **生の建玉本数** (per-system 10 / portfolio 70) で、リスク量でも
  資本配分でもない。flatten で空いた枠が数日で埋まると `allow_long` が 1 前後まで
  落ち、system2 のような回転の速い系統が枠を占め続ける (sys5 starvation)。
- 2026-08-21 は 49 建玉 (long 39 / short 10)、system2 が 10/10 で上限に張り付いていた。
- これは**アロケータ / cap 設計の問題**であって、監視の問題ではない。ここでやったのは
  「その状態を run の失敗として誤報しない」ことだけ。

cap の作り直しは別タスクとして追跡する。ここを直したことで cap 飽和が**見えなくなる
わけではない**: `skip_kinds` が JSON に残り、`entry_submitted=0 は正常 — 全 N 件が
枠不足/既保有で skip` という文言で毎回可視化される。

---

## 4. テスト

| ファイル | ブランチ | 増減 | 内容 |
|---|---|---|---|
| `tests/test_dashboard_freshness_selfheal_order.py` (新規) | `claude/monitor-webapp` | +8 | defer で黙る / self-heal で解消したら無通知 / 解消しなければ必ず通知 / 文面が新旧で区別できる / defer 無しは即通知 / deploy_missing は defer 対象外 / pending へ積まない / fresh は両パスとも静か |
| `tests/system/test_check_dashboard_freshness.py` (拡張) | `claude/monitor-webapp` | +2 | `--defer-stale-notify` で `_notify_stale` が呼ばれない / `--post-heal` が通知側まで届く。既存の `test_notify_called_with_flag` のスタブを `**kwargs` 対応にした (keyword-only 引数が増えたため) |
| `tests/test_self_monitor_check.py` (拡張) | `claude/monitor-safety-nets-20260712` | +10 (24→34) | cap 飽和は OK / 4 種の capacity skip すべて OK / `untradable` は WARN / skip 理由なしは WARN / `entry_failed>0` は WARN / 生成ゼロは WARN / 同日 `results_csv` フォールバック / 別日の残骸は使わない / 成果物欠落は fail-closed / flat book は異常でない |

既存の `test_open_run_zero_entries_is_warn` (成果物が無い entry 0) は **WARN のまま**
通ることを確認済み — fail-closed の回帰テストとして残している。

### フルスイート回帰 (失敗 ID 集合を `comm` で突合)

| ツリー / ブランチ | baseline | 修正後 | 新規失敗 | 解消 |
|---|---|---|---|---|
| `C:\Repos\quant_trading_system_0510to0906` (`claude/monitor-webapp`) | 226 failed / 2380 passed | 226 failed / **2390** passed | **0** | 0 |
| `C:	mp\qts-safety-nets` (`claude/monitor-safety-nets-20260712`) | 240 failed / 2016 passed | 240 failed / **2026** passed | **0** | 0 |

いずれも失敗 ID 集合は完全一致。既存の 3 件の collection error
(`test_cache_manager_final` / `test_core_system4_enhanced` / `test_high_impact_modules`) と
`test_app_imports.py` (import 時に `sys.exit(1)`) は本作業以前からのもので、
両ツリー共通・`--ignore` して計測した。

### 実データでの動作確認 (read-only)

```
# self_monitor (2026-08-21 の実 run に対して)
[OK] open_run: 2026-08-21: entry_submitted=0 は正常
     — 全 11 件が枠不足/既保有で skip (already_held=4, standing_cap=7)
=> worst=OK          (修正前は worst=warn -> モーニングブリーフに WARN 行が出ていた)

# freshness の 2 パス (Monday-morning 形状の sandbox で再現)
PASS 1  status=stale ... -> 「通知は self-heal 後の再チェックへ委譲」  exit=2
PASS 2  self-heal
PASS 3  status=fresh                                                  exit=0  -> 通知なし

# self-heal が落ちる 08-20 型
PASS 1  exit=2 (委譲) / PASS 2  exit=1 / PASS 3  status=stale exit=2 -> ここで通知
```

---

## 5. 参照

- 実測ログ: `logs/morning_brief/launch_20260809..20260822_0800*.log`
- 実測 run: `logs/open_run_20260811/`, `logs/open_run_20260821/` (`entry.log`,
  `completion_recon.json`, `paper_orders.json`)
- self_monitor 出力: `logs/self_monitor_20260822.json`
- cap 実装: `common/alpaca_trading.py` `evaluate_standing_cap` / `_resolve_standing_caps`
- 先行修正: `289701d` (open_run sidecar dir の取り違え、2026-08-21)
