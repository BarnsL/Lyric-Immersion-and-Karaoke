"""Deploy dist\\DesktopKaraoke over the live install at D:\\DesktopKaraoke.

WHY THIS EXISTS
---------------
Deploying by hand went wrong repeatedly:

* The target holds USER DATA that is not in the build — `lyrics\\` (the cache),
  `models\\` (2.1 GB of whisper weights), `deps\\` (1.9 GB), `settings.json`,
  `metrics.json` and the logs. A `robocopy /MIR` at the top level deletes all of
  it. This script mirrors **only** `_internal\\`, which is pure build output, and
  copies the top level WITHOUT purging.
* A stale `dev-console\\lyric-immersion-dev-console.exe` sat beside the current
  one for two weeks (TICKET-198). `_dev_console_exe()` lists it as a fallback, so
  a stale copy there is a trap: you end up running a console from a fortnight ago
  and trusting its numbers. It gets refreshed from the same source as `_internal`.
* Copying over a RUNNING exe silently half-succeeds. Every process running from
  the target is stopped first, and the script refuses to copy if one survives.
* robocopy's exit codes are a bitmask: <8 is success, >=8 is failure. Treating
  "non-zero" as failure (or, worse, ignoring it) hid a real error once already.

    python scripts/deploy_local.py [--target D:\\DesktopKaraoke] [--dry-run]
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "dist" / "DesktopKaraoke"

# User data in the TARGET that the build must never touch.
#
# These are not merely "not deleted" — they must not be OVERWRITTEN either, and
# that distinction cost a real log file. The app writes its settings, metrics and
# log NEXT TO THE EXE (appdata.py returns the exe's own folder for a portable
# frozen build), so running `dist\DesktopKaraoke\…exe` — which build.bat does on
# every build for `--selftest`, and tells you to double-click to smoke-test —
# creates those same files inside `dist`. A plain `/E` then copies them over the
# live ones. robocopy copies any file differing in size OR timestamp, in either
# direction, so an EMPTY 0-byte karaoke.log from the selftest happily replaced a
# 201 KB live log. The earlier version of this script listed these names and then
# passed them to no robocopy flag at all, so it printed "[ok] deployed" having
# just destroyed the file you would read to diagnose the deploy.
PRESERVE_FILES = ("settings.json", "metrics.json", "karaoke.log", "karaoke.log.1",
                  "startup_stderr.log", "unins000.exe", "unins000.dat")
PRESERVE_DIRS = ("lyrics", "models", "deps")


def _stat(p: Path):
    """(size, mtime) fingerprint, or None if absent. Used to prove the deploy did
    not touch user data — `.exists()` cannot, because an overwritten file still
    exists, which is how a 201 KB log being replaced by a 0-byte one passed a
    green check."""
    try:
        s = p.stat()
        return (s.st_size, int(s.st_mtime))
    except OSError:
        return None


def robocopy(src: Path, dst: Path, *flags: str, dry: bool = False) -> None:
    cmd = ["robocopy", str(src), str(dst), *flags, "/NFL", "/NDL", "/NJH", "/NJS", "/NP", "/R:2", "/W:2"]
    if dry:
        print("   DRY  " + " ".join(cmd[:5]) + " …")
        return
    rc = subprocess.run(cmd, capture_output=True, text=True).returncode
    # robocopy: 0 nothing to do, 1 copied, 2 extras, 3 = 1|2, … >=8 is a REAL error.
    if rc >= 8:
        raise SystemExit(f"[FAIL] robocopy {src} -> {dst} returned {rc} (>=8 is an error)")
    print(f"   ok   {src.name or src} -> {dst}  (robocopy rc={rc})")


def stop_processes(target: Path, dry: bool = False) -> None:
    """Kill anything running from the target tree, then verify it is gone."""
    # NB: PowerShell SINGLE-quoted strings do not treat backslash as an escape, so
    # the path goes in verbatim — only `'` needs doubling. An earlier version
    # escaped the backslashes as well, producing a literal `d:\\desktopkaraoke`
    # that matched nothing: the script cheerfully reported "nothing running" while
    # the overlay and console were both live, which is the precise false negative
    # this function exists to prevent.
    # Trailing separator matters: a bare prefix would also match a SIBLING whose
    # name merely starts with the same text (D:\DesktopKaraoke-old, D:\DesktopKaraoke2)
    # and force-kill processes that have nothing to do with this install.
    t = str(target).lower().rstrip("\\") + "\\"
    t = t.replace("'", "''")
    ps = (
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.ExecutablePath -and "
        f"$_.ExecutablePath.ToLower().StartsWith('{t}') }} | "
        "Select-Object ProcessId, ExecutablePath | ConvertTo-Json -Compress"
    )
    r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       capture_output=True, text=True)
    # A failed query returns empty stdout, which is indistinguishable from "no
    # processes" — the exact false negative this function exists to prevent, and
    # one it has already produced once (a bad quoting fix made every lookup return
    # nothing while the overlay and console were both running). Treat a broken
    # query as fatal rather than as good news.
    if r.returncode != 0 or r.stderr.strip():
        raise SystemExit("[FAIL] could not enumerate processes "
                         f"(rc={r.returncode}): {r.stderr.strip()[:200]}\n"
                         "       Refusing to deploy — copying over a running exe "
                         "half-succeeds.")
    out = r.stdout.strip()
    if not out or out == "null":
        print("   ok   nothing running from the target")
        return
    import json
    procs = json.loads(out)
    if isinstance(procs, dict):
        procs = [procs]
    for p in procs:
        print(f"   stop pid {p['ProcessId']}  {Path(p['ExecutablePath']).name}")
        if not dry:
            subprocess.run(["taskkill", "/PID", str(p["ProcessId"]), "/F", "/T"],
                           capture_output=True)
    if dry:
        return
    time.sleep(2.0)
    again = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, text=True).stdout.strip()
    if again and again != "null":
        raise SystemExit("[FAIL] a process is still running from the target — "
                         "copying over it would half-succeed. Close it and retry.")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--target", default=r"D:\DesktopKaraoke")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    dst = Path(a.target)

    if os.name != "nt":
        print("[skip] Windows-only deploy")
        return 0
    if not (SRC / "Lyric-Immersion-and-Karaoke.exe").is_file():
        raise SystemExit(f"[FAIL] no build at {SRC} — run the PyInstaller build first")
    if not dst.is_dir():
        raise SystemExit(f"[FAIL] target {dst} does not exist")

    print(f"deploy {SRC}\n    -> {dst}\n")

    # Fingerprint user data BEFORE touching anything, so step 5 can prove it was
    # not overwritten rather than merely still present.
    pre = {p: _stat(dst / p) for p in PRESERVE_FILES + PRESERVE_DIRS}
    clash = [p for p in PRESERVE_FILES if (SRC / p).exists()]
    if clash:
        print(f"   note the build folder also contains {clash} — these are excluded\n"
              f"        from the copy (the app writes them next to its own exe).\n")

    print("1. stopping processes running from the target")
    stop_processes(dst, a.dry_run)

    print("2. mirroring _internal (pure build output — safe to purge)")
    robocopy(SRC / "_internal", dst / "_internal", "/MIR", dry=a.dry_run)

    print("3. copying top level WITHOUT purge, excluding user data")
    robocopy(SRC, dst, "/E",
             "/XD", "_internal", *PRESERVE_DIRS,
             "/XF", *PRESERVE_FILES,
             dry=a.dry_run)

    print("4. reconciling the sibling dev-console fallback copy")
    sib_src, sib_dst = SRC / "_internal" / "dev-console", dst / "dev-console"
    if sib_src.is_dir():
        # `/E` creates the destination if absent, so do NOT require it to exist —
        # requiring both was how a two-week-old console survived every deploy
        # (TICKET-198). `_dev_console_exe()` lists this directory as a fallback, so
        # a stale exe here is one path resolution away from being the one you run.
        robocopy(sib_src, sib_dst, "/E", dry=a.dry_run)
    elif sib_dst.is_dir():
        # The build has no console but the target still holds one: that copy can
        # only be stale, and stale is worse than missing.
        print(f"   WARN this build ships no dev-console, but {sib_dst} still holds")
        print( "        one. It is stale by definition — delete it or rebuild the")
        print( "        console, or you may end up running a version nobody tracks.")
    else:
        print("   skip  no sibling copy on either side")

    print("\n5. verifying preserved data survived")
    # Checking `.exists()` is not enough — an overwritten file still exists. That
    # is precisely how the log clobber went unnoticed. Compare against the
    # fingerprint taken BEFORE the copy: same size and mtime, or it was touched.
    bad = []
    for p, before in pre.items():
        now = _stat(dst / p)
        if before is None:
            continue                      # wasn't there to begin with
        if now is None:
            bad.append(f"{p}: DELETED")
        elif now != before:
            bad.append(f"{p}: OVERWRITTEN (was size={before[0]}, now size={now[0]})")
        else:
            print(f"   ok   {p} untouched")
    if bad:
        raise SystemExit("[FAIL] user data was modified by the deploy:\n       "
                         + "\n       ".join(bad))

    print("\n[ok] deployed." if not a.dry_run else "\n[ok] dry run complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
