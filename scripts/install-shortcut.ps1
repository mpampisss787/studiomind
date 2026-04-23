# StudioMind — desktop shortcut installer
#
# Creates a Windows shortcut on the user's Desktop that launches the
# StudioMind web UI (pythonw -m studiomind web) and opens the browser,
# so end users don't need to touch PowerShell to use the app.
#
# Run once after 'pip install -e .':
#
#   powershell -ExecutionPolicy Bypass -File scripts\install-shortcut.ps1
#
# Uninstall: just delete the shortcut from your Desktop.

$ErrorActionPreference = "Stop"

# Find pythonw.exe next to python.exe
$pythonCmd = Get-Command python -ErrorAction SilentlyContinue
if (-not $pythonCmd) {
    Write-Host "python.exe not found on PATH. Install Python 3.12 first." -ForegroundColor Red
    exit 1
}
$pythonw = Join-Path (Split-Path $pythonCmd.Path -Parent) "pythonw.exe"
if (-not (Test-Path $pythonw)) {
    Write-Host "pythonw.exe not found next to python.exe (looked at $pythonw)." -ForegroundColor Red
    exit 1
}

# Repo root = parent of the scripts folder this file lives in
$repoRoot = Split-Path -Parent $PSScriptRoot

# Sanity check — the studiomind package should be importable from here
$pkg = Join-Path $repoRoot "src\studiomind\__init__.py"
if (-not (Test-Path $pkg)) {
    Write-Host "This script expects to live in <studiomind-repo>\scripts\. Can't find $pkg." -ForegroundColor Red
    exit 1
}

$desktop = [Environment]::GetFolderPath("Desktop")
$linkPath = Join-Path $desktop "StudioMind.lnk"

$shell = New-Object -ComObject WScript.Shell
$link = $shell.CreateShortcut($linkPath)
$link.TargetPath       = $pythonw
$link.Arguments        = "-m studiomind web"
$link.WorkingDirectory = $repoRoot
$link.IconLocation     = $pythonw
$link.Description      = "StudioMind - AI mixing engineer for FL Studio"
$link.Save()

Write-Host ""
Write-Host "Created: $linkPath" -ForegroundColor Green
Write-Host "Double-click to launch. The web UI opens at http://127.0.0.1:8040"
Write-Host ""
