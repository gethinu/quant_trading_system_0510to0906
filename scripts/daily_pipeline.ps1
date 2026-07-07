<#
.SYNOPSIS
    当日シグナル日次パイプライン (cache -> signals -> coverage -> publish -> paper_orders) を
    1 スクリプトで通す Windows Task Scheduler 用 orchestrator。

.DESCRIPTION
    段:
      1. [cache]       scripts/cache_daily_polygon.py  --start/--end {date}
      2. [signals]     apps/app_today_signals.py --headless --output-json ...
      3. [coverage]    scripts/daily_polygon_monitor.py --date {date}
      4. [narrator]    scripts/generate_narrative.py --signals {json} --output ...
      5. [publish]     scripts/publish_signals.py --input {json}
      5b.[paper_orders] scripts/paper_trading_dryrun.py --signals-json ... (default)
                         もしくは paper_trading_submit.py --confirm --yes (AutoSubmitPaper 時)
      5c.[exit_check]  scripts/paper_exit_check.py (default dry-run, --confirm で本発注)
                         現 position と SYSTEM_TRADE_RULES の照合で exit order 案を生成 / 発注
      6. [vercel]      scripts/publish_data_to_vercel.ps1

.PARAMETER Date
    対象日 (YYYY-MM-DD)。未指定なら今日 (ローカル)。

.PARAMETER Symbols
    signals step の対象シンボル (comma 区切り)。未指定なら full universe。

.PARAMETER SkipCache
    step1 (Polygon fetch) をスキップ。cache が別 task で更新済のとき用。

.PARAMETER SkipNarrator
    narrator step (AI 解説生成) をスキップ。

.PARAMETER SkipPublish
    publish step (ntfy/email 配信) をスキップ。

.PARAMETER DryRunPublish
    publish を送信せず payload 検証のみ (--dry-run)。

.PARAMETER SkipLatestCheck
    signals step で rolling cache の最新営業日チェックを skip。

.PARAMETER SkipPaperOrders
    paper_orders step (Alpaca Paper 発注 intent 生成) をスキップ。

.PARAMETER SkipExitCheck
    exit_check step (Alpaca Paper position の exit rule 照合 / 発注) をスキップ。

.PARAMETER AutoSubmitPaper
    paper_orders (entry) と exit_check (exit) の両 step で
    **実際に Paper 口座へ発注**する。無指定は dry-run のみ (JSON 出力)。
    Task Scheduler の Action に含めない限り絶対に発注しない。
    **live 口座 (実マネー) は本 pipeline では扱わない。**

.PARAMETER Tier
    paper_orders の tier。small=$1k / medium=$10k / large=$100k。
    未指定なら env ALPACA_TIER、無ければ "small"。

.EXAMPLE
    .\scripts\daily_pipeline.ps1
    .\scripts\daily_pipeline.ps1 -Date 2026-07-01 -SkipCache
    # paper 実発注 (user が明示 opt-in):
    .\scripts\daily_pipeline.ps1 -Date 2026-07-01 -SkipCache -AutoSubmitPaper -Tier small

.NOTES
    Exit codes: 0=全 OK, 2=一部 step 失敗 (WARN 送信済), 1=致命的エラー
#>

param(
    [string]$Date = "",
    [string]$Symbols = "",
    [switch]$SkipCache = $false,
    [switch]$SkipNarrator = $false,
    [switch]$SkipPublish = $false,
    [switch]$DryRunPublish = $false,
    [switch]$SkipLatestCheck = $false,
    [switch]$SkipPaperOrders = $false,
    [switch]$SkipExitCheck = $false,
    [switch]$AutoSubmitPaper = $false,
    [string]$Tier = ""
)

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ScriptDir

