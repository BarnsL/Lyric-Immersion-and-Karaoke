r"""TICKET-100 - Discord Rich Presence READER (Spotify Listening).

Background
==========
Discord exposes a local IPC pipe on Windows at \\.\pipe\discord-ipc-N
(N = 0..9, suffix increments when multiple clients run side-by-side,
e.g. Stable + Canary). Over that pipe we can issue GET_ACTIVITY to read
the LOCAL user's own activity list, including the well-known Spotify
"Listening" activity (type=2, name='Spotify', details=track, state=artist).

Scope (per TICKET-100 design)
=============================
- SELF ONLY, READ-ONLY (no GET_RELATIONSHIPS, no SET_ACTIVITY so
  Discord never shows "Playing Lyric Immersion and Karaoke").
- Hardcoded placeholder client_id for the handshake. The local IPC
  accepts unregistered ids for read-only GET_ACTIVITY (undocumented
  but stable). If Discord ever tightens this we'd need to register
  a real Discord application (see risks in the ticket).
- Spotify (type=2, name='Spotify') is the safe contract. Other type=2
  apps (Apple Music, Tidal, YT Music desktop) also map cleanly. Game
  Rich Presence (type=0) is NEVER mapped, out of scope (TICKET-101
  would gate that behind a per-game allowlist).
- Stdlib + ctypes only. pywin32 is OPTIONAL (used when present, else
  we fall back to CreateFileW / ReadFile / WriteFile via ctypes).
  This keeps cold-start cost zero when the toggle is off.

Architecture (v1.0.89, BUG-2/5/6 fix)
=====================================
A single long-lived daemon `DiscordWatcher` thread owns the IPC pipe.
It opens the pipe ONCE on startup (with exponential reconnect backoff),
issues GET_ACTIVITY on its own poll cadence, and writes the parsed track
into a lock-guarded slot. The public `get_listening_track()` is a thin,
non-blocking reader that returns a COPY of the slot in well under 1 ms;
it never spawns threads, never .join()s, never touches the pipe.

This was a redesign of the pre-v1.0.89 per-call worker thread, which
held `_lock` across blocking _send/_recv_one and was joined from the Tk
thread for up to `timeout_s` seconds. That stalled the render loop and
serialized cleanup paths against the in-flight worker.

Hard contract (the bits main.py cares about)
============================================
- available() -> bool, fast (< 50 ms), no allocations on failure.
- start_watcher() / stop_watcher() - lifecycle for the daemon thread.
- get_listening_track(timeout_s=0.5) -> dict | None.
  Returns {'title', 'artist', 'source', 'started_at'} or None.
  NON-BLOCKING: reads a lock-guarded slot and returns immediately.
  `timeout_s` is accepted for backward compatibility but ignored.
- All exceptions swallowed; failure mode is silent None.
"""

from __future__ import annotations

import json
import struct
import threading
import time
import uuid
from typing import Any, Optional


# Placeholder Discord application id. The local pipe accepts unregistered ids
# for read-only GET_ACTIVITY; SET_ACTIVITY is the only opcode that's strictly
# tied to a registered app + icons (which we explicitly do NOT call).
_CLIENT_ID = "1234567890123456789"


# ── Optional pywin32 import (preferred when available) ───────────────────────
# Pure stdlib ctypes works too (see _ct_*); pywin32 paths just give us cleaner
# named-pipe handling. Both are import-safe even when the other is missing.
try:
    import win32file  # type: ignore[import-not-found]
    import win32pipe  # type: ignore[import-not-found]  # noqa: F401  (kept for completeness)
    import pywintypes  # type: ignore[import-not-found]
    _HAS_PYWIN32 = True
except Exception:
    _HAS_PYWIN32 = False


# ── Low-level Win32 helpers (ctypes fallback) ────────────────────────────────
# We talk to the pipe with CreateFileW / WriteFile / ReadFile via kernel32.
# This avoids adding pywin32 to the install footprint (the existing app uses
# pystray + winsdk; pywin32 would be a NEW dependency just for IPC).

def _ct_load():
    """Lazy ctypes setup; returns (kernel32, structures). Import-safe on
    non-Windows: returns (None, None) if ctypes.windll isn't available."""
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return None, None
    try:
        k = ctypes.windll.kernel32
    except Exception:
        return None, None

    # CreateFileW signature (DWORD bitmasks via wintypes for 32/64-bit safety):
    k.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    k.CreateFileW.restype = wintypes.HANDLE
    k.ReadFile.argtypes = [
        wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
    ]
    k.ReadFile.restype = wintypes.BOOL
    k.WriteFile.argtypes = [
        wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
    ]
    k.WriteFile.restype = wintypes.BOOL
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    k.CloseHandle.restype = wintypes.BOOL
    return k, (ctypes, wintypes)


