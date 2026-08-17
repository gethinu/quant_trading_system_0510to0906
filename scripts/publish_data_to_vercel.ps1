<#
.SYNOPSIS
    daily_pipeline が生成した当日 JSON を Vercel が読める場所 (git 管理下の
    apps/dashboards/alpaca-next/data/) にコピーして commit + push する。

.DESCRIPTION
    results_csv/ は .gitignore 済のため Vercel build には存在せず、dashboard は
    永遠に mock を表示していた。本 step で当日 JSON を data/ にコミットし、
    Vercel の auto-deploy に実データを反映させる。

    data/ 内は「日付付きファイル名」のまま置く (lib/loadCoverage.ts が直近 7 日を
    集めて sparkline を描くため)。肥大化防止に各パターン直近 KeepDays 件のみ保持。

    2026-07-30 恒久修正 (publish 取りこぼしの root cause fix):
      根因:
        (1) このリポの唯一の git writer は本スクリプトで、06:00 の daily_pipeline
            step6 と ~08:00 の morning_brief.ps1 -AutoLatest の 2 系統から呼ばれる。
            scheduler が -RestartCount 2 / -StartWhenAvailable のため、遅延・ハング
            した 06:00 run が再起動されて self-heal と重なると、共有 .git/index に
            対して 2 本の `git add`/`commit` が競合し、片方が `.git/index.lock` を
            残す (crash / host sleep / task timeout でも残る)。
        (2) 旧実装は `git add -A` の exit code を検査していなかった。lock で add が
            落ちても素通りし、次の `git diff --cached --quiet` が「staged 差分なし」
            と判定して "data/ に差分なし。commit/push をスキップ" で **exit 0** の
            偽成功になる (= ntfy は新しいのに dashboard が凍結)。lock が commit まで
            残った日は `git commit exit=128` として顕在化。同じ根因の裏表。
      対策 (既存挙動は維持しつつ堅牢化):
        A. 二重起動ガード (named Mutex) で publish 同士の並行 git を直列化。
        B. GIT_INDEX_FILE で publish 専用 index を使い、日次 pipeline の .git/index
           とロックを共有しない。専用 index は HEAD から seed し data/ のみ stage
           するので、作業ツリーの無関係な dirty も巻き込まない。
        C. stale な .git/index.lock / HEAD.lock / <private>.lock を安全条件付き
           (mtime が閾値超 かつ git.exe 不在) で除去 + git 実行を retry で包む。
        D. すべての git step の exit code を検査。stage/commit が本当に失敗したら
           非ゼロ終了 (偽の「差分なし skip」を出さない)。
        E. publish 後に served(=HEAD にコミット済 data/) の最新 today_signals 日付が
           generated(results_csv) と一致し、かつ origin に反映済かをスクリプト自身が
           検証。ズレたら非ゼロ終了 + ntfy。成功時は静か (通知しない)。

.PARAMETER Date
    対象日 (YYYY-MM-DD)。未指定なら今日 (ローカル)。

.PARAMETER KeepDays
    data/ 内に保持する各 JSON パターンの世代数 (既定 7)。
    2026-07-02 hygiene: results_csv/ 側の source file も同じ policy で
    purge して git 履歴と disk 使用量を抑える。
    -PurgeSource:$false で source purge を無効化できる。

.PARAMETER NoPush
    commit までで push しない (ローカル検証用)。

.PARAMETER PurgeSource
    results_csv/ 側の source file (今日以外) を KeepDays 世代残して削除する。
    default $true。false 指定で無効化 (Sprint 期間中に history 保持したい時など)。

