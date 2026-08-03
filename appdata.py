"""Where Desktop Karaoke keeps its writable data — settings, the lyric cache,
and the log.

Resolved in ONE place so the overlay (main.py) and the lyric engine
(fetch_lyrics.py) always agree on the same folder.

  • From source         → next to the .py files (the repo).
  • Portable .exe        → next to the .exe, so the whole folder stays
                           self-contained and copyable (the documented
                           "portable" behaviour).
  • Installed via MSIX   → the install dir under Program Files\\WindowsApps is
                           READ-ONLY, so writing next to the .exe fails. Data
                           goes to %LOCALAPPDATA%\\DesktopKaraoke instead, which
                           the MSIX runtime transparently redirects to the
                           package's private per-user store.

Only depends on the standard library, so importing it at startup is cheap.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def is_packaged() -> bool:
    """True when running inside an MSIX/AppX package (read-only install dir)."""
    if sys.platform != "win32":
        return False
    try:
        from ctypes import windll, byref, c_uint32
        length = c_uint32(0)
        # GetCurrentPackageFullName returns APPMODEL_ERROR_NO_PACKAGE (15700)
        # when there is no package identity; packaged processes return
        # ERROR_INSUFFICIENT_BUFFER (122) for our zero-length probe buffer.
        return windll.kernel32.GetCurrentPackageFullName(byref(length), None) != 15700
    except Exception:
        return False


def data_dir() -> Path:
    """The writable data directory, created if missing."""
    if getattr(sys, "frozen", False):
        if is_packaged():
            root = Path(os.environ.get("LOCALAPPDATA") or Path.home())
            base = root / "DesktopKaraoke"
        else:
            base = Path(sys.executable).parent          # portable .exe
    else:
        base = Path(__file__).parent                    # running from source
    try:
        base.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return base