# --- .env auto-load ------------------------------------------------------
$EnvFile = Join-Path $ProjectRoot ".env"
if (Test-Path $EnvFile) {
    Get-Content $EnvFile | ForEach-Object {
        $line = $_
        if ($line -match '^\s*#') { return }
        if ($line -match '^\s*([^#=\s]+)\s*=\s*(.*)$') {
            $k = $matches[1].Trim()
            $v = $matches[2].Trim()
            if ($v.Length -ge 2) {
                if (($v.StartsWith('"') -and $v.EndsWith('"')) -or
                    ($v.StartsWith("'") -and $v.EndsWith("'"))) {
                    $v = $v.Substring(1, $v.Length - 2)
                }
            }
            if (-not (Test-Path "Env:$k")) {
                try {
                    Set-Item -Path "Env:$k" -Value $v -ErrorAction Stop
                }
                catch {}
            }
        }
    }
}

$ErrorActionPreference = "Continue"
$LogDir = Join-Path $ProjectRoot "logs"

if (-not $Date) { $Date = Get-Date -Format "yyyy-MM-dd" }
$DateCompact = $Date -replace "-", ""
$LogFile = Join-Path $LogDir "daily_pipeline_$(Get-Date -Format 'yyyyMMdd_HHmmss').log"
$SignalsJson = Join-Path $ProjectRoot "results_csv\today_signals_$DateCompact.json"
$NarrativeJson = Join-Path $ProjectRoot "results_csv\narrative_$DateCompact.json"
$PaperOrdersJson = Join-Path $ProjectRoot "results_csv\paper_orders_$DateCompact.json"
$ExitOrdersJson = Join-Path $ProjectRoot "results_csv\exit_orders_$DateCompact.json"

