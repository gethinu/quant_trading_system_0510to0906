# main 取り込み漏れの棚卸しと解消 (2026-08-22)

**対象**: `main` が **実際に動いているコード** と乖離していないかの全数突合と、
その解消。あわせて 2 件の修正 (保有日数の単位 / 指値なし limit の扱い) を land した。

- 起点 `origin/main` = `2d09839`
- 着点 `main` = `75a9f96`
- **live 発注は一切していない (paper のみ / MT5 端末不使用)**。main は何も実行して
  いないので、この取り込みによる **live 挙動の変化はゼロ**。

---

## 0. 一言でいうと

`main` は「正」ではなくなっていた。毎晩の自動発注が実際に読んでいるのは
`claude/open-auto-run` (`C:\tmp\qts-main-run`) で、そこには 1 か月ぶんの修正が
26 本たまっていたのに `main` には届いていなかった。ダッシュボード側の
`claude/monitor-webapp` も同様に並走していた。

今回 **実行ブランチぶんは全部 (残 0)**、ダッシュ側は **分離できる 14 本**を
`main` へ入れた。残りは「観測パイプラインを 2 本のブランチが並行に書き換えた」
部分で、機械的には解けないので**別途 1 パスとして切り出す** (§5)。

---

## 1. 突合の方法

各 `claude/*` `agent/*` ブランチについて:

```
git log main..<branch> --oneline     # 到達可能性
git cherry main <branch>             # patch-id 等価まで見た「本当に無いもの」
```

`git cherry` を使うのが要点で、`main` は過去に squash / 別実装で同じ内容を取り込んで
いるため、`git log` だけだと「無い」ように見えるものが大量に出る (実測: monitor-webapp
は log で 77 本 / cherry の等価判定で 12 本が既済)。

ブランチの包含関係も先に潰した (下位ブランチを二重に数えないため):

| ブランチ | 状態 |
|---|---|
| `claude/sys5-protection-fixes-20260818` | `claude/open-auto-run` に **内包** |
| `claude/cap-real-equity-20260820` | 同上 |
| `agent/mf-exit-rename-fix` | 同上 |
| `agent/open-run-observability-order` | 同上 |
| `agent/observability-notification-refactor` | `claude/monitor-webapp` に **内包** |

---

## 2. 既知項目の在り処 (確認結果)

| 項目 | commit | 起点 `2d09839` に載っていたか |
|---|---|---|
| 指値約定判定 (backtest fidelity) | `f8fbba4` | **YES** |
| エンジン欠陥 3 件 (7 系統がバックテスト可能に) | `ffb5acb` | **YES** |
| System1 の大文字固定 Close (live 影響 0) | `2d09839` | **YES** (tip) |
| 保有日数 = 立会日 | (7dfddca 内) | **NO** → 本パスで land |
| self_monitor の open_run canonical dir | `289701d` | **NO** → 本パスで land |
| S2/S3/S5/S6 の live 指値 (spec 復元) | `7dfddca` / `738834b` | **NO** → 本パスで land |

---

## 3. 取り込み表

分類は依頼どおり:
**(a)** 今すぐ main に入れて安全 (バグ修正 / テスト / ドキュメント / 既定 OFF のフラグ付きコード) /
**(b)** 既定の live 挙動を変える /
**(c)** 陳腐化・重複・すでに main にある。

### 3.1 `claude/open-auto-run` — **26 本すべて land (残 0)**

`5f68caf` の 1 merge で取り込み。**この 26 本は「main に無い」だけで、
毎晩の本番 run はすでにこのコードで動いている**。

