# Per-user Sekiclip installer for end PCs (no admin, no Python).
# Double-click Install-Sekiclip.cmd from the unzipped package.
#
# Installs to:  %LOCALAPPDATA%\Programs\Sekiclip   (app / ffmpeg)
# User data:    %LOCALAPPDATA%\Sekiclip            (prefs, logs — kept on uninstall)
$ErrorActionPreference = "Stop"

function Show-Msg([string]$Title, [string]$Text, [string]$Icon = "Information") {
    try {
        Add-Type -AssemblyName System.Windows.Forms -ErrorAction SilentlyContinue
        $buttons = [System.Windows.Forms.MessageBoxButtons]::OK
        $iconEnum = [System.Windows.Forms.MessageBoxIcon]::$Icon
        [System.Windows.Forms.MessageBox]::Show($Text, $Title, $buttons, $iconEnum) | Out-Null
    } catch {
        Write-Host $Text
    }
}

try {
    $SourceRoot = $PSScriptRoot
    if (-not (Test-Path (Join-Path $SourceRoot "Sekiclip.exe"))) {
        Show-Msg "Sekiclip Setup" "Sekiclip.exe was not found next to the installer.`n`nUnzip the full Setup package to a folder first, then run Install-Sekiclip.cmd again." "Error"
        exit 1
    }

    # Clear Mark-of-the-Web so Windows is less likely to block the first launch
    try {
        Get-ChildItem -LiteralPath $SourceRoot -Recurse -Force -ErrorAction SilentlyContinue |
            Unblock-File -ErrorAction SilentlyContinue
    } catch {}

    $ProductName = "Sekiclip"
    $InstallDir = Join-Path $env:LOCALAPPDATA "Programs\Sekiclip"
    $StartMenu = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Sekiclip"
    $Desktop = [Environment]::GetFolderPath("Desktop")
    $Version = "0.1.0-beta.1"
    $UserData = Join-Path $env:LOCALAPPDATA "Sekiclip"

    Write-Host ""
    Write-Host "  Installing $ProductName for your Windows user..."
    Write-Host "  App:  $InstallDir"
    Write-Host "  Data: $UserData  (settings; kept if you uninstall)"
    Write-Host ""

    # Close a running copy so files can be replaced (upgrade / reinstall)
    Get-Process -Name "Sekiclip" -ErrorAction SilentlyContinue |
        Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Milliseconds 500

    # Fresh app folder (does not touch user data under %LOCALAPPDATA%\Sekiclip)
    if (Test-Path -LiteralPath $InstallDir) {
        $retries = 0
        while ($retries -lt 5) {
            try {
                Remove-Item -LiteralPath $InstallDir -Recurse -Force -ErrorAction Stop
                break
            } catch {
                $retries++
                Start-Sleep -Milliseconds 400
                if ($retries -ge 5) { throw }
            }
        }
    }
    New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null

    $skip = @(
        "sekiclip_portable.txt",
        "Install-Sekiclip.cmd",
        "install_user.ps1",
        "HOW_TO_INSTALL.txt",
        "HOW_TO_INSTALL.txt.txt"
    )
    Get-ChildItem -LiteralPath $SourceRoot -Force | ForEach-Object {
        if ($skip -contains $_.Name) { return }
        $dest = Join-Path $InstallDir $_.Name
        $copyRetries = 0
        while ($true) {
            try {
                Copy-Item -LiteralPath $_.FullName -Destination $dest -Recurse -Force -ErrorAction Stop
                break
            } catch {
                $copyRetries++
                if ($copyRetries -ge 4) { throw }
                Start-Sleep -Milliseconds 400
            }
        }
    }

    # Never treat an installed copy as portable
    $portable = Join-Path $InstallDir "sekiclip_portable.txt"
    if (Test-Path -LiteralPath $portable) { Remove-Item -LiteralPath $portable -Force }

    $exe = Join-Path $InstallDir "Sekiclip.exe"
    if (-not (Test-Path -LiteralPath $exe)) {
        throw "Copy finished but Sekiclip.exe is missing under $InstallDir"
    }

    try {
        Get-ChildItem -LiteralPath $InstallDir -Recurse -Force -ErrorAction SilentlyContinue |
            Unblock-File -ErrorAction SilentlyContinue
    } catch {}

    # Start Menu + Desktop (simple find-and-run for non-technical users)
    New-Item -ItemType Directory -Force -Path $StartMenu | Out-Null
    $Wsh = New-Object -ComObject WScript.Shell

    $sc1 = $Wsh.CreateShortcut((Join-Path $StartMenu "Sekiclip.lnk"))
    $sc1.TargetPath = $exe
    $sc1.WorkingDirectory = $InstallDir
    $sc1.Description = "Sekiclip — offline media editor"
    $sc1.Save()

    $sc2 = $Wsh.CreateShortcut((Join-Path $Desktop "Sekiclip.lnk"))
    $sc2.TargetPath = $exe
    $sc2.WorkingDirectory = $InstallDir
    $sc2.Description = "Sekiclip — offline media editor"
    $sc2.Save()

    # Uninstall (app only — keeps settings in %LOCALAPPDATA%\Sekiclip)
    $UninstallPs1 = Join-Path $InstallDir "Uninstall-Sekiclip.ps1"
    $Uninstall = @"
`$ErrorActionPreference = 'Stop'
Get-Process -Name 'Sekiclip' -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Milliseconds 400
`$dir = '$InstallDir'
`$sm = '$StartMenu'
`$desk = Join-Path ([Environment]::GetFolderPath('Desktop')) 'Sekiclip.lnk'
if (Test-Path -LiteralPath `$dir) { Remove-Item -LiteralPath `$dir -Recurse -Force }
if (Test-Path -LiteralPath `$sm) { Remove-Item -LiteralPath `$sm -Recurse -Force }
if (Test-Path -LiteralPath `$desk) { Remove-Item -LiteralPath `$desk -Force }
`$reg = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\Sekiclip'
if (Test-Path `$reg) { Remove-Item -Recurse -Force `$reg }
try {
    Add-Type -AssemblyName System.Windows.Forms
    [System.Windows.Forms.MessageBox]::Show(
        "Sekiclip was removed from this PC.`n`nYour settings folder was kept:`n$UserData`n`nDelete that folder yourself if you want a full clean.",
        "Sekiclip",
        [System.Windows.Forms.MessageBoxButtons]::OK,
        [System.Windows.Forms.MessageBoxIcon]::Information
    ) | Out-Null
} catch {
    Write-Host 'Sekiclip removed. Settings folder left at:' $UserData
}
"@
    Set-Content -LiteralPath $UninstallPs1 -Value $Uninstall -Encoding utf8

    @"
