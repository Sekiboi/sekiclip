# Download ffmpeg essentials into vendor\ so releases are install-and-play.
# Safe to re-run; skips if ffmpeg.exe + ffprobe.exe already present.
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Vendor = Join-Path $Root "vendor"
New-Item -ItemType Directory -Force -Path $Vendor | Out-Null

$Need = @("ffmpeg.exe", "ffprobe.exe")
$Have = $Need | Where-Object { Test-Path (Join-Path $Vendor $_) }
if ($Have.Count -eq $Need.Count) {
    Write-Host "vendor/ already has ffmpeg + ffprobe - skip download"
    exit 0
}

$Url = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
$Tmp = Join-Path $env:TEMP "sekiclip-ffmpeg-essentials.zip"
$Extract = Join-Path $env:TEMP "sekiclip-ffmpeg-essentials"

Write-Host "Downloading ffmpeg essentials (install-and-play)..."
Write-Host "  $Url"
try {
    Invoke-WebRequest -Uri $Url -OutFile $Tmp -UseBasicParsing
} catch {
    Write-Warning "Download failed: $_"
    Write-Warning "Place ffmpeg.exe + ffprobe.exe in vendor/ manually, then re-run package."
    exit 1
}

if (Test-Path $Extract) { Remove-Item -Recurse -Force $Extract }
Expand-Archive -Path $Tmp -DestinationPath $Extract -Force

$bins = Get-ChildItem -Path $Extract -Recurse -Include ffmpeg.exe, ffprobe.exe, ffplay.exe
if (-not ($bins | Where-Object { $_.Name -eq "ffmpeg.exe" })) {
    Write-Error "Downloaded zip did not contain ffmpeg.exe"
}
foreach ($b in $bins) {
    Copy-Item $b.FullName (Join-Path $Vendor $b.Name) -Force
    Write-Host "  vendor\$($b.Name)"
}

Remove-Item $Tmp -Force -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force $Extract -ErrorAction SilentlyContinue
Write-Host "ffmpeg ready in vendor/"