# Tier 解決: CLI 引数 > env ALPACA_TIER > "small"
if (-not $Tier) {
    if ($env:ALPACA_TIER) { $Tier = $env:ALPACA_TIER } else { $Tier = "small" }
}

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

    $VenvPath = Join-Path $ProjectRoot "venv\Scripts\Activate.ps1"
    if (Test-Path $VenvPath) {
        Write-Log "venv activate: $VenvPath"
        & $VenvPath
    }
    else {
        Write-Log "WARN: venv not found, fallback to system python"
    }

    Set-Location $ProjectRoot

    # --- Step 1: cache ---------------------------------------------------
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

    # --- Step 2: signals -------------------------------------------------
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

    # --- Step 3: coverage ------------------------------------------------
    $covArgs = @(
        (Join-Path $ProjectRoot "scripts\daily_polygon_monitor.py"),
        "--date", $Date,
        "--output-dir", (Join-Path $ProjectRoot "results_csv")
    )
    $v = Invoke-Step -Name "coverage" -PyArgs $covArgs
    if ($v -eq 1) { $Failures += "coverage(exit=1)" }
    elseif ($v -eq 2) { Write-Log "[coverage] WARN: gate 閾値割れ検知 (JSON status=warn)" }

    # --- Step 4: narrator (optional / fail-safe) ------------------------
    if ($SkipNarrator) {
        Write-Log "[narrator] SkipNarrator 指定によりスキップ"
    }
    elseif (-not (Test-Path $SignalsJson)) {
        Write-Log "[narrator] signals JSON が無いためスキップ: $SignalsJson"
    }
    else {
        $narrArgs = @(
            (Join-Path $ProjectRoot "scripts\generate_narrative.py"),
            "--signals", $SignalsJson,
            "--output", $NarrativeJson
        )
        $n = Invoke-Step -Name "narrator" -PyArgs $narrArgs
        if ($n -ne 0) { Write-Log "[narrator] WARN: 生成失敗 (exit=$n)、narrative 無しで継続" }
    }

    # --- Step 5: publish (ntfy primary + email backup) ------------------
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

    # --- Step 5b: paper_orders (Alpaca Paper 発注 intent 生成 / 実発注) --
    # default = dry-run (JSON 出力のみ、実発注なし)。-AutoSubmitPaper で初めて
    # paper 口座へ送信される。live 口座 (実マネー) は本 pipeline では扱わない。
    if ($SkipPaperOrders) {
        Write-Log "[paper_orders] SkipPaperOrders 指定によりスキップ"
    }
    elseif (-not (Test-Path $SignalsJson)) {
        Write-Log "[paper_orders] signals JSON が無いためスキップ: $SignalsJson"
    }
    else {
        if ($AutoSubmitPaper) {
            Write-Log "[paper_orders] AutoSubmitPaper=ON  tier=$Tier  → paper 口座へ発注します"
            $poArgs = @(
                (Join-Path $ProjectRoot "scripts\paper_trading_submit.py"),
                "--signals-json", $SignalsJson,
                "--tier", $Tier,
                "--output-json", $PaperOrdersJson,
                "--confirm", "--yes"
            )
            $po = Invoke-Step -Name "paper_orders_submit" -PyArgs $poArgs
            if ($po -eq 1 -or $po -eq 2) { $Failures += "paper_orders(exit=$po)" }
        }
        else {
            Write-Log "[paper_orders] dry-run (submit skipped: autosubmit not enabled)  tier=$Tier"
            $poArgs = @(
                (Join-Path $ProjectRoot "scripts\paper_trading_dryrun.py"),
                "--signals-json", $SignalsJson,
                "--tier", $Tier,
                "--output-json", $PaperOrdersJson
            )
            $po = Invoke-Step -Name "paper_orders_dryrun" -PyArgs $poArgs
            if ($po -ne 0) { $Failures += "paper_orders_dryrun(exit=$po)" }
        }
    }

    # --- Step 5c: exit_check (Alpaca Paper position の exit rule 照合 / 発注) ---
    # 現 position を Alpaca から pull し、SYSTEM_TRADE_RULES と照合して
    # (a) protection (stop / trailing / take_profit) が未発注なら発注
    # (b) time-based (S2/S3/S5/S6) / SPY breakout (S7) 判定で成行 close order 生成
    # を実行する。default = dry-run (JSON 出力のみ)、-AutoSubmitPaper で実発注。
    # entry step (5b) と同じ opt-in flag をシェアする (両方 dry-run か両方本発注)。
    if ($SkipExitCheck) {
        Write-Log "[exit_check] SkipExitCheck 指定によりスキップ"
    }
    else {
        if ($AutoSubmitPaper) {
            Write-Log "[exit_check] AutoSubmitPaper=ON  → paper 口座へ exit 発注を試行します"
            $ecArgs = @(
                (Join-Path $ProjectRoot "scripts\paper_exit_check.py"),
                "--date", $Date,
                "--output-json", $ExitOrdersJson,
                "--confirm", "--yes"
            )
        }
        else {
            Write-Log "[exit_check] dry-run (submit skipped: autosubmit not enabled)"
            $ecArgs = @(
                (Join-Path $ProjectRoot "scripts\paper_exit_check.py"),
                "--date", $Date,
                "--output-json", $ExitOrdersJson
            )
        }
        $ec = Invoke-Step -Name "exit_check" -PyArgs $ecArgs
        if ($ec -eq 1) { $Failures += "exit_check(exit=1)" }
        elseif ($ec -eq 2) { $Failures += "exit_check(safety_abort)" }
    }

    # --- Step 6: publish data to Vercel (git commit + push) ------------
    if (-not (Test-Path $SignalsJson)) {
        Write-Log "[vercel] signals JSON が無いため data push をスキップ"
    }
    else {
        Write-Log "----- [vercel] 開始 -----"
        $vercelScript = Join-Path $ProjectRoot "scripts\publish_data_to_vercel.ps1"
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $vercelScript -Date $Date 2>&1 |
            ForEach-Object { Write-Log $_ }
        $vc = $LASTEXITCODE
        Write-Log "----- [vercel] 終了 (exit=$vc) -----"
        if ($vc -eq 1) { $Failures += "vercel_publish(exit=1)" }
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
