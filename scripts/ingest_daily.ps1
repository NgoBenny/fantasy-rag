# Runs one structured ingest (snapshots included) and appends the result to data/logs.
# Invoked by the Windows scheduled task that scripts/register_daily_ingest.ps1 creates.
$ErrorActionPreference = "Continue"
$repo = Split-Path -Parent $PSScriptRoot
$logDir = Join-Path $repo "data\logs"
New-Item -ItemType Directory -Force $logDir | Out-Null
$log = Join-Path $logDir ("ingest_{0}.log" -f (Get-Date -Format "yyyy-MM"))

"=== $(Get-Date -Format o)" | Out-File -Append -Encoding utf8 $log
Set-Location $repo
docker compose up -d db 2>&1 | Out-File -Append -Encoding utf8 $log
Set-Location (Join-Path $repo "backend")
$env:PYTHONIOENCODING = "utf-8"
uv run fantasy-rag ingest structured 2>&1 | Out-File -Append -Encoding utf8 $log
$ingestExit = $LASTEXITCODE
# League rosters and matchups (uses SLEEPER_USERNAME from .env; skipped if unset).
uv run fantasy-rag league import 2>&1 | Out-File -Append -Encoding utf8 $log
"exit=$ingestExit league=$LASTEXITCODE" | Out-File -Append -Encoding utf8 $log
exit $ingestExit
