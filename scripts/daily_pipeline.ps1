<#
.SYNOPSIS
    当日シグナル日次パイプライン (cache -> signals -> coverage -> publish) を
    1 スクリプトで通す Windows Task Scheduler 用 orchestrator。

.DESCRIPTION
    Phase 1 事業化: 「signal 生成 -> Vercel 反映用 JSON -> ntfy/email 配信」を無人化する。
    既存 Task 'QuantTrading_PolygonDailyMonitor' の呼び先を本スクリプトに差し替えるだけで
    4 段パイプラインに拡張される。各 step は idempotent (cache/JSON 上書き) で再実行安全。

    段:
      1. [cache]    scripts/cache_daily_polygon.py  --start/--end {date}   (Polygon fetch)
      2. [signals]  apps/app_today_signals.py --headless --output-json ...  (シグナル生成)
      3. [coverage] scripts/daily_polygon_monitor.py --date {date}          (gate 生存率)
      4. [publish]  scripts/publish_signals.py --input {json}          (ntfy + email 配信)

    step 失敗は log に残し、パイプラインは可能な範囲で継続する (signals が出れば coverage/publish は進む)。
    いずれかが失敗したら最後に ntfy (NTFY_TOPIC) へ WARN を送る。

.PARAMETER Date
    対象日 (YYYY-MM-DD)。未指定なら今日 (ローカル)。

.PARAMETER Symbols
    signals step の対象シンボル (comma 区切り)。未指定なら full universe。

.PARAMETER SkipCache
    step1 (Polygon fetch) をスキップ。cache が別 task で更新済のとき用。

.PARAMETER SkipPublish
    step4 (ntfy/email 配信) をスキップ。

.PARAMETER DryRunPublish
    step4 を送信せず payload 検証のみ (--dry-run)。

.PARAMETER SkipLatestCheck
    signals step で rolling cache の最新営業日チェックを skip (cache 未更新環境の adhoc 用)。

.EXAMPLE
    .\scripts\daily_pipeline.ps1
    .\scripts\daily_pipeline.ps1 -Date 2026-07-01 -SkipCache
    .\scripts\daily_pipeline.ps1 -DryRunPublish -Symbols AAPL,SPY -SkipLatestCheck

.NOTES
    Task Scheduler 1-liner (既存 task の Action を書き換え):
      Program:   powershell.exe
      Arguments: -NoProfile -ExecutionPolicy Bypass -File "C:\Repos\quant_trading_system_0510to0906\scripts\daily_pipeline.ps1"
    Exit codes: 0=全 OK, 2=一部 step 失敗 (WARN 送信済), 1=致命的エラー
#>

param(
    [string]$Date = "",
    [string]$Symbols = "",
    [switch]$SkipCache = $false,
    [switch]$SkipPublish = $false,
    [switch]$DryRunPublish = $false,
    [switch]$SkipLatestCheck = $false
)

$ErrorActionPreference = "Continue"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ScriptDir
$LogDir = Join-Path $ProjectRoot "logs"

if (-not $Date) { $Date = Get-Date -Format "yyyy-MM-dd" }
$DateCompact = $Date -replace "-", ""
$LogFile = Join-Path $LogDir "daily_pipeline_$(Get-Date -Format 'yyyyMMdd_HHmmss').log"
$SignalsJson = Join-Path $ProjectRoot "results_csv\today_signals_$DateCompact.json"

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

# step 実行ヘルパ: python script を叩き exit code を返す。step 名でログ整形。
function Invoke-Step {
    param(
        [string]$Name,
        [string[]]$PyArgs
    )
    Write-Log "----- [$Name] 開始 -----"
    Write-Log "python $($PyArgs -join ' ')"
    $out = & python @PyArgs 2>&1
    $code = $LASTEXITCODE
    $out | ForEach-Object { Write-Log $_ }
    Write-Log "----- [$Name] 終了 (exit=$code) -----"
    return $code
}

# 失敗時 ntfy WARN (best-effort、NTFY_TOPIC 未設定なら黙ってスキップ)
function Send-Warn {
    param([string]$Text)
    $topic = $env:NTFY_TOPIC
    if (-not $topic) {
        Write-Log "WARN 通知スキップ (NTFY_TOPIC 未設定)"
        return
    }
    $base = if ($env:NTFY_URL) { $env:NTFY_URL.TrimEnd('/') } else { "https://ntfy.sh" }
    try {
        $headers = @{ "X-Title" = "daily_pipeline WARN $Date"; "X-Priority" = "5"; "X-Tags" = "warning" }
        Invoke-RestMethod -Uri "$base/$topic" -Method Post -Headers $headers -Body $Text | Out-Null
        Write-Log "ntfy WARN 送信済"
    }
    catch {
        Write-Log "ntfy WARN 送信失敗: $_"
    }
}

