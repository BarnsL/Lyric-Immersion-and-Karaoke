# Building the Desktop Karaoke installer

End users don't need any of this — they just run the released
`DesktopKaraoke-Setup.exe` (or the portable `DesktopKaraoke.exe`). This is for
producing those files.

## One command

```bat
build.bat
```

That installs the build tools, produces **`dist\DesktopKaraoke.exe`** (a single,
self-contained, no-Python-needed app), and — if [Inno Setup](https://jrsoftware.org/isdl.php)
is installed — also **`dist\DesktopKaraoke-Setup.exe`**, the one-click installer.

## Manual steps

```bat
pip install pyinstaller -r requirements.txt
pyinstaller --noconfirm DesktopKaraoke.spec     :: -> dist\DesktopKaraoke.exe
iscc installer.iss                              :: -> dist\DesktopKaraoke-Setup.exe
```

## What the installer does (the "layman" experience)

1. Double-click `DesktopKaraoke-Setup.exe`, click **Next → Install** (no admin
   rights needed — installs per-user).
2. Two optional checkboxes: **desktop shortcut** and **Start with Windows**.
3. Done. It launches and lives in the system tray (あ). Play any song.

The packaged app is **portable**: it keeps its lyric library (`lyrics/`) and
`settings.json` right next to `DesktopKaraoke.exe`, so the whole folder is
self-contained — copy it anywhere, or make it a git repo to back up.

## Notes

- `DesktopKaraoke.exe` is windowed (no console) and bundles Python and every
  dependency (winsdk, soundcard, shazamio, pykakasi, …) via `DesktopKaraoke.spec`.
- The lyric cache is **not** bundled (copyright) — it builds itself as songs play.
- "Start with Windows" can also be toggled any time from the tray menu.
