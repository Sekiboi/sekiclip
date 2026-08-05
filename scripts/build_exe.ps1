# Build Clipwork Windows onedir (ffmpeg not bundled — use system/vendor).
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

$venvPython = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    python -m venv .venv
}
& $venvPython -m pip install -q -r requirements.txt pyinstaller
& $venvPython (Join-Path $Root "scripts\make_icon.py")

$icon = Join-Path $Root "assets\clipwork.ico"
$pyiArgs = @(
    "--noconfirm",
    "--windowed",
    "--icon", $icon,
    "--collect-all", "customtkinter",
    "--collect-all", "tkinterdnd2",
    "--add-data", "assets\clipwork.ico;assets",
    "--add-data", "assets\clipwork.png;assets",
    "--hidden-import=clipwork",
    "--hidden-import=clipwork.app",
    "--hidden-import=clipwork.media_ops",
    "run.py"
)

Write-Host "Building onedir..."
& $venvPython -m PyInstaller @pyiArgs --name Clipwork --onedir
Write-Host "Done: dist\Clipwork\Clipwork.exe"
Write-Host "Place ffmpeg.exe and ffprobe.exe in dist\Clipwork\vendor\ or keep them on PATH."
