# Runs one scheduled job and appends the result to data/logs.
#   -Job daily (default): structured ingest (snapshots) + league import
#   -Job news: news feeds -> documents, chunks, embeddings; Sleeper transactions
#   -Job forward: Tuesday 16:00 ET: fresh stats, then the Phase 8 forward run (predict)
#   -Job forward-check: Tuesday 20:30 ET: toast if this week's on-time forward run is missing
#   -Job backup: verified dump outside the repo; on Sundays also the restore drill
# Any failing job shows a Windows toast (scripts/notify.ps1); details stay in data/logs.
# Invoked by the scheduled tasks that scripts/register_daily_ingest.ps1 creates.
param([ValidateSet("daily", "news", "forward", "forward-check", "backup")] [string]$Job = "daily")
$ErrorActionPreference = "Continue"
$repo = Split-Path -Parent $PSScriptRoot
$logDir = Join-Path $repo "data\logs"
New-Item -ItemType Directory -Force $logDir | Out-Null
# One log per job: Out-File holds its file for a whole pipeline, and catch-up runs start together.
$name = if ($Job -eq "daily") { "ingest" } else { $Job }
$log = Join-Path $logDir ("{0}_{1}.log" -f $name, (Get-Date -Format "yyyy-MM"))

# Exit with the job's code; a failure also shows a toast (logs hold the details).
function Finish([int]$code, [string]$message) {
    if ($code -ne 0) {
        # Toast first, then log: a locked log must not cost the toast.
        $out = & (Join-Path $PSScriptRoot "notify.ps1") -Title "fantasy-rag: $Job failed" -Message $message
        $out | Out-File -Append -Encoding utf8 $log
    }
    exit $code
}

# One CLI step, logged. If the log can't be opened (another copy of this job holds it) PowerShell
# never starts the command and keeps the last exit code; 99 keeps that from reading as success.
function Step {
    $global:LASTEXITCODE = 99
    uv run fantasy-rag @args 2>&1 | Out-File -Append -Encoding utf8 $log
    $LASTEXITCODE
}

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
if ($Job -eq "news") {
    $newsExit = Step ingest news
    # Refresh the Phase 4 evidence after every run: stories per source (last 48 hours)
    # and how many rostered-player mentions carry the player's ID. Written to a temp file
    # first, so a failed report never replaces the last good one.
    $report = Join-Path $logDir "news_report.json"
    $tmp = "$report.tmp"
    $err = "$report.err"
    uv run fantasy-rag ingest news-report 2> $err | Out-File -Encoding utf8 $tmp
    $reportExit = $LASTEXITCODE
    if ($reportExit -eq 0) {
        Move-Item -Force $tmp $report
    } else {
        "news-report failed (exit $reportExit); kept the previous report" |
            Out-File -Append -Encoding utf8 $log
        Get-Content $err | Out-File -Append -Encoding utf8 $log
        Remove-Item -Force $tmp -ErrorAction SilentlyContinue
    }
    Remove-Item -Force $err -ErrorAction SilentlyContinue
    # Sleeper adds/drops/trades since the last roster import (waiver availability).
    $txExit = Step league sync-transactions
    "exit=$newsExit report=$reportExit transactions=$txExit" | Out-File -Append -Encoding utf8 $log
    # Task Scheduler must see a failure from any step.
    if ($newsExit -eq 0) { $newsExit = $reportExit }
    if ($newsExit -eq 0) { $newsExit = $txExit }
    Finish $newsExit "news, report or transactions failed (exit $newsExit); see $log"
}
if ($Job -eq "forward-check") {
    $msg = uv run fantasy-rag forward-check 2>&1
    $code = $LASTEXITCODE
    $msg | Out-File -Append -Encoding utf8 $log
    Finish $code "$msg"
}
if ($Job -eq "backup") {
    $code = Step backup
    if ($code -eq 0 -and (Get-Date).DayOfWeek -eq "Sunday") {
        $code = Step backup --verify-latest
    }
    Finish $code "backup or restore drill failed (exit $code); see $log"
}
if ($Job -eq "forward") {
    $ingestExit = Step ingest structured
    # Predict even if the ingest failed: the run records the data it had and when (its
    # snapshot time), and is marked late if that is after Tuesday 21:00 ET.
    $predictExit = Step predict
    "exit=$ingestExit predict=$predictExit" | Out-File -Append -Encoding utf8 $log
    $code = if ($predictExit -ne 0) { $predictExit } else { $ingestExit }
    Finish $code "forward run: ingest exit $ingestExit, predict exit $predictExit; see $log"
}
$ingestExit = Step ingest structured
# League rosters and matchups (needs SLEEPER_USERNAME in .env; fails without it).
$leagueExit = Step league import
"exit=$ingestExit league=$leagueExit" | Out-File -Append -Encoding utf8 $log
# Task Scheduler must see a failure if either job failed, or stale data looks fresh.
$code = if ($ingestExit -ne 0) { $ingestExit } else { $leagueExit }
Finish $code "daily ingest exit $ingestExit, league import exit $leagueExit; see $log"
