# Build Sekiclip Windows onedir for the end-user package (not a dev run).
# Output: dist\Sekiclip\Sekiclip.exe
# Prefer: .\scripts\package_release.ps1 (bundles ffmpeg + Setup wizard).
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

$venvPython = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    Write-Host "Creating .venv..."
    python -m venv .venv
}
Write-Host "Installing build deps..."
& $venvPython -m pip install -q -r requirements.txt pyinstaller
& $venvPython (Join-Path $Root "scripts\make_icon.py")

$icon = Join-Path $Root "assets\sekiclip.ico"
$pyiArgs = @(
    "--noconfirm",
    "--clean",
    "--windowed",
    "--name", "Sekiclip",
    "--icon", $icon,
    "--onedir",
    "--collect-all", "customtkinter",
    "--collect-all", "tkinterdnd2",
    "--collect-all", "sounddevice",
    "--collect-submodules", "sekiclip",
    "--hidden-import=PIL._tkinter_finder",
    "--hidden-import=cv2",
    "--add-data", "assets\sekiclip.ico;assets",
    "--add-data", "assets\sekiclip.png;assets",
    "--add-data", "assets\sekiclip_mark.png;assets",
    "run.py"
)

Write-Host "Building onedir (PyInstaller)..."
& $venvPython -m PyInstaller @pyiArgs

$DistApp = Join-Path $Root "dist\Sekiclip"
if (-not (Test-Path (Join-Path $DistApp "Sekiclip.exe"))) {
    Write-Error "Build failed: dist\Sekiclip\Sekiclip.exe missing"
}

$Vendor = Join-Path $DistApp "vendor"
New-Item -ItemType Directory -Force -Path $Vendor | Out-Null
foreach ($bin in @("ffmpeg.exe", "ffprobe.exe", "ffplay.exe")) {
    $src = Join-Path $Root "vendor\$bin"
    if (Test-Path $src) {
        Copy-Item $src (Join-Path $Vendor $bin) -Force
        Write-Host "Bundled $bin"
    }
}

# Short end-user note (package_release overwrites with final text)
@"
Sekiclip (beta) — offline media editor
Free forever · nothing is uploaded
"@ | Set-Content -Path (Join-Path $DistApp "README.txt") -Encoding utf8

Write-Host "Done: dist\Sekiclip\Sekiclip.exe"
Write-Host "Next: .\scripts\package_release.ps1"
