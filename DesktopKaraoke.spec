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
    excludes=["tkinter.test", "test", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="DesktopKaraoke",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,            # no console window — clean GUI app
    disable_windowed_traceback=False,
    icon="icon.ico",
)
