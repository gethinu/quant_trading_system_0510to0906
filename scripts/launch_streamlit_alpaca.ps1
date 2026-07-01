# launch_streamlit_alpaca.ps1
# Alpaca dashboard view (private, Tailscale 経由)
# Task Scheduler の "At log on" trigger から呼ばれる想定。
# Port 8502 / bind 0.0.0.0 (LAN + Tailscale overlay)

$ErrorActionPreference = "Continue"
$RepoRoot = "C:\Repos\quant_trading_system_0510to0906"
$LogDir   = Join-Path $RepoRoot "logs"
$LogFile  = Join-Path $LogDir  "streamlit_alpaca.log"
$VenvAct  = Join-Path $RepoRoot ".venv\Scripts\Activate.ps1"

if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir | Out-Null }

"" | Out-File -Append -FilePath $LogFile
"==== $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') launch_streamlit_alpaca start ====" |
    Out-File -Append -FilePath $LogFile

if (Test-Path $VenvAct) {
    & $VenvAct
} else {
    "WARN: venv Activate.ps1 not found at $VenvAct - relying on system python" |
        Out-File -Append -FilePath $LogFile
}

Set-Location $RepoRoot

streamlit run apps\dashboards\app_alpaca_dashboard.py `
    --server.port 8502 `
    --server.address 0.0.0.0 `
    --server.headless true `
    --browser.gatherUsageStats false `
    --logger.level info 2>&1 | Tee-Object -Append -FilePath $LogFile
