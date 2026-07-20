# -*- mode: python ; coding: utf-8 -*-
# PyInstaller build spec — cross-platform (PORTING.md). Windows → windowed .exe
# (onedir, wrapped by Inno into the one-click Setup.exe); Linux → onedir the
# release workflow tars; macOS → .app BUNDLE. Windows-only bits (winsdk / pycaw
# / comtypes / .ico / version resource / the overlay + dev-console child exes)
# are guarded so the same spec builds green on the ubuntu/macos CI runners.
#   pyinstaller --noconfirm DesktopKaraoke.spec   (or run build.bat)
import os
import sys
from PyInstaller.utils.hooks import collect_all

IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")

# TICKET-196: the vendored ./.deps is built for ONE CPython ABI (cp312 on the
# Windows build box). Building under a different minor version produces an .exe
# that starts fine and then fails `import numpy._core._multiarray_umath` at
# runtime — whisper silently dead, exactly the class of breakage TICKET-175/177
# exist to stop. `build.bat` guards this via scripts/check_build_deps.py, but
# invoking PyInstaller DIRECTLY skips that check, and on this machine the bare
# `python` on PATH is a 3.11 agent venv rather than the 3.12 the app needs — so
# the direct path is easy to take by accident. The spec is the one file every
# build route must load, which makes it the right place for the backstop.
if IS_WIN and os.path.isdir(".deps"):
    import glob as _glob
    _tags = {os.path.basename(p).split(".")[-2].split("-")[0]
             for p in _glob.glob(os.path.join(".deps", "**", "*.pyd"), recursive=True)
             if ".cp" in os.path.basename(p)}
    _want = "cp%d%d" % sys.version_info[:2]
    if _tags and _want not in _tags:
        raise SystemExit(
            "\n[spec] ABI MISMATCH — refusing to build a broken bundle.\n"
            "       .deps is %s, this interpreter is %s (%s)\n"
            "       Build with the matching Python, e.g.\n"
            "         C:\\Users\\<you>\\AppData\\Local\\Programs\\Python\\Python312\\python.exe "
            "-m PyInstaller --noconfirm DesktopKaraoke.spec\n"
            "       See docs/BUILD.md and ISSUES.md TICKET-196.\n"
            % ("/".join(sorted(_tags)), _want, sys.executable))

# OPTIONAL "Sync by listening" stack (faster-whisper) is bundled ONLY when it has
# been vendored into ./.deps (pip install --target .deps faster-whisper). Without
# it the default build stays lean (~150 MB) and the feature shows a "needs
# faster-whisper" hint; with it the .exe is self-contained (~650 MB) and the
# feature works out of the box. PyInstaller's hooks place the ctranslate2/PyAV
# DLLs correctly — a loose sys.path vendor fails on av._core.
WHISPER = os.path.isdir(".deps") and os.environ.get("LEAN_BUILD") != "1"

datas = [("icon.ico", ".")]
overlay_exe = os.path.join("overlay", "lyric-overlay.exe")
if os.path.isfile(overlay_exe):
    datas.append((overlay_exe, "overlay"))
# v1.1.66 dev-console: an optional Tauri companion (dev-console/) shown from
# the tray's "🛠 Developer Console" item. It's bundled ONLY when the release
# build has been produced (npm run tauri:build inside dev-console/). Without
# the exe on disk the tray item toasts a hint pointing at dev-console\README.md,
# so this branch never breaks a build — it just enables the tray launcher in
# installed copies. Runtime resolver: main.py.Overlay._dev_console_exe.
devconsole_exe = os.path.join(
    "dev-console", "src-tauri", "target", "release",
    "lyric-immersion-dev-console.exe",
)
if os.path.isfile(devconsole_exe):
    datas.append((devconsole_exe, "dev-console"))
