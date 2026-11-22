; Inno Setup script for the Windows installer.
; Build:  iscc /DAppVersion=0.1.0 /DSourceDir="..\..\dist\Drone Map Overlay" /DOutputDir="..\..\out" packaging\windows\installer.iss

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\..\dist\Drone Map Overlay"
#endif
#ifndef OutputDir
  #define OutputDir "..\..\out"
#endif

[Setup]
AppId={{6E2B4C1A-7F3D-4B8E-9A21-5C0D3E9F7A41}
AppName=Drone Map Overlay
AppVersion={#AppVersion}
AppVerName=Drone Map Overlay {#AppVersion}
AppPublisher=Drone Map Overlay
DefaultDirName={autopf}\Drone Map Overlay
DefaultGroupName=Drone Map Overlay
DisableProgramGroupPage=yes
; Installs for the current user without admin rights; can install for everyone if allowed.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#OutputDir}
OutputBaseFilename=DroneMapOverlay-{#AppVersion}-Windows-Setup
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\Drone Map Overlay.exe
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\Drone Map Overlay"; Filename: "{app}\Drone Map Overlay.exe"
Name: "{autodesktop}\Drone Map Overlay"; Filename: "{app}\Drone Map Overlay.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\Drone Map Overlay.exe"; Description: "{cm:LaunchProgram,Drone Map Overlay}"; Flags: nowait postinstall skipifsilent
