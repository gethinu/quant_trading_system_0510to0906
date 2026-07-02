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
    data/ 内に保持する各 JSON パターンの世代数 (既定 10)。

.PARAMETER NoPush
    commit までで push しない (ローカル検証用)。

.NOTES
    daily_pipeline.ps1 の最終 step から呼ばれる想定。単体実行も可。
    push 先: origin claude/monitor-webapp
#>

param(
    [string]$Date = "",
    [int]$KeepDays = 10,
    [switch]$NoPush = $false
)

$ErrorActionPreference = "Continue"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ScriptDir
if (-not $Date) { $Date = Get-Date -Format "yyyy-MM-dd" }
$DateCompact = $Date -replace "-", ""

$SrcDir = Join-Path $ProjectRoot "results_csv"
$DataDir = Join-Path $ProjectRoot "apps\dashboards\alpaca-next\data"
$Branch = "claude/monitor-webapp"

function Write-Log {
    param([string]$Message)
    Write-Host "[publish_data] $Message"
}

if (-not (Test-Path $DataDir)) {
    New-Item -ItemType Directory -Path $DataDir -Force | Out-Null
}

# 当日生成される JSON を data/ に日付付きのままコピー。
# pipeline_*.json = 新 schema (signal_pipeline/v1, 絞込フロー)。
# polygon_daily_coverage_*.json = 旧 schema (移行期は両方 push し dashboard で fallback)。
$patterns = @(
    "today_signals_$DateCompact.json",
    "pipeline_$DateCompact.json",
    "polygon_daily_coverage_$DateCompact.json",
    "narrative_$DateCompact.json"
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
$prefixes = @("today_signals_", "pipeline_", "polygon_daily_coverage_", "narrative_")
foreach ($prefix in $prefixes) {
    $files = Get-ChildItem -Path $DataDir -Filter "$prefix*.json" -File |
        Sort-Object Name -Descending
    if ($files.Count -gt $KeepDays) {
        $files | Select-Object -Skip $KeepDays | ForEach-Object {
            Remove-Item $_.FullName -Force
            Write-Log "pruned: $($_.Name)"
        }
    }
}

# git commit + push (data/ のみ対象。他の作業差分は巻き込まない)
Set-Location $ProjectRoot
$RelData = "apps/dashboards/alpaca-next/data"
& git add -- $RelData 2>&1 | ForEach-Object { Write-Log $_ }

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

& git push origin $Branch 2>&1 | ForEach-Object { Write-Log $_ }
if ($LASTEXITCODE -ne 0) {
    Write-Log "ERROR: git push 失敗 (exit=$LASTEXITCODE)"
    exit 1
}

Write-Log "push 完了: $Branch ($Date)"
exit 0