.PARAMETER RefreshAccount
    copy の前に Alpaca 口座の計測を read-only で作り直す (既定 $true)。
    build_exit_ledger.py (約定台帳 = 実現損益) -> export_alpaca_snapshot.py の順。
    発注は一切しない。-RefreshAccount:$false で無効化 (offline 検証用)。
    NOTE (human task #9 / Alpaca キー): キー未設定だと build_exit_ledger /
    export_alpaca_snapshot が exit=1 になり得るが、これは WARN 継続で publish 本体
    (signals 配信) と成否判定を **切り離している**。verify は today_signals のみを
    見るので alpaca_snapshot 欠落では publish は失敗扱いにならない。

.PARAMETER AutoLatest
    -Date を無視し、results_csv/today_signals_*.json の最新生成日を自動検出して
    publish する self-heal モード。冪等 (data/ が既に最新なら差分ゼロで exit 0)。
    06:00 の wrapper (daily_main_follow.ps1) が途中で死んで dashboard publish を
    取りこぼしても、独立した catch-up task から呼べば取り戻せる。

.PARAMETER StaleLockSeconds
    .git/index.lock 等を「stale」とみなして除去する経過秒 (既定 300)。
    git.exe が生きている間は絶対に除去しない (安全側)。

.PARAMETER GitRetryMax
    lock 等で失敗した git コマンドの最大 retry 回数 (既定 5)。

.PARAMETER NoLockGuard
    二重起動ガード (Mutex) を無効化する (デバッグ用)。通常は指定しない。

.NOTES
    daily_pipeline.ps1 の最終 step から呼ばれる想定。単体実行も可。
    push 先: origin claude/monitor-webapp
#>

param(
    [string]$Date = "",
    [int]$KeepDays = 7,
    [switch]$NoPush = $false,
    [bool]$PurgeSource = $true,
    [switch]$AutoLatest = $false,
    [bool]$RefreshAccount = $true,
    [int]$StaleLockSeconds = 300,
    [int]$GitRetryMax = 5,
    [switch]$NoLockGuard = $false
)

$ErrorActionPreference = "Continue"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ScriptDir

$SrcDir = Join-Path $ProjectRoot "results_csv"
$DataDir = Join-Path $ProjectRoot "apps\dashboards\alpaca-next\data"
$Branch = "claude/monitor-webapp"
$RelData = "apps/dashboards/alpaca-next/data"

function Write-Log {
    param([string]$Message)
    Write-Host "[publish_data] $Message"
}

# --- ntfy WARN (本当に失敗した時だけ鳴らす) ------------------------------
function Send-PublishNtfy {
    param([string]$Title, [string]$Body)
    if (-not $env:NTFY_TOPIC) {
        Write-Log "ntfy スキップ (NTFY_TOPIC 未設定): $Title"
        return
    }
    $base = if ($env:NTFY_URL) { $env:NTFY_URL.TrimEnd('/') } else { "https://ntfy.sh" }
    try {
        $h = @{ "X-Title" = $Title; "X-Priority" = "5"; "X-Tags" = "warning" }
        Invoke-RestMethod -Uri "$base/$($env:NTFY_TOPIC)" -Method Post -Headers $h -Body $Body | Out-Null
        Write-Log "ntfy WARN 送信済: $Title"
    }
    catch { Write-Log "ntfy WARN 送信失敗: $_" }
}

# -AutoLatest: self-heal path (2026-07-22 root-cause fix). The daily dashboard
# publish normally runs as the LAST step of daily_main_follow.ps1, AFTER the child
# daily_pipeline.ps1 (-SkipVercel) has finished. The ntfy notification lives INSIDE
# that child (step 5), but the dashboard publish lives in the wrapper's step 4.
# If the wrapper dies mid-run (e.g. host sleep / task timeout) the child is orphaned
# yet keeps running to completion -> ntfy fires with fresh data while the wrapper's
# publish is silently lost -> the dashboard freezes on yesterday's build.
# This mode ignores -Date and publishes the NEWEST generated
# results_csv/today_signals_*.json instead. It is idempotent: the downstream
# `git diff --cached --quiet` gate makes a re-run a no-op (exit 0) once data/ is
# already current, so it is safe to fire from an independent catch-up task
# (see scripts/morning_brief.ps1) regardless of whether the 06:00 publish succeeded.
if ($AutoLatest) {
    $latest = Get-ChildItem -Path $SrcDir -Filter "today_signals_*.json" -File -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -match 'today_signals_(\d{8})\.json$' } |
        Sort-Object Name -Descending | Select-Object -First 1
    if (-not $latest) {
        Write-Host "[publish_data] AutoLatest: results_csv に today_signals_*.json が無い。何もせず終了 (exit 0)。"
        exit 0
    }
    if ($latest.Name -match 'today_signals_(\d{8})\.json$') {
        $dc = $matches[1]
        $Date = "{0}-{1}-{2}" -f $dc.Substring(0, 4), $dc.Substring(4, 2), $dc.Substring(6, 2)
        Write-Host "[publish_data] AutoLatest: newest generated date = $Date ($($latest.Name))"
    }
    # AutoLatest is a catch-up publisher for already-generated artifacts.  A
    # read-only account refresh still changes generated_at/hash and defeats the
    # promised idempotent no-op, creating a data commit on every catch-up run.
    # Explicit -Date runs remain the account refresh path.
    if ($RefreshAccount) {
        Write-Host "[publish_data] AutoLatest: account再生成を省略 (既存bundleをbyte-stableに再送)。"
        $RefreshAccount = $false
    }
}
if (-not $Date) { $Date = Get-Date -Format "yyyy-MM-dd" }
$DateCompact = $Date -replace "-", ""

