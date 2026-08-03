# Building the Lyric Immersion and Karaoke installer

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
(with `Lyric-Immersion-and-Karaoke.exe` inside — Python and every dependency bundled, no
Python needed to run it), and — if [Inno Setup](https://jrsoftware.org/isdl.php)
is installed — also **`dist\Lyric-Immersion-and-Karaoke-Setup.exe`**, the one-click installer.

It also resolves `py -3.12` itself and runs all four build guards (below), which a
hand-run PyInstaller does not. Prefer this path.

### ⚠️ The interactive prompt: answer **N**

`build.bat` stops on a `choice /C YN` prompt, *"Bundle faster-whisper (recommended)?"*.
Answering **Y** does exactly one thing:

```bat
%PY% -m pip install "faster-whisper>=1.0"
```

That is an **unpinned install into the build environment**, and it is the precise
version-skew vector described under TICKET-177 below. `.deps` pins `faster-whisper==1.2.1`
and `av==18.0.0`; this line asks for whatever is newest, so it can leave the env holding a
different `faster-whisper` (and, transitively, a different PyAV) than the vendored `.deps`.
`collect_all` then bundles a mix of the two and `import av` dies at runtime, taking every
listen feature with it, silently. Note also that cmd parses the unquoted `>` as a
**redirect**, so the actual command run is `pip install faster-whisper` with stdout sent to
a file named `=1.0`: the `>=1.0` floor is not applied at all. (`build.bat` has been bitten
by this same `>` parsing before; see its v1.1.62 comment.)

The prompt's wording is also misleading. Whether whisper gets bundled is decided by
`WHISPER = os.path.isdir(".deps")` in the spec, not by this answer, so answering N does
**not** produce a lean build when `.deps` is present.

**Answer N.** Vendor the stack into `.deps` from `requirements-deps.txt` instead (see
"Sync by listening" below), which is the pinned, known-good set. Answer Y only if you
have no `.deps` and are deliberately installing whisper into the env, and then re-pin
`requirements-deps.txt` and rebuild `.deps` before shipping.

## Manual steps

```bat
py -3.12 -m pip install pyinstaller -r requirements.txt
py -3.12 scripts\check_build_deps.py             :: REQUIRED pre-build guard
py -3.12 scripts\check_av_dlls.py                :: REQUIRED pre-build guard
py -3.12 -m PyInstaller --noconfirm DesktopKaraoke.spec   :: -> dist\DesktopKaraoke\...exe
py -3.12 scripts\check_av_dlls.py --internal dist\DesktopKaraoke   :: REQUIRED post-build
dist\DesktopKaraoke\Lyric-Immersion-and-Karaoke.exe --selftest --out check.txt  :: REQUIRED
iscc installer.iss                               :: -> dist\Lyric-Immersion-and-Karaoke-Setup.exe
```

**Name the interpreter.** The build Python is **3.12** and `.deps` is cp312, but the
bare `python` on the Windows build box is a 3.11 agent venv. The spec refuses an ABI
mismatch outright (TICKET-196), so with a cp312 `.deps` present, `pyinstaller ...` or
`python -m PyInstaller ...` aborts rather than building the broken bundle it used to.
(That check compares ABI tags only, and it fails open when `.deps` has no tagged
modules, so it is a backstop and not a substitute for the guards below.)

**The four guard steps are not optional.** Each catches a different failure, and only
`build.bat` runs all of them for you:

| Step | What it is | What it catches |
|---|---|---|
| `scripts/check_build_deps.py` (pre-build) | Version-consistency check across the native stack (faster-whisper, ctranslate2, PyAV, tokenizers) between `.deps` and the active env | Fails on unambiguous corruption: a foreign CPython ABI tag in `.deps`, or duplicate dist-info dirs (two versions of one package). A plain version difference is a WARNING, because dist-info metadata can lag the real module files. |
| `scripts/check_av_dlls.py` (pre-build) | PE-import-table parse of every `av/*.pyd`, asserting each FFmpeg DLL it imports exists in `av.libs` | The direct detector. Version checks are only a proxy; the real failure is DLL **file identity** (delvewheel mangles a per-build hash into `avformat-62-<hash>.dll`). Stdlib `struct` only, so a missing `pefile` can never silently skip it. |
| `scripts/check_av_dlls.py --internal dist\DesktopKaraoke` (post-build) | The same PE check against the **shipped bundle** | A PyInstaller collection that mixed sources even though the environment was clean. Runs without launching the exe, so it names the missing DLL instead of returning a bare exit code. |
| `<exe> --selftest --out FILE` (post-build) | The finished exe imports the whole AI stack before any GUI shows, writes a one-line verdict, and exits 0/1 | The definitive gate. Anything the static checks missed. `build.bat` refuses to package on a non-zero exit. |

## Why a **onedir** build (a folder, not a single .exe)

The app ships as a *folder* (`dist\DesktopKaraoke\`) rather than a single
self-extracting `.exe`. A one-file build crams everything into one executable
that **re-extracts ~1 GB — including the 50 MB UniDic dictionary — to a temp
folder on every launch**, which is slow and can fail on machines with strict
antivirus. The onedir build starts **instantly** and is far more reliable. The
Inno Setup installer packs the folder into one `Lyric-Immersion-and-Karaoke-Setup.exe`, so the
end-user experience is still a single download.

## What the installer does (the "layman" experience)

1. Double-click `Lyric-Immersion-and-Karaoke-Setup.exe`, click **Next → Install** (no admin
   rights needed — installs per-user).
2. Two optional checkboxes: **desktop shortcut** and **Start with Windows**.
3. Done. It adds a **Start menu** entry, launches, and lives in the system tray
   (the purple microphone). Play any song.

The packaged app is **portable**: it keeps its lyric library (`lyrics/`) and
`settings.json` right next to `Lyric-Immersion-and-Karaoke.exe`, so the whole folder is
self-contained — copy it anywhere, or make it a git repo to back up.

## No Inno Setup? Make a Start-menu entry directly

If you only built the folder (no installer), you can still pin it to the Start
menu with a shortcut to `dist\DesktopKaraoke\Lyric-Immersion-and-Karaoke.exe` (use `icon.ico`
as the icon). PowerShell one-liner:

```powershell
$W=New-Object -ComObject WScript.Shell
$S=$W.CreateShortcut((Join-Path ([Environment]::GetFolderPath('Programs')) 'Lyric Immersion and Karaoke.lnk'))
$S.TargetPath="$PWD\dist\DesktopKaraoke\Lyric-Immersion-and-Karaoke.exe"; $S.IconLocation="$PWD\icon.ico"; $S.Save()
```

## The app icon

`icon.ico` is generated reproducibly by **`scripts/make_icon.py`** (a karaoke microphone
with sound waves on a purple gradient). To tweak it, edit the colours/geometry
constants at the top of that file and regenerate:

```bat
python scripts/make_icon.py --preview     :: rewrites icon.ico + a _icon_preview.png contact sheet
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
set PIP_CACHE_DIR=<pip-cache>
py -3.12 -m pip install --target .deps -r requirements-deps.txt
py -3.12 -m PyInstaller --noconfirm DesktopKaraoke.spec  :: auto-detects .deps (~650 MB .exe)
```

**Vendor from `requirements-deps.txt`, not from a bare `pip install faster-whisper`.**
That file pins the exact known-good native set (PyAV, ctranslate2, tokenizers,
onnxruntime, huggingface-hub). These packages ship compiled extensions whose DLL
filenames embed a per-build hash, so an unpinned install silently drifts and
reintroduces the skew described below. To bump the pin: upgrade in the build env,
run a clean build, and update the file once `--selftest` passes.

The spec sets `WHISPER = os.path.isdir(".deps")`: **no `.deps` → lean ~150 MB build**
(the feature shows a "needs faster-whisper" hint); **`.deps` present → self-contained
~650 MB build** with the feature working. PyInstaller's hooks are required — a loose
`sys.path` vendor fails on PyAV's `av._core` DLLs. The ASR model (~75 MB) downloads
to the app's data folder on first use (copy `models\` next to the `.exe` to pre-seed).

> ### ⚠️ `.deps` MUST match the build environment (TICKET-177)
> `collect_all` bundles the native stack (PyAV / ctranslate2 / faster-whisper /
> tokenizers) from **both** `.deps` and your pip environment. If they disagree on a
> version — e.g. `.deps` PyAV 17.x while your env has 18.x — a **skewed mix of Python
> modules and FFmpeg DLLs** ships and `import av` (→ faster-whisper → `align.available()`)
> dies at runtime with `av._core`, **silently** disabling *every* listen feature
> (generate-by-ear, sync-by-listening, and the wrong-lyrics reject path). This shipped
> broken in v1.1.74–v1.1.76.
>
> **Three guards make this impossible now:**
> - **In the spec** (`DesktopKaraoke.spec`, TICKET-196) — the only one that cannot be
>   bypassed. It compares the `cpXY` tag on the vendored `.pyd`s against the running
>   interpreter and refuses to build on a mismatch. This matters because **the bare
>   `python` on the Windows build box is a 3.11 agent venv while `.deps` is cp312**:
>   invoking PyInstaller directly (to dodge `build.bat`'s interactive prompt) built a
>   silently whisper-dead app that exited 0.
>
>   `build.bat` now resolves `py -3.12` itself, so the documented command works. If
>   you invoke PyInstaller by hand, name the interpreter explicitly:
>   `py -3.12 -m PyInstaller --noconfirm DesktopKaraoke.spec`
>
>   Scope: this checks the **ABI tag only**. It cannot see a version *skew* between
>   `.deps` and the env (PyAV 17 vs 18) — that is the pre-build check's job, below —
>   and it is blind to extensions that carry no `cpXY` tag at all. It fails open when
>   `.deps` has no tagged modules.
>
> The others run inside `build.bat`, so they only fire on that path:
> - **Pre-build** (`python scripts/check_build_deps.py`): fails the build if `.deps` and
>   the env disagree on any native-stack version, if `.deps` has duplicate dist-info
>   dirs (a `pip install --upgrade --target` leaves the old one behind), or if `.deps`
>   holds extensions built for a **foreign CPython ABI**. Rebuild `.deps` from scratch
>   rather than upgrading into it:
>   `rmdir /s /q .deps && py -3.12 -m pip install --target .deps -r requirements-deps.txt`
>
>   The ABI arm is deliberately stricter than the spec's: the spec passes when the build
>   tag is merely *present*, so a `.deps` vendored twice under two Pythons (holding
>   **both** cp311 and cp312) sails through it. Nothing else catches a mixed vendor tree
>   — a dist-info directory name carries no ABI tag, so the duplicate check is blind to it.
> - **Pre- and post-build** (`python scripts/check_av_dlls.py` / `--internal dist\DesktopKaraoke`):
>   the **direct** detector, and the one that actually names the fault. A version check is
>   only a proxy — dist-info metadata can lag the real module files, and this failure is
>   about **DLL file identity**, not version strings. PyAV's `av/_core.pyd` is linked
>   against delvewheel-mangled FFmpeg DLLs (`avformat-62-<hash>.dll`) whose names embed a
>   per-build hash, so a `_core.pyd` from one PyAV build needs *different files* than
>   another build's `av.libs`. This parses the PE import table and asserts every FFmpeg
>   DLL the `.pyd` imports is present. Stdlib-only (`struct`, no `pefile`), so it cannot
>   be silently skipped on a machine that happens to lack a dependency. The post-build arm
>   checks the **shipped bundle**, catching a PyInstaller collection that mixed sources
>   even when the environment was clean — and it does so without launching the exe, so it
>   reports the missing DLL by name instead of the `--selftest`'s bare exit code.
> - **Post-build** (`<exe> --selftest --out FILE`): the finished `.exe` imports the whole
>   stack before any GUI shows and exits non-zero if it can't — so a whisper-broken bundle
>   never gets packaged. Run it yourself any time to check a build:
>   `dist\DesktopKaraoke\Lyric-Immersion-and-Karaoke.exe --selftest --out check.txt`.
>
> **At runtime**, `GET /diag` exposes `whisper.available` and `whisper.last_error` — the
> same `align.available()` verdict, on the machine that actually has the build. The guards
> above prove the bundle was *assembled* correctly; this is how a deployed app or a CI
> probe confirms the listen features are genuinely live without reading logs.
>
> If you `pip install --target .deps` a package that pulls a **different** native version
> than your env, re-pin `requirements-deps.txt` so `.deps` and the env agree, and rebuild
> `.deps` from scratch rather than upgrading into it.

## Optional: deep lyric transcription (`yt-dlp` + a JS runtime)

The background **deep generation** ([docs/GENERATION.md](docs/GENERATION.md)) — which
downloads a no-lyrics-anywhere song's source audio and transcribes the whole file for a
clean generated lyric — needs **faster-whisper** (above) plus **`yt-dlp`** and a **JS
runtime** (YouTube otherwise 403s the audio download). To ship it in the portable build:

```bat
py -3.12 -m pip install --target .deps yt-dlp   :: vendor it alongside faster-whisper
:: then place a JS runtime next to the .exe so yt-dlp finds it on PATH:
::   - Node:  copy node.exe into the app folder, OR
::   - Deno:  copy the single deno.exe into the app folder (smaller, ~40 MB)
py -3.12 -m PyInstaller --noconfirm DesktopKaraoke.spec
```

`deep_transcribe.available()` is just `import yt_dlp`, and `_download_audio` only enables
`node` when it's on `PATH` — so a build **without** these still runs fine: the instant
best-effort generation stands and the deep pass quietly no-ops. (The lyric library backup
and the personal music DB are kept in a **private** repo, never the public one.)

## Linux and macOS builds

Linux and macOS packages are **not built on the Windows dev box**. They are produced by
GitHub Actions from the same `DesktopKaraoke.spec` and uploaded to the release:

```bash
# Push the release tag; CI builds and attaches Linux/macOS assets automatically.
git tag -a vX.Y.Z -m "Release vX.Y.Z"
git push origin vX.Y.Z
```

The workflow is `.github/workflows/release.yml`. It runs on `push` of a `v*.*.*` tag and
also supports `workflow_dispatch` for back-filling Linux/macOS assets after a manual
Windows-only release.

Local cross-platform builds are not practical from Windows: the native stack
(`winsdk`, soundcard, PyAV FFmpeg DLLs, `av.libs`, etc.) is platform-specific, and
PyInstaller's `onedir` collection differs by OS. Always let CI produce the Linux
(`*-linux-x86_64.tar.gz`) and macOS (`*-macos.zip`) artifacts.

## Notes

- `Lyric-Immersion-and-Karaoke.exe` is windowed (no console) and bundles Python and every
  dependency (winsdk, soundcard, shazamio, fugashi + UniDic, cutlet, …) via
  `DesktopKaraoke.spec`.
- The lyric cache is **not** bundled (copyright) — it builds itself as songs play.
- "Start with Windows" can also be toggled any time from the tray menu.