$Failures = @()

try {
    Write-Log "========================================="
    Write-Log "Daily Pipeline 開始  Date=$Date"
    Write-Log "ProjectRoot: $ProjectRoot"
    Write-Log "SignalsJson: $SignalsJson"
    Write-Log "========================================="

    # venv activate (存在すれば)
    $VenvPath = Join-Path $ProjectRoot "venv\Scripts\Activate.ps1"
    if (Test-Path $VenvPath) {
        Write-Log "venv activate: $VenvPath"
        & $VenvPath
    }
    else {
        Write-Log "WARN: venv not found, fallback to system python"
    }

    Set-Location $ProjectRoot

    # --- Step 1: cache (Polygon Grouped Daily fetch) ---------------------
    if ($SkipCache) {
        Write-Log "[cache] SkipCache 指定によりスキップ"
    }
    else {
        $cacheArgs = @(
            (Join-Path $ProjectRoot "scripts\cache_daily_polygon.py"),
            "--start", $Date, "--end", $Date
        )
        $c = Invoke-Step -Name "cache" -PyArgs $cacheArgs
        if ($c -ne 0) { $Failures += "cache(exit=$c)" }
    }

    # --- Step 2: signals (headless JSON 生成) ----------------------------
    $sigArgs = @(
        (Join-Path $ProjectRoot "apps\app_today_signals.py"),
        "--headless",
        "--output-json", $SignalsJson,
        "--date", $Date
    )
    if ($Symbols) { $sigArgs += @("--symbols", $Symbols) }
    if ($SkipLatestCheck) { $sigArgs += @("--skip-latest-check") }
    $s = Invoke-Step -Name "signals" -PyArgs $sigArgs
    if ($s -ne 0) { $Failures += "signals(exit=$s)" }

    # --- Step 3: coverage (gate 生存率モニタ、既存) ----------------------
    $covArgs = @(
        (Join-Path $ProjectRoot "scripts\daily_polygon_monitor.py"),
        "--date", $Date,
        "--output-dir", (Join-Path $ProjectRoot "results_csv")
    )
    $v = Invoke-Step -Name "coverage" -PyArgs $covArgs
    # coverage は exit=2 が「閾値割れ WARN」で失敗ではない
    if ($v -eq 1) { $Failures += "coverage(exit=1)" }
    elseif ($v -eq 2) { Write-Log "[coverage] WARN: gate 閾値割れ検知 (JSON status=warn)" }

    # --- Step 4: publish (ntfy primary + email backup) -----------------
    if ($SkipPublish) {
        Write-Log "[publish] SkipPublish 指定によりスキップ"
    }
    elseif (-not (Test-Path $SignalsJson)) {
        Write-Log "[publish] signals JSON が無いためスキップ: $SignalsJson"
        $Failures += "publish(no_signals_json)"
    }
    else {
        $pubArgs = @(
            (Join-Path $ProjectRoot "scripts\publish_signals.py"),
            "--input", $SignalsJson
        )
        if ($DryRunPublish) { $pubArgs += @("--dry-run") }
        $p = Invoke-Step -Name "publish" -PyArgs $pubArgs
        if ($p -eq 1 -or $p -eq 2) { $Failures += "publish(exit=$p)" }
    }

    # --- 集計 -----------------------------------------------------------
    Write-Log "========================================="
    if ($Failures.Count -eq 0) {
        Write-Log "Daily Pipeline 完了: 全 step OK"
        Write-Log "========================================="
        exit 0
    }
    else {
        $summary = $Failures -join ", "
        Write-Log "Daily Pipeline 完了 (一部失敗): $summary"
        Write-Log "========================================="
        Send-Warn -Text "step 失敗: $summary  (log: $LogFile)"
        exit 2
    }
}
catch {
    Write-Log "========================================="
    Write-Log "FATAL: $_"
    Write-Log $_.ScriptStackTrace
    Write-Log "========================================="
    Send-Warn -Text "FATAL: $_"
    exit 1
}
