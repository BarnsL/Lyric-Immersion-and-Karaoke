# Porting Plan: Lyric Immersion and Karaoke on Linux and macOS

Status: plan of record. Nothing in this document is started unless a phase says so.
Source tree: this repository root. Overlay renderer: the companion Tauri project
that produces `overlay/lyric-overlay.exe` for the frozen build.

## 1. Executive summary, honest version

**What works today.** The app is a Windows product: WinRT SMTC for now-playing metadata and exact playback position, WASAPI loopback for all listening features (Shazam ID, sync-by-listening, song-change detection, by-ear generation), Win32 for the click-through topmost Tk overlay, pycaw for the "which app is actually making sound" tiebreaker, and Win32 window-title scraping for CEF/Electron hosts. Every one of those is behind a lazy import or sentinel, and the codebase already imports green on Linux (audible_sessions.py:40-43, window_titles.py:31-32, discord_rpc.py:85-97). The by-ear fallback stack (soundcard + shazamio + faster-whisper) is cross-platform at the library level. So the port starts from a non-crashing, degraded-capable baseline, not from zero.

**What a real port costs.** Linux first, because it is the cheap one: MPRIS2 gives us metadata **and** position (something we feared losing), soundcard's pulseaudio backend makes every loopback call site work verbatim on PulseAudio/PipeWire, and Chromium/Electron publish MPRIS so the window-title scraper matters less. The two genuinely large Linux items are both in the overlay, not the engine: Tk has no chroma-key transparency on X11 and nothing at all on native Wayland, so Linux forces the Tauri WebView overlay to become the only renderer. Budget: the engine backends are small-to-medium each; the overlay consolidation is large and is the critical path.

**What is impossible or degraded on macOS.** The system-wide now-playing API (MediaRemote.framework) is private, and since macOS 15.4 Apple gates it to Apple-entitled processes, which killed the nowplaying-cli class of workarounds. We will not build on it. macOS metadata is therefore per-player adapters: full parity (including position) for Spotify.app and Music.app via AppleScript, title-only for browsers, nothing for everything else without a shipped WebExtension. There is no native loopback either; all listening features are off out of the box and require the user to install BlackHole 2ch and wire a Multi-Output Device. There is no public per-process audio meter, so the audible tiebreaker returns `{}` forever. macOS is a deliberately degraded port: by-ear ID only after a driver install, exact position only for two players, and permission prompts (Screen Recording or Accessibility) for window titles.

## 2. Platform abstraction seams

One rule: `main.py` and friends stop touching platform APIs directly. Each seam below is an interface with a `win32`, `linux`, and `darwin` backend, selected once at startup. The existing arbitration, caching, and parsing layers stay where they are; the inventory confirms they are already backend-agnostic.

