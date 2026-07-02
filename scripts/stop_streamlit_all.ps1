# stop_streamlit_all.ps1
# 全 Streamlit プロセスを停止する (cleanup 用)
# Port 8501 / 8502 に listen している python.exe を含めて kill する。
#
# 使い方:
#   powershell -File C:\Repos\quant_trading_system_0510to0906\scripts\stop_streamlit_all.ps1

$ErrorActionPreference = "Continue"

Write-Host "[stop_streamlit_all] Searching for streamlit processes..." -ForegroundColor Cyan

# 1) streamlit.exe (稀に存在)
$streamlitProcs = Get-Process -Name "streamlit" -ErrorAction SilentlyContinue
if ($streamlitProcs) {
    $streamlitProcs | ForEach-Object {
        Write-Host "  Stopping streamlit.exe PID=$($_.Id)" -ForegroundColor Yellow
        Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue
    }
} else {
    Write-Host "  (no streamlit.exe found)" -ForegroundColor DarkGray
}

# 2) port 8501 / 8502 の listen プロセス (python.exe が streamlit run で立ってるケース)
foreach ($port in 8501, 8502) {
    $conns = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    if ($conns) {
        $conns | ForEach-Object {
            $procId = $_.OwningProcess
            $proc   = Get-Process -Id $procId -ErrorAction SilentlyContinue
            if ($proc) {
                Write-Host "  Stopping $($proc.ProcessName) PID=$procId (port $port)" -ForegroundColor Yellow
                Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
            }
        }
    } else {
        Write-Host "  (port $port not listening)" -ForegroundColor DarkGray
    }
}

Start-Sleep -Seconds 1
Write-Host "[stop_streamlit_all] Done." -ForegroundColor Green
