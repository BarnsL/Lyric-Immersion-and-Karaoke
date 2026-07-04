"""Linux now-playing provider — MPRIS2 over the D-Bus session bus.

This is the Linux backend for the MediaSessionProvider seam (docs/PORTING.md §2):
it fills the SAME state contract the Windows SMTC watcher (main.py MediaWatcher)
produces, so the engine's arbitration/session logic consumes either unchanged:

    {"title", "artist", "album", "status", "position", "duration",
     "rate", "source", "ts"}

Design notes (from the audited port inventory):
  • MPRIS carries REAL position: the ``Position`` property is int64 microseconds.
    It is deliberately excluded from PropertiesChanged signals, so we POLL it on
    the same cadence the SMTC watcher uses (~0.15 s) via a single
    ``Properties.GetAll`` round-trip per player.
  • ``status`` is mapped onto the app-level constants that mirror WinRT's
    PlaybackStatus enum (main.py hardcodes PLAYING = 4), so downstream
    comparisons work verbatim: 'Playing'→4, 'Paused'→5, 'Stopped'→3.
  • The bus-name suffix (org.mpris.MediaPlayer2.<suffix>) substitutes for
    Windows' SourceAppUserModelId as the session identity — Spotify, VLC, mpv,
    Firefox, and Chromium/Electron apps all publish MPRIS names.
  • Pure standalone module: imported by nothing on Windows; needs ``dbus-next``
    (requirements.txt marks it sys_platform == "linux").

Smoke test:  python media_mpris.py --once   (prints the current snapshot)
Live test:   scripts/test_mpris_provider.py (mock player on a private bus; CI)
"""
from __future__ import annotations

import threading
import time

# App-level playback status constants — numerically identical to WinRT's
# GlobalSystemMediaTransportControlsSessionPlaybackStatus so main.py's
# hardcoded PLAYING = 4 comparisons keep working against this provider.
STOPPED = 3
PLAYING = 4
PAUSED = 5

_STATUS_MAP = {"Playing": PLAYING, "Paused": PAUSED, "Stopped": STOPPED}

_MPRIS_PREFIX = "org.mpris.MediaPlayer2."
_PLAYER_IFACE = "org.mpris.MediaPlayer2.Player"
_OBJ_PATH = "/org/mpris/MediaPlayer2"


def _snapshot_from_props(props: dict, source: str) -> dict:
    """Normalize one player's GetAll(Player) result to the shared contract."""
    def val(name, default=None):
        v = props.get(name)
        return default if v is None else getattr(v, "value", v)

    meta = val("Metadata", {}) or {}

    def mval(key, default=None):
        v = meta.get(key)
        return default if v is None else getattr(v, "value", v)

    artists = mval("xesam:artist", []) or []
    length_us = mval("mpris:length", 0) or 0
    pos_us = val("Position", 0) or 0
    return {
        "title": (mval("xesam:title", "") or "").strip(),
        "artist": ", ".join(a for a in artists if a).strip(),
        "album": (mval("xesam:album", "") or "").strip(),
        "status": _STATUS_MAP.get(val("PlaybackStatus", ""), STOPPED),
        "position": pos_us / 1_000_000.0,
        "duration": length_us / 1_000_000.0,
        "rate": float(val("Rate", 1.0) or 1.0),
        "source": source,
        "ts": time.time(),
    }


class MprisWatcher:
    """Polls every MPRIS player on the session bus; exposes the best snapshot.

    Mirrors the Windows MediaWatcher architecture: a daemon thread runs its own
    asyncio loop; ``get()`` returns the latest thread-safe snapshot (or None).
    'Best' = a Playing player, else the most recently seen player.
    """

    def __init__(self, poll_s: float = 0.15):
        self._poll_s = poll_s
        self._lock = threading.Lock()
        self._state: dict | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ── public API (contract-compatible with MediaWatcher) ──────────────
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="mpris-watch",
                                        daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def get(self) -> dict | None:
        with self._lock:
            return dict(self._state) if self._state else None

    # ── worker ───────────────────────────────────────────────────────────
    def _run(self):
        import asyncio
        asyncio.run(self._loop())

    async def _loop(self):
        from dbus_next.aio import MessageBus
        from dbus_next import BusType

        bus = await MessageBus(bus_type=BusType.SESSION).connect()
        dbus_intro = await bus.introspect("org.freedesktop.DBus",
                                          "/org/freedesktop/DBus")
        dbus_obj = bus.get_proxy_object("org.freedesktop.DBus",
                                        "/org/freedesktop/DBus", dbus_intro)
        dbus_if = dbus_obj.get_interface("org.freedesktop.DBus")
        proxies: dict[str, object] = {}   # bus name -> Properties interface

        while not self._stop.is_set():
            try:
                names = [n for n in await dbus_if.call_list_names()
                         if n.startswith(_MPRIS_PREFIX)]
                best = None
                for name in names:
                    props_if = proxies.get(name)
                    if props_if is None:
                        try:
                            intro = await bus.introspect(name, _OBJ_PATH)
                            obj = bus.get_proxy_object(name, _OBJ_PATH, intro)
                            props_if = obj.get_interface(
                                "org.freedesktop.DBus.Properties")
                            proxies[name] = props_if
                        except Exception:
                            continue
                    try:
                        props = await props_if.call_get_all(_PLAYER_IFACE)
                    except Exception:
                        proxies.pop(name, None)   # player went away → re-probe
                        continue
                    snap = _snapshot_from_props(props,
                                                name[len(_MPRIS_PREFIX):])
                    if snap["status"] == PLAYING:
                        best = snap
                        break                      # a playing player wins
                    best = best or snap
                if best:
                    with self._lock:
                        self._state = best
            except Exception:
                # Bus hiccup — keep the last snapshot and retry next tick.
                pass
            await _sleep(self._poll_s)


async def _sleep(s: float):
    import asyncio
    await asyncio.sleep(s)


if __name__ == "__main__":
    import json
    import sys

    w = MprisWatcher()
    w.start()
    if "--watch" in sys.argv:
        try:
            while True:
                print(json.dumps(w.get(), ensure_ascii=False))
                time.sleep(1.0)
        except KeyboardInterrupt:
            pass
    else:   # --once (default): settle briefly, print one snapshot
        time.sleep(1.2)
        print(json.dumps(w.get(), ensure_ascii=False, indent=2))
