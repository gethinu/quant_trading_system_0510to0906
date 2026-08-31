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
      2. git fetch + FAST-FORWARD ONLY to origin/main. This branch carries no
         local commits (the wrapper and the -SkipVercel patch both live on main),
         so the ff can never conflict. A non-ff state is a hard fault -> CRIT.
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

    A stale checkout is reported via CRIT ntfy + a durable autopsy file, NOT via
    the exit code: the Task Scheduler entry has RestartOnFailure Count=3, so a
    non-zero exit would re-run generation AND publish three times over.
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

# Loud, durable failure channel. A silent WARN in a log nobody reads is how this
# checkout ran 9-day-old code for 9 straight mornings without anyone noticing.
function Send-Crit {
    param([string]$Title, [string]$Text)
    Write-L "CRIT: $Title"
    $Text -split "`n" | ForEach-Object { Write-L "  CRIT| $_" }
    # durable marker: survives even if ntfy is unreachable / NTFY_TOPIC unset
    try {
        $autopsy = Join-Path $LogDir "daily_main_follow_STALE.txt"
        $stampNow = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
        Add-Content -Path $autopsy -Value "[$stampNow] $Title" -Encoding UTF8
        Add-Content -Path $autopsy -Value $Text -Encoding UTF8
    }
    catch { Write-L "autopsy write failed: $_" }
    $topic = $env:NTFY_TOPIC
    if (-not $topic) { Write-L "CRIT ntfy skipped (NTFY_TOPIC unset)"; return }
    $base = if ($env:NTFY_URL) { $env:NTFY_URL.TrimEnd('/') } else { "https://ntfy.sh" }
    try {
        # ASCII-only headers on purpose (ntfy headers are latin-1)
        $headers = @{ "X-Title" = $Title; "X-Priority" = "5"; "X-Tags" = "rotating_light" }
        Invoke-RestMethod -Uri "$base/$topic" -Method Post -Headers $headers -Body $Text | Out-Null
        Write-L "CRIT ntfy sent"
    }
    catch { Write-L "CRIT ntfy FAILED: $_" }
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

# --- 2) follow latest origin/main (FAST-FORWARD ONLY) --------------------
#
# --ff-only is load-bearing, not a style choice. With a plain `git merge` this
# branch accumulated local commits; once it carried patches whose equivalents
# later landed on main under different SHAs, every subsequent merge re-conflicted
# on the same ~16 files forever. The old code logged WARN, aborted, and ran on --
# so the 06:00 job silently generated the dashboard from 2026-08-22 code for nine
# mornings (pre-f268c4a: available_slots stuck at 10 for every system).
# A fast-forward cannot conflict and cannot create divergence.
Write-L "--- git fetch + fast-forward to origin/main ---"
& git -C $WorktreeRoot fetch origin --quiet 2>&1 | ForEach-Object { Write-L "  $_" }
$mergeOut = & git -C $WorktreeRoot merge --ff-only origin/main 2>&1
$mergeCode = $LASTEXITCODE
$mergeOut | ForEach-Object { Write-L "  $_" }
if ($mergeCode -ne 0) {
    Write-L "ERROR: fast-forward to origin/main FAILED (exit=$mergeCode)"
}

$head = (& git -C $WorktreeRoot rev-parse HEAD)
$omain = (& git -C $WorktreeRoot rev-parse origin/main)
Write-L "worktree HEAD=$head  origin/main=$omain"

# Positive invariant. Checked independently of the merge exit code so that ANY
# cause of a frozen checkout is caught -- conflict, detached HEAD, a hand-made
# commit here, a dirty tree blocking the ff -- not just the one we already saw.
if ($head -ne $omain) {
    $behind = (& git -C $WorktreeRoot rev-list --count "$($head)..$($omain)" 2>&1)
    $body = @(
        "06:00 daily monitor is NOT running origin/main.",
        "  worktree   : $WorktreeRoot",
        "  HEAD       : $head",
        "  origin/main: $omain",
        "  behind     : $behind commit(s)",
        "Dashboard numbers generated today come from STALE code.",
        "Fix: git -C $WorktreeRoot status   then, once clean and nothing local is",
        "     worth keeping: git -C $WorktreeRoot reset --hard origin/main"
    ) -join "`n"
    Send-Crit -Title "QTS STALE CHECKOUT: daily-main-follow" -Text $body
}
else {
    Write-L "OK: worktree is exactly origin/main ($head)"
}

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