Set-Location $ProjectRoot
$py = if ($env:QTS_PYTHON) { $env:QTS_PYTHON } else { "python" }

# --- git dir を解決 (worktree でも正しく) --------------------------------
$GitDir = & git rev-parse --git-dir 2>$null
if (-not $GitDir) { $GitDir = ".git" }
if (-not [System.IO.Path]::IsPathRooted($GitDir)) { $GitDir = Join-Path $ProjectRoot $GitDir }

# --- 対策A: 二重起動ガード (named Mutex) ---------------------------------
# publish 同士 (06:00 pipeline step6 と ~08:00 self-heal、restart による重複起動) の
# 並行 git 操作を直列化して index.lock 競合を根本から断つ。既に他 instance が
# publish 中なら最大 90s 待ち、それでも取れなければ静かに exit 0 (相手が publish する)。
$mutex = $null
$haveMutex = $false
if (-not $NoLockGuard) {
    try {
        $mutex = New-Object System.Threading.Mutex($false, "Global\qts_publish_data_vercel")
        try { $haveMutex = $mutex.WaitOne([TimeSpan]::FromSeconds(0)) }
        catch [System.Threading.AbandonedMutexException] { $haveMutex = $true }
        if (-not $haveMutex) {
            Write-Log "別 instance が publish 中。最大 90s 待機。"
            try { $haveMutex = $mutex.WaitOne([TimeSpan]::FromSeconds(90)) }
            catch [System.Threading.AbandonedMutexException] { $haveMutex = $true }
        }
        if (-not $haveMutex) {
            Write-Log "他 publish が 90s 経っても継続中。今回は skip (exit 0)。"
            exit 0
        }
    }
    catch {
        # Mutex が作れない環境でも publish 自体は止めない (ガード無しで継続)。
        Write-Log "WARN: mutex 取得に失敗 ($_)。ガード無しで継続。"
        $haveMutex = $false
    }
}

# --- stale な git lock を安全に除去 --------------------------------------
# git.exe が 1 つでも動いている間は絶対に触らない (誤除去防止)。mtime が
# StaleLockSeconds を超えた lock のみ「crash 残骸」とみなして除去する。
function Repair-StaleGitLocks {
    $locks = @((Join-Path $GitDir "index.lock"), (Join-Path $GitDir "HEAD.lock"))
    if ($env:GIT_INDEX_FILE) { $locks += ("{0}.lock" -f $env:GIT_INDEX_FILE) }
    $gitRunning = @(Get-Process -Name git -ErrorAction SilentlyContinue).Count -gt 0
    foreach ($lk in $locks) {
        if (-not (Test-Path $lk)) { continue }
        if ($gitRunning) {
            Write-Log "lock 検出だが git.exe 稼働中のため除去しない: $lk"
            continue
        }
        $age = (New-TimeSpan -Start (Get-Item $lk).LastWriteTime -End (Get-Date)).TotalSeconds
        if ($age -ge $StaleLockSeconds) {
            Remove-Item $lk -Force -ErrorAction SilentlyContinue
            Write-Log "stale git lock を除去: $lk (age=$([int]$age)s >= $StaleLockSeconds)"
        }
        else {
            Write-Log "lock は新しい (age=$([int]$age)s < $StaleLockSeconds)。除去せず待機。: $lk"
        }
    }
}

