# Locate Inno Setup 6 compiler (ISCC.exe). Exit 0 with path on stdout if found.
$ErrorActionPreference = "Stop"
$candidates = @(
    (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe"),
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    "${env:ProgramFiles}\Inno Setup 6\ISCC.exe"
)
foreach ($p in $candidates) {
    if ($p -and (Test-Path -LiteralPath $p)) {
        Write-Output $p
        exit 0
    }
}
Write-Error @"
Inno Setup 6 not found (ISCC.exe).

Install it once (free): https://jrsoftware.org/isinfo.php
Or: winget install --id JRSoftware.InnoSetup -e

Then re-run: .\scripts\package_release.ps1
"@
