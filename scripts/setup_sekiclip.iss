; Sekiclip end-user Windows installer (Inno Setup 6+)
; Build via scripts\package_release.ps1 (copies wizard art next to this file first).

#define MyAppName "Sekiclip"
#define MyAppVersion "0.1.0-beta.2"
#define MyAppVersionInfo "0.1.0.2"
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
InfoBeforeFile=installer_info_before.txt
InfoAfterFile=installer_info_after.txt
OutputDir=..\dist
OutputBaseFilename=Sekiclip-{#MyAppVersion}-Setup
; Icons / wizard art are copied into scripts\ by package_release.ps1 so paths stay local
SetupIconFile=setup_assets\sekiclip.ico
WizardImageFile=setup_assets\wizard_image.bmp
WizardSmallImageFile=setup_assets\wizard_small.bmp
; Classic left panel keeps 164:314 art from warping (modern can distort)
WizardImageStretch=yes
WizardImageBackColor=$A86F2F
WizardSmallImageBackColor=$A86F2F
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=classic
WizardSizePercent=100,100
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
CloseApplications=yes
RestartApplications=no
UsePreviousAppDir=yes
UsePreviousTasks=yes
AllowNoIcons=yes
VersionInfoVersion={#MyAppVersionInfo}
VersionInfoProductName={#MyAppName}
VersionInfoProductVersion={#MyAppVersionInfo}
VersionInfoCompany={#MyAppPublisher}
VersionInfoDescription={#MyAppName} Setup
VersionInfoTextVersion={#MyAppVersion}
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
Source: "..\dist\Sekiclip\*"; DestDir: "{app}"; \
  Flags: ignoreversion recursesubdirs createallsubdirs; \
  Excludes: "sekiclip_portable.txt,Install-Sekiclip.cmd,install_user.ps1,HOW_TO_INSTALL.txt,Uninstall-Sekiclip.cmd,Uninstall-Sekiclip.ps1"

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; IconFilename: "{app}\{#MyAppExeName}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; IconFilename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; \
  Flags: nowait postinstall skipifsilent; WorkingDir: "{app}"

[UninstallDelete]
Type: dirifempty; Name: "{app}"

[Code]
function InitializeSetup(): Boolean;
begin
  Result := True;
end;