# --- git を retry + stale-lock 除去で包む --------------------------------
# lock 由来の一過性失敗を吸収する。diff --cached のような「exit 1 が正常」の
# コマンドには使わない (別途 raw で扱う)。返り値 = 最終 exit code。
function Invoke-GitRetry {
    param([scriptblock]$Action, [string]$Desc)
    $code = 0
    for ($i = 1; $i -le $GitRetryMax; $i++) {
        Repair-StaleGitLocks
        $out = & $Action
        $code = $LASTEXITCODE
        if ($out) { $out | ForEach-Object { Write-Log $_ } }
        if ($code -eq 0) { return 0 }
        Write-Log "WARN: git [$Desc] 失敗 (exit=$code) attempt $i/$GitRetryMax"
        Start-Sleep -Seconds ([Math]::Min(15, 3 * $i))
    }
    Write-Log "ERROR: git [$Desc] が $GitRetryMax 回とも失敗 (exit=$code)"
    return $code
}

if (-not (Test-Path $DataDir)) {
    New-Item -ItemType Directory -Path $DataDir -Force | Out-Null
}

# --- 口座計測の再生成 (read-only) ----------------------------------------
# 2026-07-22 root-cause fix: export_alpaca_snapshot.py / build_exit_ledger.py は
# これまで **どの pipeline からも呼ばれておらず**、alpaca_snapshot_*.json は
# 誰かが手で叩いた日 (最後は 07-20) しか生成されていなかった。結果として
#   - Alpaca タブが数日前の口座で凍結
#   - exit (決済) の実績と実現損益がどこにも durable に残らない
# という状態だった。publish は毎日必ず走る唯一の step なので、ここで
# 「約定台帳 -> snapshot」の順に read-only で作り直してから copy する。
#
# 失敗しても publish 本体は止めない (signals 側の配信を巻き添えにしない)。
# 生成できなければ当日ファイルが無いだけで、copy loop が skip し、
# dashboard 側は「未計測」と正直に表示する (0 で埋めない)。
# 2026-07-30: Alpaca キー未設定による exit=1 は WARN 継続のまま。後段の verify は
# today_signals しか見ないので、これが publish の成否判定を汚すことはない (human #9)。
if ($RefreshAccount) {
    $ledgerScript = Join-Path $ProjectRoot "scripts\build_exit_ledger.py"
    $snapScript = Join-Path $ProjectRoot "scripts\export_alpaca_snapshot.py"

    if (Test-Path $ledgerScript) {
        Write-Log "[account] exit 台帳を再構成 (build_exit_ledger.py --date $Date)"
        & $py $ledgerScript --date $Date 2>&1 | ForEach-Object { Write-Log $_ }
        # exit 3 = 未計測を検知 (--fail-on-unmeasured 指定時のみ)。ここでは通知に留める。
        if ($LASTEXITCODE -ne 0) { Write-Log "[account] WARN: build_exit_ledger exit=$LASTEXITCODE (publish は継続)" }
    }
    if (Test-Path $snapScript) {
        Write-Log "[account] Alpaca snapshot を再生成 (export_alpaca_snapshot.py --date $Date)"
        & $py $snapScript --date $Date 2>&1 | ForEach-Object { Write-Log $_ }
        if ($LASTEXITCODE -ne 0) { Write-Log "[account] WARN: export_alpaca_snapshot exit=$LASTEXITCODE (publish は継続)" }
    }
}

