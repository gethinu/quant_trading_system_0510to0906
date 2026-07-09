<#
.SYNOPSIS
    Task Scheduler wrapper for the open (US market-open) auto-submit runner.

.DESCRIPTION
    Thin launcher for scripts/open_auto_run.py. Runs from THIS worktree
    (pinned to origin/main so equity-linked sizing is in effect) whose
    data_cache/results_csv are junctions to the primary repo (shared live data).

    Responsibilities kept in PowerShell (everything else is in the Python
    orchestrator so Japanese text never touches the cp932 console codepage):
      - Load the PRIMARY repo .env (Alpaca creds + NTFY_TOPIC) because this
        worktree has no .env of its own.
      - Force UTF-8 for the child python (PYTHONUTF8 / PYTHONIOENCODING) to
        avoid the cp932 UnicodeDecodeError the one-off runner hit.
      - Invoke python with pass-through flags and tee a launch log.

    PAPER ONLY. Never touches live money. The Python runner asserts paper env,
    gates on market-open + signal count, and enforces exit->entry ordering.

.NOTES
    Exit codes propagate from open_auto_run.py (0 ok / 3 aborted-by-gate).
    Keep this file ASCII-only; the Python side owns all Japanese output.
#>
param(
    [string]$Date = "",
    [switch]$DryRun = $false,
    [switch]$AllowClosed = $false,
    [switch]$SkipSignals = $false,
    [switch]$Force = $false,
    [int]$MinSignals = 10,
    [double]$PollTimeout = 300,
    [string]$PrimaryRoot = "C:\Repos\quant_trading_system_0510to0906"
)

$ErrorActionPreference = "Continue"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$WorktreeRoot = Split-Path -Parent $ScriptDir
$LogDir = Join-Path $WorktreeRoot "logs"
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }
$Stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$LaunchLog = Join-Path $LogDir "open_auto_run_launch_$Stamp.log"

function Write-Launch {
    param([string]$Message)
    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    Write-Host $line
    Add-Content -Path $LaunchLog -Value $line -Encoding UTF8
}

Write-Launch "=== open_auto_run.ps1 launch ==="
Write-Launch "WorktreeRoot: $WorktreeRoot"
Write-Launch "PrimaryRoot : $PrimaryRoot"

# --- load PRIMARY .env (creds + NTFY) into this process env --------------
$EnvFile = Join-Path $PrimaryRoot ".env"
if (Test-Path $EnvFile) {
    Write-Launch "loading .env: $EnvFile"
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
    Write-Launch "WARN: primary .env not found at $EnvFile (creds/NTFY may be missing)"
}

# hard paper guard at the wrapper level too (belt and suspenders)
if ($env:ALPACA_PAPER -and ($env:ALPACA_PAPER.ToLower() -notin @("1", "true", "yes", "y", "on"))) {
    Write-Launch "SAFETY ABORT: ALPACA_PAPER is not truthy ($($env:ALPACA_PAPER)); refusing to run."
    exit 2
}

# --- force UTF-8 for the child python ------------------------------------
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

# --- build python args ---------------------------------------------------
$py = Join-Path $WorktreeRoot "scripts\open_auto_run.py"
$pyArgs = @($py)
if ($Date) { $pyArgs += @("--date", $Date) }
$pyArgs += @("--min-signals", "$MinSignals", "--poll-timeout", "$PollTimeout")
if ($DryRun) { $pyArgs += "--dry-run" }
if ($AllowClosed) { $pyArgs += "--allow-closed" }
if ($SkipSignals) { $pyArgs += "--skip-signals" }
if ($Force) { $pyArgs += "--force" }

Set-Location $WorktreeRoot
Write-Launch ("python " + ($pyArgs -join " "))

& python @pyArgs 2>&1 | ForEach-Object { Write-Launch $_ }
$code = $LASTEXITCODE
Write-Launch "=== open_auto_run.py exit=$code ==="
exit $code
