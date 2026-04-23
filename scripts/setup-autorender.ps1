# StudioMind — auto-render setup
#
# Installs pywinauto so StudioMind can trigger FL's export dialog
# automatically (Ctrl+R + Enter) instead of asking you to press it.
#
# ONE-TIME SETUP after running this script:
#   1. Open your FL project.
#   2. File -> Export -> WAV
#   3. Set Mode to "Tracks (separate audio files)"
#   4. Set Output folder to:
#      C:\Users\<you>\StudioMind\projects\<project_name>\stems
#   5. Click Start ONCE manually.
#   FL remembers this path. All subsequent auto-renders will use it.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts\setup-autorender.ps1

$ErrorActionPreference = "Stop"

Write-Host ""
Write-Host "=== StudioMind Auto-Render Setup ===" -ForegroundColor Cyan
Write-Host ""

# Check Python
$pythonCmd = Get-Command python -ErrorAction SilentlyContinue
if (-not $pythonCmd) {
    Write-Host "python not found on PATH. Install Python 3.12 x64 first." -ForegroundColor Red
    exit 1
}

# Install pywinauto
Write-Host "Installing pywinauto..."
python -m pip install pywinauto --quiet
if ($LASTEXITCODE -ne 0) {
    Write-Host "pip install failed. Try running as admin or check your Python install." -ForegroundColor Red
    exit 1
}
Write-Host "pywinauto installed." -ForegroundColor Green

# Quick import test
python -c "from pywinauto import Desktop; from pywinauto.keyboard import send_keys; print('OK')" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "pywinauto installed but import failed — may need a restart." -ForegroundColor Yellow
} else {
    Write-Host "pywinauto import OK." -ForegroundColor Green
}

Write-Host ""
Write-Host "=== One-time FL setup (do this now) ===" -ForegroundColor Cyan
Write-Host ""
Write-Host "1. Open your FL project."
Write-Host "2. File -> Export -> WAV"
Write-Host "3. Set Mode: 'Tracks (separate audio files)'"

# Try to show the expected stems path
$projectsRoot = "$env:USERPROFILE\StudioMind\projects"
if (Test-Path $projectsRoot) {
    $projects = Get-ChildItem $projectsRoot -Directory | Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if ($projects) {
        $stemsPath = Join-Path $projects.FullName "stems"
        Write-Host "4. Set Output folder to: $stemsPath" -ForegroundColor Yellow
    } else {
        Write-Host "4. Set Output folder to: $projectsRoot\<project_name>\stems" -ForegroundColor Yellow
    }
} else {
    Write-Host "4. Set Output folder to: $env:USERPROFILE\StudioMind\projects\<project_name>\stems" -ForegroundColor Yellow
}

Write-Host "5. Click Start — export once manually."
Write-Host ""
Write-Host "After that, StudioMind will trigger exports automatically." -ForegroundColor Green
Write-Host "Restart the StudioMind web server to pick up pywinauto."
Write-Host ""
