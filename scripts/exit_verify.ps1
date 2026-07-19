<#
.SYNOPSIS
    Task Scheduler wrapper for the daily exit E2E verifier.

.DESCRIPTION
    Thin launcher for scripts/exit_verify.py. Reconciles the day's planned exits
    (exit_orders_<date>.json) against actual Alpaca fills (read-only GET) and flags
    any close that did not fill or any time-based exit that should have fired but was
    not planned. Sends ntfy only on discrepancy. Never places/cancels orders.

    Kept in PowerShell (Python owns all Japanese to avoid cp932):
      - Load the PRIMARY repo .env (Alpaca creds + NTFY_TOPIC).
      - Force UTF-8 for the child python.
      - Tee a launch log; propagate the Python exit code.

.NOTES
    Exit codes propagate from exit_verify.py (0 ok / 2 discrepancy / 1 no input).
    Keep this file ASCII-only.
#>
param(
    [string]$Date = "",
    [switch]$DryRun = $false,
    [switch]$NoAlpaca = $false,
    [switch]$NoNotify = $false,
    [string]$PrimaryRoot = "C:\Repos\quant_trading_system_0510to0906"
)

$ErrorActionPreference = "Continue"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$WorktreeRoot = Split-Path -Parent $ScriptDir
$LogDir = Join-Path $WorktreeRoot "logs"
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }
$Stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$LaunchLog = Join-Path $LogDir "exit_verify_launch_$Stamp.log"

function Write-Launch {
    param([string]$Message)
    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    Write-Host $line
    Add-Content -Path $LaunchLog -Value $line -Encoding UTF8
}

Write-Launch "=== exit_verify.ps1 launch ==="
Write-Launch "WorktreeRoot: $WorktreeRoot"
Write-Launch "PrimaryRoot : $PrimaryRoot"

# --- load PRIMARY .env (creds + NTFY_TOPIC) ------------------------------
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
    Write-Launch "WARN: primary .env not found at $EnvFile"
}

$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
# Read the child python's UTF-8 stdout as UTF-8 so the launch log is not mojibake.
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
$OutputEncoding = [System.Text.Encoding]::UTF8

# results_csv / logs are junction targets to the primary repo, so point at PrimaryRoot.
$py = Join-Path $WorktreeRoot "scripts\exit_verify.py"
$pyArgs = @(
    $py,
    "--results-dir", (Join-Path $PrimaryRoot "results_csv"),
    "--log-dir", (Join-Path $PrimaryRoot "logs")
)
if ($Date) { $pyArgs += @("--date", $Date) }
if ($DryRun) { $pyArgs += "--dry-run" }
if ($NoAlpaca) { $pyArgs += "--no-alpaca" }
if ($NoNotify) { $pyArgs += "--no-notify" }

Set-Location $WorktreeRoot
Write-Launch ("python " + ($pyArgs -join " "))

& python @pyArgs 2>&1 | ForEach-Object { Write-Launch $_ }
$code = $LASTEXITCODE
Write-Launch "=== exit_verify.py exit=$code ==="

exit $code
