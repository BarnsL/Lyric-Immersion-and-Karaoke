"""Live test for media_mpris.MprisWatcher — Linux only, real D-Bus.

Exports a MOCK MPRIS player (org.mpris.MediaPlayer2.mocktest) on the session
bus with known metadata, then starts the real provider and asserts the snapshot
matches the contract: CJK title, artist, PLAYING status mapped to the app-level
constant (4), position and duration in SECONDS.

Run under a private bus so it never touches a desktop session:
    dbus-run-session -- python scripts/test_mpris_provider.py
CI runs exactly that on ubuntu-latest (.github/workflows/ci.yml).
"""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TITLE = "テスト曲"                 # CJK on purpose — the app's bread and butter
ARTIST = "Mock Artist"
LENGTH_US = 200_000_000            # 200 s
POS_US = 42_000_000                # 42 s


async def _serve_mock():
    from dbus_next.aio import MessageBus
    from dbus_next.service import ServiceInterface, dbus_property, PropertyAccess
    from dbus_next import Variant, BusType

    class Root(ServiceInterface):
        def __init__(self):
            super().__init__("org.mpris.MediaPlayer2")

        @dbus_property(access=PropertyAccess.READ)
        def Identity(self) -> "s":
            return "Mock Test Player"

    class Player(ServiceInterface):
        def __init__(self):
            super().__init__("org.mpris.MediaPlayer2.Player")

        @dbus_property(access=PropertyAccess.READ)
        def Metadata(self) -> "a{sv}":
            return {
                "xesam:title": Variant("s", TITLE),
                "xesam:artist": Variant("as", [ARTIST]),
                "xesam:album": Variant("s", "Mock Album"),
                "mpris:length": Variant("x", LENGTH_US),
            }

        @dbus_property(access=PropertyAccess.READ)
        def PlaybackStatus(self) -> "s":
            return "Playing"

        @dbus_property(access=PropertyAccess.READ)
        def Rate(self) -> "d":
            return 1.0

        @dbus_property(access=PropertyAccess.READ)
        def Position(self) -> "x":
            return POS_US

    bus = await MessageBus(bus_type=BusType.SESSION).connect()
    bus.export("/org/mpris/MediaPlayer2", Root())
    bus.export("/org/mpris/MediaPlayer2", Player())
    await bus.request_name("org.mpris.MediaPlayer2.mocktest")
    return bus


def main() -> int:
    loop = asyncio.new_event_loop()
    loop.run_until_complete(_serve_mock())

    import threading
    threading.Thread(target=loop.run_forever, daemon=True).start()

    from media_mpris import MprisWatcher, PLAYING
    w = MprisWatcher()
    w.start()

    snap = None
    deadline = time.time() + 10.0
    while time.time() < deadline:
        snap = w.get()
        if snap and snap.get("title") == TITLE:
            break
        time.sleep(0.2)

    print("snapshot:", snap)
    checks = [
        ("title", bool(snap) and snap["title"] == TITLE),
        ("artist", bool(snap) and snap["artist"] == ARTIST),
        ("status==PLAYING(4)", bool(snap) and snap["status"] == PLAYING == 4),
        ("position 42s", bool(snap) and abs(snap["position"] - 42.0) < 0.5),
        ("duration 200s", bool(snap) and abs(snap["duration"] - 200.0) < 0.5),
        ("source suffix", bool(snap) and snap["source"] == "mocktest"),
    ]
    ok = True
    for name, res in checks:
        print(("  PASS " if res else "  FAIL ") + name)
        ok &= res
    print("MPRIS PROVIDER:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
