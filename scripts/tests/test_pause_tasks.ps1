# Regression test for scripts/pause_tasks.ps1 (Windows Task Scheduler; run by hand, not in CI):
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts/tests/test_pause_tasks.ps1
# Uses throwaway tasks named "fantasy-rag-pausetest *" (current user); the real tasks are
# never touched. Exits 1 on the first failed check.
$ErrorActionPreference = "Stop"
. (Join-Path (Split-Path -Parent $PSScriptRoot) "pause_tasks.ps1")
$pattern = "fantasy-rag-pausetest*"
$a, $b = "fantasy-rag-pausetest a", "fantasy-rag-pausetest b"

function Check([bool]$ok, [string]$what) {
    if (-not $ok) { Write-Host "FAIL: $what"; exit 1 }
    Write-Host "ok: $what"
}

$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -Command Start-Sleep 60"
foreach ($t in $a, $b) {
    Register-ScheduledTask -TaskName $t -Action $action -Force | Out-Null
}
Disable-ScheduledTask -TaskName $b | Out-Null
try {
    # A trigger firing after the update started (simulated by starting the task) can't run.
    $ranInside = $null
    Invoke-WithTasksPaused $pattern {
        try { Start-ScheduledTask -TaskName $a } catch { }  # refused: the task is disabled
        Start-Sleep 2
        $script:ranInside = Get-RunningTaskNames $pattern
    }
    Check (-not $ranInside) "a task started during the update window did not run"
    Check ((Get-ScheduledTask -TaskName $a).State -eq "Ready") "an enabled task is enabled again"
    Check ((Get-ScheduledTask -TaskName $b).State -eq "Disabled") "a disabled task stays disabled"

    # Re-registering inside the block (as -Register does) re-enables everything: undone.
    Invoke-WithTasksPaused $pattern {
        Register-ScheduledTask -TaskName $b -Action $action -Force | Out-Null
    }
    Check ((Get-ScheduledTask -TaskName $b).State -eq "Disabled") "re-registered task keeps its disabled state"

    # A run already in progress: refused, the body never runs, states restored.
    Start-ScheduledTask -TaskName $a
    Start-Sleep 2
    $bodyRan = $false
    try {
        Invoke-WithTasksPaused $pattern { $script:bodyRan = $true }
        $refused = $false
    } catch { $refused = "$_" -like "wait: running now:*$a*" }
    Check ($refused -and -not $bodyRan) "an update is refused while a task is running"
    Check ((Get-ScheduledTask -TaskName $a).State -ne "Disabled") "the running task is enabled again"

    # A failing update still restores the enabled states.
    Stop-ScheduledTask -TaskName $a
    try { Invoke-WithTasksPaused $pattern { throw "update failed" } } catch { }
    Check ((Get-ScheduledTask -TaskName $a).State -eq "Ready") "states restored after a failed update"
} finally {
    foreach ($t in $a, $b) {
        Stop-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $t -Confirm:$false -ErrorAction SilentlyContinue
    }
}
Write-Host "all passed"
