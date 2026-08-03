"""Verify the dev-console exe actually EMBEDS its frontend.

WHY THIS EXISTS
---------------
`cargo build --release` on a Tauri app looks like it works — it exits 0 and produces a
binary — but it does NOT do what `tauri build` does. Without the Tauri CLI setting its
env, the `tauri-build` step never embeds `frontendDist`; it bakes in `devUrl` instead.
The resulting console launches, then shows

    Hmmm... can't reach this page — 127.0.0.1 refused to connect

because it is trying to load a Vite dev server that isn't running. Shipped once
(v1.1.86); the build, the deploy and the app all reported success.

The check: a correctly built exe contains the hashed `index-*.js` asset names from
dist/ and does NOT contain the dev URL.

    python scripts/check_devconsole.py [--exe PATH]

Exit 0 = embedded, 1 = built wrong (or the frontend was never built).
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_EXES = [
    ROOT / "dev-console/src-tauri/target/release/lyric-immersion-dev-console.exe",
    ROOT / "dist/DesktopKaraoke/_internal/dev-console/lyric-immersion-dev-console.exe",
]
DIST = ROOT / "dev-console/dist/assets"


def check(exe: Path) -> bool:
    if not exe.exists():
        print(f"[skip] {exe} — not built")
        return True

    blob = exe.read_bytes()

    # The asset names dist/ actually produced this build. Matching on the real hashed
    # names (not just "index-") is what makes this a STALENESS check too: an exe that
    # embedded an older dist will fail, which is the other way to ship a surprise.
    expected = sorted(p.name for p in DIST.glob("index-*.js")) if DIST.is_dir() else []
    if not expected:
        print(f"[FAIL] no built frontend at {DIST} — run `npm run build` in dev-console/")
        return False

    embedded = [n for n in expected if n.encode() in blob]
    # NB: `devUrl` is present in BOTH good and bad builds — Tauri embeds the whole
    # tauri.conf.json, dev fields included. Its presence proves nothing, so it is
    # reported for context only. (An earlier version of this script failed on it and
    # produced a false positive against a perfectly good build.)
    dev_url = re.search(rb"127\.0\.0\.1:1420", blob) is not None

    print(f"  exe        : {exe}")
    print(f"  dist assets: {', '.join(expected)}")
    print(f"  embedded   : {embedded or 'NONE'}")
    print(f"  devUrl str : {'present (normal — config is embedded)' if dev_url else 'absent'}")

    missing = [n for n in expected if n not in embedded]
    if not embedded:
        print("[FAIL] the frontend is NOT embedded in this binary — it will try to load")
        print("       a Vite dev server and show 'refused to connect'.")
        print("       You almost certainly built it with `cargo build --release`, which")
        print("       exits 0 but never runs the embed step. Use the Tauri CLI:")
        print("           cd dev-console && npm run tauri:build")
        return False
    if missing:
        print(f"[FAIL] STALE build — these current dist assets are absent: {missing}")
        print("       The exe embeds an older frontend. Rebuild with `npm run tauri:build`.")
        return False
    print("[ok] current frontend fully embedded.")
    return True


def check_title_mirror() -> bool:
    """TICKET-223: the window title is duplicated across two files and two
    languages, with nothing tying them together.

    `main.py._DEVCONSOLE_TITLE` is how the tray finds an ALREADY-OPEN console so
    it can focus it rather than spawning a second one. It is matched against the
    live window title, which is set by `tauri.conf.json`. Rename the window and
    the guard fails silently: no error, no log, the tray just stops focusing.
    Cheap to assert here, effectively invisible if it ever drifts.
    """
    conf = ROOT / "dev-console" / "src-tauri" / "tauri.conf.json"
    main_py = ROOT / "main.py"
    if not conf.exists() or not main_py.exists():
        print("[skip] title mirror: tauri.conf.json or main.py missing")
        return True
    try:
        import json
        want = json.loads(conf.read_text(encoding="utf-8"))["app"]["windows"][0]["title"]
    except Exception as e:
        print(f"[fail] title mirror: could not read the window title: {e}")
        return False
    m = re.search(r'^\s*_DEVCONSOLE_TITLE\s*=\s*"([^"]*)"',
                  main_py.read_text(encoding="utf-8-sig"), re.M)
    if not m:
        print("[fail] title mirror: _DEVCONSOLE_TITLE not found in main.py")
        return False
    got = m.group(1)
    if got != want:
        print("[fail] title mirror: the tray's focus-existing-console guard is DEAD.")
        print(f"       tauri.conf.json window title : {want!r}")
        print(f"       main.py _DEVCONSOLE_TITLE    : {got!r}")
        print("       These must match exactly. Fix main.py to match the config.")
        return False
    print(f"[ok] title mirror: main.py matches tauri.conf.json ({got!r}).")
    return True


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--exe", help="check one specific exe")
    a = ap.parse_args(argv)
    targets = [Path(a.exe)] if a.exe else DEFAULT_EXES
    ok = True
    for t in targets:
        ok = check(t) and ok
        print()
    ok = check_title_mirror() and ok
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
