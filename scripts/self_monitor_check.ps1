<#
.SYNOPSIS
    Task Scheduler wrapper for the daily self-monitoring silent-failure guard.

.DESCRIPTION
    Thin launcher for scripts/self_monitor_check.py. Runs read-only checks over the
    primary repo's results_csv / logs / monitor-webapp branch and sends ONE summary
    ntfy (urgent if any WARN/CRIT). Never places orders; never touches Alpaca.

    Kept in PowerShell (everything else is Python so Japanese never hits cp932):
      - Load the PRIMARY repo .env (NTFY_TOPIC) so the summary can be pushed.
      - Force UTF-8 for the child python to avoid cp932 decode errors.
      - Tee a launch log; propagate the Python exit code.

.NOTES
    Exit codes propagate from self_monitor_check.py (0 ok / 2 warn / 3 crit).
    Keep this file ASCII-only; the Python side owns all Japanese output.
#>
param(
    [string]$Date = "",
    [switch]$DryRun = $false,
    [switch]$NoNotify = $false,
    [int]$MinSignals = 10,
    [string]$PrimaryRoot = "C:\Repos\quant_trading_system_0510to0906"
)

$ErrorActionPreference = "Continue"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$WorktreeRoot = Split-Path -Parent $ScriptDir
$LogDir = Join-Path $WorktreeRoot "logs"
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }
$Stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$LaunchLog = Join-Path $LogDir "self_monitor_launch_$Stamp.log"

function Write-Launch {
    param([string]$Message)
    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    Write-Host $line
    Add-Content -Path $LaunchLog -Value $line -Encoding UTF8
}

Write-Launch "=== self_monitor_check.ps1 launch ==="
Write-Launch "WorktreeRoot: $WorktreeRoot"
Write-Launch "PrimaryRoot : $PrimaryRoot"

# --- load PRIMARY .env (NTFY_TOPIC) into this process env ----------------
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
    Write-Launch "WARN: primary .env not found at $EnvFile (NTFY may be missing)"
}

# --- force UTF-8 for the child python ------------------------------------
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
# Read the child python's UTF-8 stdout as UTF-8 so the launch log is not mojibake.
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
$OutputEncoding = [System.Text.Encoding]::UTF8

# --- build python args ---------------------------------------------------
$py = Join-Path $WorktreeRoot "scripts\self_monitor_check.py"
$pyArgs = @($py, "--repo-root", $PrimaryRoot, "--min-signals", "$MinSignals")
if ($Date) { $pyArgs += @("--date", $Date) }
if ($DryRun) { $pyArgs += "--dry-run" }
if ($NoNotify) { $pyArgs += "--no-notify" }

Set-Location $WorktreeRoot
Write-Launch ("python " + ($pyArgs -join " "))

& python @pyArgs 2>&1 | ForEach-Object { Write-Launch $_ }
$code = $LASTEXITCODE
Write-Launch "=== self_monitor_check.py exit=$code ==="

exit $code
