; Inno Setup script — builds a one-click "DesktopKaraoke-Setup.exe".
; Install Inno Setup (free), then run:  iscc installer.iss
; (build.bat does this automatically if iscc is on PATH.)

#define AppName "Desktop Karaoke"
#define AppVer  "1.0.0"
#define AppExe  "DesktopKaraoke.exe"

[Setup]
AppName={#AppName}
AppVersion={#AppVer}
AppPublisher=Desktop Karaoke
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
UninstallDisplayIcon={app}\{#AppExe}
OutputDir=dist
OutputBaseFilename=DesktopKaraoke-Setup
SetupIconFile=icon.ico
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
ArchitecturesInstallIn64BitMode=x64compatible

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"
Name: "startup";     Description: "Start {#AppName} automatically when &Windows starts"; GroupDescription: "Startup:"; Flags: unchecked

[Files]
Source: "dist\{#AppExe}"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#AppName}";        Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}";  Filename: "{app}\{#AppExe}"; Tasks: desktopicon
Name: "{userstartup}\{#AppName}";  Filename: "{app}\{#AppExe}"; Tasks: startup

[Run]
Filename: "{app}\{#AppExe}"; Description: "Launch {#AppName} now"; Flags: nowait postinstall skipifsilent
