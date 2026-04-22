# StudioMind Windows Installer
# Run in PowerShell: irm https://raw.githubusercontent.com/mpampisss787/studiomind/main/scripts/install_windows.ps1 | iex
# Or manually: .\install_windows.ps1

$ErrorActionPreference = "Stop"

Write-Host "`n=== StudioMind Installer ===" -ForegroundColor Cyan
Write-Host "AI mixing engineer for FL Studio`n"

# --- 1. Check Python ---
$python = $null
foreach ($cmd in @("python", "python3", "py")) {
    try {
        $ver = & $cmd --version 2>&1
        if ($ver -match "Python 3\.\d+") {
            $python = $cmd
            Write-Host "[OK] Found $ver" -ForegroundColor Green
            break
        }
    } catch {}
}
if (-not $python) {
    Write-Host "[ERROR] Python 3 not found. Install from https://python.org" -ForegroundColor Red
    exit 1
}

# --- 2. Check Git ---
try {
    $gitVer = git --version 2>&1
    Write-Host "[OK] Found $gitVer" -ForegroundColor Green
} catch {
    Write-Host "[ERROR] Git not found. Install from https://git-scm.com" -ForegroundColor Red
    exit 1
}

# --- 3. Clone or update repo ---
$installDir = "$env:USERPROFILE\StudioMind"
if (Test-Path "$installDir\.git") {
    Write-Host "[..] Updating existing installation..." -ForegroundColor Yellow
    Push-Location $installDir
    git pull --ff-only
    Pop-Location
} else {
    Write-Host "[..] Cloning StudioMind..." -ForegroundColor Yellow
    git clone https://github.com/mpampisss787/studiomind.git $installDir
}

# --- 4. Install Python package ---
Write-Host "[..] Installing Python dependencies..." -ForegroundColor Yellow
Push-Location $installDir
& $python -m pip install -e ".[render]" --quiet
Pop-Location
Write-Host "[OK] Python package installed" -ForegroundColor Green

# --- 5. Copy FL device script ---
$flHardwareDirs = @(
    "$env:USERPROFILE\Documents\Image-Line\FL Studio\Settings\Hardware",
    "$env:USERPROFILE\Documents\Image-Line\FL Studio 2024\Settings\Hardware",
    "${env:ProgramFiles}\Image-Line\FL Studio\Settings\Hardware",
    "${env:ProgramFiles(x86)}\Image-Line\FL Studio\Settings\Hardware"
)

$scriptSrc = "$installDir\scripts\device_StudioMind.py"
$installed = $false

foreach ($dir in $flHardwareDirs) {
    if (Test-Path $dir) {
        Copy-Item $scriptSrc "$dir\device_StudioMind.py" -Force
        Write-Host "[OK] Device script installed to: $dir" -ForegroundColor Green
        $installed = $true
        break
    }
}

if (-not $installed) {
    Write-Host "[WARN] Could not find FL Studio Hardware folder automatically." -ForegroundColor Yellow
    Write-Host "       Manually copy this file:" -ForegroundColor Yellow
    Write-Host "       FROM: $scriptSrc" -ForegroundColor White
    Write-Host "       TO:   <FL Studio>\Settings\Hardware\device_StudioMind.py" -ForegroundColor White
}

# --- 6. Check for loopMIDI ---
$loopMidi = Get-Process -Name "loopMIDI" -ErrorAction SilentlyContinue
if ($loopMidi) {
    Write-Host "[OK] loopMIDI is running" -ForegroundColor Green
} else {
    $loopExe = "${env:ProgramFiles(x86)}\Tobias Erichsen\loopMIDI\loopMIDI.exe"
    if (Test-Path $loopExe) {
        Write-Host "[WARN] loopMIDI is installed but not running. Start it and create a port named 'StudioMind'" -ForegroundColor Yellow
    } else {
        Write-Host "[WARN] loopMIDI not found. Download from https://www.tobias-erichsen.de/software/loopmidi.html" -ForegroundColor Yellow
        Write-Host "       After installing, create a virtual MIDI port named 'StudioMind'" -ForegroundColor Yellow
    }
}

# --- 7. Done ---
Write-Host "`n=== Installation Complete ===" -ForegroundColor Cyan
Write-Host "`nNext steps:" -ForegroundColor White
Write-Host "  1. Install loopMIDI (if not already) and create a port named 'StudioMind'"
Write-Host "  2. Open FL Studio -> Options -> MIDI Settings"
Write-Host "     - Input:  Select the 'StudioMind' port, set Controller type to 'StudioMind'"
Write-Host "     - Output: Select the 'StudioMind' port, set Controller type to 'StudioMind'"
Write-Host "  3. Test the connection:"
Write-Host "     studiomind ping" -ForegroundColor Green
Write-Host ""
