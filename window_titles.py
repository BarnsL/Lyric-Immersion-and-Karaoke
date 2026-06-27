r"""TICKET-102 - Window-title scraper (Steam Overlay / Discord embedded / Electron).

Background
==========
SMTC (Windows.Media.Control) is the canonical "what's playing" source on
Windows, but a CEF/Electron host that embeds a music site (Steam Overlay's
in-game browser, Discord channel YouTube/Spotify iframes, Slack and Teams
with embedded media) DOES NOT publish to SMTC. From the user's point of
view the music IS playing (it's loud, it's on their main screen) but the
overlay never lights up.

The trick that works: scrape the visible top-level window titles for the
small allowlist of processes where this is the dominant failure mode, and
look for the same trailing site suffix (` - YouTube`, ` - Spotify`, ...)
that already gates the browser-tab path in clean_title().

Scope (per TICKET-102 design)
=============================
- TWO tiers of processes:
    HIGH (default ON):   steamwebhelper.exe (Steam Overlay CEF),
                          discord.exe, discordcanary.exe, discordptb.exe,
                          slack.exe, teams.exe.
    LOW  (default OFF):  chrome.exe, msedge.exe, brave.exe, firefox.exe,
                          opera.exe, vivaldi.exe, arc.exe.
  Standalone browsers ALREADY publish through SMTC for the major music
  sites; scraping them in parallel risks double-counting unrelated tabs
  (Gmail, Twitter, Slack-in-a-tab). The toggle is opt-in.
- A music-marker suffix on an allowlisted window is the load-bearing
  filter — anything else from those processes is ignored.
- Stdlib + ctypes only. No pywin32 dependency (matches discord_rpc.py).
- Non-Windows is a no-op: available() returns False, get_current_track()
  returns None, the daemon never starts. Linux test runs stay green.

Architecture (mirror of discord_rpc.py)
=======================================
A single long-lived `WindowTitleWatcher` daemon thread owns the enum loop.
Every poll it does:
  1. EnumWindows -> list of HWND.
  2. Filter to visible top-level windows.
  3. PID -> exe basename via OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)
     + QueryFullProcessImageNameW; reject any process not in the active
     tier set BEFORE reading the window title (privacy: non-allowlisted
     window text is never read).
  4. SendMessageTimeoutW(WM_GETTEXT, SMTO_ABORTIFHUNG, 100ms) for the
     title text — avoids stalling on a hung Chrome tab the way a naive
     GetWindowTextW would.
  5. Suffix-match the title against the music-marker set; strip suffix,
     parse out a likely (title, artist) pair, publish to the slot.

Public API (the bits main.py cares about)
=========================================
- start_watcher(poll_s=2.0)         # idempotent
- stop_watcher()                    # idempotent
- get_current_track() -> dict|None  # NEVER blocks > 1ms (lock-guarded slot copy)
    Returns {'title','artist','source','process','window_handle',
             'priority','last_update_t'} or None.
- set_generic_browsers(on: bool)    # flip the LOW tier on/off at runtime
- set_poll(poll_s: float)           # live-tune cadence
- available() -> bool               # whether Win32 user32/kernel32 reachable
- _current_snapshot() -> dict       # diag-only: tier flags + slot age
"""

from __future__ import annotations

import os
import re
import threading
import time
from typing import Any, Optional


# ── Process tiers ────────────────────────────────────────────────────────────
# HIGH = always on when window_titles_on=1. The user's reported failure mode
# (steamwebhelper.exe playing ReGLOSS without SMTC) sits here.
_HIGH_PRIORITY_PROCS = frozenset({
    "steamwebhelper.exe",
    "discord.exe",
    "discordcanary.exe",
    "discordptb.exe",
    "slack.exe",
    "teams.exe",
    "ms-teams.exe",
})

# LOW = generic-browser fallback. Default OFF (knob window_titles_generic_browsers).
# Standalone browsers already feed SMTC for the major suffixes; toggling this
# on scrapes them too, accepting some false-positive risk in exchange for
# coverage when SMTC stalls on a tab switch.
_LOW_PRIORITY_PROCS = frozenset({
    "chrome.exe",
    "msedge.exe",
    "brave.exe",
    "firefox.exe",
    "opera.exe",
    "vivaldi.exe",
    "arc.exe",
})


