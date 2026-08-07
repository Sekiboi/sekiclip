# Build the END-USER beta release.
# Primary ship file: dist\Sekiclip-<ver>-Setup.exe  (wizard, no admin)
# Optional: portable zip for USB / no-install use
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

Write-Host "=== Sekiclip end-user release ==="

# Require wizard toolchain up front
$Iscc = & (Join-Path $Root "scripts\ensure_inno.ps1")
Write-Host "Inno Setup: $Iscc"

# Bundle video tools so install-and-play works on a clean PC
& (Join-Path $Root "scripts\fetch_ffmpeg.ps1")
if ($LASTEXITCODE -ne 0) {
    Write-Error "ffmpeg bundle failed. End-user video will not work. Fix network or drop ffmpeg.exe + ffprobe.exe into vendor\"
}

& (Join-Path $Root "scripts\build_exe.ps1")

$DistApp = Join-Path $Root "dist\Sekiclip"
$Exe = Join-Path $DistApp "Sekiclip.exe"
if (-not (Test-Path -LiteralPath $Exe)) {
    Write-Error "Build missing $Exe"
}

$Vendor = Join-Path $DistApp "vendor"
New-Item -ItemType Directory -Force -Path $Vendor | Out-Null
foreach ($bin in @("ffmpeg.exe", "ffprobe.exe", "ffplay.exe")) {
    $src = Join-Path $Root "vendor\$bin"
    if (Test-Path -LiteralPath $src) {
        Copy-Item -LiteralPath $src -Destination (Join-Path $Vendor $bin) -Force
        Write-Host "Bundled $bin"
    }
}
if (-not (Test-Path -LiteralPath (Join-Path $Vendor "ffmpeg.exe"))) {
    Write-Error "vendor\ffmpeg.exe missing in build — refuse to ship incomplete end-user package"
}
if (-not (Test-Path -LiteralPath (Join-Path $Vendor "ffprobe.exe"))) {
    Write-Error "vendor\ffprobe.exe missing in build — refuse to ship incomplete end-user package"
}

# End-user note next to the installed app (not a developer guide)
$EndUserReadme = @"
Sekiclip (beta) — offline media editor
Free forever · nothing is uploaded · MIT license

This folder is the app install. You normally open Sekiclip from the Start Menu.

Settings and logs are stored separately under:
  %LOCALAPPDATA%\Sekiclip

Uninstall: Start Menu → Sekiclip → Uninstall Sekiclip
  or Windows Settings → Apps → Sekiclip
"@
Set-Content -LiteralPath (Join-Path $DistApp "README.txt") -Value $EndUserReadme -Encoding utf8

# Never package as portable in the main app tree
Remove-Item -LiteralPath (Join-Path $DistApp "sekiclip_portable.txt") -Force -ErrorAction SilentlyContinue

$venvPython = Join-Path $Root ".venv\Scripts\python.exe"
$Version = & $venvPython -c "from sekiclip import __version__; print(__version__)"
Write-Host "Version: $Version"

# ── Primary: Windows wizard Setup.exe ────────────────────────────────
# Copy branding next to the .iss so Inno resolves paths reliably (same folder).
$SetupAssets = Join-Path $Root "scripts\setup_assets"
New-Item -ItemType Directory -Force -Path $SetupAssets | Out-Null
foreach ($f in @("sekiclip.ico", "wizard_image.bmp", "wizard_small.bmp")) {
    $src = Join-Path $Root "assets\$f"
    if (-not (Test-Path -LiteralPath $src)) {
        Write-Error "Missing branding asset for installer: $src - run scripts\make_icon.py"
    }
    Copy-Item -LiteralPath $src -Destination (Join-Path $SetupAssets $f) -Force
    $len = (Get-Item -LiteralPath $src).Length
    Write-Host ("Setup asset: {0} ({1} bytes)" -f $f, $len)
}

# Icon next to app exe (shells / icon cache helpers)
Copy-Item (Join-Path $Root "assets\sekiclip.ico") (Join-Path $DistApp "sekiclip.ico") -Force
Copy-Item (Join-Path $Root "assets\sekiclip.png") (Join-Path $DistApp "sekiclip.png") -Force

Write-Host "Building Setup wizard (Inno Setup)..."
$iss = Join-Path $Root "scripts\setup_sekiclip.iss"
if (-not (Test-Path (Join-Path $SetupAssets "wizard_image.bmp"))) {
    Write-Error "wizard_image.bmp not staged for Inno"
}
if (-not (Test-Path (Join-Path $SetupAssets "sekiclip.ico"))) {
    Write-Error "sekiclip.ico not staged for Inno"
}
& $Iscc $iss
if ($LASTEXITCODE -ne 0) {
    Write-Error "Inno Setup compile failed (exit $LASTEXITCODE)"
}

$SetupExe = Join-Path $Root "dist\Sekiclip-$Version-Setup.exe"
# Inno may write version with same string
if (-not (Test-Path -LiteralPath $SetupExe)) {
    $alt = Get-ChildItem (Join-Path $Root "dist") -Filter "Sekiclip-*-Setup.exe" -File |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1
    if ($alt) {
        $SetupExe = $alt.FullName
        Write-Host "Setup exe: $SetupExe"
    } else {
        Write-Error "Setup.exe not found under dist\"
    }
} else {
    Write-Host "PRIMARY: $SetupExe"
}

# ── Optional: portable zip (advanced / USB) ──────────────────────────
$PortableMarker = Join-Path $DistApp "sekiclip_portable.txt"
"Portable mode: settings under .\data next to this app" | Set-Content -LiteralPath $PortableMarker -Encoding utf8
$PortableZip = Join-Path $Root "dist\Sekiclip-$Version-portable.zip"
if (Test-Path -LiteralPath $PortableZip) { Remove-Item -LiteralPath $PortableZip -Force }
Compress-Archive -Path (Join-Path $DistApp "*") -DestinationPath $PortableZip -Force
Write-Host "Optional portable: $PortableZip"
Remove-Item -LiteralPath $PortableMarker -Force -ErrorAction SilentlyContinue

# Remove old intermediate / confusing artifacts if present
@(
    "Sekiclip-$Version-Setup.zip",
    "Sekiclip-$Version-win64-Setup-User.zip",
    "Sekiclip-$Version-win64-portable.zip"
) | ForEach-Object {
    $p = Join-Path $Root "dist\$_"
    if (Test-Path -LiteralPath $p) {
        Remove-Item -LiteralPath $p -Force
        Write-Host "Removed legacy package: $_"
    }
}
$stage = Join-Path $Root "dist\Sekiclip-SetupStage"
if (Test-Path -LiteralPath $stage) {
    Remove-Item -LiteralPath $stage -Recurse -Force
}

& (Join-Path $Root "scripts\write_checksums.ps1")

Write-Host ""
Write-Host "=== Give testers this file ==="
Write-Host "  $SetupExe"
Write-Host "  (optional) $PortableZip"
Write-Host ""
Get-ChildItem (Join-Path $Root "dist") -File | ForEach-Object {
    "  $($_.Name)  $([math]::Round($_.Length/1MB, 1)) MB"
}
