# -*- mode: python ; coding: utf-8 -*-
# PyInstaller build spec for Desktop Karaoke → a single windowed .exe.
#   pyinstaller --noconfirm DesktopKaraoke.spec   (or run build.bat)
from PyInstaller.utils.hooks import collect_all

datas = [("icon.ico", ".")]
binaries = []
hiddenimports = [
    "pystray._win32", "PIL._tkinter_finder", "PIL.ImageTk",
    "winsdk.windows.media.control", "winsdk.windows.foundation",
    "winsdk.windows.storage.streams",
    # local modules imported lazily inside functions — pin them so the
    # frozen build always includes them.
    "appdata", "songchange", "api", "character", "recognize", "fetch_lyrics",
]

# Packages that ship data files / dynamically-imported submodules.
# NOTE: unidic_lite bundles the ~50 MB dictionary fugashi/cutlet need at runtime
# — collect_all is what pulls those data files into the exe.
for pkg in ("winsdk", "soundcard", "shazamio", "pykakasi", "jaconv",
            "fugashi", "unidic_lite", "cutlet", "mojimoji",
            "pypinyin", "hangul_romanize", "deep_translator", "syncedlyrics",
            "pystray", "spotipy", "aiohttp", "aiosignal", "pydub", "numpy"):
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception:
        pass

a = Analysis(
    ["main.py"],
    pathex=[],
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
