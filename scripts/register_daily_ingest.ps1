# Registers the "fantasy-rag ingest" scheduled task for the current user (no admin needed).
# Cadence: daily 6 PM, plus 2 PM and 9 PM Wednesday-Saturday (after afternoon injury reports;
# evenings are when this PC is usually on).
# StartWhenAvailable runs a missed trigger as soon as the PC is back on.
# Remove with: Unregister-ScheduledTask -TaskName "fantasy-rag ingest" -Confirm:$false
#   (and "fantasy-rag news", "fantasy-rag forward", "fantasy-rag forward-check", "fantasy-rag backup")
$script = Join-Path $PSScriptRoot "ingest_daily.ps1"
$hidden = Join-Path $PSScriptRoot "run_hidden.vbs"
# Every job runs through run_hidden.vbs: no console window pops up, and the task still records
# the job's exit code. Failure toasts (notify.ps1) still show.
function JobAction([string]$Job) {
    $arg = "`"$hidden`" powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$script`""
    if ($Job) { $arg += " -Job $Job" }
    New-ScheduledTaskAction -Execute "wscript.exe" -Argument $arg
}
$action = JobAction ""
$triggers = @(
    New-ScheduledTaskTrigger -Daily -At 6:00pm
    New-ScheduledTaskTrigger -Weekly -DaysOfWeek Wednesday, Thursday, Friday, Saturday -At 2:00pm
    New-ScheduledTaskTrigger -Weekly -DaysOfWeek Wednesday, Thursday, Friday, Saturday -At 9:00pm
)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 1)
Register-ScheduledTask -TaskName "fantasy-rag ingest" -Action $action -Trigger $triggers `
    -Settings $settings -Description "fantasy-rag: nflverse + Sleeper ingest and injury snapshots" -Force

# News: every hour while the PC is on (PLAN: every 30-60 minutes in season).
$newsAction = JobAction "news"
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
$forwardAction = JobAction "forward"
$tuesday = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Tuesday -At $local.ToString("HH:mm")
$forwardSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1)
Register-ScheduledTask -TaskName "fantasy-rag forward" -Action $forwardAction -Trigger $tuesday `
    -Settings $forwardSettings -Description "fantasy-rag: Phase 8 Tuesday forward predictions" -Force

# Pre-deadline check: Tuesdays 11:30 ET (30 minutes before the forward run's deadline).
$check = [System.DateTime]::SpecifyKind((Get-Date).Date.AddHours(11).AddMinutes(30), "Unspecified")
$checkLocal = [System.TimeZoneInfo]::ConvertTime($check, $eastern, [System.TimeZoneInfo]::Local)
$checkAction = JobAction "forward-check"
Register-ScheduledTask -TaskName "fantasy-rag forward-check" -Action $checkAction `
    -Trigger (New-ScheduledTaskTrigger -Weekly -DaysOfWeek Tuesday -At $checkLocal.ToString("HH:mm")) `
    -Settings (New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 10)) `
    -Description "fantasy-rag: warn if this week's on-time forward run is missing" -Force

# Backup: daily 7 PM, outside the repo (%USERPROFILE%antasy-rag-backups or BACKUP_DIR);
# on Sundays the job also restores the newest dump into a fresh container and checks it.
$backupAction = JobAction "backup"
Register-ScheduledTask -TaskName "fantasy-rag backup" -Action $backupAction `
    -Trigger (New-ScheduledTaskTrigger -Daily -At 7:00pm) `
    -Settings (New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 30)) `
    -Description "fantasy-rag: verified local backup (weekly restore drill)" -Force
