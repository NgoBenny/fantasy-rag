# Registers the "fantasy-rag ingest" scheduled task for the current user (no admin needed).
# Cadence from PLAN.md: daily, and 3x daily Wednesday-Saturday when injury reports change.
# StartWhenAvailable runs a missed trigger as soon as the PC is back on.
# Remove with: Unregister-ScheduledTask -TaskName "fantasy-rag ingest" -Confirm:$false
$script = Join-Path $PSScriptRoot "ingest_daily.ps1"
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$script`""
$triggers = @(
    New-ScheduledTaskTrigger -Daily -At 7:00am
    New-ScheduledTaskTrigger -Weekly -DaysOfWeek Wednesday, Thursday, Friday, Saturday -At 1:00pm
    New-ScheduledTaskTrigger -Weekly -DaysOfWeek Wednesday, Thursday, Friday, Saturday -At 6:00pm
)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 1)
Register-ScheduledTask -TaskName "fantasy-rag ingest" -Action $action -Trigger $triggers `
    -Settings $settings -Description "fantasy-rag: nflverse + Sleeper ingest and injury snapshots" -Force
