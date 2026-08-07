# Write SHA256SUMS.txt for top-level release files under dist\ (zips / setup exe).
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Dist = Join-Path $Root "dist"
if (-not (Test-Path $Dist)) {
    Write-Error "dist\ not found. Build first (scripts\build_exe.ps1)."
}

$Out = Join-Path $Dist "SHA256SUMS.txt"
$lines = New-Object System.Collections.Generic.List[string]
Get-ChildItem -Path $Dist -File |
    Where-Object {
        $_.Name -ne "SHA256SUMS.txt" -and (
            $_.Extension -in @(".zip", ".exe") -or
            $_.Name -like "Sekiclip-*"
        )
    } |
    ForEach-Object {
        $hash = (Get-FileHash -Algorithm SHA256 -Path $_.FullName).Hash.ToLowerInvariant()
        $lines.Add("$hash  $($_.Name)")
    }

if ($lines.Count -eq 0) {
    Write-Warning "No release files (zip/exe) found in dist\ yet."
}

$lines | Set-Content -Path $Out -Encoding utf8
Write-Host "Wrote $Out ($($lines.Count) files)"
