<#
.SYNOPSIS
    main-following daily pipeline wrapper.

.DESCRIPTION
    Runs the daily pipeline against the LATEST origin/main code, then publishes
    the resulting data/ from the PRIMARY (monitor-webapp) worktree.

    Why a wrapper: signal generation should track latest main, but the Vercel
    publish (publish_data_to_vercel.ps1) hard-codes branch claude/monitor-webapp
    and commits ProjectRoot's HEAD -- so it must run from the primary tree, not
    from a main worktree. This wrapper separates the two concerns:

      1. Load the PRIMARY .env (creds + NTFY) -- this worktree has none.
      2. git fetch + merge origin/main into this branch (claude/daily-main-follow
         = origin/main + the -SkipVercel patch + this wrapper). Merge (not reset
         --hard) keeps the 2-file patch on top. Conflict -> abort + last-known-good.
      3. Run scripts\daily_pipeline.ps1 -SkipVercel from THIS worktree (latest
         main code) -> writes to the shared (junctioned) results_csv, no publish.
      4. Run the PRIMARY tree's publish_data_to_vercel.ps1 (commit + push to
         claude/monitor-webapp) so Vercel reflects the fresh data.

    PAPER ONLY. Never live. Keep this file ASCII-only; Japanese output belongs to
    the Python side to avoid the cp932 console codepage issue.

.PARAMETER Date
    Target date YYYY-MM-DD (default: today local).

.PARAMETER DryRun
    Follow-main + a fast NON-CLOBBERING generation smoke (subset symbols, skips
    cache/narrator/paper/exit, dry ntfy) + LOG (not execute) the publish command.
    Use a throwaway -Date so the published today_signals is not overwritten.

.PARAMETER PrimaryRoot
    The monitor-webapp worktree that owns .env and the publish script.

.PARAMETER DrySymbols
    Symbol subset used only in -DryRun to keep the generation smoke fast.

.NOTES
    Exit code propagates from the generation step (0 ok / 2 partial).
#>
param(
    [string]$Date = "",
    [switch]$DryRun = $false,
    [string]$PrimaryRoot = "C:\Repos\quant_trading_system_0510to0906",
    [string]$DrySymbols = "AAPL,MSFT,NVDA",
    [switch]$AutoSubmitPaper = $false,
    [string]$Tier = ""
)

$ErrorActionPreference = "Continue"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$WorktreeRoot = Split-Path -Parent $ScriptDir
$LogDir = Join-Path $WorktreeRoot "logs"
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }
$Stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$Log = Join-Path $LogDir "daily_main_follow_$Stamp.log"

function Write-L {
    param([string]$m)
    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $m
    Write-Host $line
    Add-Content -Path $Log -Value $line -Encoding UTF8
}

Write-L "=== daily_main_follow start (DryRun=$DryRun) ==="
Write-L "WorktreeRoot=$WorktreeRoot"
Write-L "PrimaryRoot =$PrimaryRoot"

# --- 1) load PRIMARY .env (creds + NTFY) into this process env -----------
$EnvFile = Join-Path $PrimaryRoot ".env"
if (Test-Path $EnvFile) {
    Write-L "loading .env: $EnvFile"
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
            try { Set-Item -Path "Env:$k" -Value $v -ErrorAction Stop } catch {}
        }
    }
}
else {
    Write-L "WARN: primary .env not found at $EnvFile (creds/NTFY may be missing)"
}
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

# hard paper guard (belt and suspenders; python asserts paper too)
if ($env:ALPACA_PAPER -and ($env:ALPACA_PAPER.ToLower() -notin @("1", "true", "yes", "y", "on"))) {
    Write-L "SAFETY ABORT: ALPACA_PAPER is not truthy ($($env:ALPACA_PAPER)); refusing to run."
    exit 2
}

# --- 2) follow latest origin/main (merge; keep local patch) --------------
Write-L "--- git fetch + merge origin/main ---"
& git -C $WorktreeRoot fetch origin --quiet 2>&1 | ForEach-Object { Write-L "  $_" }
& git -C $WorktreeRoot merge --no-edit origin/main 2>&1 | ForEach-Object { Write-L "  $_" }
if ($LASTEXITCODE -ne 0) {
    Write-L "WARN: merge origin/main failed -> abort merge, continue on current code (last-known-good)"
    & git -C $WorktreeRoot merge --abort 2>&1 | ForEach-Object { Write-L "  $_" }
}
$head = (& git -C $WorktreeRoot rev-parse --short HEAD)
$omain = (& git -C $WorktreeRoot rev-parse --short origin/main)
Write-L "worktree HEAD=$head  origin/main=$omain"

# --- 3) generation via daily_pipeline.ps1 -SkipVercel (latest main code) --
$pipeline = Join-Path $ScriptDir "daily_pipeline.ps1"
$pArgs = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $pipeline, "-SkipVercel")
if ($Date) { $pArgs += @("-Date", $Date) }
if ($AutoSubmitPaper) { $pArgs += "-AutoSubmitPaper" }
if ($Tier) { $pArgs += @("-Tier", $Tier) }
if ($DryRun) {
    # fast, non-clobbering smoke: subset symbols, skip heavy/side-effecting steps
    $pArgs += @("-Symbols", $DrySymbols, "-SkipCache", "-SkipNarrator",
        "-SkipPaperOrders", "-SkipExitCheck", "-DryRunPublish", "-SkipLatestCheck")
}
Write-L "--- [generate] daily_pipeline.ps1 -SkipVercel (DryRun=$DryRun) ---"
& powershell.exe @pArgs 2>&1 | ForEach-Object { Write-L "  | $_" }
$genCode = $LASTEXITCODE
Write-L "[generate] exit=$genCode"

# --- 4) publish from PRIMARY (monitor-webapp) tree -----------------------
$pubDate = if ($Date) { $Date } else { Get-Date -Format "yyyy-MM-dd" }
$pubScript = Join-Path $PrimaryRoot "scripts\publish_data_to_vercel.ps1"
if ($DryRun) {
    Write-L "[publish] DryRun: skip execution. Production would run:"
    Write-L "[publish]   powershell -File `"$pubScript`" -Date $pubDate   (cwd=$PrimaryRoot)"
}
elseif (-not (Test-Path $pubScript)) {
    Write-L "[publish] publish script missing (skip): $pubScript"
}
else {
    Write-L "--- [publish] primary publish_data_to_vercel.ps1 -Date $pubDate ---"
    Push-Location $PrimaryRoot
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $pubScript -Date $pubDate 2>&1 |
        ForEach-Object { Write-L "  | $_" }
    $pubCode = $LASTEXITCODE
    Pop-Location
    Write-L "[publish] exit=$pubCode"
}

Write-L "=== daily_main_follow done (generate exit=$genCode) ==="
exit $genCode
