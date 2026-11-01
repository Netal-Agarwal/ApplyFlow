# ApplyFlow Phase 3: Windows Task Scheduler tasks. Run once, in PowerShell, from the project folder:
#   powershell -ExecutionPolicy Bypass -File scripts\schedule_tasks.ps1
# Creates (or replaces) these tasks for the current user, in the \ApplyFlow\ folder:
#   ApplyFlow plan+digest  20:00 Sun-Thu   plan the next working day, then email the digest
#   ApplyFlow send         09:00 Mon-Fri   wakes the computer, runs as soon as possible after a missed start, 4 h limit
#   ApplyFlow check-inbox  11:00, 15:00, 19:45 daily
# Output is appended to data\logs\<command>.log. Remove them all with scripts\remove_tasks.ps1.
# Times are this computer's clock: keep Windows on India Standard Time (the send window is 09:00-10:45 IST).

$ErrorActionPreference = "Stop"
$Project = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Python = Join-Path $Project ".venv\Scripts\python.exe"
$Logs = Join-Path $Project "data\logs"
$Folder = "\ApplyFlow\"

if (-not (Test-Path $Python)) { throw "No virtualenv python at $Python - create .venv first (see README)." }
New-Item -ItemType Directory -Force $Logs | Out-Null

function New-AfAction([string]$Command, [string]$Log) {
    # cmd.exe for the >> redirect; UTF-8 so company names never break the log
    $line = "set PYTHONIOENCODING=utf-8&& `"$Python`" -m applyflow $Command >> `"$Logs\$Log`" 2>&1"
    New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c $line" -WorkingDirectory $Project
}

function Register-Af([string]$Name, $Actions, $Triggers, $Settings) {
    $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
    Register-ScheduledTask -TaskName $Name -TaskPath $Folder -Action $Actions -Trigger $Triggers -Settings $Settings `
        -Principal $principal -Force | Out-Null
    Write-Host "Registered $Folder$Name"
}

# plan + digest: 20:00 Sunday-Thursday (the next day's batch, reviewed in the evening)
$planTrigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday, Monday, Tuesday, Wednesday, Thursday -At 20:00
$planSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-Af "ApplyFlow plan+digest" @((New-AfAction "plan" "plan.log"), (New-AfAction "digest" "digest.log")) `
    $planTrigger $planSettings

# send: 09:00 Monday-Friday; wake the computer; run as soon as possible after a missed start; stop after 4 hours.
# `send` itself waits for 09:00, stops at 10:45 on an on-time start, sends until 13:00 after a late start, and
# defers what's left to the next working day.
$sendTrigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At 09:00
$sendSettings = New-ScheduledTaskSettingsSet -WakeToRun -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 4) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew
Register-Af "ApplyFlow send" (New-AfAction "send" "send.log") $sendTrigger $sendSettings

# check-inbox: 11:00, 15:00 and 19:45 every day
$inboxTriggers = @((New-ScheduledTaskTrigger -Daily -At 11:00), (New-ScheduledTaskTrigger -Daily -At 15:00),
                   (New-ScheduledTaskTrigger -Daily -At 19:45))
$inboxSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew
Register-Af "ApplyFlow check-inbox" (New-AfAction "check-inbox" "check-inbox.log") $inboxTriggers $inboxSettings

Write-Host ""
Write-Host "Done. Check them in Task Scheduler (Task Scheduler Library > ApplyFlow) or with:"
Write-Host "  Get-ScheduledTask -TaskPath '$Folder' | Get-ScheduledTaskInfo"
Write-Host "Wake timers must be allowed: Control Panel > Power Options > Change plan settings > Change advanced power"
Write-Host "settings > Sleep > Allow wake timers = Enable (on battery and plugged in)."