# ── Music-marker suffix patterns (positive signal) ──────────────────────────
# Mirror the BROWSER_HINTS suffix-strip set in main.py:clean_title (line ~736)
# so we have a single mental model: a window title ending in one of these is
# a music tab on a music site. The separator (en/em dash / hyphen / pipe) is
# flexible because each music site has shifted its dash glyph over time.
_SUFFIX_RE = re.compile(
    r"""\s*[-–—|]\s*(?:
            YouTube\ Music
          | YouTube
          | Spotify
          | SoundCloud
          | Bandcamp
          | Apple\ Music
          | Tidal
          | Deezer
          | Amazon\ Music
          | Niconico
          | ニコニコ動画
          | nicovideo
          | Bilibili
          | bilibili
          | Mixcloud
        )\s*$""",
    re.VERBOSE | re.IGNORECASE,
)


# ── Negative-marker suffixes (definitely NOT music, reject outright) ────────
# A generic-browser hit with one of these is an email / doc / chat tab.
_NEG_SUFFIX_RE = re.compile(
    r"""\s*[-–—|]\s*(?:
            Gmail | Inbox
          | Google\ Docs | Sheets | Slides | Notion | Linear
          | GitHub | Jira | Confluence | Figma
          | Twitter | X | Reddit | Facebook | Instagram | LinkedIn
          | Discord
        )\s*$""",
    re.VERBOSE | re.IGNORECASE,
)

# Pure hostnames / empty-tab placeholders.
_BARE_NON_MUSIC = frozenset({
    "new tab", "new private tab", "新しいタブ",
    "youtube.com", "youtube", "music.youtube.com",
    "soundcloud.com", "spotify.com", "open.spotify.com",
})


# ── Non-content window classes to skip even on an allowlisted PID ───────────
# Shell classes obviously aren't a music tab; CoreWindow is the immersive
# UWP shell. We DON'T allowlist by class name otherwise because Steam
# Overlay's class has shifted across builds (SDL_app -> CEFCLIENT ->
# Chrome_WidgetWin_0/1 -> valveoverlay).
_SKIP_CLASSES = frozenset({
    "Shell_TrayWnd",
    "Progman",
    "WorkerW",
    "Windows.UI.Core.CoreWindow",
})


# ── Hard per-cycle time budget ──────────────────────────────────────────────
# A full enum + title-read pass MUST complete in under this many seconds or
# the cycle bails. EnumWindows is sub-ms and SendMessageTimeoutW caps each
# title read at 100 ms, so this is mostly belt-and-braces against a worst
# case where 50+ allowlisted windows exist on one box.
_CYCLE_BUDGET_S = 0.050


# ── ctypes plumbing (Win32 user32 + kernel32 via stdlib only) ───────────────

def _ct_load():
    """Lazy ctypes setup. Returns (user32, kernel32, ctypes_module, wintypes)
    or (None, None, None, None) on non-Windows / missing windll."""
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return None, None, None, None
    try:
        u = ctypes.windll.user32
        k = ctypes.windll.kernel32
    except Exception:
        return None, None, None, None

    # EnumWindows callback signature: BOOL CALLBACK(HWND, LPARAM)
    # Use WINFUNCTYPE for stdcall on x64; CFUNCTYPE would corrupt the stack.
    u.EnumWindows.argtypes = [ctypes.c_void_p, wintypes.LPARAM]
    u.EnumWindows.restype = wintypes.BOOL

    u.IsWindowVisible.argtypes = [wintypes.HWND]
    u.IsWindowVisible.restype = wintypes.BOOL

    u.GetWindowThreadProcessId.argtypes = [wintypes.HWND,
                                           ctypes.POINTER(wintypes.DWORD)]
    u.GetWindowThreadProcessId.restype = wintypes.DWORD

    u.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    u.GetClassNameW.restype = ctypes.c_int

    # SendMessageTimeoutW lets a hung target abort the call after a
    # caller-controlled timeout instead of blocking forever like
    # GetWindowTextW (which goes through SendMessage internally).
    u.SendMessageTimeoutW.argtypes = [
        wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
        wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_void_p),
    ]
    u.SendMessageTimeoutW.restype = wintypes.LPARAM

    u.GetForegroundWindow.argtypes = []
    u.GetForegroundWindow.restype = wintypes.HWND

    k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k.OpenProcess.restype = wintypes.HANDLE
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    k.CloseHandle.restype = wintypes.BOOL
    k.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD,
        wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD),
    ]
    k.QueryFullProcessImageNameW.restype = wintypes.BOOL

    return u, k, ctypes, wintypes