def _ct_open(pipe: str):
    """Open a Windows named pipe for read+write. Returns the HANDLE or None.

    The pipe is opened in BLOCKING mode (Discord's pipes use byte streams
    and the handshake is request/response). The hard timeout is enforced
    at the watcher loop level by virtue of running on its own thread."""
    k, mods = _ct_load()
    if k is None:
        return None
    ctypes, wintypes = mods
    GENERIC_READ = 0x80000000
    GENERIC_WRITE = 0x40000000
    OPEN_EXISTING = 3
    INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value
    h = k.CreateFileW(pipe, GENERIC_READ | GENERIC_WRITE, 0, None,
                      OPEN_EXISTING, 0, None)
    if not h or h == INVALID_HANDLE_VALUE:
        return None
    return h


def _ct_write(h, data: bytes) -> bool:
    k, mods = _ct_load()
    if k is None:
        return False
    ctypes, wintypes = mods
    written = wintypes.DWORD(0)
    ok = k.WriteFile(h, data, len(data), ctypes.byref(written), None)
    return bool(ok) and written.value == len(data)


def _ct_read(h, n: int) -> Optional[bytes]:
    k, mods = _ct_load()
    if k is None:
        return None
    ctypes, wintypes = mods
    buf = (ctypes.c_char * n)()
    got = wintypes.DWORD(0)
    ok = k.ReadFile(h, buf, n, ctypes.byref(got), None)
    if not ok or got.value == 0:
        return None
    return bytes(buf[:got.value])


def _ct_close(h) -> None:
    try:
        k, _ = _ct_load()
        if k and h:
            k.CloseHandle(h)
    except Exception:
        pass


# ── Activity selection / mapping (pure, no IPC) ─────────────────────────────

def _pick_activity(activities: list) -> Optional[dict]:
    """Deterministic selection across multiple LISTENING activities (e.g.
    Spotify on phone AND a Discord music bot in a voice channel):
    Spotify first, then any other type==2 with non-empty details+state,
    else None."""
    # Stable preference: Spotify by name match.
    for a in activities:
        try:
            if int(a.get("type", -1)) == 2 and (a.get("name") or "").lower() == "spotify":
                if (a.get("details") or "").strip() and (a.get("state") or "").strip():
                    return a
        except Exception:
            continue
    # Any other LISTENING activity (Apple Music, Tidal, YT Music desktop)
    # with both fields populated.
    for a in activities:
        try:
            if int(a.get("type", -1)) == 2:
                if (a.get("details") or "").strip() and (a.get("state") or "").strip():
                    return a
        except Exception:
            continue
    return None


# Discord's Spotify activity persists for ~30 s after PAUSE; we treat it as
# stale if the timestamps.start is older than this many seconds (per the
# ticket risk note about stale entries on a paused Spotify session).
_STALE_AFTER_S = 600.0


def _track_from_activity(act: dict) -> Optional[dict]:
    """Map an Activity dict (from Discord) to the SMTC-shaped lite tuple
    the karaoke source-merge wants. Returns None for stale / malformed
    entries. type==0 (PLAYING, game RP) is REJECTED here as a defensive
    second line (TODO: TICKET-101 would gate that behind discord_game_rpc)."""
    # TODO TICKET-101: per-game Rich Presence parsing for rhythm games would
    # plug in HERE behind a separate 'discord_game_rpc' tune knob (default 0)
    # and a per-application_id allowlist.
    try:
        atype = int(act.get("type", -1))
    except Exception:
        return None
    if atype != 2:
        return None
    title = (act.get("details") or "").strip()
    artist = (act.get("state") or "").strip()
    if not title or not artist:
        return None
    # Stale-check using timestamps.start (Spotify's pause-persistence window).
    # Discord serves timestamps in MILLISECONDS-since-epoch.
    started_at: Optional[float] = None
    ts = act.get("timestamps") or {}
    raw_start = ts.get("start")
    if raw_start:
        try:
            started_at = float(raw_start) / 1000.0
            if time.time() - started_at > _STALE_AFTER_S:
                return None
        except Exception:
            started_at = None
    name = (act.get("name") or "").lower()
    source = "spotify" if name == "spotify" else "other"
    return {
        "title": title,
        "artist": artist,
        "source": source,
        "started_at": started_at,
    }


