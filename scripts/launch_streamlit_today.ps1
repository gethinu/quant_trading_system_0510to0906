# launch_streamlit_today.ps1
# 当日 signal 生成 / 詳細分析 view (private, Tailscale 経由)
# Task Scheduler の "At log on" trigger から呼ばれる想定。
# Port 8501 / bind 0.0.0.0 (LAN + Tailscale overlay)

$ErrorActionPreference = "Continue"
$RepoRoot = "C:\Repos\quant_trading_system_0510to0906"
$LogDir   = Join-Path $RepoRoot "logs"
$LogFile  = Join-Path $LogDir  "streamlit_today.log"
$VenvAct  = Join-Path $RepoRoot ".venv\Scripts\Activate.ps1"

if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir | Out-Null }

# --- 起動時 log header ---
"" | Out-File -Append -FilePath $LogFile
"==== $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') launch_streamlit_today start ====" |
    Out-File -Append -FilePath $LogFile

# --- venv activate ---
if (Test-Path $VenvAct) {
    & $VenvAct
} else {
    "WARN: venv Activate.ps1 not found at $VenvAct - relying on system python" |
        Out-File -Append -FilePath $LogFile
}

Set-Location $RepoRoot

# --- Streamlit launch (foreground, log tee) ---
# 0.0.0.0 = LAN + Tailscale overlay 両方に listen
# headless = auto-open browser 抑止
streamlit run apps\app_today_signals.py `
    --server.port 8501 `
    --server.address 0.0.0.0 `
    --server.headless true `
    --browser.gatherUsageStats false `
    --logger.level info 2>&1 | Tee-Object -Append -FilePath $LogFile
