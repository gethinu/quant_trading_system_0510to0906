# ランナー廃止と main 一本化 (計画 2026-08-22 / 実施 2026-08-25)

**目的**: 毎晩の自動発注とダッシュボード/観測を **すべて `main` から動かす**。
`claude/open-auto-run` のような「稼働しているのに main に届いていないブランチ」を
無くし、`docs/MAIN_RECONCILE_20260822.md` が扱った「取り込み漏れ」を構造的に再発
させない。

- **paper only** (Alpaca paper)。live 発注・実マネー・MT5 端末は一切扱っていない。
- 起点 `origin/main` = `9545d45`
- 着点 `origin/main` = `a2bb9fd`
- **ロールバック用タグ (push 済)**: `runner-retired-open-auto-run-20260825`
  (別名 `runner-retired-open-auto-run-20260822`。どちらも `738834b` を指す)
- **`claude/open-auto-run` は削除していない**。スケジューラが参照しなくなっただけ。

---

## 0. 一言でいうと

「ランナーを廃止した」とは、**毎晩 22:35 に動くコードの置き場所を、専用ブランチから
`main` に変えた**という意味。トレードのやり方は何も変えていない。

これまでは `C:\tmp\qts-main-run` という作業コピーが `claude/open-auto-run` という
専用ブランチに乗っていて、そこに溜めた修正を誰かが手で `main` へ移す必要があった。
移し忘れると「動いているコードが main に無い」状態になる (実際 1 か月で 26 commit
溜まった)。今回その作業コピーを `main` そのものに切り替えたので、**発注側と
ダッシュ側が同じ 1 本の幹を読む**ようになり、移し忘れが起こりようがなくなった。

戻し方は 1 行 (§4)。ブランチは消していないので、いつでも元に戻せる。

---

## 1. PHASE 1 — 保留されていた観測系 13 commit を main へ

`docs/MAIN_RECONCILE_20260822.md` §5.1 が「機械的に解けない」として保留した 13 本。
`claude/open-auto-run` と `claude/monitor-webapp` が
`build_execution_recon.py` / `paper_exit_check.py` / `publish_signals.py` /
`ntfy.py` を **並行に書き換えていた**部分。

cherry-pick ではなく **ブランチ merge 2 本**で入れた (系譜を残すため)。

| commit | 内容 |
|---|---|
| `e454fad` | Merge `claude/monitor-webapp` (12 commit + cry-wolf 2 本 + データ) — 52 file / 76 hunk |
| `a21a444` | Merge `agent/exit-overdue-enforcement` (`2aa8ab8` `1a4092e`) — 9 file / 21 hunk |
| `a2bb9fd` | cherry-pick `8a3caa6` (self-monitor の cry-wolf 修正。PHASE 2 で SelfMonitor を main から動かすため) |

`git cherry origin/main <branch>` は **`claude/open-auto-run` / `claude/monitor-webapp` /
`agent/exit-overdue-enforcement` の 3 本すべてで 0**。main は 3 本の厳密な superset。

### 1.1 衝突解決の方針 (file 別)

大原則: **発注系は main (open-auto-run 側) が新しい / ダッシュ系は monitor-webapp が
新しい**。両者は多くの file で **直交する**変更をしていたので、基本は union。

