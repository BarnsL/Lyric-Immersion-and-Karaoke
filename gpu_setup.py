"""Optional GPU acceleration — fetched on demand, never bundled.

Transcription (sync-by-listening and last-resort lyric *generation*) runs on the
CPU by default and that is fine: a 16-second clip transcribes in ~2 seconds, and
generation is a rare last-resort path. GPU (CUDA) makes it a couple of seconds
faster per chunk — a marginal win that costs **~1.9 GB** of NVIDIA cuBLAS/cuDNN
libraries. Bundling that into everyone's install (most machines can't even use it)
would be absurd, so it is **opt-in and downloaded on demand** for the user who
explicitly wants it and has an NVIDIA GPU.

The libraries are the SAME official NVIDIA wheels that ``pip install faster-whisper
[cuda]`` would pull — fetched straight from **PyPI** (we host nothing) at the exact
versions that match the bundled CTranslate2, then unpacked next to the app where
``align._ensure_deps_path`` already looks (``<data_dir>/deps/nvidia/...``). On the
next transcription, ``align`` finds the cuBLAS/cuDNN DLLs and uses CUDA; with no
GPU or no libraries it silently stays on the CPU.

SECURITY (this downloads native DLLs, so it is hardened like the updater):
  • **Verified HTTPS, PyPI hosts only** — the metadata comes from ``pypi.org`` and
    the wheels from ``files.pythonhosted.org`` over a cert-verifying TLS context;
    any other host/scheme is refused, so a tampered response can't redirect the
    download elsewhere.
  • **Integrity checked, fail-closed** — every wheel is verified against the
    SHA-256 that PyPI publishes in its own JSON metadata; a mismatch aborts and
    nothing is installed.
  • **Safe extraction** — wheels are unzipped with zip-slip protection and a size
    cap, and only the ``nvidia/`` payload is written.
  • A ``.gpu_ready`` marker is written ONLY after every wheel verified + extracted,
    so a half-finished download never looks installed.

Standard library only.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import ssl
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

import appdata

# Pinned to the versions that ship with the bundled CTranslate2 (4.8.0) — the same
# set vendored for local GPU builds. cuBLAS + cuDNN are what CTranslate2 loads on
# CUDA; nvRTC backs runtime kernel compilation.
_PKGS = (
    ("nvidia-cublas-cu12", "12.9.2.10"),
    ("nvidia-cudnn-cu12", "9.23.2.1"),
    ("nvidia-cuda-nvrtc-cu12", "12.9.86"),
)
_MARKER = ".gpu_ready"
_SENTINEL = "nvidia/cublas/bin/cublas64_12.dll"   # the keystone DLL CTranslate2 needs
_MAX_WHEEL = 2 * 1024 ** 3        # cap any single wheel download at 2 GB
_CTX = ssl.create_default_context()   # verifies TLS certs — never disabled
_UA = "DesktopKaraoke-gpu-setup"

# Rough download footprint, shown to the user before they commit to it.
APPROX_MB = 1500


_gpu_present = None        # memoized — GPU presence can't change during a session


def nvidia_gpu_present() -> bool:
    """True if an NVIDIA GPU + driver is installed — the driver always provides
    ``nvcuda.dll``, so loading it is a cheap, dependency-free probe. Memoized
    because the tray re-checks it on every menu render. (Having the GPU doesn't
    mean the CUDA *libraries* are here yet — that's what the download provides;
    see ``gpu_ready``.)"""
    global _gpu_present
    if _gpu_present is None:
        try:
            import ctypes
            ctypes.WinDLL("nvcuda.dll")
            _gpu_present = True
        except Exception:
            _gpu_present = False
    return _gpu_present


def _deps_dir() -> Path:
    """The writable ``deps`` folder next to the app, where ``align`` looks for
    vendored libraries. Created if missing."""
    d = appdata.data_dir() / "deps"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return d


def gpu_ready(deps: Path | None = None) -> bool:
    """True once the CUDA libraries are fully installed (marker present and the
    keystone cuBLAS DLL on disk)."""
    deps = deps or _deps_dir()
    return (deps / _MARKER).exists() and (deps / _SENTINEL).exists()


def status() -> str:
    """One-word state for the tray label: 'ready', 'available' (GPU present, not
    yet downloaded), or 'none' (no NVIDIA GPU)."""
    if gpu_ready():
        return "ready"
    return "available" if nvidia_gpu_present() else "none"


# ── Game-aware Whisper device selection ──────────────────────────────────────
# Whisper (faster-whisper / ctranslate2) is the only GPU work the app does, and a
# transcription briefly pegs the card — which can hitch a game. So during a
# FULLSCREEN game we keep off the game's GPU: an idle SECOND NVIDIA GPU if one
# exists, else the CPU. (ctranslate2 is CUDA-or-CPU only — an integrated GPU can't run
# it, so the integrated GPU can't serve as the spare.)
_game_cache = {"t": -1e9, "on": False}
_cuda_count = None


def game_active(ttl: float = 3.0) -> bool:
    """True if an exclusive-fullscreen game is running, via the Windows shell's
    user-notification state (the same signal Windows uses to silence toasts during
    games). One cheap syscall, memoized for `ttl` s. Borderless-windowed games that
    don't take exclusive fullscreen may not trip this."""
    import time
    now = time.monotonic()
    if now - _game_cache["t"] < ttl:
        return _game_cache["on"]
    on = False
    try:
        import ctypes
        st = ctypes.c_int(0)
        # SHQueryUserNotificationState → 3 == QUNS_RUNNING_D3D_FULL_SCREEN
        if ctypes.windll.shell32.SHQueryUserNotificationState(ctypes.byref(st)) == 0:
            on = (st.value == 3)
    except Exception:
        on = False
    _game_cache.update(t=now, on=on)
    return on


