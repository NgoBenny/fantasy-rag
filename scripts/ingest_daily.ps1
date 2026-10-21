# Runs one scheduled job and appends the result to data/logs.
#   -Job daily (default): structured ingest (snapshots) + league import
#   -Job news: news feeds -> documents, chunks, embeddings; Sleeper transactions
#   -Job forward: Tuesday 10:30 ET: fresh stats, then the Phase 8 forward run (predict)
#   -Job forward-check: Tuesday 11:30 ET: toast if this week's on-time forward run is missing
#   -Job backup: verified dump outside the repo; on Sundays also the restore drill
# Any failing job shows a Windows toast (scripts/notify.ps1); details stay in data/logs.
# Invoked by the scheduled tasks that scripts/register_daily_ingest.ps1 creates.
param([ValidateSet("daily", "news", "forward", "forward-check", "backup")] [string]$Job = "daily")
$ErrorActionPreference = "Continue"
$repo = Split-Path -Parent $PSScriptRoot
$logDir = Join-Path $repo "data\logs"
New-Item -ItemType Directory -Force $logDir | Out-Null
$name = @{ daily = "ingest"; news = "news"; forward = "forward"; "forward-check" = "forward"; backup = "backup" }[$Job]
$log = Join-Path $logDir ("{0}_{1}.log" -f $name, (Get-Date -Format "yyyy-MM"))

# Exit with the job's code; a failure also shows a toast (logs hold the details).
function Finish([int]$code, [string]$message) {
    if ($code -ne 0) {
        & (Join-Path $PSScriptRoot "notify.ps1") -Title "fantasy-rag: $Job failed" -Message $message |
            Out-File -Append -Encoding utf8 $log
    }
    exit $code
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
    uv run fantasy-rag ingest news 2>&1 | Out-File -Append -Encoding utf8 $log
    $newsExit = $LASTEXITCODE
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
    uv run fantasy-rag league sync-transactions 2>&1 | Out-File -Append -Encoding utf8 $log
    $txExit = $LASTEXITCODE
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
    uv run fantasy-rag backup 2>&1 | Out-File -Append -Encoding utf8 $log
    $code = $LASTEXITCODE
    if ($code -eq 0 -and (Get-Date).DayOfWeek -eq "Sunday") {
        uv run fantasy-rag backup --verify-latest 2>&1 | Out-File -Append -Encoding utf8 $log
        $code = $LASTEXITCODE
    }
    Finish $code "backup or restore drill failed (exit $code); see $log"
}
if ($Job -eq "forward") {
    uv run fantasy-rag ingest structured 2>&1 | Out-File -Append -Encoding utf8 $log
    $ingestExit = $LASTEXITCODE
    # Predict even if the ingest failed: the run records the data it had and when (its
    # snapshot time), and is marked late if that is after Tuesday noon ET.
    uv run fantasy-rag predict 2>&1 | Out-File -Append -Encoding utf8 $log
    $predictExit = $LASTEXITCODE
    "exit=$ingestExit predict=$predictExit" | Out-File -Append -Encoding utf8 $log
    $code = if ($predictExit -ne 0) { $predictExit } else { $ingestExit }
    Finish $code "forward run: ingest exit $ingestExit, predict exit $predictExit; see $log"
}
uv run fantasy-rag ingest structured 2>&1 | Out-File -Append -Encoding utf8 $log
$ingestExit = $LASTEXITCODE
# League rosters and matchups (needs SLEEPER_USERNAME in .env; fails without it).
uv run fantasy-rag league import 2>&1 | Out-File -Append -Encoding utf8 $log
$leagueExit = $LASTEXITCODE
"exit=$ingestExit league=$leagueExit" | Out-File -Append -Encoding utf8 $log
# Task Scheduler must see a failure if either job failed, or stale data looks fresh.
$code = if ($ingestExit -ne 0) { $ingestExit } else { $leagueExit }
Finish $code "daily ingest exit $ingestExit, league import exit $leagueExit; see $log"