| file | hunk | 解決 | 理由 |
|---|---|---|---|
| `scripts/build_execution_recon.py` | 17 | **union** | 完全に直交していた。main の exit 分類 (`fired`/`rejected`/`suppressed`/`armed` + `_exit_is_submitted` の `accepted` 判定、2026-08-19) を残し、monitor-webapp の funnel 配線 `patch_pipeline_funnel` (260 行) / `date_mismatch` guard / `source`・`source_observed_at` stamp を足した。main の分類は「broker 拒否 12 件が armed (=保護が張れた) として表示されていた」実害の修正なので落とせない |
| `scripts/paper_exit_check.py` | 8 | main | rename 対応 entry metadata / 全件ページング / 保護カバレッジ集計 / role sidecar / OCO rearm / `broker_unreachable` — すべて main が上位 |
| `common/alpaca_trading.py` | 6 | main | spec 指値 (`limit_price`)・`untradable` skip・equity 連動サイジング・端株 qty。発注経路は main が上位 |
| `scripts/publish_signals.py` | 9 | main | `_apply_legacy_status` (legacy scalar と構造化 field を必ず一致させ、未試行なら key ごと落とす)。`ed85e5a` の新しい規約 |
| `common/signal_export.py` | 2 | main | 同上。`publish_status: "not_attempted"` を書くと旧ダッシュが未試行を緑で描く |
| `common/publishers/ntfy.py` | 6 | main | 実装差は「空の latin-1 有効ヘッダーをそのまま通すか `"notification"` にするか」だけ。挙動は同等で、main は 2026-07-13 incident の記録を持つ |
| `common/publishers/execution_summary.py` | 2 | main | 両側とも同一コード。monitor-webapp 側を採ると同じブロックが **二重に**出る (整形差による衝突) |
| `scripts/check_dashboard_freshness.py` | 2 | **monitor-webapp** | `--served-ref` / `--served-basis` / `--no-fetch` / `--defer-stale-notify` / `--post-heal` は追加のみ (`219ba77` `fae206c`) |
| `scripts/morning_brief.ps1` | 1 | **monitor-webapp** | cry-wolf 修正 (`fae206c`): stale 通知を self-heal の後ろへ |
| `scripts/daily_polygon_monitor.py` | 1 | **monitor-webapp** | funnel 配線 (`6418363`) の追加のみ |
| `scripts/publish_execution_summary.py` | 4 | 3 = monitor-webapp / 1 = main | funnel 配線は追加なので採用。`_resolve_recon` だけ main。§1.2 参照 |
| dashboard TS 4 file | 5 | main + union | `format.ts` は血統マーカー (main)、`status.ts` は `isRegistryDelisted` import (monitor-webapp、実際に使われている)、`PipelineSection.tsx` は **両方** (lineage 警告バナー + 血統凡例)、`AlpacaSection.tsx` は文言 (monitor-webapp) + 凡例 (main) を手で合成 |
| add/add 26 file (`common/validation/*` 等) | — | main | AST 比較で **formatting / import 順のみの差**と確認。main 側は lint 済 (`75a9f96`) |
| `docs/BACKTEST_LIMIT_FILL_FIX_20260820.md` | — | monitor-webapp | main 側の注記「これらの参照は main には無い」は **統合前から既に嘘**だった (`common/validation/` は `894d363` で land 済)。注記の無い側を採用 |

#### 1.2 `_resolve_recon` — 唯一「両方もっともらしい」判断

同日再実行で古い recon を使い回すかどうか。

- monitor-webapp: `today_signals` が読めない時は **既存 recon を再利用**する。
- main: 再利用は run_id 完全一致の時だけ。読めなければ **再構築**する。

**main を採った**。signals が読めない異常時に、monitor-webapp 版は朝の recon の数字を
夜の実績であるかのように黙って表示してしまう (fail-silent)。main 版は sig 0 という
見てすぐ分かる異常値になる (fail-loud)。この repo は「stale を真値と誤認する」事故を
繰り返しているので fail-loud を選んだ。

### 1.3 auto-merge が **黙って壊した** 2 件 (衝突として出なかった)

テキスト衝突にならなかったので git は何も言わないが、壊れていた:

1. `scripts/check_dashboard_freshness.py` — `newest_bundle` / `_fetch_served_html` /
   `_minutes_since` / `check_served_run` の **4 関数が二重定義**され、Python は後勝ち
   なので **main 側の古い `check_served_run` が有効**になっていた。呼び出し側は
   `repo_root=` / `served_ref=` を渡す新 signature なので、**朝の鮮度チェックが
   TypeError で落ちる**ところだった。重複ブロック (98 行) を削除。
2. `tests/test_registry_publisher_exception_isolation.py` — `dumped = json.dumps(...)`
   の代入行と `import json` が落ち、`F821 Undefined name`。復元。

再発防止として、**両ブランチが触った全 py file について「同名 top-level def の重複」
を機械スキャン**した (merge 1: 70 file、merge 2: 7 file、いずれも 0 件)。
TS 側も同様に走査し、`StatusSummary.tsx` で `executionLabel` が **import と局所定義の
二重**になっていたのを検出・削除した。

### 1.4 セマンティクスが真に食い違った 1 件 — `measured` の意味