def cuda_device_count() -> int:
    """Number of CUDA GPUs ctranslate2 can use (0 if none). Memoized for the session."""
    global _cuda_count
    if _cuda_count is None:
        try:
            import ctranslate2
            _cuda_count = int(ctranslate2.get_cuda_device_count())
        except Exception:
            _cuda_count = 0
    return _cuda_count


def _gpu_utils() -> dict:
    """{cuda_index: gpu_util_%} via NVML (nvml.dll ships with the NVIDIA driver — no
    subprocess, no window). {} if NVML can't be loaded or queried."""
    try:
        import ctypes
        nvml = ctypes.CDLL("nvml.dll")
        if nvml.nvmlInit_v2() != 0:
            return {}
    except Exception:
        return {}
    out = {}
    try:
        import ctypes
        cnt = ctypes.c_uint(0)
        if nvml.nvmlDeviceGetCount_v2(ctypes.byref(cnt)) != 0:
            return out

        class _U(ctypes.Structure):
            _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]

        for i in range(cnt.value):
            h = ctypes.c_void_p()
            if nvml.nvmlDeviceGetHandleByIndex_v2(i, ctypes.byref(h)) != 0:
                continue
            u = _U()
            if nvml.nvmlDeviceGetUtilizationRates(h, ctypes.byref(u)) == 0:
                out[i] = int(u.gpu)
    except Exception:
        pass
    finally:
        try:
            nvml.nvmlShutdown()
        except Exception:
            pass
    return out


def pick_inference_device(avoid_when_gaming: bool = True,
                          solo_override: bool = False):
    """Where Whisper should run RIGHT NOW → ``(device, index, reason)``.

    POLICY (TICKET-103, user request): GPU acceleration is opt-in for
    multi-GPU machines only. On a single-GPU machine we always stay on CPU
    so the lone card can never fight whatever else the user is doing on it
    (game, video decode, browser hardware-accel, etc.); Whisper does fine
    on CPU and the user explicitly asked for this safety floor. The user
    can flip ``solo_override`` (tune knob ``gpu_solo_override``) to force
    GPU use on a single-GPU machine when they want the speed.

    Picks the LEAST-utilized CUDA GPU instead of assuming the game is always on
    GPU 0 (TICKET-080 — on this rig the the external GPU is cuda:1 and gets the game,
    so the old 'skip 0' rule put Whisper right on the game's card). When a
    fullscreen game is active, any GPU at >=30% util is treated as the game's
    and skipped; if everything is busy we fall to CPU so the transcribe can't
    hitch the game. When not gaming, prefers the idlest GPU but stays on cuda:0
    when the difference is small so the model cache stays warm there."""
    n = cuda_device_count()
    if n <= 0:
        return ("cpu", 0, "no CUDA GPU")
    if n == 1:
        # TICKET-103: single-GPU → CPU unless the user explicitly opted in
        # via gpu_solo_override. Even with the override, the gaming guard
        # still kicks in (no point fighting the game for the only card).
        if not solo_override:
            return ("cpu", 0, "single GPU → CPU (policy: solo GPU stays free)")
        if avoid_when_gaming and game_active():
            return ("cpu", 0, "game: single GPU → CPU (override on, but gaming)")
        return ("cuda", 0, "single GPU (override on)")
    utils = _gpu_utils()                              # {idx: util%} — missing = idle
    gaming = avoid_when_gaming and game_active()
    BUSY = 30
    ranked = sorted(range(n), key=lambda i: utils.get(i, 0))
    if gaming:
        free = [i for i in ranked if utils.get(i, 0) < BUSY]
        if not free:
            return ("cpu", 0, "game: all GPUs busy → CPU")
        i = free[0]
        return ("cuda", i, f"game: idlest GPU {i} ({utils.get(i, 0)}%)")
    best = ranked[0]
    if utils.get(best, 0) + 5 >= utils.get(0, 0):
        return ("cuda", 0, "default")                 # tied or cuda:0 idle enough
    return ("cuda", best,
            f"idle GPU {best} ({utils.get(best, 0)}% vs cuda:0 {utils.get(0, 0)}%)")


def _is_pypi_https(url: str) -> bool:
    """True only for an https:// URL on PyPI's own hosts. Every fetch is gated
    through this so a manipulated metadata response can't point the download at an
    attacker host or downgrade to plain HTTP."""
    try:
        u = urllib.parse.urlparse(url or "")
    except Exception:
        return False
    if u.scheme != "https":
        return False
    host = (u.hostname or "").lower()
    return host in ("pypi.org", "files.pythonhosted.org")