| commit | 内容 | 分類 | 対応 |
|---|---|---|---|
| `738834b` | S3/S5/S6 の live 指値を spec へ復元 (×0.93 / ×0.97 / ×1.05) | **(b) 承認済 spec 復元** | land |
| `7dfddca` | S2 の live を spec へ復元 (+4% 指値 / +4% 利確 / **立会日保有**) | **(b) 承認済 spec 復元** | land (B1 の実体を含む) |
| `ba9722d` | 非同期 close の 200 を失敗に数えない / status 正規化 | (a) | land |
| `5923fe9` | portfolio cap の equity 基準を実 equity へ | **(b) フラグ `CAP_USE_REAL_EQUITY` 既定 OFF** | land (main 既定は無変更) |
| `53f830d` | orphan の stale ATR でハエ叩き stop を張らない | (b) 保護価格が変わる = バグ修正 | land |
| `837c8bb` | 保護エンジン硬化の回帰テスト + 仕様ドキュメント | (a) | land |
| `b7ec925` | broker 拒否を armed から分離、保護カバレッジを artifact に | (a) | land |
| `20164b7` | orphan 既定 stop / `$0.01` クランプ / qty 競合の全拒否を解消。OCO 昇格は **`PROTECT_USE_OCO` 既定 OFF** | (b) 既定は 1 建玉 1 常駐注文 (従来も broker 上は 1 本しか通っていない = 挙動中立) | land |
| `64ae9b1` | broker が `tradable=False` の銘柄は失敗でなく skip | (b) バグ修正 | land |
| `b6ec30b` | orphan の「帰属なし」と「exit 不能」を分離 | (a) | land |
| `21342f8` | already-protected を exit 失敗に数えない (runner) | (a) | land |
| `a024b82` | rename alias を保有株数でゲート、台帳不変条件を維持 | (a) | land |
| `d9e72aa` | execution-summary ntfy delivery sidecar (runner 側) | (a) | land |
| `0ccbfef` | rename 後の建玉の entry metadata 解決 | (a) | land |
| `8976bba` | signals run_id を producer に刻み recon で検証 | (a) | land |
| `ed85e5a` | 未試行 delivery を neutral に、2 表現を atomic 書き込み | (a) | land |
| `c2a601e` / `cb67017` | CI: main は repo 全体 lint、運用ブランチ PR だけ ratchet | (a) | land |
| `bf19d5a` | dashboard publish の前に reconcile | (a) | land |
| `505f4f4` | publish スクリプト同期 | (a) | land |
| `7aa1de2` | `.env` を repo root から解決 (snapshot/exit_ledger の無言欠落を止める) | (a) | land |
| `b8c97c7` | portfolio cap の trim 理由を signals JSON に載せる | (a) | land |
| `2ed890e` | 薄シグナル判定を cap 前の候補数に戻す | (a) | land |
| `09956a5` | 保有建玉を entry coid で帰属 (sys3-7 の枠飢餓を解消) | (a) | land |
| `af29208` | exit funnel 配線 + fired/armed 分離 + `__unassigned__` | (a) | land |
| `0018afb` | exit tag 解決の durable 化 + orphan の可視化 | (a) | land |
| `b756613` / `704da41` | data snapshot | (c) データ | merge に同梱 |

**merge の conflict 解決 (5 file / 8 hunk)** — 判断根拠つき:

| file | 解決 |
|---|---|
| `scripts/open_auto_run.py` | `exit_artifacts` (ROLE_*/write_with_sidecar, main 由来) と `order_status` (is_working 等, ブランチ由来) の **両方** を import。`_poll_order_ids` の deadline は引数化済なので main 側のローカル再定義と working set を削除。`publish()` は `-> int` 契約どおり 0 を返す側を採用 |
| `scripts/paper_exit_check.py` | main の role/sidecar 出力と、ブランチの ORPHAN / UNTRADABLE 警告を **併存** |
| `tests/test_execution_summary_20260707.py` | 両ブランチのテストを併存 (import 重複のみ解消) |
| `config/ticker_renames.json` | ブランチ側 (main の superset) |
| `tests/test_open_auto_run_thin_gate_precap.py` | publish/notify stub を int 返しへ (上の `publish()` 決定に整合) |

main 側の既存修正 (`f8fbba4` / `ffb5acb` / `2d09839`) が auto-merge 後も残っていること
を確認済み。

### 3.2 `claude/monitor-safety-nets-20260712`