# --- dashboard bundle preflight (repair only deterministic fields) --------
# publish の直前に同日 signals/pipeline/recon を 1 bundle として materialize する。
# date/run/schema/count/provenance が一致しない bundle は data commit に載せない。
$bundleScript = Join-Path $ProjectRoot "scripts\prepare_dashboard_bundle.py"
if (-not (Test-Path $bundleScript)) {
    Write-Log "ERROR: dashboard bundle preflight が無い: $bundleScript"
    Send-PublishNtfy -Title "bundle preflight MISSING $Date" `
        -Body "prepare_dashboard_bundle.py が無いため publish を fail-closed しました。"
    exit 1
}
Write-Log "dashboard bundle preflight: same-date/run/schema + funnel/Exit coverage"
& $py $bundleScript --date $Date --results-dir $SrcDir --require-exit 2>&1 |
    ForEach-Object { Write-Log $_ }
$bundleExit = $LASTEXITCODE
if ($bundleExit -ne 0) {
    Write-Log "ERROR: dashboard bundle preflight failed (exit=$bundleExit)。publish しません。"
    Send-PublishNtfy -Title "bundle preflight FAIL $Date" `
        -Body "dashboard bundle contract違反 (exit=$bundleExit)。不整合/未計測のまま公開せず停止しました。"
    exit 1
}

# 当日生成される JSON を data/ に日付付きのままコピー。
# pipeline_*.json = 新 schema (signal_pipeline/v1, 絞込フロー)。
# polygon_daily_coverage_*.json = 旧 schema (移行期は両方 push し dashboard で fallback)。
$patterns = @(
    "today_signals_$DateCompact.json",
    "pipeline_$DateCompact.json",
    "dashboard_bundle_$DateCompact.json",
    "polygon_daily_coverage_$DateCompact.json",
    "narrative_$DateCompact.json",
    # execution summary (夜の実績通知) の ntfy 配信状態。signals 側の
    # publish_delivery は朝の予告便専用で、実績通知の成否はここにしか残らない。
    "notify_delivery_$DateCompact.json",
    # Alpaca paper 口座の read-only スナップショット (scripts/export_alpaca_snapshot.py)。
    # monitor の Alpaca タブがこれを読む。無い日は skip される (copy loop で握り潰し)。
    "alpaca_snapshot_$DateCompact.json"
)

# 当日 JSON は後段の「data commit」節で results_csv から git index へ直接 stage する
# (working tree / $DataDir を経由しない = 執行 worktree を汚さない)。ここでは copy しない。

# --- verify helpers ------------------------------------------------------
function Get-NewestSignalDateInDir {
    param([string]$Dir)
    $best = $null
    Get-ChildItem -Path $Dir -Filter "today_signals_*.json" -File -ErrorAction SilentlyContinue |
        ForEach-Object {
            if ($_.Name -match 'today_signals_(\d{8})\.json$') {
                $d = [int]$matches[1]
                if ($null -eq $best -or $d -gt $best) { $best = $d }
            }
        }
    return $best
}

function Get-NewestSignalDateInRef {
    # 指定 ref (例: origin/claude/monitor-webapp) の data/ にコミット済の最新
    # today_signals 日付。Vercel が build 時に読む「served」状態そのもの。
    param([string]$Ref)
    $best = $null
    $names = & git ls-tree -r --name-only $Ref -- $RelData 2>$null
    foreach ($n in $names) {
        if ($n -match 'today_signals_(\d{8})\.json$') {
            $d = [int]$matches[1]
            if ($null -eq $best -or $d -gt $best) { $best = $d }
        }
    }
    return $best
}