| Interface | Extracted from | Backends |
|---|---|---|
| `MediaSessionProvider` | main.py:507-556 (SMTC poll loop), main.py:527, 535-539 (position extrapolation inputs), main.py:542 (session identity) | win32: winsdk SMTC (as-is). linux: MPRIS2 via `dbus-next` (asyncio, so `MediaWatcher._loop` ports 1:1); poll `Position` on the existing 0.15 s cadence (it is excluded from PropertiesChanged), subscribe to `Seeked`; bus-name suffix replaces `SourceAppUserModelId`. darwin: AppleScript/ScriptingBridge adapters for Spotify.app and Music.app (title/artist/album/duration/`player position`/state), browser tab titles as title-only. Contract: fill `{title, artist, album, status, position, duration, rate, source}`; `_pick` (main.py:598-727) and `_session_key` (main.py:447-454) consume it unchanged. |
| `PlaybackStatus` mapping | main.py:302 (hardcoded WinRT `PLAYING = 4`) | Replace the raw constant with an app-level enum; map MPRIS `'Playing'` and AppleScript player states onto it. This is the one hard-coupled constant every backend must translate. |
| `AudibleMeter` | audible_sessions.py:81, :88, :128-130 (pycaw `GetAllSessions` + `IAudioMeterInformation.GetPeakValue`); only `_enumerate_once` moves, the TTL cache/watchdog/aggregation shell (audible_sessions.py:37-57, 156-229) stays | linux: `pulsectl` `sink_input_list()` (proplist gives pid + binary name, matching the existing exe-basename aggregation) + `get_peak_sample` on the sink monitor. darwin: none practical, return `{}` and let `_pick` fall through to sticky (main.py:642-661, 692-727 already tolerate an empty map). |
| `WindowTitleSource` | window_titles.py:177-228, 262-291, 297-337, 538-615 (Win32 enum loop); parsing layer 105-151, 342-401 is pure and stays | linux/X11: python-ewmh (`_NET_CLIENT_LIST`, `_NET_WM_NAME`, `_NET_WM_PID` + `/proc/<pid>/comm`, `_NET_ACTIVE_WINDOW`), near line-for-line. Wayland: zwlr-foreign-toplevel-management on wlroots, no-op on GNOME/KDE. darwin: Quartz `CGWindowListCopyWindowInfo` (needs Screen Recording TCC) or AX API (needs Accessibility TCC). |
| `DiscordIpc` | discord_rpc.py:509, :678 (pipe path), :516-519, :119-168 (CreateFile/ReadFile/WriteFile); framing and activity mapping (174-242) are pure and stay | linux/darwin: stdlib `AF_UNIX` socket at `$XDG_RUNTIME_DIR/discord-ipc-N` (+ flatpak/snap paths) or `$TMPDIR/discord-ipc-N` on macOS. Smallest seam in the app. |
| `LoopbackCapture` | align.py:339-355, recognize.py:27-60, songchange.py:134-177 (the shared `all_microphones(include_loopback=True)` + `isloopback` + name-pairing pattern) | linux: no code change, soundcard's pulseaudio backend accepts the same call and exposes monitor sources; the `'spk.name in m.name'` heuristic survives because monitors are described "Monitor of <sink>". darwin: `isloopback` is hardwired False (coreaudio.py:195-197), so add a darwin branch that selects a named device (BlackHole 2ch, user-configurable). |
| `OverlayWindow` | main.py:1970-1980, 9684-9740 (click-through + z-guard), main.py:286, 2422-2425, 9788-9792 (chroma key), main.py:1954-1967 (work area), main.py:2045-2108 (monitors), main.py:45-48 (DPI), main.py:13603-13612 (timer) | The big one. On Linux the answer is not "port Tk", it is "make the Tauri overlay (already exists for GPU mode, `_tauri_target_bounds` main.py:9897, 9955-9973) the sole renderer": true per-pixel alpha via webkit2gtk RGBA, click-through and always-on-top via Tauri APIs. macOS: same Tauri answer; the pyobjc-into-Tk route (NSWindow behind a Tk root) is documented fragile and rejected. Work area: `Gdk.Monitor.get_workarea()` / `NSScreen.visibleFrame` (Y flipped). Monitors: GDK or `screeninfo` / `NSScreen.screens` + `CGDisplayCreateUUIDFromDisplayID`. DPI and timeBeginPeriod: no-ops off Windows. |
| `SingleInstance` | main.py:13419-13445 (`CreateMutexW` + ERROR_ALREADY_EXISTS), gate at 13597 | linux/darwin: `fcntl.flock(LOCK_EX|LOCK_NB)` on a lockfile in the app data dir (no abstract sockets on macOS); port 8765 remains the secondary signal. |
| `ProcessTuning` | main.py:13463-13530 (affinity + priority, TICKET-129), applied at 13634 | linux: `os.sched_setaffinity` + `os.nice()`, SMT from `/sys/devices/system/cpu/*/topology`. darwin: no hard affinity exists; `os.nice()` only, skip pinning. |
| `FontResolver` | main.py:62-75 (`_PIL_FONTS`/`_TK_MAIN_FONT`), 7068-7077 (bare-filename `ImageFont.truetype` relying on `C:\Windows\Fonts`); lyric-overlay-tauri/src/style.css:75-115 | linux: Noto Sans CJK via fontconfig (`fc-match -f %{file}`), append `'Noto Sans CJK JP', 'Noto Sans'` to the CSS stacks. darwin: Hiragino Sans / Apple SD Gothic Neo / PingFang SC + system font APIs. |
| `TrayIcon`, `Autostart`, `Paths`, `OcrEngine` | tray, Run-key/Startup autostart, `%APPDATA%`/`%LOCALAPPDATA%` path builders, and the Windows OCR path in the sync-assist code | pystray already abstracts tray on all three (AppIndicator on Linux). Autostart: XDG `~/.config/autostart/*.desktop` / `LaunchAgents` plist. Paths: `platformdirs`. OCR: swap the Windows engine for `tesseract` via `pytesseract` on Linux (packaged in the AppImage), Vision.framework via pyobjc on macOS later. |
| Packaging split | DesktopKaraoke.spec:25-29 (winsdk hiddenimports), :54, :69, :73-76 (pycaw/comtypes/collect loop); requirements.txt:9 (winsdk unmarked), :27 (pycaw already `sys_platform == "win32"`) | Add platform markers to winsdk; gate the spec's collect loop by `sys.platform`; declare libpulse0/libpulse as a system dep on Linux (soundcard dlopens it, pulseaudio.py:17-20, PyInstaller cannot bundle it). |

