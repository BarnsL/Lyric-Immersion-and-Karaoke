# Building the Desktop Karaoke installer

End users don't need any of this — they just run the released
`DesktopKaraoke-Setup.exe` (or the portable folder). This page is for producing
those files.

## One command

```bat
build.bat
```

That installs the build tools, produces the app in **`dist\DesktopKaraoke\`**
(with `DesktopKaraoke.exe` inside — Python and every dependency bundled, no
Python needed to run it), and — if [Inno Setup](https://jrsoftware.org/isdl.php)
is installed — also **`dist\DesktopKaraoke-Setup.exe`**, the one-click installer.

## Manual steps

```bat
pip install pyinstaller -r requirements.txt
pyinstaller --noconfirm DesktopKaraoke.spec     :: -> dist\DesktopKaraoke\DesktopKaraoke.exe
iscc installer.iss                              :: -> dist\DesktopKaraoke-Setup.exe
```

## Why a **onedir** build (a folder, not a single .exe)

The app ships as a *folder* (`dist\DesktopKaraoke\`) rather than a single
self-extracting `.exe`. A one-file build crams everything into one executable
that **re-extracts ~1 GB — including the 50 MB UniDic dictionary — to a temp
folder on every launch**, which is slow and can fail on machines with strict
antivirus. The onedir build starts **instantly** and is far more reliable. The
Inno Setup installer packs the folder into one `DesktopKaraoke-Setup.exe`, so the
end-user experience is still a single download.

## What the installer does (the "layman" experience)

1. Double-click `DesktopKaraoke-Setup.exe`, click **Next → Install** (no admin
   rights needed — installs per-user).
2. Two optional checkboxes: **desktop shortcut** and **Start with Windows**.
3. Done. It adds a **Start menu** entry, launches, and lives in the system tray
   (the purple microphone). Play any song.

The packaged app is **portable**: it keeps its lyric library (`lyrics/`) and
`settings.json` right next to `DesktopKaraoke.exe`, so the whole folder is
self-contained — copy it anywhere, or make it a git repo to back up.

## No Inno Setup? Make a Start-menu entry directly

If you only built the folder (no installer), you can still pin it to the Start
menu with a shortcut to `dist\DesktopKaraoke\DesktopKaraoke.exe` (use `icon.ico`
as the icon). PowerShell one-liner:

```powershell
$W=New-Object -ComObject WScript.Shell
$S=$W.CreateShortcut((Join-Path ([Environment]::GetFolderPath('Programs')) 'Desktop Karaoke.lnk'))
$S.TargetPath="$PWD\dist\DesktopKaraoke\DesktopKaraoke.exe"; $S.IconLocation="$PWD\icon.ico"; $S.Save()
```

## The app icon

`icon.ico` is generated reproducibly by **`make_icon.py`** (a karaoke microphone
with sound waves on a purple gradient). To tweak it, edit the colours/geometry
constants at the top of that file and regenerate:

```bat
python make_icon.py --preview     :: rewrites icon.ico + a _icon_preview.png contact sheet
```

It writes a multi-size `.ico` (16–256 px); the small tray sizes use a bolder,
simplified master so the mic stays legible at 16 px. The icon is embedded into
the `.exe` and bundled as a data file by `DesktopKaraoke.spec`, so **rebuild the
app after changing it** for the new icon to show in the tray, taskbar, and Start
menu.

## Notes

- `DesktopKaraoke.exe` is windowed (no console) and bundles Python and every
  dependency (winsdk, soundcard, shazamio, fugashi + UniDic, cutlet, …) via
  `DesktopKaraoke.spec`.
- The lyric cache is **not** bundled (copyright) — it builds itself as songs play.
- "Start with Windows" can also be toggled any time from the tray menu.