def available() -> bool:
    """Cheap probe: are the Win32 APIs we need reachable on this box?

    True on a normal Windows install, False on Linux/macOS or a stripped
    Windows that's missing user32 (essentially never). Used by /diag and
    the tray menu to decide whether to even surface the toggle."""
    u, k, _ct, _wt = _ct_load()
    return u is not None and k is not None


# Internal cache so EnumWindows callbacks don't re-load ctypes every call.
_ct_cache: Optional[tuple] = None


def _ct_get():
    """Return the cached ctypes (user32, kernel32, ctypes, wintypes) tuple,
    loading once on first use. Returns (None,)*4 on non-Windows."""
    global _ct_cache
    if _ct_cache is None:
        _ct_cache = _ct_load()
    return _ct_cache


# ── PID -> exe basename ─────────────────────────────────────────────────────

# PROCESS_QUERY_LIMITED_INFORMATION (0x1000) works on protected processes
# where PROCESS_QUERY_INFORMATION (0x0400) gets ACCESS_DENIED — important
# for Discord on Win11 which runs with extra integrity.
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def _exe_basename_for_pid(pid: int) -> Optional[str]:
    """Resolve a PID to its image basename (e.g. 'steamwebhelper.exe').
    Returns None if the process is gone or we lack access. Swallows
    all errors — this is best-effort."""
    u, k, ct, wt = _ct_get()
    if k is None:
        return None
    h = None
    try:
        h = k.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return None
        buf = (ct.c_wchar * 1024)()
        size = wt.DWORD(1024)
        ok = k.QueryFullProcessImageNameW(h, 0, buf, ct.byref(size))
        if not ok:
            return None
        full = buf[:size.value]
        # os.path.basename works on a wchar path on Windows; normalize case
        # so the allowlist compare is consistent regardless of which casing
        # the OS hands back.
        return os.path.basename(full).lower()
    except Exception:
        return None
    finally:
        if h:
            try:
                k.CloseHandle(h)
            except Exception:
                pass


# ── Window title / class accessors (with hard timeout) ──────────────────────

# Win32 message ids — see WinUser.h.
_WM_GETTEXT = 0x000D
_WM_GETTEXTLENGTH = 0x000E
_SMTO_ABORTIFHUNG = 0x0002


def _safe_window_text(hwnd: int) -> str:
    """Read a window's text with a 100 ms abort-if-hung timeout. Returns
    '' on any failure. Never blocks the caller more than ~100 ms even when
    the target's message pump is wedged."""
    u, _k, ct, _wt = _ct_get()
    if u is None:
        return ""
    try:
        buf = (ct.c_wchar * 512)()
        out = ct.c_void_p(0)
        # WPARAM = buffer-size-in-chars, LPARAM = buffer pointer.
        rv = u.SendMessageTimeoutW(
            hwnd, _WM_GETTEXT, 512,
            ct.cast(buf, ct.c_void_p).value or 0,
            _SMTO_ABORTIFHUNG, 100, ct.byref(out),
        )
        if not rv:
            return ""
        # buf is now a null-terminated wchar string; .value stops at the
        # null terminator, which is what we want (length in `out` includes
        # neither the NUL nor a partial write on a hung target).
        return buf.value
    except Exception:
        return ""


def _window_class(hwnd: int) -> str:
    u, _k, ct, _wt = _ct_get()
    if u is None:
        return ""
    try:
        buf = (ct.c_wchar * 256)()
        n = u.GetClassNameW(hwnd, buf, 256)
        return buf[:n] if n else ""
    except Exception:
        return ""


# ── Title parser ────────────────────────────────────────────────────────────

