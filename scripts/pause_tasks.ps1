# Invoke-WithTasksPaused: run a block while no matching scheduled task can start or is running.
# Checking "nothing is running" once isn't enough: a trigger can fire right after the check.
# So the tasks are disabled first, then checked (a run already in progress keeps going when
# its task is disabled), and every task's previous enabled state is restored in `finally`.
# A trigger that fires while its task is disabled is skipped, not made up later.
# Dot-source it: . scripts/pause_tasks.ps1   (tested by scripts/tests/test_pause_tasks.ps1)

function Get-RunningTaskNames([string]$Pattern) {
    # The scheduler's own list of running instances; a disabled task's State hides its run.
    $svc = New-Object -ComObject Schedule.Service
    $svc.Connect()
    @($svc.GetRunningTasks(0) | Where-Object { $_.Name -like $Pattern } | ForEach-Object Name)
}

function Invoke-WithTasksPaused([string]$Pattern, [scriptblock]$Body) {
    $tasks = @(Get-ScheduledTask -TaskName $Pattern -ErrorAction SilentlyContinue)
    $enabled = @($tasks | Where-Object State -ne "Disabled" | ForEach-Object TaskName)
    $disabled = @($tasks | Where-Object State -eq "Disabled" | ForEach-Object TaskName)
    try {
        foreach ($t in $enabled) { Disable-ScheduledTask -TaskName $t | Out-Null }
        $running = Get-RunningTaskNames $Pattern
        if ($running) { throw "wait: running now: $($running -join ', ')" }
        & $Body
    } finally {
        # Re-registering inside the block re-enables tasks: put back exactly what was there.
        foreach ($t in $enabled) { Enable-ScheduledTask -TaskName $t | Out-Null }
        foreach ($t in $disabled) {
            if (Get-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue) {
                Disable-ScheduledTask -TaskName $t | Out-Null
            }
        }
    }
}