## 3. Phased roadmap

### Phase 0: import-clean everywhere (small, no new OS needed)

Goal: `python -c "import main"` (or a headless entry) exits 0 on ubuntu and macos runners.

- [ ] Add `; sys_platform == "win32"` to `winsdk` in requirements.txt:9 (pycaw at :27 already has it) and guard the main.py:507 import behind the `MediaSessionProvider` selector.
- [ ] Replace the raw `PLAYING = 4` at main.py:302 with the status enum and a WinRT mapping in the win32 backend.
- [ ] Make main.py:45-48 (DPI), 13603-13612 (timeBeginPeriod), 13463-13530 (affinity) explicit platform-dispatched no-ops instead of relying on swallowed exceptions.
- [ ] Split DesktopKaraoke.spec:69 so winsdk/pycaw/comtypes collect only on win32.
- [ ] CI: GitHub Actions matrix `{ubuntu-latest, macos-latest, windows-latest}` running the import gate plus the pure-logic unit tests (title parser window_titles.py:105-151, `_pick` ladder, discord framing).

Testable without owning Linux/macOS: all of it, in CI.

### Phase L1: Linux MVP (medium engine + large overlay)

Deliverable: AppImage that identifies Spotify/VLC/browser playback via MPRIS, syncs by listening on PipeWire, renders via the Tauri overlay on X11 (and XWayland).

- [ ] `MediaSessionProvider.linux`: `dbus-next` MPRIS backend (enumerate `org.mpris.MediaPlayer2.*`, Metadata/PlaybackStatus/Rate, poll `Position`, handle `Seeked`); reuse the wall-clock extrapolation in `MediaWatcher.get()` (main.py:729-761) untouched.
- [ ] `AudibleMeter.linux` with pulsectl inside `_enumerate_once` only.
- [ ] `DiscordIpc` AF_UNIX transport (+ flatpak/snap socket paths).
- [ ] Verify loopback capture end to end: align.py, recognize.py (t_cap anchor recognize.py:48-51 is backend-agnostic), songchange.py soak under PipeWire, including the songchange.py:159-163 backoff across `pipewire-pulse` restarts.
- [ ] Overlay: Tauri-only render path on Linux, click-through + always-on-top + per-monitor placement; X11 first, wlroots foreign-toplevel later; document GNOME Wayland as "run under XWayland".
- [ ] `WindowTitleSource.x11` via python-ewmh (Steam Overlay and Discord embeds still need it even with MPRIS).
- [ ] FontResolver via fontconfig, Noto Sans CJK in the CSS stacks.
- [ ] Packaging: PyInstaller onedir + appimagetool, libpulse0 listed as external, single-instance via flock.

Testable without hardware: MPRIS backend against a fake `dbus-next` service and against real players inside **WSL2/WSLg** (WSLg gives Wayland+XWayland, PulseAudio, and a session D-Bus, so MPRIS + monitor-source capture + X11 overlay can all be exercised there); headless X via Xvfb in CI for the ewmh scraper. Needs real hardware: PipeWire soak, Wayland compositor matrix, multi-monitor, GPU overlay smoothness.

### Phase L2: Linux polish (small-medium)

- [ ] OCR sync-assist via tesseract (bundled) replacing the Windows OCR engine.
- [ ] Autostart (.desktop), tray via pystray/AppIndicator, `platformdirs` paths, updater (AppImage zsync or a self-check against GitHub releases, matching the existing gh-release flow).
- [ ] Wayland-native click-through where compositors allow (wlr layer-shell via Tauri plugin), else documented XWayland.