@echo off
title Uninstall Sekiclip
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0Uninstall-Sekiclip.ps1"
"@ | Set-Content -LiteralPath (Join-Path $InstallDir "Uninstall-Sekiclip.cmd") -Encoding ascii

    $sc3 = $Wsh.CreateShortcut((Join-Path $StartMenu "Uninstall Sekiclip.lnk"))
    $sc3.TargetPath = Join-Path $InstallDir "Uninstall-Sekiclip.cmd"
    $sc3.WorkingDirectory = $InstallDir
    $sc3.Save()

    # Settings → Apps (per-user)
    $regPath = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\Sekiclip"
    New-Item -Path $regPath -Force | Out-Null
    $sizeKb = 0
    try {
        $sizeKb = [int]((Get-ChildItem -LiteralPath $InstallDir -Recurse -File -ErrorAction SilentlyContinue |
                Measure-Object -Property Length -Sum).Sum / 1KB)
    } catch {}
    Set-ItemProperty -Path $regPath -Name "DisplayName" -Value "Sekiclip"
    Set-ItemProperty -Path $regPath -Name "DisplayVersion" -Value $Version
    Set-ItemProperty -Path $regPath -Name "Publisher" -Value "Sekiboi"
    Set-ItemProperty -Path $regPath -Name "InstallLocation" -Value $InstallDir
    Set-ItemProperty -Path $regPath -Name "DisplayIcon" -Value $exe
    Set-ItemProperty -Path $regPath -Name "UninstallString" -Value "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$UninstallPs1`""
    Set-ItemProperty -Path $regPath -Name "QuietUninstallString" -Value "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$UninstallPs1`""
    Set-ItemProperty -Path $regPath -Name "NoModify" -Value 1 -Type DWord
    Set-ItemProperty -Path $regPath -Name "NoRepair" -Value 1 -Type DWord
    if ($sizeKb -gt 0) {
        Set-ItemProperty -Path $regPath -Name "EstimatedSize" -Value $sizeKb -Type DWord
    }

    $ff = Join-Path $InstallDir "vendor\ffmpeg.exe"
    $fp = Join-Path $InstallDir "vendor\ffprobe.exe"
    $toolsOk = (Test-Path -LiteralPath $ff) -and (Test-Path -LiteralPath $fp)
    if (-not $toolsOk) {
        Write-Host "  Warning: video tools missing from vendor folder."
    }

    Write-Host "  Done. Starting Sekiclip..."
    Start-Process -FilePath $exe -WorkingDirectory $InstallDir
    exit 0
}
catch {
    $msg = "Install failed:`n`n$($_.Exception.Message)`n`nYou can still open Sekiclip.exe from the unzipped folder."
    Write-Host $msg
    Show-Msg "Sekiclip Setup" $msg "Error"
    exit 1
}