# served(origin/$Branch の data/) >= generated(results_csv) を検証。push 済コミットを
# 直接見るので「commit したブランチ」に依存しない。一致 -> $true (静か)。不一致 -> ntfy + $false。
function Test-PublishServed {
    $gen = Get-NewestSignalDateInDir -Dir $SrcDir
    if ($null -eq $gen) {
        Write-Log "verify: results_csv に today_signals が無い。検証スキップ。"
        return $true
    }
    & git fetch origin $Branch 2>&1 | ForEach-Object { Write-Log $_ }
    $served = Get-NewestSignalDateInRef -Ref "origin/$Branch"
    $ok = ($null -ne $served -and $served -ge $gen)
    if (-not $ok) {
        Write-Log "verify FAIL: served(origin/$Branch)=$served < generated(results_csv)=$gen"
        Send-PublishNtfy -Title "publish verify FAIL $Date" `
            -Body "dashboard publish 検証失敗: served(origin/$Branch)=$served < generated=$gen。data/ が $Branch に届いていません。"
        return $false
    }

    # 日付だけでは同日再生成の stale blob を検知できない。signals/pipeline/manifest
    # の exact git blob id を source と origin ref で比較する。
    $verifyNames = @(
        "today_signals_$DateCompact.json",
        "pipeline_$DateCompact.json",
        "dashboard_bundle_$DateCompact.json"
    )
    foreach ($name in $verifyNames) {
        $localPath = Join-Path $SrcDir $name
        if (-not (Test-Path $localPath)) {
            Write-Log "verify FAIL: local artifact missing: $name"
            return $false
        }
        $localBlob = "$(& git hash-object -- $localPath 2>$null)".Trim()
        $remoteSpec = "${RelData}/$name"
        $servedBlob = "$(& git rev-parse "origin/${Branch}:${remoteSpec}" 2>$null)".Trim()
        if (-not $localBlob -or -not $servedBlob -or $localBlob -ne $servedBlob) {
            Write-Log "verify FAIL: blob mismatch $name local=$localBlob served=$servedBlob"
            Send-PublishNtfy -Title "publish blob verify FAIL $Date" `
                -Body "$name のblobが origin/$Branch と一致しません。同日stale publishを検知しました。"
            return $false
        }
    }
    Write-Log "verify OK: served date=$served, exact bundle blobs match generated source"
    return $true
}

# ============================================================================
# data commit は「常に $Branch (claude/monitor-webapp) の tip」に載せる
# ----------------------------------------------------------------------------
# 2026-08-04 root-cause fix:
#   旧実装は private index を **current worktree の HEAD** から seed し (read-tree HEAD)、
#   `git commit` で current HEAD を進めていた。そのため daily_pipeline step6 /
#   morning_brief が open-auto-run (C:\tmp\qts-main-run) や daily-main-follow
#   (C:\tmp\qts-daily-main) の worktree から publish を呼ぶと、data commit が
#   **執行ブランチ** に載り、後段の `git push origin $Branch` は「動いていない
#   local $Branch (stale)」を送るだけ -> origin/claude/monitor-webapp が凍結し
#   Vercel が何営業日も古いまま (2026-08-04: b756613 が open-auto-run に滞留)。
#   対策 (どの worktree / HEAD から走っても同じ結果):
#     - base = origin/$Branch の tip (無ければ local $Branch)。current HEAD は使わない。
#     - publish 専用 index を base の tree から seed し、当日 JSON を results_csv から
#       `git hash-object` で直接 stage (working tree / $DataDir を一切触らない)。
#     - `git commit-tree` で base を親にコミットを作り、その **新コミットを直接**
#       `origin/$Branch` へ push。local ref も worktree も動かさない (執行系・
#       freeze-baseline に副作用ゼロ)。
#     - non-fast-forward は origin を取り直して 1 回だけ rebuild + retry。
# ============================================================================

# 世代整理の対象 prefix (data/ 内の日付付き JSON 群)。
$prefixes = @("today_signals_", "pipeline_", "dashboard_bundle_", "polygon_daily_coverage_", "narrative_", "alpaca_snapshot_", "exit_ledger_")

