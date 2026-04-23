# StudioMind — Microsoft MIDI Services bootstrap
#
# Downloads and launches the two installers StudioMind depends on:
#   1. Windows MIDI Services Runtime + SDK + Tools (x64)
#   2. Basic MIDI 1.0 Loopback plugin (x64)
#
# The plugin is a preview MSIX and requires Developer Mode enabled
# (Settings -> System -> For developers -> Developer Mode ON) BEFORE running.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts\install-midi-services.ps1
#
# Or directly from GitHub on a fresh machine (no git needed):
#   iex (irm https://raw.githubusercontent.com/mpampisss787/studiomind/main/scripts/install-midi-services.ps1)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"  # Invoke-WebRequest is glacial without this

function Assert-Prereqs {
    if ([Environment]::Is64BitOperatingSystem -eq $false) {
        throw "This script requires 64-bit Windows."
    }
    # Developer Mode check (registry)
    $devKey = "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\AppModelUnlock"
    $devMode = (Get-ItemProperty -Path $devKey -Name "AllowDevelopmentWithoutDevLicense" -ErrorAction SilentlyContinue).AllowDevelopmentWithoutDevLicense
    if ($devMode -ne 1) {
        Write-Host ""
        Write-Host "WARNING: Developer Mode is NOT enabled." -ForegroundColor Yellow
        Write-Host "The Loopback plugin installer will fail without it." -ForegroundColor Yellow
        Write-Host "Enable it at: Settings -> System -> For developers -> Developer Mode" -ForegroundColor Yellow
        Write-Host ""
        $cont = Read-Host "Continue anyway? [y/N]"
        if ($cont -notmatch '^[yY]') { exit 1 }
    }
}

function Get-LatestAssets {
    # Microsoft/MIDI publishes its releases as pre-releases, so /releases/latest
    # returns 404. Pull the full /releases list and find the newest release that
    # has both installers we need — runtime and loopback are sometimes shipped
    # in different releases.
    Write-Host "Fetching Microsoft MIDI Services releases..."
    $headers = @{ "User-Agent" = "studiomind-installer" }
    $releases = Invoke-RestMethod "https://api.github.com/repos/microsoft/MIDI/releases?per_page=30" `
                                   -Headers $headers

    $runtime = $null
    $loopback = $null
    $runtimeTag = $null
    $loopbackTag = $null

    foreach ($rel in $releases) {
        if ($rel.draft) { continue }
        if (-not $runtime) {
            $match = $rel.assets | Where-Object {
                $_.name -like "Windows.MIDI.Services.SDK.Runtime.and.Tools*x64.exe"
            } | Select-Object -First 1
            if ($match) { $runtime = $match; $runtimeTag = $rel.tag_name }
        }
        if (-not $loopback) {
            $match = $rel.assets | Where-Object {
                $_.name -like "Windows.MIDI.Services.Basic.MIDI*Loopback*x64.exe"
            } | Select-Object -First 1
            if ($match) { $loopback = $match; $loopbackTag = $rel.tag_name }
        }
        if ($runtime -and $loopback) { break }
    }

    if (-not $runtime)  { throw "Runtime/SDK/Tools installer not found in any recent release." }
    if (-not $loopback) { throw "Loopback plugin installer not found in any recent release." }

    Write-Host "  Runtime:  $($runtime.name)   [$runtimeTag]"
    Write-Host "  Loopback: $($loopback.name)  [$loopbackTag]"
    return @{ Runtime = $runtime; Loopback = $loopback }
}

function Download-Asset {
    param($Asset, $OutDir)
    $out = Join-Path $OutDir $Asset.name
    if (Test-Path $out) {
        Write-Host "  Already downloaded: $($Asset.name)"
        return $out
    }
    Write-Host "  Downloading $($Asset.name) ..."
    Invoke-WebRequest -Uri $Asset.browser_download_url -OutFile $out
    return $out
}

# ── Main ──────────────────────────────────────────────────────────────

Assert-Prereqs

$downloads = Join-Path $env:USERPROFILE "Downloads"
if (-not (Test-Path $downloads)) { New-Item -ItemType Directory -Path $downloads | Out-Null }

$assets = Get-LatestAssets
$runtimePath  = Download-Asset -Asset $assets.Runtime  -OutDir $downloads
$loopbackPath = Download-Asset -Asset $assets.Loopback -OutDir $downloads

Write-Host ""
Write-Host "=== Step 1/2: Installing MIDI Services Runtime + SDK + Tools ===" -ForegroundColor Cyan
Write-Host "An installer UI will appear. Accept defaults."
Write-Host ""
Start-Process -FilePath $runtimePath -Wait

Write-Host ""
Write-Host "Runtime installed." -ForegroundColor Green
Write-Host ""
Write-Host "It is strongly recommended to RESTART WINDOWS now before installing the loopback plugin." -ForegroundColor Yellow
Write-Host "Restart, then re-run this script — it will skip the download and go straight to step 2."
Write-Host ""
$choice = Read-Host "Restart now? [y/N] (answer N only if you want to install loopback immediately without restart)"

if ($choice -match '^[yY]') {
    Write-Host "Restarting in 10 seconds. Re-run this script after login."
    shutdown /r /t 10
    exit 0
}

Write-Host ""
Write-Host "=== Step 2/2: Installing Basic MIDI 1.0 Loopback plugin ===" -ForegroundColor Cyan
Write-Host "An installer UI will appear. Accept defaults."
Write-Host "If this fails with 'Developer mode must be enabled', enable it and re-run this script."
Write-Host ""
Start-Process -FilePath $loopbackPath -Wait

Write-Host ""
Write-Host "Loopback plugin installed." -ForegroundColor Green
Write-Host ""
Write-Host "=== Final step — do this yourself ===" -ForegroundColor Cyan
Write-Host "1. Open 'Windows MIDI Settings' (search Start menu)."
Write-Host "2. Click 'Finish MIDI Setup'."
Write-Host "   This creates 'Default App Loopback (A)' and 'Default App Loopback (B)' endpoints."
Write-Host ""
Write-Host "Then continue the StudioMind install with: pip install -e ."