# ── Long-lived watcher thread ───────────────────────────────────────────────

class DiscordWatcher:
    """A daemon thread that owns ONE Discord IPC connection across its
    lifetime, polls GET_ACTIVITY on a fixed cadence, and publishes the
    latest mapped track into a lock-guarded slot.

    Public API is intentionally tiny:
      start()  - idempotent; spins up the daemon if not already running.
      stop()   - idempotent; signals the thread and closes the pipe.
      get()    - returns a COPY of the latest track dict, or None.

    The Tk thread NEVER calls .join() or blocks on the worker. The slot is
    guarded by a `threading.Lock` whose critical section is two attribute
    reads + a shallow dict copy (sub-millisecond)."""

    # Loop cadence safety bounds (caller can tune via set_poll(); 0.5..30s).
    _POLL_MIN_S = 0.5
    _POLL_MAX_S = 30.0
    # Reconnect backoff: start short, cap at one minute.
    _BACKOFF_START_S = 5.0
    _BACKOFF_MAX_S = 60.0

    def __init__(self, poll_s: float = 5.0) -> None:
        self._poll_s = self._clamp_poll(poll_s)
        # Slot: latest mapped track or None. Protected by `_slot_lock`.
        self._slot_lock = threading.Lock()
        self._slot: Optional[dict] = None
        self._slot_updated_t: float = 0.0
        # Pipe state lives on the worker thread; never touched from outside.
        self._handle: Any = None
        self._handle_kind: str = ""
        self._pipe_name: str = ""
        self._handshaked: bool = False
        self._backoff_s: float = self._BACKOFF_START_S
        self._next_connect_t: float = 0.0
        self._warned_missing: bool = False
        # Lifecycle.
        self._stop_evt = threading.Event()
        # Bounded wait primitive so stop() wakes the worker promptly even if
        # it's mid-sleep between polls.
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._start_lock = threading.Lock()

    # ── lifecycle ────────────────────────────────────────────────────────────

    @classmethod
    def _clamp_poll(cls, v: float) -> float:
        try:
            v = float(v)
        except Exception:
            v = 5.0
        if v < cls._POLL_MIN_S:
            v = cls._POLL_MIN_S
        if v > cls._POLL_MAX_S:
            v = cls._POLL_MAX_S
        return v

    def set_poll(self, poll_s: float) -> None:
        """Live-tune the loop cadence. Wakes the worker so a long sleep
        between polls doesn't delay the new rate from taking effect."""
        self._poll_s = self._clamp_poll(poll_s)
        self._wake.set()

    def start(self) -> None:
        """Idempotent: spawn the daemon thread if not already alive."""
        with self._start_lock:
            t = self._thread
            if t is not None and t.is_alive():
                return
            self._stop_evt.clear()
            self._wake.clear()
            self._thread = threading.Thread(
                target=self._run, name="discord-rpc-watcher", daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        """Idempotent: signal the worker to exit and close the pipe. Does NOT
        join the thread (Tk thread must not block). The daemon will die on
        its own at the next loop iteration; the cached pipe handle is closed
        synchronously from here so a fresh start() reconnects cleanly."""
        self._stop_evt.set()
        self._wake.set()
        # Force-close the pipe handle from this thread. The worker thread
        # may be mid-_recv_one(); closing the handle from underneath it makes
        # the ReadFile return promptly with an error, and the loop notices
        # `_stop_evt` and exits. We don't grab a lock around _disconnect
        # because the only other accessor is the worker, which only writes
        # the slot (not the handle) under its own lock.
        try:
            self._disconnect()
        except Exception:
            pass
        # Clear the slot so a downstream consumer doesn't keep seeing a stale
        # track after the user toggled the feature off.
        with self._slot_lock:
            self._slot = None
            self._slot_updated_t = 0.0

    def is_running(self) -> bool:
        t = self._thread
        return bool(t and t.is_alive() and not self._stop_evt.is_set())

    # ── public read path (called by Tk thread) ───────────────────────────────

    def get(self) -> Optional[dict]:
        """Return a COPY of the latest mapped track dict, or None. Never
        blocks more than a microsecond (one lock acquire + shallow copy)."""
        with self._slot_lock:
            s = self._slot
            if s is None:
                return None
            # Shallow copy is enough: values are all primitives.
            return dict(s)

    def slot_age(self) -> float:
        """Seconds since the slot was last written (for diagnostics)."""
        with self._slot_lock:
            t = self._slot_updated_t
        return (time.time() - t) if t else float("inf")

    # ── worker loop (runs on the daemon thread only) ─────────────────────────

    def _run(self) -> None:
        """Connect-then-poll loop. On any IPC error we drop the handle and
        let the next iteration reconnect (with backoff). We sleep on `_wake`
        so stop() and set_poll() take effect immediately."""
        while not self._stop_evt.is_set():
            try:
                self._tick_once()
            except Exception:
                # Defensive: never let the daemon die on an unhandled error.
                # Drop any open handle so the next pass starts clean.
                try:
                    self._disconnect()
                except Exception:
                    pass
            # Wait up to `_poll_s` for either a stop or a poll-cadence change.
            # `_wake` is a one-shot signal; clear it after the wait so the
            # next iteration sleeps normally.
            self._wake.wait(timeout=self._poll_s)
            self._wake.clear()

    def _tick_once(self) -> None:
        """One iteration of the worker loop: ensure connection, fetch
        activity, map it, publish to the slot."""
        # Honour backoff: if we recently failed to connect, sit out this tick.
        if self._handle is None:
            now = time.time()
            if now < self._next_connect_t:
                return
            if not self._connect():
                return
        # We're connected (or believe we are). Fetch activity.
        act = self._do_get_activity()
        if act is None:
            # Could be "no listening activity" (valid) or "IPC errored mid-call"
            # (in which case _do_get_activity already disconnected us).
            # Either way, clear the slot so consumers don't see stale tracks
            # after the user stops listening.
            self._publish(None)
            return
        track = _track_from_activity(act)
        # `track` can be None when the activity exists but is stale or
        # malformed; publish either way so the downstream consumer reflects
        # the current reality.
        self._publish(track)

    def _publish(self, track: Optional[dict]) -> None:
        with self._slot_lock:
            self._slot = track
            self._slot_updated_t = time.time()

    # ── IPC primitives (worker-thread only) ──────────────────────────────────

    def _send(self, opcode: int, payload: dict) -> bool:
        """Send one Discord IPC frame: <opcode:le32><length:le32><json bytes>."""
        if self._handle is None:
            return False
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        header = struct.pack("<II", opcode, len(body))
        frame = header + body
        try:
            if self._handle_kind == "pywin32":
                err, _written = win32file.WriteFile(self._handle, frame)  # type: ignore[union-attr]
                return err == 0
            return _ct_write(self._handle, frame)
        except Exception:
            return False

    def _recv_one(self) -> Optional[tuple[int, dict]]:
        """Read ONE frame: returns (opcode, parsed_json_dict) or None on
        error. BLOCKING on the worker thread - this is fine because the Tk
        thread is fully decoupled (no .join())."""
        if self._handle is None:
            return None
        try:
            if self._handle_kind == "pywin32":
                err, hdr = win32file.ReadFile(self._handle, 8)  # type: ignore[union-attr]
                if err != 0 or len(hdr) != 8:
                    return None
            else:
                hdr = _ct_read(self._handle, 8)
                if not hdr or len(hdr) != 8:
                    return None
            opcode, length = struct.unpack("<II", hdr)
            if length <= 0 or length > (1 << 20):     # 1 MiB sanity cap
                return None
            if self._handle_kind == "pywin32":
                err, body = win32file.ReadFile(self._handle, length)  # type: ignore[union-attr]
                if err != 0 or len(body) != length:
                    return None
            else:
                # ReadFile can return less than requested on a single call;
                # loop until we have the full body or hit EOF.
                body = b""
                remaining = length
                while remaining > 0:
                    chunk = _ct_read(self._handle, remaining)
                    if not chunk:
                        return None
                    body += chunk
                    remaining -= len(chunk)
            try:
                return opcode, json.loads(body.decode("utf-8", errors="replace"))
            except Exception:
                return None
        except Exception:
            return None

    def _disconnect(self) -> None:
        """Drop the cached handle (idempotent, swallows errors). Safe to call
        from stop() on a non-worker thread: the only racing reader is the
        worker, which checks `self._handle is None` at every IPC boundary."""
        h = self._handle
        kind = self._handle_kind
        self._handle = None
        self._handle_kind = ""
        self._pipe_name = ""
        self._handshaked = False
        if h is None:
            return
        try:
            if kind == "pywin32":
                win32file.CloseHandle(h)  # type: ignore[union-attr]
            else:
                _ct_close(h)
        except Exception:
            pass

    def _connect(self) -> bool:
        """Try to open the first available Discord IPC pipe (-0 through -9)
        AND complete the v1 handshake. Returns True on success. Uses an
        exponential backoff so a missing Discord client doesn't burn cycles
        every poll. ONLY called from the worker thread (so the 10 synchronous
        CreateFileW probes that BUG-6 flagged no longer touch the Tk thread)."""
        now = time.time()
        if now < self._next_connect_t:
            return False                            # backoff in effect
        for i in range(10):
            if self._stop_evt.is_set():
                return False
            pipe = r"\\.\pipe\discord-ipc-%d" % i
            h = None
            kind = ""
            # Prefer pywin32 when available; cleaner error semantics than
            # ctypes for named-pipe quirks (e.g. ERROR_PIPE_BUSY).
            if _HAS_PYWIN32:
                try:
                    h = win32file.CreateFile(  # type: ignore[union-attr]
                        pipe,
                        win32file.GENERIC_READ | win32file.GENERIC_WRITE,  # type: ignore[union-attr]
                        0, None, win32file.OPEN_EXISTING, 0, None)  # type: ignore[union-attr]
                    kind = "pywin32"
                except pywintypes.error:  # type: ignore[union-attr]
                    h = None
                except Exception:
                    h = None
            if h is None:
                h = _ct_open(pipe)
                if h is not None:
                    kind = "ctypes"
            if h is None:
                continue
            # Got a handle; install it and try the handshake.
            self._handle = h
            self._handle_kind = kind
            self._pipe_name = pipe
            if self._handshake():
                self._backoff_s = self._BACKOFF_START_S   # reset on success
                self._warned_missing = False
                self._handshaked = True
                return True
            # Handshake failed: drop this handle and try the next pipe.
            self._disconnect()
        # No pipe present: schedule the next attempt and (once) note it.
        self._next_connect_t = now + self._backoff_s
        self._backoff_s = min(self._BACKOFF_MAX_S, self._backoff_s * 2.0)
        if not self._warned_missing:
            self._warned_missing = True
            try:
                import logging
                logging.getLogger().info(
                    "discord_rpc: Discord IPC pipe not present, will retry "
                    "(backoff up to 60s)")
            except Exception:
                pass
        return False

    def _handshake(self) -> bool:
        """Send opcode=0 {v:1, client_id:..} and read the READY frame."""
        if not self._send(0, {"v": 1, "client_id": _CLIENT_ID}):
            return False
        frame = self._recv_one()
        if not frame:
            return False
        opcode, _data = frame
        # Frame 1 is DISPATCH / READY for a successful handshake; some Discord
        # builds return CLOSE (opcode=2) when the id is rejected. Accept
        # opcode=1 broadly because the exact payload schema has shifted
        # across builds.
        return opcode == 1

    def _do_get_activity(self) -> Optional[dict]:
        """Ensure we're connected, send GET_ACTIVITY, parse the response.
        Returns the chosen activity dict or None. Drops the cached pipe on
        any IPC error so the next iteration reconnects cleanly."""
        if self._handle is None and not self._connect():
            return None
        nonce = uuid.uuid4().hex
        # Opcode 1 = FRAME. cmd=GET_ACTIVITY is undocumented but supported by
        # the local pipe; the official 'GET_SELECTED_VOICE_CHANNEL'-shaped
        # commands won't return activities. We pull the local user's id with
        # GET_CURRENT_USER first when we don't already have it.
        if not self._send(1, {"cmd": "GET_CURRENT_USER", "nonce": nonce,
                              "args": {}}):
            self._disconnect()
            return None
        frame = self._recv_one()
        if not frame:
            self._disconnect()
            return None
        _, data = frame
        user_id = None
        try:
            user_id = ((data.get("data") or {}).get("id")) or None
        except Exception:
            user_id = None
        if not user_id:
            return None
        nonce = uuid.uuid4().hex
        if not self._send(1, {"cmd": "GET_ACTIVITY", "nonce": nonce,
                              "args": {"user_id": user_id}}):
            self._disconnect()
            return None
        # GET_ACTIVITY isn't on every Discord build. If the server replies
        # with an error frame, fall back to the per-channel-fetch later;
        # for now, treat any error as "no track" and bail cleanly.
        frame = self._recv_one()
        if not frame:
            self._disconnect()
            return None
        _, data = frame
        # The activity list lives under data.data on success; many builds
        # actually return a single 'activity' instead. Accept both.
        d = data.get("data") if isinstance(data, dict) else None
        if not isinstance(d, dict):
            return None
        if isinstance(d.get("activities"), list):
            activities = d["activities"]
        elif isinstance(d.get("activity"), dict):
            activities = [d["activity"]]
        else:
            activities = []
        return _pick_activity(activities)


# ── Module-level singleton & public API ──────────────────────────────────────
# A single watcher serves the whole process. main.py drives the lifecycle
# via start_watcher() / stop_watcher() when the feature toggle changes.

_watcher_lock = threading.Lock()
_watcher: Optional[DiscordWatcher] = None


def start_watcher(poll_s: float = 5.0) -> None:
    """Idempotent: ensure the daemon watcher is running. Adjusts the poll
    cadence to `poll_s` if the watcher is already running."""
    global _watcher
    with _watcher_lock:
        if _watcher is None:
            _watcher = DiscordWatcher(poll_s=poll_s)
        else:
            _watcher.set_poll(poll_s)
        _watcher.start()


def stop_watcher() -> None:
    """Idempotent: signal the watcher to exit and close its pipe. Leaves
    the singleton object in place so a subsequent start_watcher() reuses
    its slot lock (and so get_listening_track() callers continue to see
    the cleared None slot rather than crashing on a missing attribute)."""
    global _watcher
    with _watcher_lock:
        w = _watcher
    if w is not None:
        w.stop()


def available() -> bool:
    """Quick non-blocking check: does ANY Discord IPC pipe exist? Returns
    False if a running watcher is in backoff (so callers can early-out
    without spinning). Does NOT touch the watcher's connection state.

    Note: when the watcher is running this is essentially "do we have a
    fresh slot?" - we ask the watcher rather than probing pipes ourselves,
    which would race with the worker on the same kernel handles.
    """
    with _watcher_lock:
        w = _watcher
    if w is not None and w.is_running():
        # Watcher owns the pipe; treat "have a slot" or "recent slot write"
        # as available. slot_age() returns inf if never written.
        return w.slot_age() < 60.0 or w.get() is not None
    # No watcher running: cheapest probe is to try to open one of the
    # well-known pipe names. We can't use os.path.exists on \\.\pipe paths
    # (Python's path layer normalizes them and the check fails); CreateFileW
    # is the canonical test. This path is for callers that want a one-shot
    # availability probe WITHOUT spinning up the watcher (e.g. tray-icon
    # enable-state).
    for i in range(10):
        pipe = r"\\.\pipe\discord-ipc-%d" % i
        h = _ct_open(pipe)
        if h is not None:
            _ct_close(h)
            return True
    return False


def get_listening_track(timeout_s: float = 0.5) -> Optional[dict]:
    """Return the user's current Spotify (or other LISTENING) track from
    Discord, or None. NON-BLOCKING: returns in under 1 ms by reading a
    lock-guarded slot that the watcher daemon thread keeps fresh.

    The `timeout_s` parameter is accepted for backward compatibility with
    pre-v1.0.89 callers but ignored: this function no longer spawns a
    worker, no longer .join()s, and never touches the IPC pipe.

    If the watcher is not running (start_watcher() was never called or
    stop_watcher() was called), returns None immediately.

    Returns:
        {'title': str, 'artist': str,
         'source': 'spotify' | 'other',
         'started_at': float | None}     # POSIX seconds, or None
        or None when:
          - Discord isn't running / pipe not found
          - no LISTENING activity right now
          - the entry is stale (Spotify pause persistence > 10 min)
          - the watcher hasn't published a slot yet
          - any IPC error / handshake reject / malformed frame
    """
    del timeout_s  # kept for backward-compat signature; no longer used
    with _watcher_lock:
        w = _watcher
    if w is None or not w.is_running():
        return None
    return w.get()


# ── Self-test (run "python discord_rpc.py" to probe locally) ────────────────

if __name__ == "__main__":
    print("pywin32:", _HAS_PYWIN32)
    print("available (pre-watcher):", available())
    start_watcher(poll_s=2.0)
    print("watcher started; sleeping 6s for a poll round-trip...")
    time.sleep(6.0)
    print("available (post-watcher):", available())
    print("track:", get_listening_track())
    stop_watcher()
