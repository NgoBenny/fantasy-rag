# Creates or updates the checkout the scheduled tasks run from, so editing this working tree
# never breaks a collection run. It tracks reviewed `main` on GitHub and has its own .env
# (copied once, ignored by git) and its own virtual environment; it shares the database
# container (docker-compose.yml pins the project name).
#   powershell -File scripts/setup_runner.ps1              # create, or update after a merge
#   powershell -File scripts/setup_runner.ps1 -Register    # also point the scheduled tasks at it
# Never runs during a job: the fantasy-rag tasks are disabled for the whole update (refused if
# one is already running) and restored afterwards (scripts/pause_tasks.ps1); a trigger due in
# that minute is skipped. Scheduled runs never pull; update by running this between jobs. It applies no migrations (the database
# is shared: coordinate schema changes as before).
param(
    [string]$Path = (Join-Path $env:USERPROFILE "fantasy-rag-runner"),
    [string]$Branch = "main",
    [switch]$Register
)
$ErrorActionPreference = "Stop"
$dev = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot "pause_tasks.ps1")

function Run([string]$what) {
    # Native commands don't stop on failure under $ErrorActionPreference in PowerShell 5.1.
    if ($LASTEXITCODE -ne 0) { throw "$what failed (exit $LASTEXITCODE)" }
}

Invoke-WithTasksPaused "fantasy-rag*" {
    if (-not (Test-Path (Join-Path $Path ".git"))) {
        $url = git -C $dev remote get-url origin; Run "git remote get-url"
        git clone --branch $Branch $url $Path; Run "git clone"
    } else {
        if (git -C $Path status --porcelain --untracked-files=no) {
            throw "$Path has local changes; it must stay an exact copy of $Branch"
        }
        git -C $Path fetch origin; Run "git fetch"
        git -C $Path checkout $Branch; Run "git checkout"
        git -C $Path merge --ff-only "origin/$Branch"; Run "git merge --ff-only"
    }

    $envFile = Join-Path $Path ".env"
    if (-not (Test-Path $envFile)) {
        Copy-Item (Join-Path $dev ".env") $envFile
        "copied .env from $dev (edit $envFile to change the runner's settings)"
    } elseif ((Get-FileHash $envFile).Hash -ne (Get-FileHash (Join-Path $dev ".env")).Hash) {
        "note: $envFile differs from $dev\.env (kept as is)"
    }

    # Seed the caches once: the embedding model, and today's Sleeper players file (fetched at
    # most once a day, so a fresh runner must not fetch it again).
    $cache = Join-Path $Path "data\cache"
    if (-not (Test-Path $cache) -and (Test-Path (Join-Path $dev "data\cache"))) {
        Copy-Item -Recurse (Join-Path $dev "data\cache") $cache
        "copied data\cache from $dev"
    }

    Push-Location (Join-Path $Path "backend")
    try { uv sync --frozen; Run "uv sync" } finally { Pop-Location }
    "runner at $Path on $(git -C $Path rev-parse --short HEAD) ($Branch)"

    if ($Register) {
        # Same task names: re-registering replaces the existing tasks, never duplicates them.
        & (Join-Path $Path "scripts\register_daily_ingest.ps1")
        Get-ScheduledTask -TaskName "fantasy-rag*" |
            Select-Object TaskName, @{ n = "Runs"; e = { $_.Actions[0].Arguments } } | Format-Table -AutoSize
    }
}
# Restored states: tasks that were enabled before are enabled again.
Get-ScheduledTask -TaskName "fantasy-rag*" | Select-Object TaskName, State | Format-Table -AutoSize
