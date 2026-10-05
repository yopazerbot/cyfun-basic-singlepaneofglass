# Start the application locally with auto-reload, reading .env from the repository root.
# Usage: .\scripts\run-dev.ps1 [-Python C:\path\to\python.exe] [-Port 8000]
param(
    [string]$Python = "",
    [int]$Port = 8000
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
if (-not $Python) {
    if (Test-Path "$root\.venv\Scripts\python.exe") { $Python = "$root\.venv\Scripts\python.exe" }
    elseif (Test-Path "$env:USERPROFILE\.venvs\cyfun\Scripts\python.exe") { $Python = "$env:USERPROFILE\.venvs\cyfun\Scripts\python.exe" }
    else { $Python = "python" }
}
if (-not (Test-Path "$root\.env")) {
    Copy-Item "$root\.env.example" "$root\.env"
    Write-Host "Created .env from .env.example. Edit it when you want Entra ID sign-in or connectors." -ForegroundColor Yellow
}
$env:PYTHONPATH = "$root\app"
Write-Host "Starting on http://localhost:$Port (first login: admin / admin, then set a new password)" -ForegroundColor Cyan
& $Python -m uvicorn cyfun.main:app --host 127.0.0.1 --port $Port --reload --reload-dir "$root\app"
