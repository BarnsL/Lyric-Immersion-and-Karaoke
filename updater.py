"""In-app updater.

Three distribution channels, three behaviours:

  • Installed from the **Microsoft Store** (MSIX) → the Store delivers updates
    automatically, so this module does nothing (``check()`` returns ``None``).
  • The **portable .exe** → checks GitHub Releases and, if a newer build is
    published as a ``.zip`` of the onedir folder, downloads it and launches a
    helper that swaps it in (keeping your lyric cache + settings) and relaunches.
  • Running **from source** → no self-update (use ``git pull``); ``check()`` still
    reports whether a newer release tag exists.

SECURITY (an updater downloads and runs code, so it's hardened):
  • **Verified HTTPS only, GitHub hosts only.** Every request uses a cert-verifying
    TLS context, and both the API and the asset URL must be ``https://`` on
    github.com / *.githubusercontent.com — a tampered API response can't redirect
    the download elsewhere or downgrade to HTTP.
  • **Integrity checked.** If the release publishes a SHA-256 (a ``<asset>.sha256``
    file or a 64-hex digest in the notes), the downloaded zip is verified against
    it and a mismatch **aborts** the update (fail-closed).
  • **Safe extraction.** The zip is extracted with path-traversal ("zip-slip")
    protection, capped in size, and must contain ``DesktopKaraoke.exe``.
  • Best-effort + offline-safe: any network/parse/verify error falls back to just
    opening the Releases page; nothing is applied unless it passed every check.

Only the standard library is used, so importing this at startup is cheap.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import ssl
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

import appdata
from version import __version__

REPO = "BarnsL/Desktop-Karaoke"
API_LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{REPO}/releases/latest"
_UA = f"DesktopKaraoke/{__version__}"
_MAX_ZIP = 2 * 1024 ** 3        # cap a downloaded update at 2 GB
_CTX = ssl.create_default_context()   # verifies TLS certs — never disabled

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


def _is_github_https(url: str) -> bool:
    """True only for an https:// URL on GitHub's own hosts. Everything the updater
    fetches is gated through this, so a manipulated release/API payload can't point
    the download at an attacker host or a plain-HTTP endpoint."""
    try:
        u = urllib.parse.urlparse(url or "")
    except Exception:
        return False
    if u.scheme != "https":
        return False
    host = (u.hostname or "").lower()
    return host in ("github.com", "api.github.com") \
        or host.endswith(".githubusercontent.com")


def _get(url: str, timeout: float = 8.0) -> bytes:
    """Fetch a small resource over verified HTTPS from GitHub (raises otherwise)."""
    if not _is_github_https(url):
        raise ValueError("refusing non-GitHub or non-HTTPS URL")
    req = urllib.request.Request(url, headers={
        "User-Agent": _UA, "Accept": "application/vnd.github+json",
    })
    with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as r:
        return r.read(8 * 1024 * 1024)        # JSON/checksum are tiny


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

    info = {version, url, asset_url, asset_name, sha_url, sha_body, notes}.
    Returns None when packaged (the Store updates those) or on any error.
    """
    if appdata.is_packaged():
        return None
    try:
        data = json.loads(_get(API_LATEST, timeout))
    except Exception:
        return None
    tag = (data.get("tag_name") or "").strip()
    if not tag or not is_newer(tag):
        return None

    assets = data.get("assets", []) or []
    zip_asset = None
    for a in assets:
        low = (a.get("name") or "").lower()
        if low.startswith("desktopkaraoke") and low.endswith(".zip") \
                and _is_github_https(a.get("browser_download_url", "")):
            zip_asset = a
            break

    sha_url = None
    if zip_asset:
        want = (zip_asset.get("name") or "").lower() + ".sha256"
        for a in assets:
            if (a.get("name") or "").lower() == want \
                    and _is_github_https(a.get("browser_download_url", "")):
                sha_url = a.get("browser_download_url")
                break
    m = re.search(r"\b[a-f0-9]{64}\b", (data.get("body") or "").lower())
    return {
        "version": tag.lstrip("vV"),
        "url": data.get("html_url") or RELEASES_PAGE,
        "asset_url": (zip_asset or {}).get("browser_download_url"),
        "asset_name": (zip_asset or {}).get("name"),
        "sha_url": sha_url,
        "sha_body": m.group(0) if m else None,
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


def _safe_extract(zf, dest: Path):
    """Extract guarding against zip-slip: every member must resolve INSIDE dest
    (no absolute paths, no '..' escaping the staging folder)."""
    base = dest.resolve()
    for name in zf.namelist():
        target = (base / name).resolve()
        if target != base and base not in target.parents:
            raise RuntimeError(f"unsafe path in update zip: {name!r}")
    zf.extractall(base)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(256 * 1024), b""):
            h.update(blk)
    return h.hexdigest()


def stage_update(info, log=None) -> bool:
    """Portable build only: download the new onedir build (.zip) over verified
    HTTPS, check its SHA-256 (if the release published one), extract it safely, and
    launch a helper that waits for THIS process to exit, swaps the files in
    (keeping the lyric cache + settings), and relaunches.

    Returns True if the app should now exit to let the swap proceed. Returns
    False (and opens the Releases page) if it can't self-update here or anything —
    download, host check, checksum, or extraction — fails.
    """
    def _say(msg):
        if log:
            try:
                log(f"[update] {msg}")
            except Exception:
                pass

    url = (info or {}).get("asset_url") or ""
    if not (can_self_update() and url.lower().endswith(".zip")
            and _is_github_https(url)):
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

        # ── download (verified TLS, GitHub host, size-capped) ──
        zip_path = staging / "update.zip"
        _say(f"downloading {info.get('asset_name') or url}")
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=120, context=_CTX) as r, \
                open(zip_path, "wb") as f:
            total = 0
            while True:
                chunk = r.read(256 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > _MAX_ZIP:
                    raise RuntimeError("update download exceeds size cap")
                f.write(chunk)

        # ── integrity: verify SHA-256 if the release published one (fail-closed) ──
        want = None
        if info.get("sha_url"):
            try:
                txt = _get(info["sha_url"]).decode("ascii", "ignore").lower()
                m = re.search(r"[a-f0-9]{64}", txt)
                want = m.group(0) if m else None
            except Exception:
                want = None
        want = want or info.get("sha_body")
        if want:
            if _sha256(zip_path) != want:
                raise RuntimeError("checksum mismatch — refusing update")
            _say("checksum verified")
        else:
            _say("no checksum published — relying on GitHub verified HTTPS")

        # ── extract (zip-slip safe) + sanity check ──
        newroot = staging / "new"
        with zipfile.ZipFile(zip_path) as z:
            _safe_extract(z, newroot)
        src = _find_app_root(newroot)
        if not src:
            raise RuntimeError("update package has no DesktopKaraoke.exe")

        helper = staging / "apply_update.cmd"
        helper.write_text(
            _HELPER.format(pid=os.getpid(), src=src, dst=install_dir,
                           exe=install_dir / "DesktopKaraoke.exe"),
            encoding="ascii",
        )
        _say(f"v{info.get('version')} verified + staged; relaunching to apply")
        # Detached, no console window, so it survives our exit.
        subprocess.Popen(["cmd", "/c", str(helper)],
                         creationflags=0x08000000 | 0x00000008, close_fds=True)
        return True
    except Exception as e:
        _say(f"failed ({e!r}); opening Releases page")
        _open_releases(info)
        return False
