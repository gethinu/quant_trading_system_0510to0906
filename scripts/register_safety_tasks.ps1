<#
.SYNOPSIS
    Register the daily safety-net scheduled tasks (self-monitor + exit-verify).

.DESCRIPTION
    Creates two Windows Task Scheduler tasks, hidden, matching the existing
    QuantTrading_OpenAutoRun principal (Interactive logon, Limited run level):

      QuantTrading_SelfMonitor : 07:15 JST daily  -> scripts/self_monitor_check.ps1
                                 (did the 06:00 daily run? signals abundant?
                                  Vercel published? did last night's open-run fill?)
      QuantTrading_ExitVerify  : 07:20 JST daily  -> scripts/exit_verify.ps1
                                 (did planned exits actually fill? any due exit missed?)

    Both are READ-ONLY (no order placement). They send ONE ntfy summary; urgent only
    on anomaly. Times are chosen AFTER the 06:00 daily (main-follow) and AFTER the
    prior evening's 22:35/23:35 open-run, so a single morning pass covers the whole cycle.

    This script does NOT run any check itself; it only registers the schedule. Review,
    then run it once. Re-running is idempotent (unregister + re-register).

.PARAMETER WorktreeRoot
    Folder holding scripts/self_monitor_check.ps1 & exit_verify.ps1. Default is the
    production run dir C:\tmp\qts-main-run — the safety-net branch must be merged into
    whatever that worktree tracks (git pull) first. For interim use before merging,
    pass the safety-nets worktree: -WorktreeRoot C:\tmp\qts-safety-nets.

.PARAMETER PrimaryRoot
    Primary repo (.env + junction target). Passed through to the launchers.

.PARAMETER Unregister
    Remove the two tasks instead of creating them.

.EXAMPLE
    # register (after reviewing / merging)
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\register_safety_tasks.ps1
    # interim (run from the safety-nets worktree before merge)
    ... -File scripts\register_safety_tasks.ps1 -WorktreeRoot C:\tmp\qts-safety-nets
    # remove
    ... -File scripts\register_safety_tasks.ps1 -Unregister
#>
param(
    [string]$WorktreeRoot = "C:\tmp\qts-main-run",
    [string]$PrimaryRoot = "C:\Repos\quant_trading_system_0510to0906",
    [switch]$Unregister = $false
)

$ErrorActionPreference = "Stop"

$SelfMonitorTask = "QuantTrading_SelfMonitor"
$ExitVerifyTask = "QuantTrading_ExitVerify"

function Remove-TaskIfExists {
    param([string]$Name)
    $existing = Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
    if ($existing) {
        Unregister-ScheduledTask -TaskName $Name -Confirm:$false
        Write-Host "removed: $Name"
    }
}

if ($Unregister) {
    Remove-TaskIfExists -Name $SelfMonitorTask
    Remove-TaskIfExists -Name $ExitVerifyTask
    Write-Host "done (unregister)."
    return
}

$SelfMonitorPs1 = Join-Path $WorktreeRoot "scripts\self_monitor_check.ps1"
$ExitVerifyPs1 = Join-Path $WorktreeRoot "scripts\exit_verify.ps1"
foreach ($p in @($SelfMonitorPs1, $ExitVerifyPs1)) {
    if (-not (Test-Path $p)) {
        throw "launcher not found: $p  (merge the safety-nets branch into $WorktreeRoot, or pass -WorktreeRoot C:\tmp\qts-safety-nets)"
    }
}

# principal: match QuantTrading_OpenAutoRun (Interactive, Limited).
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 15) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

function Register-SafetyTask {
    param([string]$Name, [string]$Ps1, [string]$At)
    $action = New-ScheduledTaskAction -Execute "powershell.exe" `
        -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Ps1`" -PrimaryRoot `"$PrimaryRoot`"" `
        -WorkingDirectory $WorktreeRoot
    $trigger = New-ScheduledTaskTrigger -Daily -At $At
    Remove-TaskIfExists -Name $Name
    Register-ScheduledTask -TaskName $Name -Action $action -Trigger $trigger `
        -Principal $principal -Settings $settings `
        -Description "QuantTrading safety net (read-only, paper). Registered $(Get-Date -Format 'yyyy-MM-dd')." | Out-Null
    Write-Host "registered: $Name at $At daily -> $Ps1"
}

# 07:15 / 07:20 JST: after 06:00 daily and after the prior evening's open-run.
Register-SafetyTask -Name $SelfMonitorTask -Ps1 $SelfMonitorPs1 -At "07:15"
Register-SafetyTask -Name $ExitVerifyTask -Ps1 $ExitVerifyPs1 -At "07:20"

Write-Host ""
Write-Host "done. verify with: Get-ScheduledTask -TaskName 'QuantTrading_SelfMonitor','QuantTrading_ExitVerify'"
Write-Host "first-run smoke test (no ntfy):"
Write-Host "  powershell -File `"$SelfMonitorPs1`" -DryRun"
Write-Host "  powershell -File `"$ExitVerifyPs1`" -Date <YYYY-MM-DD> -DryRun"
