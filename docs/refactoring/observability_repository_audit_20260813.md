# Quant observability / ntfy repository audit — 2026-08-13

## 結論

「未計測」は一つのUIバグではない。生成、execution reconciliation、通知、Git data
publish、Next loader が別々の契約とブランチで動き、同じ日付の異なるrunを混ぜていた。
特に22:35のopen runnerは **dashboard publishの後** にexecution summaryを作り、その
副作用でローカルpipelineのExit/funnelを更新していた。そのため、通知本文ではExitが
観測できてもVercelへ送ったpipelineはExit=0のまま、という状態を毎日再現できる。

今回の変更は売買判断・発注数量・broker APIを変更しない。観測経路を次の順序と契約へ
寄せる。

1. signals / orders / exitsをreconcileする。
2. 同一date + run_idからdashboard bundleを決定的にmaterializeする。
3. schema、count、provenance、content hashを検証する。
4. 検証済みbundleをpublishする。
5. 通知のchannel別accepted/failed状態を記録する。

## 本番で確認した事実

- `scripts/open_auto_run.py` のproduction runtimeは `publish -> notify` の順だった。
  `notify` が呼ぶ `publish_execution_summary.py` は通知だけでなくrecon生成とpipelineの
  Exit/funnel書換えも行う。公開後の書換えは次回publishまでVercelへ届かない。
- 直近4日で、reconの `exit_submitted` 合計 / 公開pipeline Exit合計は
  `08-10: 12/0`, `08-11: 13/0`, `08-12: 11/0`, `08-13: 12/0` だった。
- 追跡済み `pipeline_20260807..13.json` は毎日、Exitを除く35 phase中
  `measured=true` が0、countありだがfalseが14、null/falseが21だった。一方、同日の
  `today_signals` はfunnel実数を保持していた。
- `PipelineSection.tsx` はschemaの `measured` ではなく `count != null` を表示条件にし、
  countあり/未検証を実測に見せていた。
- `common/signal_export.py` は同日のsignals JSONを夜に作り直す際、朝の
  `meta.publish_status` を消していた。古いstatusを保持すると別runの成功を偽装するため、
  新runは明示的に `not_attempted` から始める必要がある。
- monitor branchのntfy transportはemoji入り `X-Title` をrequestsへ渡し、latin-1
  header encodeで全retryが落ちる状態を再現できた。HTTP 2xxは端末到達ではなく
  ntfy serverの受理なので、状態名は `accepted` とする。
- freshness / publish検証は最大日付だけを比較しており、同日rerunの古いblobを成功と
  判定した。

## 今回のPRで実装する境界

### Dashboard / notification PR (`claude/monitor-webapp` 向け)

- `today_signals.funnel` とexecution reconからpipelineをsource-awareにmaterializeする。
- 同じsourceは新しいrun_idで更新し、別sourceの実測は保護する。ratioはmerge後に再計算。
- sys7へ共有株式universe値が来た場合はTgt=1を捏造せず、理由付きunavailableにする。
- date/run/schema/non-negative integer/phase completeness/monotonicity/Exit coverageを
  publish前にfail-closed検証する。
- signals、pipeline、任意のnarrative/account snapshotのSHA-256を
  `dashboard_bundle_YYYYMMDD.json` に記録する。reconは注文詳細を含み得るため公開せず、
  provenance hashだけをmanifestの `sources` に記録する。
- push後は日付だけでなくsignals/pipeline/manifestのexact Git blobを照合する。
- `-AutoLatest` は既存artifactのcatch-upに限定し、volatileなaccount snapshotを再生成せず
  同一sourceの再実行をbyte-stableなno-opにする。
- Next loaderはmanifestがある場合、そのfile/hash/runだけを組み合わせる。不一致時は
  silent fallbackせず表示停止 + bundle警告を出す。旧データは「未検証」と明示する。
- UIは `measured === true && finite count` のみ実測表示する。countあり/falseは「未検証」、
  null/falseは「未計測」に分ける。
- signal publishはchannel別状態をsecret-freeで記録し、ntfy失敗 + email成功を
  `fallback_accepted` として区別する。新runは `not_attempted` から開始する。
- 日次pipelineのsignal通知は `--fallback` を渡す。execution summary、morning brief、
  PowerShell直送はまだ共通outboxへ移していないため、今回の状態表示はcurrent signal
  forecastのみを対象とする。
- ntfy headerをtransport直前にlatin-1 safe化し、topic masking失敗時はfail-closedにする。

### Runtime PR (`claude/open-auto-run` 向け)

- open runnerを `notify/reconcile -> publish` に変更し、片方が失敗しても両段を実行する。
- notify/publishの終了コードをcompletion recordへ保存する。
- 取引とDONE.lock完了後の観測劣化はexit 4とし、wrapperは0/4で
  `RESET_ONCE.flag` を消費する。pre-trade abortは従来どおりmarkerを保持する。

この分割は、Vercelが追うbranchとWindows schedulerが実行するbranchが異なるため必要。
長期的には下記P0-1でruntime branchそのものを統合する。

## Repository scorecard

監査対象はtracked 1,073 files。内訳はPython 620、Markdown 152、JSON 96、PowerShell
42、TypeScript 25、TSX 10だった。

