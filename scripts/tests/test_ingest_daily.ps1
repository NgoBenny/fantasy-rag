# ingest_daily.ps1 when its log is held open, on a throwaway copy with fake uv, docker and toast
# (no database, network or real task). Windows only.
#   1. forward + forward-check together: forward-check holding its log never stops forward.
#   2. Another copy of forward holds the log: the job fails with a toast, never a silent exit 0.
$ErrorActionPreference = "Stop"
$root = Join-Path $env:TEMP ("ingest-daily-test-" + [guid]::NewGuid())
$failed = 0
function Check([string]$what, [bool]$ok) {
    "{0}  {1}" -f $(if ($ok) { "ok  " } else { $script:failed++; "FAIL" }), $what
}
try {
    New-Item -ItemType Directory -Force "$root\scripts", "$root\backend", "$root\data\logs", "$root\bin" | Out-Null
    Copy-Item (Join-Path $PSScriptRoot "..\ingest_daily.ps1") "$root\scripts\"
    Set-Content "$root\scripts\notify.ps1" 'param($Title, $Message); Set-Content (Join-Path $PSScriptRoot "..\toast.txt") $Message'
    Set-Content "$root\bin\docker.cmd" "@exit /b 0"
    Set-Content "$root\bin\uv.cmd" "@echo %* >> `"$root\uv_calls.txt`""
    $env:PATH = "$root\bin;$env:PATH"
    $month = Get-Date -Format "yyyy-MM"

    function RunForward {
        Remove-Item "$root\uv_calls.txt", "$root\toast.txt" -ErrorAction SilentlyContinue
        $ErrorActionPreference = "Continue"  # the job's stderr is expected here, not a test crash
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$root\scripts\ingest_daily.ps1" -Job forward *> $null
        $LASTEXITCODE
    }
    function Hold([string]$name) {  # the way a running Out-File pipeline holds a log
        [IO.File]::Open("$root\data\logs\${name}_$month.log", "Append", "Write", "Read")
    }

    $held = Hold "forward-check"
    try { $code = RunForward } finally { $held.Dispose() }
    $calls = @(Get-Content "$root\uv_calls.txt" -ErrorAction SilentlyContinue)
    Check "forward-check's held log doesn't block forward (exit $code)" ($code -eq 0)
    Check "ingest and predict both ran" (($calls -match "ingest structured") -and ($calls -match "predict"))

    $held = Hold "forward"
    try { $code = RunForward } finally { $held.Dispose() }
    Check "a locked log fails the job (exit $code)" ($code -eq 99)
    Check "the command never started" (-not (Test-Path "$root\uv_calls.txt"))
    Check "the failure still shows a toast" (Test-Path "$root\toast.txt")
} finally {
    Remove-Item -Recurse -Force $root -ErrorAction SilentlyContinue
}
if ($failed) { exit 1 }
"passed"
