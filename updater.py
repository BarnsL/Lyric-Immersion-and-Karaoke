"""In-app updater.

Three distribution channels, three behaviours:

  • Installed from the **Microsoft Store** (MSIX) → the Store delivers updates
    automatically, so this module does nothing (``check()`` returns ``None``).
  • The **portable .exe** → checks GitHub Releases and, if a newer build is
    published as a ``.zip`` of the onedir folder, downloads it and launches a
    helper that swaps it in (keeping your lyric cache + settings) and relaunches.
  • Running **from source** → no self-update (use ``git pull``); ``check()`` still
    reports whether a newer release tag exists.

Everything is best-effort and offline-safe: any network/parse error is treated
as "no update", and a failed self-update falls back to opening the Releases page.
Only the standard library is used, so importing this at startup is cheap.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import threading
import time
import urllib.request
from pathlib import Path

import appdata
from version import __version__

REPO = "BarnsL/Desktop-Karaoke"
API_LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{REPO}/releases/latest"
_UA = f"DesktopKaraoke/{__version__}"

# Batch helper: wait for our PID to exit, copy the new files over the install dir
# WITHOUT purging (so lyrics/ + settings.json survive), then relaunch.
_HELPER = (
    "@echo off\r\n"
    ":wait\r\n"
    'tasklist /fi "PID eq {pid}" 2>nul | find "{pid}" >nul && '
    "(timeout /t 1 /nobreak >nul & goto wait)\r\n"
    'robocopy "{src}" "{dst}" /E /R:2 /W:1 /NFL /NDL /NJH /NJS /NP >nul\r\n'
    'start "" "{exe}"\r\n'
)


def current_version() -> str:
    return __version__


def _parse(v) -> tuple:
    nums = []
    for part in str(v).strip().lstrip("vV").replace("-", ".").split("."):
        if part.isdigit():
            nums.append(int(part))
        else:
            break
    return tuple(nums) or (0,)


def is_newer(remote, local: str = __version__) -> bool:
    return _parse(remote) > _parse(local)


def can_self_update() -> bool:
    """Self-replacement only makes sense for the portable frozen build — not a
    Store/MSIX install (read-only, Store-managed) and not a source checkout."""
    return getattr(sys, "frozen", False) and not appdata.is_packaged()


def check(timeout: float = 8.0):
    """Return info about a NEWER GitHub release, else None.

    info = {version, url, asset_url, asset_name, notes}. Returns None when
    packaged (the Store updates those) or on any network/parse error.
    """
    if appdata.is_packaged():
        return None
    try:
        req = urllib.request.Request(API_LATEST, headers={
            "Accept": "application/vnd.github+json", "User-Agent": _UA,
        })
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.load(r)
    except Exception:
        return None
    tag = (data.get("tag_name") or "").strip()
    if not tag or not is_newer(tag):
        return None
    asset = None
    for a in data.get("assets", []):
        name = (a.get("name") or "").lower()
        if name.endswith((".zip", ".msix", ".exe")):
            asset = a
            break
    return {
        "version": tag.lstrip("vV"),
        "url": data.get("html_url") or RELEASES_PAGE,
        "asset_url": (asset or {}).get("browser_download_url"),
        "asset_name": (asset or {}).get("name"),
        "notes": (data.get("body") or "").strip()[:500],
    }


def background_check(callback, delay: float = 15.0):
    """Run check() off the UI thread after a short delay; call callback(info) if
    a newer release exists. Daemon thread, never raises."""
    def _run():
        try:
            time.sleep(delay)
        except Exception:
            pass
        info = check()
        if info:
            try:
                callback(info)
            except Exception:
                pass
    threading.Thread(target=_run, name="dk-update-check", daemon=True).start()


def _open_releases(info=None):
    import webbrowser
    try:
        webbrowser.open((info or {}).get("url") or RELEASES_PAGE)
    except Exception:
        pass


def _find_app_root(extracted: Path):
    """The folder that actually holds DesktopKaraoke.exe (zip root, or one down)."""
    if (extracted / "DesktopKaraoke.exe").exists():
        return extracted
    for child in extracted.iterdir():
        if child.is_dir() and (child / "DesktopKaraoke.exe").exists():
            return child
    return None


def stage_update(info, log=None) -> bool:
    """Portable build only: download the new onedir build (.zip), extract it, and
    launch a helper that waits for THIS process to exit, swaps the files in
    (keeping the lyric cache + settings), and relaunches.

    Returns True if the app should now exit to let the swap proceed. Returns
    False (and opens the Releases page) if it can't self-update here or anything
    goes wrong.
    """
    def _say(msg):
        if log:
            try:
                log(f"[update] {msg}")
            except Exception:
                pass

    url = (info or {}).get("asset_url") or ""
    if not (can_self_update() and url.lower().endswith(".zip")):
        _open_releases(info)
        return False

    import tempfile
    import zipfile
    import subprocess

    try:
        install_dir = Path(sys.executable).parent
        staging = Path(tempfile.gettempdir()) / "DesktopKaraoke_update"
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True, exist_ok=True)

        zip_path = staging / "update.zip"
        _say(f"downloading {info.get('asset_name') or url}")
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=120) as r, open(zip_path, "wb") as f:
            shutil.copyfileobj(r, f)

        newroot = staging / "new"
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(newroot)
        src = _find_app_root(newroot)
        if not src:
            raise RuntimeError("update package has no DesktopKaraoke.exe")

        helper = staging / "apply_update.cmd"
        helper.write_text(
            _HELPER.format(pid=os.getpid(), src=src, dst=install_dir,
                           exe=install_dir / "DesktopKaraoke.exe"),
            encoding="ascii",
        )
        _say(f"v{info.get('version')} staged; relaunching to apply")
        # Detached, no console window, so it survives our exit.
        subprocess.Popen(["cmd", "/c", str(helper)],
                         creationflags=0x08000000 | 0x00000008, close_fds=True)
        return True
    except Exception as e:
        _say(f"failed ({e!r}); opening Releases page")
        _open_releases(info)
        return False