# base tip を解決 (origin 優先 = Vercel が build で読む ref)。current HEAD は使わない。
& git fetch origin $Branch 2>&1 | ForEach-Object { Write-Log $_ }
$Base = & git rev-parse --verify -q "refs/remotes/origin/$Branch"
if (-not $Base) { $Base = & git rev-parse --verify -q "refs/heads/$Branch" }
if (-not $Base) {
    Write-Log "ERROR: $Branch tip を origin/local どちらでも解決できない。commit 中止。"
    Send-PublishNtfy -Title "publish BASE FAIL $Date" -Body "$Branch の tip を解決できず data commit を作成できません。"
    exit 1
}
$Base = "$Base".Trim()
Write-Log "commit base = $Base ($Branch tip; current HEAD ではない)"

# base から新しい data commit を plumbing で作る。差分なしなら "" を、失敗なら $null を返す。
# working tree / index (.git/index) には一切触れず、専用 index (GIT_INDEX_FILE) のみ使う。
function New-DataCommitOnBase {
    param([string]$BaseCommit)
    $baseTree = "$(& git rev-parse "$BaseCommit^{tree}")".Trim()

    $PrivateIndex = Join-Path $GitDir "index.publish"
    Remove-Item $PrivateIndex -Force -ErrorAction SilentlyContinue
    $env:GIT_INDEX_FILE = $PrivateIndex
    try {
        # 専用 index を $Branch tip の tree から seed (対策B; read-tree HEAD ではなく base)。
        $rc = Invoke-GitRetry -Desc "read-tree base ($Branch)" -Action { & git read-tree $BaseCommit 2>&1 }
        if ($rc -ne 0) { Write-Log "ERROR: private index の seed に失敗 (base=$BaseCommit)。"; return $null }

        # 当日 JSON を results_csv から index へ直接 stage (working tree 非経由)。
        $staged = 0
        foreach ($p in $patterns) {
            $src = Join-Path $SrcDir $p
            if (-not (Test-Path $src)) { Write-Log "skip (not found): $p"; continue }
            $blob = "$(& git hash-object -w -- $src)".Trim()
            if (-not $blob) { Write-Log "ERROR: hash-object 失敗: $p"; return $null }
            & git update-index --add --cacheinfo "100644,$blob,$RelData/$p" 2>&1 | ForEach-Object { Write-Log $_ }
            if ($LASTEXITCODE -ne 0) { Write-Log "ERROR: update-index --add 失敗: $p"; return $null }
            Write-Log "staged: $p"
            $staged++
        }
        if ($staged -eq 0) {
            Write-Log "WARN: stage 対象 JSON が 1 件も無い ($Date)。commit をスキップ。"
            return ""
        }

        # KeepDays 世代整理: 専用 index の data/ を prefix 毎に最新 KeepDays 件だけ残す。
        # 削除は index-only (`git update-index --force-remove`) で working tree を触らない。
        $idxFiles = & git ls-files -- $RelData 2>$null
        foreach ($prefix in $prefixes) {
            $names = @($idxFiles | Where-Object { $_ -match "/$([regex]::Escape($prefix))\d{8}\.json$" } | Sort-Object -Descending)
            if ($names.Count -gt $KeepDays) {
                $names | Select-Object -Skip $KeepDays | ForEach-Object {
                    & git update-index --force-remove -- $_ 2>&1 | ForEach-Object { Write-Log $_ }
                    Write-Log "pruned: $_"
                }
            }
        }

        # results_csv/ 側の source file も (今日以外を) 世代整理。disk / git 履歴保護。
        if ($PurgeSource -and (Test-Path $SrcDir)) {
            foreach ($prefix in $prefixes) {
                $files = Get-ChildItem -Path $SrcDir -Filter "$prefix*.json" -File -ErrorAction SilentlyContinue |
                    Sort-Object Name -Descending
                if ($files.Count -gt $KeepDays) {
                    $files | Select-Object -Skip $KeepDays | ForEach-Object {
                        Remove-Item $_.FullName -Force -ErrorAction SilentlyContinue
                        Write-Log "pruned (source): $($_.Name)"
                    }
                }
            }
        }

        # 差分ゲート: private index の tree が base tree と同一なら冪等 no-op。
        $newTree = "$(& git write-tree)".Trim()
        if (-not $newTree) { Write-Log "ERROR: write-tree 失敗。"; return $null }
        if ($newTree -eq $baseTree) {
            Write-Log "data/ に差分なし (既に最新: $Branch)。commit はスキップ。"
            return ""
        }

        $msg = "chore(data): daily update $Date"
        $new = "$(& git commit-tree $newTree -p $BaseCommit -m $msg)".Trim()
        if (-not $new) { Write-Log "ERROR: commit-tree 失敗。"; return $null }
        Write-Log "commit-tree 作成: $new (parent=$BaseCommit, branch=$Branch)"
        return $new
    }
    finally {
        # 専用 index は用済み。既定 index / 作業ツリーは終始触っていない。
        Remove-Item Env:GIT_INDEX_FILE -ErrorAction SilentlyContinue
    }
}