def _parse_title(raw: str) -> Optional[tuple[str, str]]:
    """Map a raw window title to (title, artist) or None if it doesn't
    look like a music-bearing tab.

    Rules:
      * MUST end in a music-marker suffix; strip it.
      * MUST NOT end in a non-music suffix.
      * Strip a leading 'Channel: ' if present (Steam Overlay style).
      * Then split on ' - ', ' — ', ' – ', ' | ' (first occurrence) to
        give two halves. Pass the FIRST half as the title and the SECOND
        as the artist hint — clean_title() in main.py will swap them if
        the heuristic / Shazam-live disagrees.
      * Single-token title (no separator) -> (title, '').

    Returns None for empty input, hostname-only titles, negative suffixes,
    or anything that doesn't carry a music marker after a strip.
    """
    if not raw:
        return None
    t = raw.strip()
    if not t:
        return None
    low = t.lower()
    if low in _BARE_NON_MUSIC:
        return None
    # Reject NON-music suffixes BEFORE the music suffix check — a tab title
    # could end in ' - Discord' on an allowlisted Discord process, but that's
    # the channel name, not a song.
    if _NEG_SUFFIX_RE.search(t):
        return None
    m = _SUFFIX_RE.search(t)
    if not m:
        return None
    body = _SUFFIX_RE.sub("", t).strip()
    if not body:
        return None
    # Steam Overlay sometimes prefixes the embedding channel: 'Channel: Song'
    if ":" in body[:32]:
        head, sep, tail = body.partition(":")
        if sep and tail.strip() and len(head) < 30 and " " not in head.strip():
            # Heuristic: short single-word prefix followed by ':' is a
            # channel-name decoration; drop it.
            body = tail.strip()
    # Split on first separator. Order matters: ' — ' (em dash with spaces),
    # then ' – ' (en dash), then ' - ', then ' | '.
    for sep in (" — ", " – ", " - ", " | "):
        if sep in body:
            a, _s, b = body.partition(sep)
            a, b = a.strip(), b.strip()
            if a and b:
                # We don't try to guess which is artist vs title here —
                # downstream clean_title() with source='window-title:...' has
                # the artist-aware heuristics already. Pass title=first,
                # artist=second; Shazam-live will correct on the merge.
                return (a, b)
            if a and not b:
                return (a, "")
            if b and not a:
                return (b, "")
    return (body, "")


# ── Long-lived watcher thread ───────────────────────────────────────────────

