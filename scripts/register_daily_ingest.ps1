# Registers the "fantasy-rag ingest" scheduled task for the current user (no admin needed).
# Cadence: daily 6 PM, plus 2 PM and 9 PM Wednesday-Saturday (after afternoon injury reports;
# evenings are when this PC is usually on).
# StartWhenAvailable runs a missed trigger as soon as the PC is back on.
# Remove with: Unregister-ScheduledTask -TaskName "fantasy-rag ingest" -Confirm:$false
$script = Join-Path $PSScriptRoot "ingest_daily.ps1"
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$script`""
$triggers = @(
    New-ScheduledTaskTrigger -Daily -At 6:00pm
    New-ScheduledTaskTrigger -Weekly -DaysOfWeek Wednesday, Thursday, Friday, Saturday -At 2:00pm
    New-ScheduledTaskTrigger -Weekly -DaysOfWeek Wednesday, Thursday, Friday, Saturday -At 9:00pm
)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 1)
Register-ScheduledTask -TaskName "fantasy-rag ingest" -Action $action -Trigger $triggers `
    -Settings $settings -Description "fantasy-rag: nflverse + Sleeper ingest and injury snapshots" -Force
