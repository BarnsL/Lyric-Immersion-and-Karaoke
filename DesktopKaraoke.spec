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
WHISPER = os.path.isdir(".deps")

datas = [("icon.ico", ".")]
# Lyrics baked into the app for songs the providers always miss (feelingradation):
# seeded into the runtime cache at startup by _seed_bundled_lyrics().
if os.path.isdir("bundled_lyrics"):
    datas.append(("bundled_lyrics", "bundled_lyrics"))
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
    "appdata", "version", "updater", "songchange", "align", "api", "character", "recognize", "fetch_lyrics", "gpu_setup",
    "playlist_import", "playlist_import_gui", "concert_ocr", "deep_transcribe", "confidence",
]

# Packages that ship data files / dynamically-imported submodules.
# NOTE: unidic_lite bundles the ~50 MB dictionary fugashi/cutlet need at runtime
# — collect_all is what pulls those data files into the exe.
for pkg in ("winsdk", "soundcard", "shazamio", "pykakasi", "jaconv",
            "fugashi", "unidic_lite", "cutlet", "mojimoji",
            "pypinyin", "hangul_romanize", "deep_translator", "syncedlyrics",
            "pystray", "spotipy", "aiohttp", "aiosignal", "pydub", "numpy",
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
    excludes=["tkinter.test", "test", "pytest", "matplotlib", "scipy",
              "IPython", "notebook", "pandas", "PyQt5", "PyQt6", "PySide2",
              "PySide6", "wx", "sphinx", "setuptools._vendor", "wordninja"],
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
    name="DesktopKaraoke",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,            # no console window — clean GUI app
    disable_windowed_traceback=False,
    icon="icon.ico",
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="DesktopKaraoke",
)