# TICKET-124: NO bundled lyrics are shipped. This is a sellable product — every lyric
# must be FOUND BY CODE at runtime (providers / YouTube captions / OCR / by-ear), never
# copyrighted text baked into the build. (bundled_lyrics/ was removed from the repo.)
binaries = []
hiddenimports = [
    "PIL._tkinter_finder", "PIL.ImageTk", "PIL.ImageGrab",
    # local modules imported lazily inside functions — pin them so the
    # frozen build always includes them.
    "appdata", "version", "updater", "songchange", "align", "api", "character", "recognize", "fetch_lyrics", "gpu_setup", "metrics",
    # TICKET-212: the per-knob documentation served by GET /tune. Imported inside
    # the request handler (so a missing/corrupt docs module degrades to no
    # tooltips rather than a dead endpoint), which means the analyzer would not
    # find it. Without this pin the console shows 235 undocumented knobs in the
    # packaged build while looking perfect from source.
    "tune_docs",
    # TICKET-184: the out-of-process Whisper worker. Only imported inside the
    # `--whisper-worker` argv branch, so pin it — without this the frozen app
    # can't spawn its own crash-isolated child.
    "whisper_worker",
    # optional LLM lyric disambiguator — lazy-imported inside _decide_by_ear;
    # pin so the frozen build includes it (stdlib urllib only, no new deps).
    "llm_disambiguate",
    "playlist_import", "playlist_import_gui", "concert_ocr", "ocr_lyrics", "deep_transcribe", "confidence",
    # TICKET-169: movie-site subtitle fetcher — lazy-imported in the captions
    # worker; pin so the frozen build includes it (stdlib-only module).
    "movie_subs",
    # v1.1.57 offline concert audio analysis — lazy-imported in main.py's
    # _analyze_concert_audio thread; pin so the frozen build includes it.
    "concert_audio", "faster_whisper.audio",
    # TICKET-100: Discord IPC reader (lazy-imported in main.py only when the
    # tray toggle is ON, but pin it here so the frozen build includes it).
    "discord_rpc",
    # TICKET-102: window-title scraper (Steam Overlay / Discord / Slack / Teams
    # CEF hosts that DON'T publish to SMTC). Lazy-imported in main.py; pin here
    # so the frozen build always includes it. Stdlib + ctypes only, no new deps.
    "window_titles",
    # TICKET-112: YouTube description metadata extractor (composer / vocals /
    # original-artist tags on browser sources). Lazy-imported in main.py inside
    # _maybe_fetch_yt_description; pin here so PyInstaller bundles it.
    "yt_description",
    # (TICKET-118 audible-session Core Audio metering — audible_sessions/pycaw/
    # comtypes — is Windows-only; pinned in the IS_WIN block below.)
    # M2: GPU-driven lyric renderer child process. Lazy-imported in main.py
    # ONLY when sys.argv contains --gpu-renderer-child (dispatched from the
    # same exe). pygame + moderngl + PIL are picked up via collect_all; the
    # renderer module itself needs an explicit pin since it's not imported in
    # the normal startup path.
    "gpu_renderer",
    "moderngl", "moderngl.context", "moderngl.program",
    "pygame", "pygame.image", "pygame.display", "pygame.event", "pygame.time",
]
# ── platform-specific hidden imports ──────────────────────────────────────
if IS_WIN:
    hiddenimports += [
        "pystray._win32",
        # SMTC now-playing + built-in Windows OCR (concert_ocr.py):
        "winsdk.windows.media.control", "winsdk.windows.foundation",
        "winsdk.windows.storage.streams", "winsdk.windows.storage",
        "winsdk.windows.media.ocr", "winsdk.windows.globalization",
        "winsdk.windows.graphics.imaging",
        # audible-session Core Audio metering (TICKET-118):
        "audible_sessions", "pycaw", "pycaw.pycaw", "comtypes", "comtypes.gen",
    ]
elif IS_LINUX:
    # MPRIS now-playing (media_mpris) + the pystray backends X11 / AppIndicator.
    hiddenimports += ["media_mpris", "dbus_next", "dbus_next.aio",
                      "pystray._xorg", "pystray._appindicator", "pystray._gtk"]
elif IS_MAC:
    hiddenimports += ["pystray._darwin"]

# BUGFIX (v1.1.70): PyInstaller ALWAYS runs the pkg_resources runtime hook
# (pyi_rth_pkgres) at startup; pkg_resources imports jaraco.text/.functools/
# .context. setuptools 78 ships those ONLY vendored under setuptools/_vendor as a
# PEP-420 namespace that collect_submodules can't walk, so the frozen build had no
# jaraco and the WINDOWED bootloader HUNG on a modal "No module named 'jaraco'"
# dialog (looks like a silent freeze; the pre-7/11 build predated this setuptools
# layout). Fix: a real TOP-LEVEL `jaraco.text` is installed in the build env, and
# setuptools' vendor importer (APPENDED to sys.meta_path) yields to it. Bundle the
# concrete jaraco subpackages + their data (the namespace parent can't be walked).
for _jp in ("jaraco.text", "jaraco.functools", "jaraco.context"):
    try:
        _jd, _jb, _jh = collect_all(_jp)
        datas += _jd
        binaries += _jb
        hiddenimports += _jh
    except Exception:
        pass
hiddenimports += ["jaraco", "jaraco.text", "jaraco.functools",
                  "jaraco.context", "more_itertools"]

