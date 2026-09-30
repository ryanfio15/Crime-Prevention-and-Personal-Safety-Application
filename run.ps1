# Starts the local stack: database (Docker), schema migrations, and the web
# server with auto-reload. Usage, from the repo root:
#
#   .\run.ps1              start everything
#   .\run.ps1 -Refresh     also pull the latest crime data before starting
#
# Python edits under safety/ restart the server automatically; edits under
# web/ only need a browser refresh. Ctrl+C stops the server.

param(
    [switch]$Refresh
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"

function Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }

# 1. Docker must be running before the database container can start.
Step "Checking Docker"
# Windows PowerShell 5.1 turns a native command's stderr into an error record,
# and under "Stop" that aborts the script -- so `docker info` failing (Docker
# not running) would throw here before the auto-start below ever ran. Relax
# the preference for this block and rely on $LASTEXITCODE instead.
$ErrorActionPreference = "Continue"
docker info *> $null
if ($LASTEXITCODE -ne 0) {
    $dockerDesktop = "C:\Program Files\Docker\Docker\Docker Desktop.exe"
    if (-not (Test-Path $dockerDesktop)) {
        throw "Docker is not running and Docker Desktop was not found. Start Docker and try again."
    }
    Write-Host "Starting Docker Desktop (this can take a minute)..."
    Start-Process $dockerDesktop
    $deadline = (Get-Date).AddMinutes(3)
    do {
        Start-Sleep -Seconds 3
        docker info *> $null
    } until ($LASTEXITCODE -eq 0 -or (Get-Date) -gt $deadline)
    if ($LASTEXITCODE -ne 0) { throw "Docker did not start within 3 minutes." }
}
$ErrorActionPreference = "Stop"

# 2. Database. --wait blocks until the healthcheck in docker-compose.yml passes.
Step "Starting database"
docker compose up -d --wait
if ($LASTEXITCODE -ne 0) { throw "Database failed to start." }

# 3. Python environment. Only does real work on first run or when
#    requirements.txt changes.
if (-not (Test-Path $python)) {
    Step "Creating Python environment"
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw "Could not create .venv. Is Python installed and on PATH?" }
}
Step "Checking Python packages"
& $python -m pip install -q --disable-pip-version-check -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw "pip install failed." }

if (-not (Test-Path ".env")) { Copy-Item ".env.example" ".env" }

# 4. Schema. Applies only migrations that have not run yet, so new files in
#    db/migrations/ are picked up automatically.
Step "Applying database migrations"
& $python -m safety.migrate
if ($LASTEXITCODE -ne 0) { throw "Migrations failed." }

# 5. Data. Backfill on an empty database; otherwise only when asked.
$status = & $python -m safety.etl.run status | Out-String | ConvertFrom-Json
if (-not $status.city_snapshots) {
    Step "No data loaded yet - running the 24-month backfill (about 3 minutes)"
    & $python -m safety.etl.run backfill --city phl
    if ($LASTEXITCODE -ne 0) { throw "Backfill failed." }
} elseif ($Refresh) {
    Step "Pulling latest data"
    & $python -m safety.etl.run incremental --city phl
    if ($LASTEXITCODE -ne 0) { throw "Incremental load failed." }
}

# 6. Web server.
Step "Starting web server"
Write-Host "Open http://127.0.0.1:8000/  (Ctrl+C to stop)" -ForegroundColor Green
& $python -m uvicorn safety.api.main:app --host 127.0.0.1 --port 8000 --reload --reload-dir safety