def _get(url: str, timeout: float = 15.0) -> bytes:
    if not _is_pypi_https(url):
        raise ValueError("refusing non-PyPI or non-HTTPS URL")
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as r:
        return r.read(4 * 1024 * 1024)        # metadata JSON is small


def _wheel_for(name: str, ver: str):
    """Resolve a package@version to its Windows wheel (url, sha256) via PyPI's
    JSON metadata — picking the cp-agnostic ``win_amd64`` wheel NVIDIA publishes."""
    data = json.loads(_get(f"https://pypi.org/pypi/{name}/{ver}/json"))
    best = None
    for f in data.get("urls", []) or []:
        fn = (f.get("filename") or "").lower()
        url = f.get("url") or ""
        if fn.endswith("win_amd64.whl") and _is_pypi_https(url):
            sha = (f.get("digests") or {}).get("sha256")
            if sha:
                best = (url, sha.lower())
                break
    if not best:
        raise RuntimeError(f"no verified win_amd64 wheel for {name} {ver}")
    return best


def _safe_members(zf: zipfile.ZipFile, base: Path):
    """Yield the members under ``nvidia/`` whose paths resolve INSIDE base
    (zip-slip guard); skip wheel metadata."""
    root = base.resolve()
    for name in zf.namelist():
        if not name.startswith("nvidia/") or name.endswith("/"):
            continue
        target = (root / name).resolve()
        if target != root and root not in target.parents:
            raise RuntimeError(f"unsafe path in wheel: {name!r}")
        yield name


def _download_wheel(url: str, sha256: str, dest: Path, on_bytes=None) -> Path:
    """Stream a wheel to disk over verified HTTPS (size-capped) and verify its
    SHA-256 against PyPI's digest — fail-closed (a mismatch raises and the partial
    file is removed)."""
    if not _is_pypi_https(url):
        raise ValueError("refusing non-PyPI wheel URL")
    h = hashlib.sha256()
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    total = 0
    with urllib.request.urlopen(req, timeout=120, context=_CTX) as r, \
            open(dest, "wb") as f:
        size = int(r.headers.get("Content-Length") or 0)
        while True:
            chunk = r.read(512 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > _MAX_WHEEL:
                raise RuntimeError("wheel exceeds size cap")
            h.update(chunk)
            f.write(chunk)
            if on_bytes:
                try:
                    on_bytes(total, size)
                except Exception:
                    pass
    if h.hexdigest() != sha256:
        try:
            dest.unlink()
        except Exception:
            pass
        raise RuntimeError("wheel checksum mismatch — refusing GPU libraries")
    return dest


def download_gpu_libs(progress=None, log=None) -> bool:
    """Download + install the CUDA libraries on demand. Returns True on full
    success (GPU then usable on the next transcription), False on any failure —
    in which case nothing is marked ready and the app stays on the CPU.

    ``progress(pkg_index, pkg_total, name, done_bytes, total_bytes)`` is called as
    it streams; ``log(msg)`` receives human-readable steps. Both optional."""
    def _say(m):
        if log:
            try:
                log(f"[gpu] {m}")
            except Exception:
                pass

    if not nvidia_gpu_present():
        _say("no NVIDIA GPU detected — GPU acceleration not applicable")
        return False
    if gpu_ready():
        _say("CUDA libraries already installed")
        return True

    import tempfile
    deps = _deps_dir()
    staging = Path(tempfile.mkdtemp(prefix="dk_gpu_"))
    try:
        for i, (name, ver) in enumerate(_PKGS):
            _say(f"resolving {name} {ver}")
            url, sha = _wheel_for(name, ver)
            whl = staging / f"{name}.whl"
            _say(f"downloading {name} ({i + 1}/{len(_PKGS)})")
            _download_wheel(
                url, sha, whl,
                on_bytes=lambda d, t, i=i, n=name:
                    progress and progress(i, len(_PKGS), n, d, t))
            _say(f"verifying + extracting {name}")
            with zipfile.ZipFile(whl) as z:
                for member in _safe_members(z, deps):
                    out = deps / member
                    out.parent.mkdir(parents=True, exist_ok=True)
                    with z.open(member) as src, open(out, "wb") as dst:
                        shutil.copyfileobj(src, dst, 1024 * 1024)
            try:
                whl.unlink()
            except Exception:
                pass
        if not (deps / _SENTINEL).exists():
            raise RuntimeError("install incomplete — keystone DLL missing")
        (deps / _MARKER).write_text("ok", encoding="ascii")
        _say("CUDA libraries installed — GPU will be used on the next song")
        return True
    except Exception as e:
        _say(f"failed ({e!r}); staying on CPU")
        return False
    finally:
        shutil.rmtree(staging, ignore_errors=True)


if __name__ == "__main__":      # quick manual check
    print("NVIDIA GPU present:", nvidia_gpu_present())
    print("GPU libraries ready:", gpu_ready(), "→ status:", status())
