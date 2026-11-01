# ApplyFlow Phase 3: remove the Task Scheduler tasks created by scripts\schedule_tasks.ps1.
#   powershell -ExecutionPolicy Bypass -File scripts\remove_tasks.ps1
# Logs in data\logs\ are kept. To stop sending without removing anything: python -m applyflow pause

$Folder = "\ApplyFlow\"
$tasks = Get-ScheduledTask -TaskPath $Folder -ErrorAction SilentlyContinue
if (-not $tasks) {
    Write-Host "No ApplyFlow tasks found."
    exit 0
}
foreach ($t in $tasks) {
    Unregister-ScheduledTask -TaskName $t.TaskName -TaskPath $Folder -Confirm:$false
    Write-Host "Removed $Folder$($t.TaskName)"
}
