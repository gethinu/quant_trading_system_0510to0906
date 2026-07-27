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

.PARAMETER AutoLatest
    -Date を無視し、results_csv/today_signals_*.json の最新生成日を自動検出して
    publish する self-heal モード。冪等 (data/ が既に最新なら差分ゼロで exit 0)。
    06:00 の wrapper (daily_main_follow.ps1) が途中で死んで dashboard publish を
    取りこぼしても、独立した catch-up task から呼べば取り戻せる。

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
    [bool]$RefreshAccount = $true
)

$ErrorActionPreference = "Continue"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ScriptDir

$SrcDir = Join-Path $ProjectRoot "results_csv"
$DataDir = Join-Path $ProjectRoot "apps\dashboards\alpaca-next\data"
$Branch = "claude/monitor-webapp"

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
}
if (-not $Date) { $Date = Get-Date -Format "yyyy-MM-dd" }
$DateCompact = $Date -replace "-", ""

function Write-Log {
    param([string]$Message)
    Write-Host "[publish_data] $Message"
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
if ($RefreshAccount) {
    $py = if ($env:QTS_PYTHON) { $env:QTS_PYTHON } else { "python" }
    $ledgerScript = Join-Path $ProjectRoot "scripts\build_exit_ledger.py"
    $snapScript = Join-Path $ProjectRoot "scripts\export_alpaca_snapshot.py"

    if (Test-Path $ledgerScript) {
        Write-Log "[account] exit 台帳を再構成 (build_exit_ledger.py --date $Date)"
        & $py $ledgerScript --date $Date 2>&1 | ForEach-Object { Write-Log $_ }
        # exit 3 = 未計測を検知 (--fail-on-unmeasured 指定時のみ)。ここでは通知に留める。
        if ($LASTEXITCODE -ne 0) { Write-Log "[account] WARN: build_exit_ledger exit=$LASTEXITCODE" }
    }
    if (Test-Path $snapScript) {
        Write-Log "[account] Alpaca snapshot を再生成 (export_alpaca_snapshot.py --date $Date)"
        & $py $snapScript --date $Date 2>&1 | ForEach-Object { Write-Log $_ }
        if ($LASTEXITCODE -ne 0) { Write-Log "[account] WARN: export_alpaca_snapshot exit=$LASTEXITCODE" }
    }
}

# 当日生成される JSON を data/ に日付付きのままコピー。
# pipeline_*.json = 新 schema (signal_pipeline/v1, 絞込フロー)。
# polygon_daily_coverage_*.json = 旧 schema (移行期は両方 push し dashboard で fallback)。
$patterns = @(
    "today_signals_$DateCompact.json",
    "pipeline_$DateCompact.json",
    "polygon_daily_coverage_$DateCompact.json",
    "narrative_$DateCompact.json",
    # Alpaca paper 口座の read-only スナップショット (scripts/export_alpaca_snapshot.py)。
    # monitor の Alpaca タブがこれを読む。無い日は skip される (copy loop で握り潰し)。
    "alpaca_snapshot_$DateCompact.json"
)

$copied = 0
foreach ($p in $patterns) {
    $src = Join-Path $SrcDir $p
    if (Test-Path $src) {
        Copy-Item $src (Join-Path $DataDir $p) -Force
        Write-Log "copied: $p"
        $copied++
    }
    else {
        Write-Log "skip (not found): $p"
    }
}

if ($copied -eq 0) {
    Write-Log "WARN: コピー対象 JSON が 1 件も無い ($Date)。commit をスキップ。"
    exit 2
}

# 世代整理: 各 prefix について日付付きファイルを最新 KeepDays 件だけ残す。
# 2026-07-02 fix: filename は today_signals_YYYYMMDD.json 形式のため
# `Sort-Object Name -Descending` は lexical でも date 降順と等価 (8桁 zero-padded)。
# 削除は Remove-Item ではなく `git rm` で explicit に (commit の diff に載せる)。
$prefixes = @("today_signals_", "pipeline_", "polygon_daily_coverage_", "narrative_", "alpaca_snapshot_", "exit_ledger_")
Set-Location $ProjectRoot
$RelData = "apps/dashboards/alpaca-next/data"
foreach ($prefix in $prefixes) {
    $files = Get-ChildItem -Path $DataDir -Filter "$prefix*.json" -File |
        Sort-Object Name -Descending
    if ($files.Count -gt $KeepDays) {
        $files | Select-Object -Skip $KeepDays | ForEach-Object {
            # git rm で削除 (git 追跡下なら stage も同時に done)。
            $rel = Join-Path $RelData $_.Name
            & git rm -f --quiet -- $rel 2>&1 | ForEach-Object { Write-Log $_ }
            if ($LASTEXITCODE -ne 0) {
                # 追跡外の file (fresh copy 直前など): 生 Remove-Item に fallback
                Remove-Item $_.FullName -Force -ErrorAction SilentlyContinue
            }
            Write-Log "pruned: $($_.Name)"
        }
    }
}

# results_csv/ 側の source file も (今日以外を) 世代整理。disk / git 履歴保護。
if ($PurgeSource -and (Test-Path $SrcDir)) {
    foreach ($prefix in $prefixes) {
        $files = Get-ChildItem -Path $SrcDir -Filter "$prefix*.json" -File |
            Sort-Object Name -Descending
        if ($files.Count -gt $KeepDays) {
            $files | Select-Object -Skip $KeepDays | ForEach-Object {
                Remove-Item $_.FullName -Force -ErrorAction SilentlyContinue
                Write-Log "pruned (source): $($_.Name)"
            }
        }
    }
}

# git commit + push (data/ のみ対象。他の作業差分は巻き込まない)。
# `-A` で削除も一緒に stage する (git rm した分は既に staged だが安全側)。
& git add -A -- $RelData 2>&1 | ForEach-Object { Write-Log $_ }

# staged 差分が無ければ push 不要 (--allow-empty で毎回コミットは repo を汚すので回避)
& git diff --cached --quiet -- $RelData
if ($LASTEXITCODE -eq 0) {
    Write-Log "data/ に差分なし。commit/push をスキップ。"
    exit 0
}

$msg = "chore(data): daily update $Date"
& git commit -m $msg 2>&1 | ForEach-Object { Write-Log $_ }
if ($LASTEXITCODE -ne 0) {
    Write-Log "ERROR: git commit 失敗 (exit=$LASTEXITCODE)"
    exit 1
}

if ($NoPush) {
    Write-Log "NoPush 指定: commit のみで終了。"
    exit 0
}

# 2026-07-14 fix (root cause B): local $Branch が origin より遅れていると push が
# non-fast-forward で reject され、data commit は溜まる一方で Vercel には永遠に届かず
# ダッシュが凍結する (07-12 で停止していた原因)。しかも daily_main_follow は
# generate 側の exit code しか見ないため「push 失敗」が silent に握り潰されていた。
# 対策: push が失敗したら fetch + rebase (autostash で dirty tree を退避) して origin
# 先頭に data commit を載せ替え、1 回だけ retry する。それでも駄目なら LOUD に fail。
function Invoke-PushWithRebaseHeal {
    & git push origin $Branch 2>&1 | ForEach-Object { Write-Log $_ }
    if ($LASTEXITCODE -eq 0) { return $true }

    Write-Log "WARN: push rejected (likely non-fast-forward). fetch + rebase-onto-origin で自己修復を試行。"
    & git fetch origin $Branch 2>&1 | ForEach-Object { Write-Log $_ }
    # autostash で未コミット作業ツリーを退避しつつ、ローカルの data commit を origin 先頭へ rebase。
    & git -c rebase.autostash=true rebase "origin/$Branch" 2>&1 | ForEach-Object { Write-Log $_ }
    if ($LASTEXITCODE -ne 0) {
        Write-Log "ERROR: rebase 失敗 (conflict?)。abort して手動対応が必要。"
        & git rebase --abort 2>&1 | ForEach-Object { Write-Log $_ }
        return $false
    }
    Write-Log "rebase 成功。origin 先頭へ載せ替え済み。push を retry。"
    & git push origin $Branch 2>&1 | ForEach-Object { Write-Log $_ }
    return ($LASTEXITCODE -eq 0)
}

if (-not (Invoke-PushWithRebaseHeal)) {
    Write-Log "ERROR: git push 失敗 (self-heal 後も未 push)。ダッシュボードは更新されません。"
    # LOUD 通知: silent success を避ける。NTFY_TOPIC があれば WARN を飛ばす。
    if ($env:NTFY_TOPIC) {
        $base = if ($env:NTFY_URL) { $env:NTFY_URL.TrimEnd('/') } else { "https://ntfy.sh" }
        try {
            $h = @{ "X-Title" = "publish_data PUSH FAIL $Date"; "X-Priority" = "5"; "X-Tags" = "warning" }
            Invoke-RestMethod -Uri "$base/$($env:NTFY_TOPIC)" -Method Post -Headers $h `
                -Body "dashboard data push failed (non-FF/self-heal exhausted): $Branch $Date" | Out-Null
        }
        catch { Write-Log "ntfy WARN 送信失敗: $_" }
    }
    exit 1
}

Write-Log "push 完了: $Branch ($Date)"
exit 0