- parse可能なproduction Python: 343 files / 約116,235 LOC
- test Python: 249 files / 約53,532 LOC
- productionのbroad exception (`Exception` / `BaseException` / bare): 約3,018
- pass-only exception handler: 約954
- 直接参照されるenv key: 162種類 / 86 files / 338 accesses
- trackedだがignore対象: 29 files（nested worktree、editor設定、root出力等を含む）
- 最大ファイル: `scripts/run_all_systems_today.py` 6,601 LOC
- 最大級関数: `compute_today_signals` 約1,856 LOC
- CI: Linux/Python中心。Next build/typecheckと実Windows PowerShell jobがない
- `npm audit` (2026-08-13): critical 1 / high 2。直接依存Next 14.2.5とPostCSS、
  transitive nanoidが対象。dashboardはstatic exportだが、build supply chainとして別PRで
  upgradeと回帰確認が必要

テスト量は強みだが、branch/runtimeの分断とerror swallowingにより、テスト済みコードが
production producerへ届いたかを証明できていない。

## 優先度付きリファクタリング提案

### P0-1. Runtime source of truthを一つにする

現状は `main`, `claude/open-auto-run`, `claude/daily-main-follow`,
`claude/monitor-webapp` が長寿命で分岐し、scheduler worktreeとVercel branchへ手動伝播する。
producer versionをbundle manifestに出し、許可したcommit/ref以外からのpublishを拒否する。
最終形は一つのrelease refをWindows schedulerとdashboard publisherがcheckoutする構成。

受入条件:

- scheduler recordにproducer commitが残る。
- CIが各runtime branchのproducer driftを検知する。
- 手動cherry-pick/landing scriptなしでreleaseを伝播できる。

### P0-2. NotificationService + durable outbox

現在はPython publisher registry、execution summary直送、morning brief直送、PowerShellの
raw `Invoke-RestMethod`、freshness専用outboxに分岐する。typed `NotificationMessage` と
`NotificationRouter.publish_text()` へ統合し、event-id、channel result、attempt、TTL、
retry、email fallbackを一つのoutboxへ保存する。PowerShellはPython CLIを呼ぶだけにする。

注意点:

- HTTP 2xxは `accepted`。端末配送を名乗らない。
- topic、email address、provider responseをdashboard/structured logへ保存しない。
- timeout後retryの重複をevent-id dedupeで抑える。
- 「email backup有効」を設定するならstartupで必要設定を検証する。現状の主要経路では
  backupは実質有効でない。

受入条件は429→成功、timeout→fallback、全失敗→outbox、replay、重複抑止、secret非露出。

### P0-3. Canonical dashboard bundle compiler

今回導入したpreflightを最終的な唯一のpipeline builderへ昇格し、
`daily_polygon_monitor`, `build_execution_recon`, `publish_execution_summary` に残る
post-hoc patchを廃止する。phase schemaは将来、booleanだけでなく
`measurement_status: measured | unavailable | invalid | stale`、`source`, `observed_at`,
`run_id`, `reason` を必須にする。

受入条件:

- 同一source入力からbyte-stableなbundleが得られる。
- same-day rerun、date/run mismatch、partial system、invalid count、sys7共有universeをfixture化。
- commit-treeの前とVercel served後に同じmanifest hashを検証する。

### P1-1. Observability configをtyped settingsへ集約

全162 envを一括移行せず、まず `NotificationSettings`, `RuntimePaths`,
`PublishSettings` のみconstructor injectionへ移す。repo root、Python executable、branch、
retry/fallback policyの優先順位をCLI > env > config > defaultで固定し、secretは
configured/unconfiguredだけをlogへ出す。

受入条件はWindows path、旧env alias、coercion、missing secret、readonly directoryの
table testと、新module内の直接 `os.getenv` / `C:\\Repos` 参照ゼロ。

### P1-2. CI / dependency / hygiene ratchet

- path-filter付き `npm ci && npm run build` とbundle loader fixture testを追加する。
- `windows-latest` でPowerShell parserだけでなくnotification/publish CLI smokeを実行する。
- Next/PostCSS/nanoid監査警告はupgrade専用PRで解消し、`npm audit` baselineをCI化する。
- `git ls-files -ci --exclude-standard` をallowlist化し、nested `.worktrees/` とroot実行出力を
  tracked対象から外す。
- changed production filesに限り、新規broad-except-pass、500行超関数、絶対user pathを
  増やさないratchetを導入する。既存負債はbaseline化し、巨大core分割はgolden parity付き
  の別PRにする。

## Rollout / rollback

1. Runtime PRをmergeし、scheduler worktreeをそのcommitへadvanceする。
2. 同じ保守枠でDashboard PRをmergeし、publisherのprimary checkoutも更新する。
   `C:\\tmp\\qts-main-run\\results_csv` junctionがそのprimary checkoutを向くことを確認する。
3. 同日のsource一式からpreflightを実行し、manifest付きdata-only publishを一度行う。
4. Vercelでbundle verified、funnel 34/35（sys7共有Tgtは理由付きunavailable）または35/35、
   Exit 7/7、current runのntfy stateを確認する。
5. 翌営業日の22:35 runでorigin blob/run/hashがlocalと一致することを確認する。

コードPRへ当日のgenerated JSONを手修正で混ぜない。緊急表示復旧が必要なら、同日全sourceを
compilerで再生成・検証したdata-only commitに分ける。これによりproducerの再発を隠さず、
日次自動commitとの競合とreview noiseを避ける。

rollbackはdashboard loader/UIを先に戻しても売買へ影響しない。runtimeの順序を戻す場合も
取引完了marker契約（exit 4でRESET消費）を維持し、観測失敗による翌日再flattenを防ぐ。
