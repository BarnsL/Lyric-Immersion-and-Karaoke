; Inno Setup script — builds a one-click "DesktopKaraoke-Setup.exe".
; Install Inno Setup (free), then run:  iscc installer.iss
; (build.bat does this automatically if iscc is on PATH.)

#define AppName "Lyric Immersion and Karaoke"
#define AppVer  "1.1.41"
#define AppExe  "Lyric-Immersion-and-Karaoke.exe"

[Setup]
AppName={#AppName}
AppVersion={#AppVer}
AppPublisher=Lyric Immersion and Karaoke
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
UninstallDisplayIcon={app}\{#AppExe}
OutputDir=dist
OutputBaseFilename=Lyric-Immersion-and-Karaoke-Setup
SetupIconFile=icon.ico
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
ArchitecturesInstallIn64BitMode=x64compatible
; Shown as a page near the end of the wizard — tells the user the app lives in
; the system tray and that ALL settings are under the (right-click) tray icon,
; since there is no normal window.
InfoAfterFile=packaging\after_install.txt

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"
Name: "startup";     Description: "Start {#AppName} automatically when &Windows starts"; GroupDescription: "Startup:"; Flags: unchecked

[Files]
; onedir build: bundle the whole app folder (exe + its dependencies)
Source: "dist\DesktopKaraoke\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}";        Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}";  Filename: "{app}\{#AppExe}"; Tasks: desktopicon
Name: "{userstartup}\{#AppName}";  Filename: "{app}\{#AppExe}"; Tasks: startup

[Run]
Filename: "{app}\{#AppExe}"; Description: "Launch {#AppName} now"; Flags: nowait postinstall skipifsilent