| commit | 内容 | 分類 | 対応 |
|---|---|---|---|
| `ae36a3a` | publish 鮮度を **origin ref** で判定 (local branch では毎日 CRIT) | (a) | **cherry-pick `8a2b43a`** |
| `289701d` | open_run は canonical な `open_run_<YYYYMMDD>` だけを見る (one-shot sidecar 誤読) | (a) | **cherry-pick `69a6450`** |
| `c6aeed7` / `7b7cc7a` / `0effe8f` / `bc8c7a8` | safety-nets 本体 / launcher ASCII / pipeline+freshness / data_fresh を full_backup 基準に | (c) **すでに main にある** (PR #146 `0eb5765` の squash。`scripts/self_monitor_check.py` の該当実装を実地確認) | 対応不要 |
| `8a3caa6` / `13faeaa` | entry_submitted=0 の「枠満杯」と「本物の失敗」の区別 + doc | (c) **本日 (08-22) 別セッションが作業中** | §5 参照 (未 land) |

### 3.3 `claude/monitor-webapp` — 14 本 land / 残りは §5

| commit | 内容 | 分類 | 対応 |
|---|---|---|---|
| `da9fbcc` | **CPCV / purge+embargo / moving-block bootstrap / Deflated Sharpe / survivorship guard** (すべてフラグ既定 OFF、本番 module から import されない) | (a) | **land `894d363`** (`CLAUDE.md` のみ conflict → 節を union) |
| `9e08f55` | pre-commit を Windows-safe に | (a) | land `38d9b9f` |
| `8fb9cb6` | NarrativeCard の閉じない `**` で静的生成が無限ループ→OOM | (a) | land `3642a22` |
| `5707179` | Phase1 常設ゲート (measurement/served/freshness/monotonic/snapshot/funnel) | (a) | land `14f3a91` |
| `db9a2e6` | alpaca data 検証スクリプト + publish/exit hardening 運用ドキュメント | (a) | land `c995aed` |
| `d9e750a` | pipeline_20260730 を fired/armed 新セマンティクスで再計測 | (a) | land `c0ff8c3` |
| `5ec0e6c` | producer → recon → preflight を 1 本の鎖として検証 | (a) | land `da26188` |
| `c8c180f` | publish した run が production に届かないことの検出 | (a) | land `cc9cb6b` |
| `a473453` | ハードコードパスの表記を forward slash に統一 | (a) | land `7f02cc0` |
| `8bca2f9` | exit-ledger 鮮度を **立会セッション**で判定 (暦日 today ではない) | (a) | land `e8ca2ba` |
| `b7f095b` / `e00e1c3` / `f9d09c9` / `7e11d4b` | 朝の read-only 再測定タスクの棚上げ / signal-score 校正 probe NO-GO / 指値修正後の validation 再測定 / テストカバレッジ監査 | (a) ドキュメント | land `bbe993d` `8149daa` `2c82ff1` `0b7a87c` |
| `960487c` | System3/5/6 の指値到達判定 | (c) **main に `f8fbba4` として既済** | 対応不要 |
| `b36ca68` | time exit の broker 送信状態表示 | (c) **main に `6ac2e82` として既済** | 対応不要 |
| `b9b57ce` | `.env` を repo root から解決 | (c) open-auto-run `7aa1de2` と等価 (本パスで land 済) | 対応不要 |
| `aeed0f6` | already-protected を exit 失敗に数えない | (c) open-auto-run `21342f8` と等価 (land 済) | 対応不要 |
| `538c39b` + `da22f16` | exit funnel の fired/armed 分離 + 配線 | (c) open-auto-run `af29208` が「da22f16+538c39b equivalent」として land 済 | 対応不要 |
| `10ae0e3` | CI ratchet | (c) open-auto-run `cb67017` と等価 (land 済) | 対応不要 |
| `26385b0` `019f13a` `0ace8c8` `5d6cc5a` `65de7ef` `6418363` `04e645d` `219ba77` `4db3b54` `ae7f202` `19ba970` `7ab97fb` | dashboard bundle / recon binding / freshness / deploy hook の系列 | (a) だが **機械的に分離できない** | **未 land** — §5 |
| `fae206c` / `6219ad4` | ダッシュ stale 警報を self-heal の後ろへ | (c) **本日 (08-22) 別セッションが作業中** | §5 |
| 各 `chore(data): daily update` | ダッシュ配信データ | (c) データ | 対応不要 |

### 3.4 その他のブランチ

| ブランチ | 判定 |
|---|---|
| `claude/daily-main-follow` | `9e3bc88` は `af29208` と等価 (land 済)。残るのは `6db50a3` (main 追随の daily wrapper) — このブランチ自身の存在意義なので land しない。**(c)** |
| `agent/exit-overdue-enforcement` | 大半は monitor-webapp と同じ系列。固有は `2aa8ab8` (submit 制御の分離 + overdue 露出) と `1a4092e` (import 整列)。`2aa8ab8` は dashboard TS 4 file + `paper_exit_check.py` で衝突 → **§5** |
| `agent/exit-verify-dryrun-alert` | `52e087b` は main の `3cb09e3` (PR #160) と同趣旨で **main が後発・上位**。**(c)** |
| `claude/land-observability-20260818` | `5b9118a` / `751cdab` は fired/armed + lineage で 3.3 の系列と同じ。`edc38c8` (ntfy の正直な受理報告) は `bf19d5a` (land 済) と重複。**(c)/§5** |
| `claude/fix-close-fill-accounting-20260820` | `63215b4` は open-auto-run `ba9722d` と同趣旨 (land 済)。**(c)** |
| `claude/pipeline-funnel-cache-fix` | `9916d1a` は main の `8bed1d5` (PR #140) として既済。**(c)** |
| `claude/alpaca-orders` (2026-07-01) | `signals_json_to_orders` / narrator / dashboard の初期実装。main には別経路で入っており、`common/alpaca_trading.py` の現行実装がその後継。**(c) 陳腐化** |

---

## 4. 今回の 2 修正

### 4.1 保有日数 = 立会日 (`789b03c`)

`SystemTradeRules.max_holding_days` (S2=2 / S3=3 / S5=6 / S6=3) は
docs/systems と `strategies/system{N}_strategy.py` の `compute_exit`
(`idx = entry_idx + offset` = bar 単位) が示すとおり **立会日ベースの spec**。
live 側の `compute_holding_days` は暦日で数えていた。

実装は `claude/open-auto-run` (7dfddca) に既にあり、**同じものを 2 つ書かず**
merge で持ってきたうえで、換算を `common/trading_days.py` へ切り出した:

- `count_trading_days(d0, d1)` … `(d0, d1]` の NYSE 立会日数
- `add_trading_days(d0, n)` … `d0` から n 立会日後
- 退避: NYSE calendar → `np.busday_count`/`busday_offset` → 暦日。
  退避時も **`(d0, d1]` 半開区間**へ揃える (素の `busday_count` は `[d0, d1)` で 1 日ずれる)

これで **live (`compute_holding_days`) と `common/trade_management` の
`max_exit_date` が同じ実体**を使う (以前 `max_exit_date` は
`signal_date + timedelta(days=...)` = 暦日という 2 つ目の定義だった)。
`common.alpaca_trading` は `common.trade_management` を import しているため、
どちらかに置くと循環参照になる → 中立モジュールにした。

実測 (2026 年 NYSE。07-03 は独立記念日の振替休場、11-26 は感謝祭):

| 区間 | 暦日 (旧) | 立会日 (新) |
|---|---|---|
| 金 08-21 → 月 08-24 | 3 | **1** |
| 水 07-01 → 土 07-04 | 3 | **1** |
| 水 11-25 → 金 11-27 | 2 | **1** |
| 金 06-26 → 火 07-07 | 11 | **6** |
| 月 08-17 → 水 08-19 | 2 | 2 (据え置き) |

`max_exit_date`: S5 が金 06-26 起点で 07-02 (暦日) → **07-07** (立会 6 日)。

依頼にあった 2 件のテストは修正後の値になっている:
`compute_holding_days("2026-07-01","2026-07-04")` は **1** (3 ではない)、
`test_system5_time_based_at_6_days` は 2026-06-26(金) → **2026-07-07(火)**。
新規 `tests/test_holding_days_trading_unit_20260822.py` (20 件) が
**金曜またぎ**と**祝日またぎ**の両方、退避ラダー、live↔`max_exit_date` の単位一致、
S2 の time exit が立会 2 日目に発火することを固定する。

### 4.2 指値なし limit を成行へ落とさない (`241275a`)

`_DEFAULT_SYSTEM_ORDER_TYPE` が limit の system (S2/S3/S5/S6) の行に指値が
無いとき、`signals_to_orders` / `signals_json_to_orders` は `order_type` を
**market へ黙って落として発注**していた (テストがその挙動を spec として固定してもいた)。

「誤発注を防ぐための fallback」という意図だったが、**成行で出すこと自体が誤発注**:

- S3 「前日終値 −7% に指値買」→「いま成行で買う」(7% 高く買う)
- S2 「前日終値 +4% 以上で指値売」→「いま成行で売る」

同 module の `_side_from_row` (side 不正行) と同じ方針に揃えた:

- `SKIP_LIMIT_WITHOUT_PRICE = "skip:limit_without_price"`。
  `skip_reason` は `skip:limit_without_price:<system>_<missing|invalid=...>`
- `scripts/build_execution_recon.py` の `drop_breakdown` は skip_reason の 2 番目の
  segment を kind にするので、朝の recon に **`limit_without_price: N`** として
  そのまま出る (新しい配線は不要)
- 件数は `logger.warning` + `_audit_log(skip_limit_without_price_summary)` でも出す
- 非 dry_run では submit しない。ただし **結果リストからは落とさない** (silent drop 禁止)
- 判定は `_coerce_limit_price` に集約 (None / 空文字 / 非数値 / 0 以下 / NaN)
- market system (S1/S4/S7) は無変更

**合成**: 7dfddca / 738834b で emitter が spec 指値を載せるようになったので、
この skip は通常発火しない。**発火したときに見えること**が目的。

---

## 5. 意図的に land しなかったもの (ユーザー判断待ち)

### 5.1 観測パイプラインの並行書き換え (monitor-webapp 側、12 commit)

`26385b0` `019f13a` `0ace8c8` `5d6cc5a` `65de7ef` `6418363` `04e645d`
`219ba77` `4db3b54` `ae7f202` `19ba970` `7ab97fb`
(+ `agent/exit-overdue-enforcement` の `2aa8ab8`)

**理由**: `claude/open-auto-run` と `claude/monitor-webapp` は
`scripts/build_execution_recon.py` / `paper_exit_check.py` / `publish_signals.py` /
`publish_execution_summary.py` / `common/publishers/ntfy.py` を **並行に書き換えて
いる**。open-auto-run を land した後の merge probe で **24 file / 約 85 hunk** の
conflict が出た (うち `build_execution_recon.py` だけで 16 hunk)。

ここは「テキストの衝突」ではなく **どちらのセマンティクスが現行か** の判断で、
誤って解くと exit funnel / recon の鎖が **無言で** 壊れる (過去に何度も誤警報の
原因になっている箇所)。分離できるものは §3.3 で 14 本 land 済み。残りは
**dashboard bundle 系だけを対象にした 1 パス**として切り出すのが安全。

これらは分類上 **(a)** だが、機械的に安全に land できないので保留した。

### 5.2 本日 (2026-08-22) 別セッションが作業中のもの

- `claude/monitor-webapp`: `fae206c` `6219ad4` (ダッシュ stale 警報を self-heal の後ろへ)
- `claude/monitor-safety-nets-20260712`: `8a3caa6` `13faeaa` (entry_submitted=0 の切り分け)

両者は同じ `docs/MORNING_BRIEF_CRY_WOLF_20260822.md` を作る **1 つの取り組み**で、
片方だけ land すると割れる (`8a3caa6` 単体は clean に当たるが、対の `fae206c` は
`scripts/check_dashboard_freshness.py` で衝突する)。**進行中の作業であって
取り込み漏れではない**ので触っていない。

### 5.3 既定 live 挙動を変えるが承認済 / フラグ OFF のもの (land した理由の明記)

| 項目 | 扱い |
|---|---|
| S2/S3/S5/S6 の live 指値 (`7dfddca` / `738834b`) | **ユーザー承認済の documented spec 復元**。trunk を稼働ブランチに合わせるため land |
| `CAP_USE_REAL_EQUITY` (`5923fe9`) | コード既定 **OFF**。main の既定挙動は無変更 |
| `PROTECT_USE_OCO` (`20164b7`) | コード既定 **OFF**。既定は「1 建玉 1 常駐注文」で、従来も broker 上は 1 本しか通っていなかった (挙動中立) |
| protective stop の % フロア (`20164b7` / `PROTECT_STOP_FLOOR_ENABLED` 既定 ON) | `$0.01` = 実質無保護に潰れるバグの修正。`=0` で旧挙動へ戻せる |
| untradable skip (`64ae9b1`) | 確実に失敗する発注をしないだけ |

**未承認のまま黙って入れた既定 live 挙動の変更は無い。**

---

## 6. テストと回帰

live worktree (`.env` あり / `data_cache` は本番へ junction) で、同一フラグの
before/after を **失敗 ID 集合**で突合した。

```
python -m pytest tests -o addopts='' -q -p no:randomly -p no:cacheprovider \
  --ignore=tests/test_app_imports.py \
  --ignore=tests/test_today_modules_lightweight.py \
  --continue-on-collection-errors -rfE
```

- `tests/test_app_imports.py` は import 時に `sys.exit(1)` するため collection 全体を
  INTERNALERROR で落とす (既存の壊れたファイル)。
- `tests/test_today_modules_lightweight.py::TestTodaySignalsEdgeCases::test_empty_data_handling`
  は **実ネットワークへ HTTPS を張ったまま無限に待つ** (計測時 50 分以上 CPU 0)。
  before/after 双方から同じく除外した。どちらも本パスの変更とは無関係。

| | 失敗 + エラー |
|---|---|
| baseline (`2d09839`) | **238** (225 failed + 13 errors) |
| after (`75a9f96`) | 本文末尾に記載 |

**新規失敗 0** が受け入れ条件。1 回目の突合で出た 9 件は以下のとおり全て
**テスト側の前提が古い**もので、`75a9f96` で解消した (`fix(test): 統合後の期待値を…`):

| 失敗 | 原因 | 対応 |
|---|---|---|
| `test_exit_verify.py` × 4 | 07-08(水)→07-12(日) を「4d」と書いた **暦日前提** | today を 07-13(月) に。金曜またぎの回帰ガードを追加 |
| `test_open_auto_run_thin_signals_exit.py::test_exit_check_script_does_not_read_signals_json` | A1 の契約は「exit **判断**が signals に依存しない」ことなのに、文字列 `today_signals` の不在で固定していた。`8976bba` が recon 用 provenance だけを読むようになった | AST で「today_signals を読む関数は `_signals_run_id` だけ」「呼び出しは 1 箇所」「結果が if/while の条件に入らない」を固定 (契約は前より厳しい) |
| `test_paper_exit_check_broker_unreachable.py` × 1 | stub が新しい `symbol_aliases` kwarg を受けない | `lambda *a, **k` |
| `test_alpaca_exit_orders.py` × 3 | `common/broker_alpaca._load_env_once` が最初に broker へ触れたテストの時点で運用者の `.env` を **プロセス全体**へ `load_dotenv` する。その `.env` は `PROTECT_USE_OCO=1` を持つため、**単体では通るのにフルランで落ちる** | 保護系トグルを毎テスト明示的に落とす autouse fixture を追加し、運用 env から独立させた |

さらに merge 直後に 3 件 (`test_system356_live_spec_20260822.py::TestBacktestPathUntouched`)
が落ちた。これは **統合したからこそ出た本物の衝突**で、live 指値の復元 (`738834b`) の
テストが `_frame()` の ±2% バーで `compute_entry` が指値を返す前提だったのに対し、
main の `f8fbba4` が **当日バーの到達判定**を入れていたため。指値の *値* を比べる
テストは到達するバーを作るよう直し、**到達しないバーが None になること**を新テストで
別に固定した (`c37d3bb`)。

repo 全体 `ruff check .` は **All checks passed** (取り込んだ新規ファイルの
lint 債務 6 件を `75a9f96` で解消済み)。

---

## 7. 残タスク

1. §5.1 の dashboard/観測系 12 commit を、専用の 1 パスで land する
   (`build_execution_recon.py` のセマンティクスをどちらに寄せるかの判断が要る)。
2. `tests/test_today_modules_lightweight.py` のネットワーク依存を切る
   (現状フルスイートが実質完走できない)。
3. `tests/test_app_imports.py` の `sys.exit(1)` を消す (collection を落とす)。
4. baseline 238 failed の内訳整理 (本パス以前からの既存債務)。