### Phase M1: macOS MVP, deliberately degraded (medium-large)

Deliverable: notarized .app where Spotify/Music get full metadata + exact position, everything else degrades to by-ear (after BlackHole install) or window titles.

- [ ] `MediaSessionProvider.darwin`: ScriptingBridge/osascript adapters for Spotify.app and Music.app (`player position` feeds the same extrapolation math); browser tab-title adapter (title-only, no position). Explicitly do NOT touch MediaRemote.
- [ ] `LoopbackCapture.darwin`: named-device selection (BlackHole 2ch default, user-overridable) since `isloopback` can never be true (coreaudio.py:195-197); first-run guidance for BlackHole + Multi-Output Device setup, including the honest caveats (admin install, volume keys stop working, routing breaks on output switch).
- [ ] `AudibleMeter.darwin`: permanent `{}`, sticky arbitration.
- [ ] `WindowTitleSource.darwin`: Quartz CGWindowList behind the Screen Recording prompt, foreground via `NSWorkspace.frontmostApplication`.
- [ ] Overlay: Tauri renderer with `setIgnoresMouseEvents`/window level handled by Tauri itself; `NSScreen.visibleFrame` with Y-flip; no CPU pinning.
- [ ] Packaging: py2app or PyInstaller .app, codesign + notarize, TCC usage strings.
- [ ] Stretch (M2): WebExtension posting `navigator.mediaSession` metadata + `currentTime` to the existing localhost API (api.py) for browser position; Swift Core Audio process-tap helper (macOS 14.4+) to remove the BlackHole requirement.

Testable without a Mac: `macos-latest` CI runners cover import gate, AppleScript adapters against a mocked osascript, unit tests, and even real Music.app scripting to a degree (runners have Music installed but no audio). Needs real hardware: BlackHole routing, TCC prompt flows, notarization gatekeeper check, overlay-over-fullscreen behavior.

## 4. What stays Windows-only forever, unless…

- **System-wide now-playing on macOS**, unless Apple ships a public MediaRemote replacement or entitles third parties (post-15.4 the private route is closed; do not revisit).
- **Per-process audible metering on macOS**, unless we ship a compiled Swift helper using Core Audio process taps (macOS 14.4+, audio-recording TCC), which is disproportionate for a tiebreaker.
- **Silent, permissionless window-title scraping**: macOS gates it behind TCC prompts; GNOME/KDE native Wayland has no client API at all, unless we ship a Shell extension/KWin script.
- **Driver-free loopback capture on macOS**, unless we build the ScreenCaptureKit or process-tap helper (large, needs Screen Recording TCC and notarized helper).
- **Hard CPU affinity on macOS** (no public API, hints only) and **client-chosen monitor placement plus global z-order on native Wayland** (compositor policy by design).
- **The Tk chroma-key overlay itself** (main.py:286, 2422-2425): it never leaves Windows; other platforms are Tauri-only.

## 5. Testing strategy

- **Now (Phase 0):** Actions matrix on ubuntu/macos/windows running the import gate and pure-logic tests every push. This locks in the "already imports green on Linux" property so it cannot regress.
- **Phase L1 onward:** ubuntu job grows a smoke tier: Xvfb + a mock MPRIS player (`dbus-next` test service) asserting the provider fills the session dict and maps `'Playing'` correctly; a null-sink PulseAudio (`pulseaudio --start` or pipewire in the runner) asserting `all_microphones(include_loopback=True)` yields a monitor and a 1 s capture returns samples; container images (Ubuntu LTS, Fedora, Arch) for install-matrix checks of the AppImage.
- **Suggested Linux bench:** WSL2 with WSLg is a useful first-pass Linux dev environment: session D-Bus for MPRIS and Discord sockets, PulseAudio server for monitor capture, XWayland for the ewmh scraper and overlay bring-up. Anything that passes there should still get a final pass on one real bare-metal distro before release.
- **macOS:** macos runners for import + adapter unit tests; a physical or MacStadium/Scaleway rented Mac is required once per M1 milestone for BlackHole, TCC, and notarization, and is the explicit gate on shipping M1.
- **Release gate per platform:** the Phase 0 import matrix green, the platform smoke tier green, and one manual end-to-end on real hardware (play a JP track in Spotify, confirm ID, sync, and overlay).
