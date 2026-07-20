<#
.SYNOPSIS
    Task Scheduler wrapper for the host-side morning ops brief.

.DESCRIPTION
    Thin launcher for scripts/morning_brief.py. Runs read-only checks across the
    quant repo (self_monitor) and the mt5 repo (terminal reconcile, zombie audit,
    HUMAN_TASK_QUEUE), fetches weather, computes the previous-day diff and sends ONE
    concise ntfy to the phone (urgent if any RED/WARN). Never places orders; paper-only.

    Kept in PowerShell (everything else is Python so Japanese never hits cp932):
      - Load the PRIMARY repo .env (NTFY_TOPIC) so the brief can be pushed.
      - Force UTF-8 for the child python to avoid cp932 decode errors.
      - Tee a launch log; propagate the Python exit code.

.NOTES
    Exit codes propagate from morning_brief.py (0 ok / 2 warn / 3 red).
    Keep this file ASCII-only; the Python side owns all Japanese output.
#>
param(
    [string]$Date = "",
    [switch]$DryRun = $false,
    [switch]$NoNotify = $false,
    [switch]$NoWeather = $false,
    [string]$PrimaryRoot = "C:\Repos\quant_trading_system_0510to0906",
    [string]$Mt5Root = "C:\Repos\mt5_Bundle-of-edges"
)

$ErrorActionPreference = "Continue"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Split-Path -Parent $ScriptDir
$LogDir = Join-Path $PrimaryRoot "logs\morning_brief"
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }
$Stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$LaunchLog = Join-Path $LogDir "launch_$Stamp.log"

function Write-Launch {
    param([string]$Message)
    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    Write-Host $line
    Add-Content -Path $LaunchLog -Value $line -Encoding UTF8
}

Write-Launch "=== morning_brief.ps1 launch ==="
Write-Launch "RepoRoot   : $RepoRoot"
Write-Launch "PrimaryRoot: $PrimaryRoot"
Write-Launch "Mt5Root    : $Mt5Root"

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
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
$OutputEncoding = [System.Text.Encoding]::UTF8

# --- build python args ---------------------------------------------------
$py = Join-Path $RepoRoot "scripts\morning_brief.py"
$pyArgs = @($py, "--primary-root", $PrimaryRoot, "--mt5-root", $Mt5Root)
if ($Date) { $pyArgs += @("--date", $Date) }
if ($DryRun) { $pyArgs += "--dry-run" }
if ($NoNotify) { $pyArgs += "--no-notify" }
if ($NoWeather) { $pyArgs += "--no-weather" }

Set-Location $RepoRoot
Write-Launch ("python " + ($pyArgs -join " "))

& python @pyArgs 2>&1 | ForEach-Object { Write-Launch $_ }
$code = $LASTEXITCODE
Write-Launch "=== morning_brief.py exit=$code ==="

exit $code