`tests/test_pipeline_funnel_fallback.py` (main 固有、PR#140) が
「funnel 由来の count は `measured=False`」を契約として固定していたのに対し、
`6418363` は「funnel は実測なので `measured=True`、出所は `source` field で示す」に
変えていた。統合直後、この 2 テストが落ちた。

**ダッシュボードの実装が決着をつけた**。`PipelineSection.tsx` は
`count あり + measured=false` を **「未検証 = producer 契約の不整合」**として描き、
`loadDashboardBundle.ts` は `funnel_measured < 34` の bundle を不合格にする。
つまり main の旧契約が作る状態は、UI 自身が「壊れている」と宣言する組み合わせだった。

→ `6418363` の意味論を採用し、テストを更新した。**緩めたのではなく**、
`source == "today_signals.funnel"` の固定と、spy_only (sys7) の Tgt が共有 universe 値を
採らず `unmeasured_reason` つきで未計測を維持することの固定を **足した**。

### 1.5 `2aa8ab8` — 採らなかった部分と理由

`2aa8ab8` は「exit 案を作っただけの状態を発注済と呼ばない」ための変更。
観測部分 (`time_exit_due` / `time_exit_unsubmitted` / `execution_health`) と
opt-in フラグ `--fail-on-unsubmitted-time-exit` は採った。採らなかったのは 2 点:

1. **exit code 3 への相乗り**。main では `3 = broker_unreachable`
   (「exit 0 件は flat book ではなく取得失敗」) で、専用テストと `daily_pipeline.ps1` の
   consumer がある。同じ 3 を返すと呼び出し側が区別できないため、ゲートは
   **別コード 4** にした (`paper_exit_check.py` の 0/1/2/3 は既使用、4 は空き)。
2. **`daily_pipeline.ps1` へのゲート配線**。`2aa8ab8` は **dry-run 側の分岐にも**
   `--fail-on-unsubmitted-time-exit` を渡していた。daily_pipeline の exit_check は
   `role=proposal` の pass で、実発注は夜の `open_auto_run` が行う。つまり
   「期限到来 time exit が未送信」は **この pass では正常状態**であり、配線すると
   **毎朝必ず失敗が立つ** (2026-08-22 に潰したばかりの cry-wolf と同型)。配線しない。

同様に dashboard / `export_alpaca_snapshot.py` の執行状態表示は main (`6ac2e82` +
exit artifact の role 分離) が `2aa8ab8` の上位版。proposal 由来は
`pending_execution` / `awaiting_execution` (失敗ではない)、execution 由来の未送信だけ
`blocked_unsubmitted_time_exit`。`2aa8ab8` 版は朝の提案を全部赤にする。

ゲート自体は残っているので、有効にしたければ `--fail-on-unsubmitted-time-exit` を
明示的に渡せばよい (exit=4)。

### 1.6 判断を保留した hunk

**無し**。全 97 hunk (merge1 76 + merge2 21) を根拠つきで解決した。
唯一 §1.5-1 の「新しい exit code 4 を作る」だけは、どちらのブランチにも無い選択なので
**こちらの判断**である旨を明記しておく。元に戻すなら `paper_exit_check.py` の
`return 4` と `daily_pipeline.ps1` の `-eq 4` 行を消せばよい (ゲートは既定 OFF なので
現状では到達しない)。

---

## 2. PHASE 2 — スケジューラを main へ向ける

### 2.1 実際に何が動いていたか (調査結果)

`Get-ScheduledTask` で全数確認したところ、**5 つの別ワークツリー / 別ブランチ**から
動いていた。「ランナー 1 本」ではなかった。

| タスク | 時刻 (JST) | 変更前のコード置き場 | ブランチ |
|---|---|---|---|
| `QuantTrading_OpenAutoRun` | 22:35 / 23:35 | `C:\tmp\qts-main-run` | `claude/open-auto-run` |
| ↑ の `-PrimaryRoot` (= `.env` と publish script の出所) | | `C:\tmp\qts-release-mw` | detached `aeed0f6` (08-18) |
| `QuantTrading_ExitVerify` | 07:20 | `C:\tmp\qts-exitverify-main` | `claude/fix-close-fill-accounting-20260820` |
| `QuantTrading_SelfMonitor` | 07:15 | `C:\tmp\qts-safety-nets` | `claude/monitor-safety-nets-20260712` |
| `QuantTrading_MorningBrief` | 08:00 | `C:\Repos\quant_trading_system_0510to0906` | `claude/monitor-webapp` |
| `QuantTrading_PolygonDailyMonitor` | 06:00 | `C:\tmp\qts-daily-main` | `claude/daily-main-follow` |

**副次的な発見**: 毎晩の publish は `-PrimaryRoot` 経由で
`C:\tmp\qts-release-mw` の `publish_data_to_vercel.ps1` (08-18 時点、main より **38 行
古い**) を実行していた。これも取り込み漏れの一種。

### 2.2 変更内容

**A. ランナー本体 — ワークツリーのブランチを切り替え (タスク定義の path は不変)**

```
C:\tmp\qts-main-run :  claude/open-auto-run (738834b)  ->  main (a2bb9fd)
```

`.env`・`data_cache`/`logs`/`results_csv` の junction はすべて untracked / 実体なので
checkout をまたいでそのまま残る (確認済)。

**B. スケジューラのタスク定義 3 件**

| タスク | 変更前 | 変更後 |
|---|---|---|
| `QuantTrading_OpenAutoRun` | `-PrimaryRoot "C:\tmp\qts-release-mw"` | `-PrimaryRoot "C:\tmp\qts-main-run"` |
| `QuantTrading_ExitVerify` | `-File "C:\tmp\qts-exitverify-main\scripts\exit_verify.ps1"`, cwd 同 | `-File "C:\tmp\qts-main-run\scripts\exit_verify.ps1"`, cwd `C:\tmp\qts-main-run` |
| `QuantTrading_SelfMonitor` | `-File "C:\tmp\qts-safety-nets\scripts\self_monitor_check.ps1"`, cwd 同 | `-File "C:\tmp\qts-main-run\scripts\self_monitor_check.ps1"`, cwd `C:\tmp\qts-main-run` |

トリガー・実行ユーザー・その他の設定は変更していない (`Set-ScheduledTask -Action` のみ)。
ExitVerify / SelfMonitor の `-PrimaryRoot` は `C:\Repos\...` のまま (データの出所)。

> **`-PrimaryRoot` を動かしても環境変数は 1 バイトも変わらないことを実測で確認した。**
> 変更前は PowerShell が `qts-release-mw\.env` を読み、その後 python の `load_dotenv` が
> `qts-main-run\.env` から不足分だけ埋めていた (`override=False`)。変更後は両方とも
> `qts-main-run\.env`。**実効 45 key の差分 0**。とくに
> **`PROTECT_USE_OCO` は前も後も未設定 (= 既定 OFF)**、`CAP_USE_REAL_EQUITY=1` は
> 2026-08-20 に承認済のまま。**戦略フラグは一切触っていない。**

**C. 変更しなかったもの (理由つき)**

- `QuantTrading_PolygonDailyMonitor` — `daily_main_follow.ps1` が毎回
  `git fetch origin` + `git merge --no-edit origin/main` してから `daily_pipeline.ps1` を
  走らせる。**設計上すでに main 追随**なので触る必要が無い。
- `QuantTrading_MorningBrief` — これだけ `-PrimaryRoot` から
  `publish_data_to_vercel.ps1` と `check_dashboard_freshness.py` を解決する
  (`morning_brief.ps1` の実装)。`-PrimaryRoot` は `.env`・`data_cache`・`results_csv`
  の置き場でもあるので `C:\Repos\...` から動かせない。**残タスク** (§5)。
  ただし現時点で `morning_brief.ps1` / `morning_brief.py` /
  `check_dashboard_freshness.py` / `publish_data_to_vercel.ps1` の 4 file は
  `claude/monitor-webapp` と `origin/main` で **byte 一致**なので、今日の実害は 0。
- `QuantTrading_OneShotFlatten_20260820` — 2026-08-20 の一回限りタスク (発火済)。
  参照先は `qts-main-run` だがスクリプト自体が untracked。掃除対象 (§5)。
- **`claude/monitor-webapp` ブランチは残す**。これは開発ブランチであると同時に
  **Vercel が読むダッシュボードのデータ配信ブランチ**。publish は `commit-tree` で
  `origin/claude/monitor-webapp` の tip に直接載せる (local HEAD もワークツリーも
  動かさない) ので、実行側がどのブランチに居ても機能する。

---

## 3. 検証

### 3.1 回帰テスト — 新規失敗 0

**同一ワークツリー** `C:\tmp\qts-main-run` (本番 `.env` あり / `data_cache`・`logs`・
`results_csv` は本番への junction) で before/after を取り、**失敗 ID 集合**を突合した。

```
python -m pytest tests -o addopts='' -q -p no:randomly -p no:cacheprovider \
  --ignore=tests/test_app_imports.py \
  --ignore=tests/test_today_modules_lightweight.py \
  --continue-on-collection-errors -rfE
```

| | failed | errors | 合計 | passed |
|---|---|---|---|---|
| baseline (`9545d45` = 統合前の origin/main) | 224 | 13 | **237** | 2835 |
| after (`a21a444`) | 224 | 13 | **237** | **2952** |

- **新規失敗 0 / 解消 0。失敗 ID 集合は完全一致** (`comm` で突合)。
- passed が +117 なのは、統合で入った新規テストがすべて通っているため。
- `test_app_imports.py` (import 時 `sys.exit(1)` で collection を落とす) と
  `test_today_modules_lightweight.py` (実ネットワークで無限待ち) は既知の壊れた file で
  before/after 双方から同じく除外 (`docs/MAIN_RECONCILE_20260822.md` §6 と同条件)。
- `a2bb9fd` (self-monitor cherry-pick) は `tests/test_self_monitor_check.py` **34 件**が
  通ることを個別に確認。
- `ruff check .` は **All checks passed** (repo 全体)。

### 3.2 exit funnel / recon / publish の実動作確認

コンパイルだけでなく **実データで動かした**。本番の `results_csv` を汚さないために、
`origin/main` の使い捨てワークツリー `C:\tmp\qts-verify-main` を作り、本番 `.env` と
`data_cache` (junction) を与えたうえで **`results_csv`/`logs` だけスクラッチ**にし、
実際の 08-24 / 08-25 の artifact を複製して実行した。

> `open_auto_run.py` は結果 dir を `ROOT/results_csv` に固定しており、
> `--dry-run` でも recon を書き換える (= その日の publish を fail-closed にする既知の罠)。
> このため本番ワークツリーでの dry-run は **意図的に避けた**。

**(a) ランナー全段 (main のコード)**

```
python scripts/open_auto_run.py --date 2026-08-24 --dry-run --allow-closed \
       --skip-signals --no-publish --force            ->  exit 0
```
- exit 段: 保護エンジン (端株の常駐注文不可、stop FLOOR、qty 全量予約による抑止) が動作
- entry 段: `skip:limit_without_price:system2_missing` × 10 —
  **`241275a` (指値なし limit を成行へ落とさない) が main 上で機能**していることを実測
- notify 段: recon 再構築 (lineage 不一致を検出して stale recon を破棄) → ntfy dry-run
  ```
  X-Title: 📊 08-24 exec sig12 entry0 exit0
  Tgt 5239 → sig 12 → gen 12 → entry 0 → fill 0
  exit 0 fired (close 0 / protect 0) · 14 armed
  ⚠ drop: limit_without_price 10
  ```
  fired/armed 分離 (main) と `drop_breakdown` への `limit_without_price` 露出が
  両方出ている = §1.1 の union が実際に機能している。

**(b) funnel 配線 (`6418363`) — 統合で新しく入ったコード**

pipeline の funnel phase 35 件を意図的に `count=None, measured=False` に潰してから
`publish_execution_summary.py --dry-run` を実行:

```
pipeline Exit 配線: 7 system (ntfy と同一 recon)
pipeline funnel 配線: 35 phase (today_signals と同一 source)
->  measured 0/35  ->  35/35   sources={'today_signals.funnel': 35}
```

**(c) dashboard bundle (`26385b0`) / 鮮度 (`219ba77`)**

```
materialize_dashboard_bundle(require_exit=True)
  -> date=2026-08-24 run=20260824_060841_9774b5
     measurement={"funnel_measured":35,"funnel_total":35,"exit_measured":7}  warnings=[]

check_dashboard_freshness.py --check-served
  -> status=fresh generated=2026-08-25 served=2026-08-25 basis=ref:origin/claude/monitor-webapp
  -> [dashboard_deploy] status=served run=20260825_060813_b0b084
```

**(d) publish 本体 (push なし)**

```
publish_data_to_vercel.ps1 -Date 2026-08-24 -NoPush -PurgeSource $false   ->  exit 0
  dashboard bundle preflight: funnel=35/35 Exit=7/7
  commit base = bc94e4c (claude/monitor-webapp tip; current HEAD ではない)
  commit-tree 作成: ad03cd3 (parent=bc94e4c, branch=claude/monitor-webapp)
  NoPush 指定: origin push を省略
```
→ main のワークツリーから実行しても、publish は正しく
`origin/claude/monitor-webapp` の tip に載る (local HEAD を動かさない)。

### 3.3 付け替え後のタスクを実際に起動

スケジューラが打つのと同じコマンドラインで実行 (通知だけ抑止):

```
C:\tmp\qts-main-run\scripts\exit_verify.ps1        -PrimaryRoot C:\Repos\... -NoNotify -> exit 0
   [source] exit_orders_20260821.json (role=execution)  [OK] 乖離なし
C:\tmp\qts-main-run\scripts\self_monitor_check.ps1 -PrimaryRoot C:\Repos\... -NoNotify -> exit 2
   daily OK / pipeline OK / data_fresh OK / signals OK(13) / publish OK
   open_run WARN: 2026-08-24 ABORT (clock_unavailable)  => worst=WARN
```
SelfMonitor の exit=2 は **WARN の正常な終了コード**。WARN の中身は 08-24 の run が
Alpaca clock 取得失敗で abort したという **本作業と無関係な既存の観測**。

---

## 4. ロールバック

### 4.1 ランナーだけ元に戻す (最短)

```powershell
git -C C:\tmp\qts-main-run checkout -B claude/open-auto-run runner-retired-open-auto-run-20260825
schtasks /Change /TN QuantTrading_OpenAutoRun /TR "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File \"C:\tmp\qts-main-run\scripts\open_auto_run.ps1\" -PrimaryRoot \"C:\tmp\qts-release-mw\""
```
タグ `runner-retired-open-auto-run-20260825` (= `738834b`) は origin に push 済なので、
ブランチが動いていても実行時点の状態を正確に復元できる。

### 4.2 スケジューラ 3 件を元に戻す

変更前の定義は
`scratchpad/scheduler_before.json` に退避してあるが、内容は本ファイル §2.1 の表がすべて。

```powershell
$P='C:\Repos\quant_trading_system_0510to0906'
Set-ScheduledTask -TaskName QuantTrading_ExitVerify -Action (New-ScheduledTaskAction -Execute powershell.exe `
  -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"C:\tmp\qts-exitverify-main\scripts\exit_verify.ps1`" -PrimaryRoot `"$P`"" `
  -WorkingDirectory 'C:\tmp\qts-exitverify-main')
Set-ScheduledTask -TaskName QuantTrading_SelfMonitor -Action (New-ScheduledTaskAction -Execute powershell.exe `
  -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"C:\tmp\qts-safety-nets\scripts\self_monitor_check.ps1`" -PrimaryRoot `"$P`"" `
  -WorkingDirectory 'C:\tmp\qts-safety-nets')
```

### 4.3 コード統合だけ戻す

`origin/main` を `9545d45` に戻す (= `e454fad` `a21a444` `a2bb9fd` を落とす)。
**force push が要る**ので、実行前に必ず判断すること。個別に戻すなら
`git revert -m 1 e454fad` / `git revert -m 1 a21a444` / `git revert a2bb9fd`。

---

## 5. 残タスク

1. **`main` は自動更新されない**。`C:\tmp\qts-main-run` はブランチ `main` に居るが、
   `origin/main` が進んでも自分では追いつかない。運用に組み込むなら
   `git -C C:\tmp\qts-main-run pull --ff-only` を 22:35 の前に回す必要がある。
   `open_auto_run.ps1` に自動 pull を仕込むことは **意図的にしていない**
   (レビュー前のコードが自動で本番実行されるのは別種のリスク)。
2. **`QuantTrading_MorningBrief` だけ `claude/monitor-webapp` のワークツリーから動く**
   (§2.2-C)。1 ブランチは 1 ワークツリーにしか checkout できないため、`main` を
   `C:\Repos\...` 側に置くなら `qts-main-run` を detached にする等の入れ替えが要る。
   現時点で該当 4 file は byte 一致なので実害 0。
3. `QuantTrading_OneShotFlatten_20260820` (発火済の一回限りタスク) の削除。
4. `docs/MAIN_RECONCILE_20260822.md` §7 の残り: `test_today_modules_lightweight.py` の
   ネットワーク依存、`test_app_imports.py` の `sys.exit(1)`、baseline 237 件の内訳整理。
5. §1.5-1 で新設した exit code 4 は、必要なら別コード体系に整理してよい
   (現状ゲートは既定 OFF で未到達)。
