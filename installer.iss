; Inno Setup script — builds a one-click "Lyric-Immersion-and-Karaoke-Setup.exe".
; Install Inno Setup (free), then run:  iscc installer.iss
; (build.bat does this automatically if iscc is on PATH.)

#define AppName "Lyric Immersion and Karaoke"
#define AppVer  "1.1.73"
#define AppExe  "Lyric-Immersion-and-Karaoke.exe"

[Setup]
AppName={#AppName}
AppVersion={#AppVer}
AppPublisher=Purple Industries
AppPublisherURL=https://github.com/BarnsL/Lyric-Immersion-and-Karaoke
; Real metadata on the Setup.exe itself — an installer with no version resource is
; a Defender/SmartScreen heuristic signal, same as the app exe. (Does not replace
; code-signing; lowers the false-positive rate on clean machines.)
VersionInfoVersion={#AppVer}
VersionInfoCompany=Purple Industries
VersionInfoProductName={#AppName}
VersionInfoProductVersion={#AppVer}
VersionInfoDescription={#AppName} Setup
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
; LAYPERSON-EASY wizard: no directory page (per-user default is always right)
; and no ready/summary page — run the exe, pick shortcuts, it installs.
DisableDirPage=yes
DisableReadyPage=yes
; Upgrades must "just work" with the app sitting in the tray: PrepareToInstall
; below auto-closes the running app + its GPU overlay child (hidden, no window),
; so users never see "file in use" retry dialogs. Restart Manager is disabled in
; favor of that explicit close (RM can't cleanly stop the windowless tray pair).
CloseApplications=no
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

[Code]
// Auto-close the running app (tray engine + GPU overlay child) so an UPGRADE
// over a running install never hits "file in use" — the #1 layperson upgrade
// failure. Hidden windows (SW_HIDE), so nothing flashes on screen. The same
// close runs before UNinstall so that path is clean too. The app's own
// single-instance mutex is Local\DesktopKaraoke.SingleInstance; killing by
// image name covers both the PyInstaller stub and its child.
procedure KillApp();
var
  R, i: Integer;
begin
  // Three passes: killing the engine mid-startup can ORPHAN an overlay child
  // whose spawn was already in flight (verified live: a just-launched app made
  // the single-pass kill lose the race — the orphan held a file lock and the
  // silent install aborted with exit code 5). Repeating the pair with short
  // waits closes the race; taskkill on a missing image is a fast no-op.
  for i := 1 to 3 do
  begin
    Exec(ExpandConstant('{sys}\taskkill.exe'),
         '/F /T /IM Lyric-Immersion-and-Karaoke.exe', '',
         SW_HIDE, ewWaitUntilTerminated, R);
    Exec(ExpandConstant('{sys}\taskkill.exe'),
         '/F /T /IM lyric-overlay.exe', '',
         SW_HIDE, ewWaitUntilTerminated, R);
    Sleep(500);   // let the OS release file locks between passes
  end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  KillApp();
  Result := '';
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
    KillApp();
end;
