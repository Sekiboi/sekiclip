; Sekiclip — end-user Windows installer (wizard).
; No admin. Installs for the current Windows user only.
; Build: package_release.ps1 (calls ISCC after PyInstaller).

#define MyAppName "Sekiclip"
#define MyAppVersion "0.1.0-beta.1"
#define MyAppVersionInfo "0.1.0.1"
#define MyAppPublisher "Sekiboi"
#define MyAppExeName "Sekiclip.exe"
#define MyAppURL "https://github.com/Sekiboi/sekiclip"
#define MyAppId "{{8F3C2A1B-9D4E-4F6A-B2C1-SEKICLIP0BETA}}"

[Setup]
AppId={#MyAppId}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppCopyright=Copyright (C) 2026 {#MyAppPublisher}
DefaultDirName={localappdata}\Programs\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
DisableReadyPage=no
LicenseFile=..\LICENSE
InfoBeforeFile=..\scripts\installer_info_before.txt
InfoAfterFile=..\scripts\installer_info_after.txt
OutputDir=..\dist
OutputBaseFilename=Sekiclip-{#MyAppVersion}-Setup
SetupIconFile=..\assets\sekiclip.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
WizardSizePercent=120
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
CloseApplications=yes
RestartApplications=no
UsePreviousAppDir=yes
UsePreviousTasks=yes
AllowNoIcons=yes
; Version resources on Setup.exe / uninstaller
VersionInfoVersion={#MyAppVersionInfo}
VersionInfoProductName={#MyAppName}
VersionInfoProductVersion={#MyAppVersionInfo}
VersionInfoCompany={#MyAppPublisher}
VersionInfoDescription={#MyAppName} Setup
VersionInfoTextVersion={#MyAppVersion}
; Do not ask for restart
AlwaysShowDirOnReadyPage=yes
ShowLanguageDialog=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Messages]
WelcomeLabel1=Welcome to {#MyAppName} Setup
WelcomeLabel2=This installs {#MyAppName} (beta) on your PC — offline media editor.%n%nNo admin password. No account. Nothing is uploaded.%n%nClick Next to continue.
FinishedHeadingLabel=Installation complete
FinishedLabelNoIcons={#MyAppName} is installed. You can launch it now.
FinishedLabel={#MyAppName} is installed. You can launch it from the Start Menu or the shortcut you chose.
ClickFinish=Click Finish to exit Setup.

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"; Flags: checkedonce

[Files]
; App tree from PyInstaller onedir — never ship portable marker or installer scripts
Source: "..\dist\Sekiclip\*"; DestDir: "{app}"; \
  Flags: ignoreversion recursesubdirs createallsubdirs; \
  Excludes: "sekiclip_portable.txt,Install-Sekiclip.cmd,install_user.ps1,HOW_TO_INSTALL.txt,Uninstall-Sekiclip.cmd,Uninstall-Sekiclip.ps1"

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; \
  Flags: nowait postinstall skipifsilent; WorkingDir: "{app}"

[UninstallDelete]
; Only remove empty leftover dirs under the app folder if any
Type: dirifempty; Name: "{app}"

[Code]
function InitializeSetup(): Boolean;
begin
  Result := True;
end;
