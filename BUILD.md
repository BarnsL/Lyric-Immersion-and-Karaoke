# Building the Desktop Karaoke installer

End users don't need any of this — they install from the **Microsoft Store**
(one click, auto-updating, no security warnings). This page is for producing the
packages.

## Microsoft Store package (the recommended distribution)

```powershell
.\packaging\build_msix.ps1 -CertThumbprint <yourDevCertThumbprint>   # local test build
```

Produces **`dist\DesktopKaraoke.msix`** — the PyInstaller app wrapped as a
full-trust MSIX with branded tiles. To actually publish it (reserve the name,
fill identity, upload), follow **[STORE_SUBMISSION.md](STORE_SUBMISSION.md)**.
The Store signs the package, which is what gets it past SmartScreen and Smart App
Control — the thing that blocks plain unsigned `.exe` installers.

> Why MSIX and not the `.exe` installer below? An unsigned `Setup.exe` is blocked
> by Smart App Control and warned-on by SmartScreen. Store-signed MSIX is the only
> way to a genuine zero-warning, one-click install for non-technical users.

## Portable / Inno `.exe` build (for sideloading or non-Store distribution)

### One command

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

## Optional: "Sync by listening" (faster-whisper)

The tray's **🎤 Sync by listening** transcribes the live vocals to align the lyrics
when Shazam can't ID the exact cut. It needs **faster-whisper**, which is **heavy
(~500 MB) and off by default**. To include it in the build, vendor it into `.deps`
first (kept off the C: drive):

```bat
set PIP_CACHE_DIR=D:\pip-cache
pip install --target .deps faster-whisper
pyinstaller --noconfirm DesktopKaraoke.spec    :: now auto-detects .deps and bundles it (~650 MB .exe)
```

The spec sets `WHISPER = os.path.isdir(".deps")`: **no `.deps` → lean ~150 MB build**
(the feature shows a "needs faster-whisper" hint); **`.deps` present → self-contained
~650 MB build** with the feature working. PyInstaller's hooks are required — a loose
`sys.path` vendor fails on PyAV's `av._core` DLLs. The ASR model (~75 MB) downloads
to the app's data folder on first use (copy `models\` next to the `.exe` to pre-seed).

## Optional: deep lyric transcription (`yt-dlp` + a JS runtime)

The background **deep generation** ([docs/GENERATION.md](docs/GENERATION.md)) — which
downloads a no-lyrics-anywhere song's source audio and transcribes the whole file for a
clean generated lyric — needs **faster-whisper** (above) plus **`yt-dlp`** and a **JS
runtime** (YouTube otherwise 403s the audio download). To ship it in the portable build:

```bat
pip install --target .deps yt-dlp        :: vendor it alongside faster-whisper
:: then place a JS runtime next to the .exe so yt-dlp finds it on PATH:
::   - Node:  copy node.exe into the app folder, OR
::   - Deno:  copy the single deno.exe into the app folder (smaller, ~40 MB)
pyinstaller --noconfirm DesktopKaraoke.spec
```

`deep_transcribe.available()` is just `import yt_dlp`, and `_download_audio` only enables
`node` when it's on `PATH` — so a build **without** these still runs fine: the instant
best-effort generation stands and the deep pass quietly no-ops. (The lyric library backup
and the personal music DB are kept in a **private** repo, never the public one.)

## Notes

- `DesktopKaraoke.exe` is windowed (no console) and bundles Python and every
  dependency (winsdk, soundcard, shazamio, fugashi + UniDic, cutlet, …) via
  `DesktopKaraoke.spec`.
- The lyric cache is **not** bundled (copyright) — it builds itself as songs play.
- "Start with Windows" can also be toggled any time from the tray menu.
