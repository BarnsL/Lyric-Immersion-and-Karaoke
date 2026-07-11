# -*- mode: python ; coding: utf-8 -*-
# PyInstaller build spec for Desktop Karaoke → a single windowed .exe.
#   pyinstaller --noconfirm DesktopKaraoke.spec   (or run build.bat)
import os
from PyInstaller.utils.hooks import collect_all

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
    "pystray._win32", "PIL._tkinter_finder", "PIL.ImageTk", "PIL.ImageGrab",
    "winsdk.windows.media.control", "winsdk.windows.foundation",
    "winsdk.windows.storage.streams", "winsdk.windows.storage",
    # concert-banner OCR (concert_ocr.py) uses the built-in Windows OCR engine:
    "winsdk.windows.media.ocr", "winsdk.windows.globalization",
    "winsdk.windows.graphics.imaging",
    # local modules imported lazily inside functions — pin them so the
    # frozen build always includes them.
    "appdata", "version", "updater", "songchange", "align", "api", "character", "recognize", "fetch_lyrics", "gpu_setup", "metrics",
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
    # TICKET-118: audible-session preference (Core Audio peak meter → pick the
    # AUDIBLE SMTC session when multiple tabs publish to SMTC and one is muted).
    # Lazy-imported inside audible_sessions.get_process_audio_levels(); pin
    # pycaw + comtypes.gen so the frozen build can resolve the runtime COM
    # proxy stubs comtypes generates lazily.
    "audible_sessions",
    "pycaw", "pycaw.pycaw",
    "comtypes", "comtypes.gen",
    # M2: GPU-driven lyric renderer child process. Lazy-imported in main.py
    # ONLY when sys.argv contains --gpu-renderer-child (dispatched from the
    # same exe). pygame + moderngl + PIL are picked up via collect_all; the
    # renderer module itself needs an explicit pin since it's not imported in
    # the normal startup path.
    "gpu_renderer",
    "moderngl", "moderngl.context", "moderngl.program",
    "pygame", "pygame.image", "pygame.display", "pygame.event", "pygame.time",
]

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
for pkg in ("winsdk", "soundcard", "shazamio", "pykakasi", "jaconv",
            "fugashi", "unidic_lite", "cutlet", "mojimoji",
            "pypinyin", "ToJyutping", "hangul_romanize", "deep_translator", "syncedlyrics",
            "pystray", "spotipy", "aiohttp", "aiosignal", "pydub", "numpy",
            # TICKET-118: pycaw uses comtypes-generated proxy stubs at runtime
            # — collect_all pulls the data files / lazy submodules PyInstaller
            # would otherwise miss in the frozen build.
            "pycaw", "comtypes",
            # M2: pygame-ce (SDL2) + moderngl drive the GPU renderer child.
            # collect_all pulls the SDL DLLs, OpenGL fallback, and moderngl's
            # platform-specific extension modules.
            "pygame", "moderngl",
            # yt-dlp: pulls a YouTube video's own caption track (accurate lyrics
            # + perfect timing, locked to the video) — strictly better than a
            # provider LRC for browser videos. ~10 MB, pure Python.
            "yt_dlp",
            # The faster-whisper stack is appended only when ./.deps exists.
            *(("faster_whisper", "ctranslate2", "av", "tokenizers",
               "huggingface_hub", "onnxruntime") if WHISPER else ())):
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
    icon="icon.ico",
    # Embed real Windows file metadata (company / product / version). An exe with
    # NO version resource is a strong Defender/SmartScreen false-positive signal on
    # clean machines. version_info.txt keeps its four numbers in sync with version.py.
    version="version_info.txt" if os.path.isfile("version_info.txt") else None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="DesktopKaraoke",
)
