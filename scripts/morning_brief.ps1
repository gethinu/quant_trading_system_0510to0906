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

# --- dashboard publish self-heal + freshness alert (2026-07-22 root-cause) -
# The 06:00 daily_main_follow.ps1 publishes the dashboard as its LAST step, AFTER
# the child daily_pipeline.ps1 (which fires the ntfy) finishes. The ntfy lives in
# the child; the dashboard publish lives in the wrapper. If the wrapper dies
# mid-run the orphaned child still ntfy's fresh data while the publish is silently
# lost -> the site freezes on yesterday's build even though ntfy shows today
# (seen 2026-07-22: ntfy=07-22, dashboard=07-21). This launcher runs ~08:00 JST,
# host-side and INDEPENDENT of that fragile wrapper, so it is the reliable place
# to (1) DETECT the publish gap and alert on recurrence, then (2) SELF-HEAL by
# republishing the newest generated data. -AutoLatest is idempotent (a no-op once
# data/ is already current). Paper-only; data publish only, never orders.
#
# 2026-08-22 cry-wolf fix -- ORDER OF DETECT vs ALERT
# ---------------------------------------------------
# The detect pass used to alert BEFORE the self-heal ran, so it alerted on a state
# the very next step was about to fix. At 08:00 JST that state is the NORMAL one:
# the 06:00 daily advances results_csv/ but does not publish, so origin's data/ is
# still on yesterday until the self-heal below pushes. Measured 2026-08-09..22:
# 14/14 mornings fired "publish が取りこぼされています" and were verified fresh ~10 s
# later. Now the stale alert is DEFERRED past the self-heal and only fires if the
# re-check still sees a gap (i.e. the self-heal genuinely failed, as on 08-20 when
# the bundle preflight rejected the publish). The alert is not disabled -- it is
# moved to the only point where it means something.
#
# --check-served stays on the FIRST pass on purpose: it judges a *previously*
# published run against the live HTML, so the self-heal does not affect it, and
# running it after a fresh push would only ever see an age-0 bundle (always inside
# the deploy grace = a vacuous check).
$pubScript = Join-Path $PrimaryRoot "scripts\publish_data_to_vercel.ps1"
$freshChk = Join-Path $PrimaryRoot "scripts\check_dashboard_freshness.py"
$canSelfHeal = Test-Path $pubScript
Push-Location $PrimaryRoot
try {
    if (Test-Path $freshChk) {
        # --check-served: git push が成功していても Vercel の build が発火しない/
        # 落ちる場合がある。その時ローカル同士の比較 (results_csv vs data/) は
        # 「fresh」と答えてしまうので、本番 HTML に publish 済み run_id が現れるかも
        # 見る (2026-08-17: publish 成功・blob 一致なのに本番は朝の run のままだった)。
        # 正常な deploy 遅延 (実測 ~20 分) は grace 内なので警告しない。
        $preArgs = @($freshChk, "--repo-root", $PrimaryRoot, "--notify", "--check-served")
        if ($canSelfHeal) {
            # self-heal がこの後に走るなら stale 通知は再チェックへ委譲する。
            $preArgs += "--defer-stale-notify"
            Write-Launch "--- [dashboard_freshness] detect (stale alert deferred until after self-heal) ---"
        }
        else {
            Write-Launch "--- [dashboard_freshness] detect (no self-heal available -> alert now) ---"
        }
        & python @preArgs 2>&1 | ForEach-Object { Write-Launch $_ }
        Write-Launch "[dashboard_freshness] pre-heal exit=$LASTEXITCODE (0=ok, 2=stale, 3=deploy missing)"
    }
    if ($canSelfHeal) {
        Write-Launch "--- [dashboard_selfheal] publish_data_to_vercel.ps1 -AutoLatest ---"
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $pubScript -AutoLatest 2>&1 |
            ForEach-Object { Write-Launch $_ }
        Write-Launch "[dashboard_selfheal] exit=$LASTEXITCODE"

        # 治療後の再チェック。ここで **まだ** stale なら本物 (self-heal が効かなかった)。
        # 通常運転の朝はここで fresh になるので通知は飛ばない = cry wolf が止まる。
        if (Test-Path $freshChk) {
            Write-Launch "--- [dashboard_freshness] re-check after self-heal (the ONLY pass allowed to alert) ---"
            & python $freshChk --repo-root $PrimaryRoot --notify --post-heal 2>&1 |
                ForEach-Object { Write-Launch $_ }
            Write-Launch "[dashboard_freshness] post-heal exit=$LASTEXITCODE (0=ok -> no alert, 2=still stale -> ALERT)"
        }
    }
}
finally {
    Pop-Location
}

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
