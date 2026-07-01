<#
.SYNOPSIS
    Polygon.io Grouped Daily で sys1-7 gate 生存率を日次モニタリングする
    Windows Task Scheduler 用ラッパー。

.DESCRIPTION
    Task 名 'QuantTrading_PolygonDailyMonitor' から毎日 06:00 JST に呼ばれる想定。
    venv activate → daily_polygon_monitor.py 実行 → exit code check → log 追記。
    parent runbook: docs/HUMAN_TASK_polygon_daily_monitor_20260701.md

.PARAMETER Date
    対象取引日 (YYYY-MM-DD)。未指定なら前営業日 (python 側 default)。

.PARAMETER DryRun
    Polygon fetch をスキップ (skeleton 動作確認用)。

.EXAMPLE
    .\scripts\daily_polygon_monitor.ps1
    .\scripts\daily_polygon_monitor.ps1 -DryRun
    .\scripts\daily_polygon_monitor.ps1 -Date 2026-06-30

.NOTES
    参考パターン: scripts/daily_auto_run.ps1
    Exit codes: 0=ok, 2=warn (閾値割れ), 1=error
#>

param(
    [string]$Date = "",
    [switch]$DryRun = $false
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ScriptDir
$LogDir = Join-Path $ProjectRoot "logs"
$LogFile = Join-Path $LogDir "polygon_monitor_$(Get-Date -Format 'yyyyMMdd_HHmmss').log"

if (-not (Test-Path $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
}

function Write-Log {
    param([string]$Message)
    $Timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $Line = "[$Timestamp] $Message"
    Write-Host $Line
    Add-Content -Path $LogFile -Value $Line -Encoding UTF8
}

try {
    Write-Log "========================================="
    Write-Log "Polygon Daily Monitor 開始"
    Write-Log "ProjectRoot: $ProjectRoot"
    Write-Log "LogFile:     $LogFile"
    Write-Log "========================================="

    # venv activate (存在する場合のみ、存在しなければ system python)
    $VenvPath = Join-Path $ProjectRoot "venv\Scripts\Activate.ps1"
    if (Test-Path $VenvPath) {
        Write-Log "venv activate: $VenvPath"
        & $VenvPath
    }
    else {
        Write-Log "WARN: venv not found, fallback to system python"
    }

    # python script kick
    $PyScript = Join-Path $ProjectRoot "scripts\daily_polygon_monitor.py"
    if (-not (Test-Path $PyScript)) {
        throw "python script not found: $PyScript"
    }

    $Args = @($PyScript)
    if ($Date) { $Args += @("--date", $Date) }
    if ($DryRun) { $Args += @("--dry-run") }
    $Args += @("--output-dir", (Join-Path $ProjectRoot "results_csv"))

    Write-Log "python $($Args -join ' ')"
    $Output = & python @Args 2>&1
    $ExitCode = $LASTEXITCODE
    $Output | ForEach-Object { Write-Log $_ }

    Write-Log "python exit code: $ExitCode"

    switch ($ExitCode) {
        0 { Write-Log "OK: coverage 閾値割れなし" }
        2 { Write-Log "WARN: 閾値割れ検知 (JSON 内 status=warn を確認)" }
        default { Write-Log "ERROR: 監視 script が失敗 (exit=$ExitCode)" }
    }

    Write-Log "========================================="
    Write-Log "Polygon Daily Monitor 終了 (exit=$ExitCode)"
    Write-Log "========================================="
    exit $ExitCode
}
catch {
    Write-Log "========================================="
    Write-Log "FATAL: $_"
    Write-Log $_.ScriptStackTrace
    Write-Log "========================================="
    exit 1
}
