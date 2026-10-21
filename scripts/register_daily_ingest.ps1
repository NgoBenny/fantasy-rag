# Registers the "fantasy-rag ingest" scheduled task for the current user (no admin needed).
# Cadence: daily 6 PM, plus 2 PM and 9 PM Wednesday-Saturday (after afternoon injury reports;
# evenings are when this PC is usually on).
# StartWhenAvailable runs a missed trigger as soon as the PC is back on.
# Remove with: Unregister-ScheduledTask -TaskName "fantasy-rag ingest" -Confirm:$false
#   (and "fantasy-rag news" for the hourly news task, "fantasy-rag forward" for Tuesdays)
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

# News: every hour while the PC is on (PLAN: every 30-60 minutes in season).
$newsAction = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$script`" -Job news"
$hourly = New-ScheduledTaskTrigger -Once -At (Get-Date).Date `
    -RepetitionInterval (New-TimeSpan -Hours 1) -RepetitionDuration (New-TimeSpan -Days 3650)
$newsSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
Register-ScheduledTask -TaskName "fantasy-rag news" -Action $newsAction -Trigger $hourly `
    -Settings $newsSettings -Description "fantasy-rag: hourly news ingest" -Force

# Phase 8 forward run: Tuesdays 10:30 America/New_York, converted to this PC's zone, so the
# snapshot lands before the noon ET deadline. A run the PC misses starts when it is back
# on and is then recorded as late (or not at all): a missed Tuesday is reported missing.
$eastern = [System.TimeZoneInfo]::FindSystemTimeZoneById("Eastern Standard Time")
$today = [System.DateTime]::SpecifyKind((Get-Date).Date.AddHours(10).AddMinutes(30), "Unspecified")
$local = [System.TimeZoneInfo]::ConvertTime($today, $eastern, [System.TimeZoneInfo]::Local)
$forwardAction = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$script`" -Job forward"
$tuesday = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Tuesday -At $local.ToString("HH:mm")
$forwardSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1)
Register-ScheduledTask -TaskName "fantasy-rag forward" -Action $forwardAction -Trigger $tuesday `
    -Settings $forwardSettings -Description "fantasy-rag: Phase 8 Tuesday forward predictions" -Force