class WindowTitleWatcher:
    """Daemon thread that enumerates visible top-level windows on a fixed
    cadence, filters to an allowlisted set of processes, scrapes their
    titles, and publishes the latest music-marker hit into a lock-guarded
    slot. Mirrors DiscordWatcher's contract verbatim:

      start() / stop()                 - idempotent lifecycle.
      get()                            - shallow-copy of the slot, sub-ms.
      slot_age()                       - diag: seconds since last write.
      set_poll(s) / set_generic(on)    - live-tune cadence + tier.

    The Tk thread NEVER joins this thread."""

    _POLL_MIN_S = 0.5
    _POLL_MAX_S = 30.0

    def __init__(self, poll_s: float = 2.0,
                 generic_browsers: bool = False) -> None:
        self._poll_s = self._clamp_poll(poll_s)
        self._generic_on = bool(generic_browsers)
        # Slot.
        self._slot_lock = threading.Lock()
        self._slot: Optional[dict] = None
        self._slot_updated_t: float = 0.0
        # Lifecycle.
        self._stop_evt = threading.Event()
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._start_lock = threading.Lock()
        # PID -> exe-basename cache (PIDs are stable for a window's lifetime).
        # Cleared on stop() so a recycled PID can't carry a stale name.
        self._pid_cache: dict[int, Optional[str]] = {}

    @classmethod
    def _clamp_poll(cls, v: float) -> float:
        try:
            v = float(v)
        except Exception:
            v = 2.0
        if v < cls._POLL_MIN_S:
            v = cls._POLL_MIN_S
        if v > cls._POLL_MAX_S:
            v = cls._POLL_MAX_S
        return v

    def set_poll(self, poll_s: float) -> None:
        # HOTFIX (workflow w79kqkeiv perf finding): only wake when the value
        # ACTUALLY changes. main.py calls start_watcher()->set_poll() every
        # Tk tick when SMTC is silent; unconditional _wake.set() turned the
        # 2s poll into a ~20Hz busy-loop with OpenProcess+SendMessageTimeoutW
        # spam on every allowlisted window.
        v = self._clamp_poll(poll_s)
        if v == self._poll_s:
            return
        self._poll_s = v
        self._wake.set()

    def set_generic(self, on: bool) -> None:
        """Flip the LOW tier (generic browsers) on/off at runtime. Wakes
        the worker so the next enum cycle uses the new tier set."""
        # HOTFIX (workflow w79kqkeiv perf finding): same idempotency fix as
        # set_poll above.
        on = bool(on)
        if on == self._generic_on:
            return
        self._generic_on = on
        self._wake.set()

    def start(self) -> None:
        """Idempotent: spawn the daemon thread if not alive. Performs ONE
        synchronous enum pass to prime the slot before returning, so the
        first Tk-thread get() after a start_watcher() doesn't see None
        purely because the worker hasn't ticked yet."""
        with self._start_lock:
            t = self._thread
            if t is not None and t.is_alive():
                return
            self._stop_evt.clear()
            self._wake.clear()
            # Prime the slot synchronously so callers get a fresh read even
            # before the worker's first wake. Swallow errors — a failed
            # prime just means the slot stays None until the first tick.
            try:
                self._tick_once()
            except Exception:
                pass
            self._thread = threading.Thread(
                target=self._run, name="window-title-watcher", daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        self._stop_evt.set()
        self._wake.set()
        with self._slot_lock:
            self._slot = None
            self._slot_updated_t = 0.0
        self._pid_cache.clear()

    def is_running(self) -> bool:
        t = self._thread
        return bool(t and t.is_alive() and not self._stop_evt.is_set())

    def get(self) -> Optional[dict]:
        with self._slot_lock:
            s = self._slot
            return dict(s) if s else None

    def slot_age(self) -> float:
        with self._slot_lock:
            t = self._slot_updated_t
        return (time.time() - t) if t else float("inf")

    # ── worker loop (daemon thread only) ─────────────────────────────────────

    def _run(self) -> None:
        while not self._stop_evt.is_set():
            try:
                self._tick_once()
            except Exception:
                # Defensive: never let the daemon die on an unhandled error.
                pass
            self._wake.wait(timeout=self._poll_s)
            self._wake.clear()

    def _active_procs(self) -> frozenset:
        """Return the union of allowlisted process basenames for the
        current tier configuration."""
        if self._generic_on:
            return _HIGH_PRIORITY_PROCS | _LOW_PRIORITY_PROCS
        return _HIGH_PRIORITY_PROCS

    def _tick_once(self) -> None:
        """One enumeration cycle. Walks visible top-level windows, picks
        the BEST (foreground-preferred) music-bearing title from the
        allowlisted procs, and publishes to the slot."""
        u, k, ct, wt = _ct_get()
        if u is None or k is None:
            return
        active = self._active_procs()
        deadline = time.time() + _CYCLE_BUDGET_S
        # Best candidate so far. Tuple: (priority_rank, is_foreground, exe,
        # hwnd, parsed_title, parsed_artist, raw_title, window_class).
        # priority_rank: 0=HIGH, 1=LOW. Lower wins.
        best: Optional[tuple] = None
        # Foreground HWND so we can prefer the tab the user is actually on.
        try:
            fg_hwnd = u.GetForegroundWindow()
        except Exception:
            fg_hwnd = 0

        # The local-scope capture lets the C callback safely mutate `best`.
        state = {"best": None}

        WNDENUMPROC = ct.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

        def _cb(hwnd, _lparam):
            # Bail early if cycle budget is blown — caller will skip this
            # tick rather than risk dragging out a long enum.
            if time.time() >= deadline:
                return False
            try:
                if not u.IsWindowVisible(hwnd):
                    return True
                # PID lookup is cheap (no syscall to the target process).
                pid_holder = wt.DWORD(0)
                u.GetWindowThreadProcessId(hwnd, ct.byref(pid_holder))
                pid = int(pid_holder.value)
                if pid == 0:
                    return True
                # PID->exe cache (cleared on stop). Avoid OpenProcess+Query
                # on every cycle for the same PID.
                exe = self._pid_cache.get(pid, "__miss__")
                if exe == "__miss__":
                    exe = _exe_basename_for_pid(pid)
                    self._pid_cache[pid] = exe
                if exe is None or exe not in active:
                    # PRIVACY: we have NOT read the window title for this
                    # process yet, and we never will if it's not on the
                    # allowlist. Documented invariant.
                    return True
                wcls = _window_class(hwnd)
                if wcls in _SKIP_CLASSES:
                    return True
                # NOW it's safe to read text.
                title = _safe_window_text(hwnd)
                if not title:
                    return True
                parsed = _parse_title(title)
                if parsed is None:
                    return True
                ptitle, partist = parsed
                # Tier rank for tie-breaking.
                prio = 0 if exe in _HIGH_PRIORITY_PROCS else 1
                is_fg = 1 if hwnd == fg_hwnd else 0
                cand = (prio, -is_fg, exe, int(hwnd), ptitle, partist, title, wcls)
                cur = state["best"]
                # Prefer (lower prio, foreground first); first match wins
                # within a tier+foreground bucket.
                if cur is None or cand[:2] < cur[:2]:
                    state["best"] = cand
            except Exception:
                # Swallow per-window errors so one bad HWND doesn't kill
                # the entire enum.
                pass
            return True

        cb = WNDENUMPROC(_cb)
        try:
            u.EnumWindows(cb, 0)
        except Exception:
            return

        best = state["best"]
        if best is None:
            # Nothing matched this cycle — clear the slot so a track that
            # has gone away doesn't linger.
            self._publish(None)
            return

        prio, _fg, exe, hwnd, ptitle, partist, raw, wcls = best
        priority = "high" if prio == 0 else "low"
        track = {
            "title": ptitle,
            "artist": partist,
            "source": "window-title:" + exe,
            "process": exe,
            "window_handle": hwnd,
            "window_class": wcls,
            "raw_title": raw,
            "priority": priority,
            "last_update_t": time.time(),
        }
        self._publish(track)

    def _publish(self, track: Optional[dict]) -> None:
        with self._slot_lock:
            self._slot = track
            self._slot_updated_t = time.time()


# ── Module-level singleton & public API ──────────────────────────────────────

_watcher_lock = threading.Lock()
_watcher: Optional[WindowTitleWatcher] = None


def start_watcher(poll_s: float = 2.0,
                  generic_browsers: bool = False) -> None:
    """Idempotent: ensure the watcher daemon is running. Adjusts the poll
    cadence and tier flag if the watcher is already alive."""
    global _watcher
    with _watcher_lock:
        if _watcher is None:
            _watcher = WindowTitleWatcher(
                poll_s=poll_s, generic_browsers=generic_browsers,
            )
        else:
            _watcher.set_poll(poll_s)
            _watcher.set_generic(generic_browsers)
        _watcher.start()


def stop_watcher() -> None:
    """Idempotent: signal the watcher to exit. Leaves the singleton object
    in place so a subsequent start_watcher() reuses it."""
    global _watcher
    with _watcher_lock:
        w = _watcher
    if w is not None:
        w.stop()


def set_generic_browsers(on: bool) -> None:
    """Live-flip the LOW tier on/off without restarting the watcher."""
    with _watcher_lock:
        w = _watcher
    if w is not None:
        w.set_generic(on)


def set_poll(poll_s: float) -> None:
    """Live-tune the poll cadence."""
    with _watcher_lock:
        w = _watcher
    if w is not None:
        w.set_poll(poll_s)


def get_current_track() -> Optional[dict]:
    """Return the latest scraped music-bearing window title, or None.

    NON-BLOCKING: returns in under 1 ms by reading a lock-guarded slot
    that the watcher daemon thread keeps fresh.

    Returns:
        {'title': str, 'artist': str,
         'source': 'window-title:<exe>',
         'process': str,                # e.g. 'steamwebhelper.exe'
         'window_handle': int,
         'window_class': str,
         'raw_title': str,              # pre-strip, for diag
         'priority': 'high' | 'low',
         'last_update_t': float}        # POSIX seconds
        or None when no allowlisted window currently carries a music suffix.
    """
    with _watcher_lock:
        w = _watcher
    if w is None or not w.is_running():
        return None
    return w.get()


def _current_snapshot() -> dict:
    """Diag-only: tier flags + slot age, even when no track is held."""
    with _watcher_lock:
        w = _watcher
    if w is None:
        return {"running": False, "generic": False, "slot_age_s": None,
                "track": None}
    return {
        "running": w.is_running(),
        "generic": bool(w._generic_on),
        "slot_age_s": (round(w.slot_age(), 2)
                       if w.slot_age() != float("inf") else None),
        "track": w.get(),
    }


# ── Self-test (run "python window_titles.py" to probe locally) ──────────────

if __name__ == "__main__":
    print("available:", available())
    start_watcher(poll_s=2.0, generic_browsers=False)
    print("watcher started; sleeping 4s for a poll round-trip...")
    time.sleep(4.0)
    print("track:", get_current_track())
    print("snapshot:", _current_snapshot())
    stop_watcher()
