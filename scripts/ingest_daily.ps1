# Runs one structured ingest (snapshots included) and appends the result to data/logs.
# Invoked by the Windows scheduled task that scripts/register_daily_ingest.ps1 creates.
$ErrorActionPreference = "Continue"
$repo = Split-Path -Parent $PSScriptRoot
$logDir = Join-Path $repo "data\logs"
New-Item -ItemType Directory -Force $logDir | Out-Null
$log = Join-Path $logDir ("ingest_{0}.log" -f (Get-Date -Format "yyyy-MM"))

"=== $(Get-Date -Format o)" | Out-File -Append -Encoding utf8 $log
Set-Location $repo
# A run that catches up right after boot finds Docker Desktop still off: start it and wait.
docker info *> $null
if ($LASTEXITCODE -ne 0) {
    "starting Docker Desktop" | Out-File -Append -Encoding utf8 $log
    Start-Process "C:\Program Files\Docker\Docker\Docker Desktop.exe"
    for ($i = 0; $i -lt 36; $i++) {
        Start-Sleep 5
        docker info *> $null
        if ($LASTEXITCODE -eq 0) { break }
    }
}
docker compose up -d --wait db 2>&1 | Out-File -Append -Encoding utf8 $log
Set-Location (Join-Path $repo "backend")
$env:PYTHONIOENCODING = "utf-8"
uv run fantasy-rag ingest structured 2>&1 | Out-File -Append -Encoding utf8 $log
$ingestExit = $LASTEXITCODE
# League rosters and matchups (needs SLEEPER_USERNAME in .env; fails without it).
uv run fantasy-rag league import 2>&1 | Out-File -Append -Encoding utf8 $log
$leagueExit = $LASTEXITCODE
"exit=$ingestExit league=$leagueExit" | Out-File -Append -Encoding utf8 $log
# Task Scheduler must see a failure if either job failed, or stale data looks fresh.
if ($ingestExit -ne 0) { exit $ingestExit }
exit $leagueExit