# Packages that ship data files / dynamically-imported submodules.
# NOTE: unidic_lite bundles the ~50 MB dictionary fugashi/cutlet need at runtime
# — collect_all is what pulls those data files into the exe.
_collect_pkgs = [
    "soundcard", "shazamio", "pykakasi", "jaconv",
    "fugashi", "unidic_lite", "cutlet", "mojimoji",
    "pypinyin", "ToJyutping", "hangul_romanize", "deep_translator", "syncedlyrics",
    "pystray", "spotipy", "aiohttp", "aiosignal", "pydub", "numpy",
    # M2: pygame-ce (SDL2) + moderngl drive the GPU renderer child.
    # collect_all pulls the SDL DLLs, OpenGL fallback, and moderngl's
    # platform-specific extension modules.
    "pygame", "moderngl",
    # yt-dlp: pulls a YouTube video's own caption track (accurate lyrics
    # + perfect timing, locked to the video) — strictly better than a
    # provider LRC for browser videos. ~10 MB, pure Python.
    "yt_dlp",
]
if IS_WIN:
    # SMTC + audible-session Core Audio metering (comtypes runtime proxy stubs).
    _collect_pkgs += ["winsdk", "pycaw", "comtypes"]
elif IS_LINUX:
    _collect_pkgs += ["dbus_next"]         # MPRIS now-playing over D-Bus
# The faster-whisper stack is appended only when ./.deps exists.
if WHISPER:
    _collect_pkgs += ["faster_whisper", "ctranslate2", "av", "tokenizers",
                      "huggingface_hub", "onnxruntime"]
for pkg in _collect_pkgs:
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception:
        pass

a = Analysis(
    ["main.py"],
    pathex=[".deps"] if WHISPER else [],   # vendored faster-whisper (optional)
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Trim heavy libraries the app never imports (they can get pulled in
    # transitively and bloat the build). Safe to drop — none are used.
    # setuptools._vendor stays excluded (keeps the build lean): pkg_resources'
    # startup import of jaraco.text now resolves to the TOP-LEVEL jaraco bundled
    # above — setuptools' vendor importer is appended to sys.meta_path, so the real
    # top-level package wins. (Excluding _vendor WITHOUT that top-level jaraco is
    # what caused the "No module named 'jaraco'" startup hang.)
    excludes=["tkinter.test", "test", "pytest", "matplotlib", "scipy",
              "IPython", "notebook", "pandas", "PyQt5", "PyQt6", "PySide2",
              "PySide6", "wx", "sphinx", "setuptools._vendor", "wordninja",
              # BUNDLE DIET (v1.1.74, live-caught): OTHER projects' pip installs
              # leaked into the frozen build through transitive import probes —
              # torch alone added 3.6 GB and pushed the release zip past
              # GitHub's 2 GiB asset limit. NONE of these are app imports
              # (verified by grep); faster-whisper runs on ctranslate2, which
              # stays. tokenizers stays too (faster-whisper needs it).
              "torch", "torchvision", "torchaudio", "triton",
              "paddle", "paddleocr", "cv2", "spacy", "thinc",
              "transformers", "datasets", "sklearn", "numba", "llvmlite",
              "sympy", "networkx", "jax", "tensorflow", "keras"],
    noarchive=False,
)
pyz = PYZ(a.pure)

# ONEDIR build: the .exe lives in a folder next to its dependencies. This starts
# instantly (a one-file build re-extracts ~100 MB — including the 50 MB UniDic
# dictionary — to a temp folder on EVERY launch, which is slow and brittle). The
# Inno Setup installer wraps this folder into the one-click DesktopKaraoke-Setup.exe.
# Platform icon + Windows version resource (both no-ops on the other OSes).
if IS_WIN:
    _icon = "icon.ico"
elif IS_MAC:
    _icon = "icon.icns" if os.path.isfile("icon.icns") else None
else:
    _icon = None
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,    # onedir — binaries live alongside, not inside
    name="Lyric-Immersion-and-Karaoke",   # exe filename = the repo name (was DesktopKaraoke)
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,            # no console window — clean GUI app
    disable_windowed_traceback=False,
    icon=_icon,
    # Embed real Windows file metadata (company / product / version). An exe with
    # NO version resource is a strong Defender/SmartScreen false-positive signal on
    # clean machines. version_info.txt keeps its four numbers in sync with version.py.
    version=("version_info.txt" if (IS_WIN and os.path.isfile("version_info.txt"))
             else None),
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="DesktopKaraoke",
)
# macOS: wrap the onedir into a proper .app so it's double-clickable and
# codesign/notarize-able (the release workflow zips DesktopKaraoke.app).
if IS_MAC:
    app = BUNDLE(
        coll,
        name="Lyric Immersion and Karaoke.app",
        icon=_icon,
        bundle_identifier="us.purpleindustries.lyric-immersion",
        info_plist={
            "CFBundleShortVersionString": os.environ.get("LI_VERSION", "1.1.74"),
            "LSMinimumSystemVersion": "11.0",
            "NSHighResolutionCapable": True,
            # TCC usage strings (window-title reading / capture guidance):
            "NSMicrophoneUsageDescription":
                "Listen to system audio (via a loopback device) to identify songs "
                "and sync lyrics.",
        },
    )