$NewCommit = New-DataCommitOnBase -BaseCommit $Base
if ($null -eq $NewCommit) {
    Send-PublishNtfy -Title "publish COMMIT FAIL $Date" -Body "data commit の作成に失敗 (stage/commit-tree)。dashboard は更新されていません。"
    exit 1
}
if ($NewCommit -eq "") {
    # 差分なし = 既に最新。冪等 no-op。served を確認して終了。
    if (Test-PublishServed) { exit 0 } else { exit 1 }
}

if ($NoPush) {
    Write-Log "NoPush 指定: origin push を省略。作成コミット=$NewCommit (parent=$Base, branch=$Branch)。"
    Write-Log "  確認: git log --stat -1 $NewCommit  /  git ls-tree -r --name-only $NewCommit -- $RelData"
    exit 0
}

# 新コミットを直接 origin/$Branch へ push (local ref / worktree を介さない)。
function Push-CommitToBranch {
    param([string]$Commit)
    & git push origin "$($Commit):refs/heads/$Branch" 2>&1 | ForEach-Object { Write-Log $_ }
    return ($LASTEXITCODE -eq 0)
}

$pushed = Push-CommitToBranch -Commit $NewCommit
if (-not $pushed) {
    # non-fast-forward: origin が進んだ。取り直して rebuild + 1 回 retry (rebase 相当)。
    Write-Log "WARN: push rejected (likely non-fast-forward)。origin/$Branch を取り直して rebuild + retry。"
    & git fetch origin $Branch 2>&1 | ForEach-Object { Write-Log $_ }
    $Base2 = & git rev-parse --verify -q "refs/remotes/origin/$Branch"
    if ($Base2) {
        $Base2 = "$Base2".Trim()
        $NewCommit2 = New-DataCommitOnBase -BaseCommit $Base2
        if ($null -ne $NewCommit2 -and $NewCommit2 -ne "") {
            $NewCommit = $NewCommit2
            $pushed = Push-CommitToBranch -Commit $NewCommit
        }
        elseif ($NewCommit2 -eq "") {
            Write-Log "rebuild 後は差分なし (origin が既に最新)。push 不要。"
            $pushed = $true
        }
    }
}

if (-not $pushed) {
    Write-Log "ERROR: git push 失敗 (self-heal 後も未 push)。ダッシュボードは更新されません。"
    Send-PublishNtfy -Title "publish PUSH FAIL $Date" `
        -Body "dashboard data push failed (non-FF/self-heal exhausted): $Branch $Date (commit=$NewCommit)"
    exit 1
}

# --- 対策E: publish 後の検証 (origin/$Branch served >= generated) ----------
if (Test-PublishServed) {
    Write-Log "push/verify 完了: $Branch ($Date) commit=$NewCommit"
    exit 0
}
else {
    Write-Log "ERROR: publish 後の verify に失敗 (origin/$Branch が generated に届いていない)。"
    exit 1
}
