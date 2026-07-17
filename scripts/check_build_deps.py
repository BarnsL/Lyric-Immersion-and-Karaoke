"""Pre-build consistency guard for the bundled AI (faster-whisper) stack.

TICKET-175. The spec bundles the optional "sync/generate by ear" stack
(faster-whisper + ctranslate2 + PyAV + tokenizers) from a vendored `.deps`
folder (`pip install --target .deps faster-whisper`), with `pathex=[".deps"]`.
PyInstaller's `collect_all("av")` then searches BOTH `.deps` and the active
environment — so if `.deps` holds one PyAV version and the environment holds
another, a version-SKEWED mix of Python modules + FFmpeg DLLs gets bundled and
`import av` (hence `import faster_whisper`, hence `align.available()`) dies at
runtime with an `av._core` error. That silently disabled generate-by-ear,
sync-by-listening AND the wrong-lyrics reject path in every shipped build from
v1.1.74 to v1.1.76 — the app just showed "needs faster-whisper" hints and every
listen feature degraded, with nothing in the log.

This runs BEFORE PyInstaller. It FAILS the build (exit 1) only on UNAMBIGUOUS
corruption — duplicate dist-info dirs (two versions of one package present). A
plain version difference vs the env is a WARNING, because *.dist-info metadata
can lag the actual module files (a manual copy of just av/ updates the .pyd but
leaves the old dist-info), so a hard error there could block a build whose
bundled module is fine. The POST-BUILD `--selftest` smoke test is the definitive
gate — it imports the real frozen module and fails the build if it's broken.

Run: python scripts/check_build_deps.py   (build.bat calls it in step 1)
"""
from __future__ import annotations

import glob
import os
import re
import sys
from importlib import metadata as im

# The heavy native stack that must be VERSION-CONSISTENT between `.deps` and the
# build environment (each ships compiled extensions / DLLs that must match).
STACK = ["av", "ctranslate2", "faster-whisper", "tokenizers"]
_IMPORT_NAME = {"faster-whisper": "faster_whisper"}

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEPS = os.path.join(REPO, ".deps")


def _deps_versions(pkg: str) -> list[str]:
    """ALL versions of `pkg` vendored in `.deps`, from its *.dist-info dir names.
    Returns a list because `pip install --upgrade --target` LEAVES the old
    dist-info behind next to the new one — so two versions can coexist and the
    module files are whichever pip wrote last. More than one entry here means
    `.deps` is corrupt and must be rebuilt from scratch (rmdir then reinstall)."""
    norm = pkg.replace("-", "_").lower()
    found = set()
    for pat in (f"{pkg}-*.dist-info", f"{norm}-*.dist-info"):
        for d in glob.glob(os.path.join(DEPS, pat)):
            base = os.path.basename(d)[: -len(".dist-info")]
            if "-" in base:
                found.add(base.split("-", 1)[1])
    return sorted(found)


def _env_version(pkg: str) -> str | None:
    for name in (pkg, _IMPORT_NAME.get(pkg, pkg)):
        try:
            return im.version(name)
        except Exception:
            continue
    return None


def main() -> int:
    lean = os.environ.get("LEAN_BUILD") == "1"
    if not os.path.isdir(DEPS):
        if lean:
            print("[deps-check] LEAN_BUILD=1 and no .deps — building WITHOUT the AI stack (ok).")
            return 0
        print("[deps-check] WARNING: no ./.deps folder — this will be a LEAN build with NO\n"
              "             generate-by-ear / sync-by-listening / wrong-lyrics-reject. To ship\n"
              "             the full app, vendor the stack first:\n"
              "                 pip install --target .deps faster-whisper\n"
              "             (set LEAN_BUILD=1 to silence this and build lean on purpose.)")
        return 0  # a lean build is a valid choice, just a loud one

    skews, missing, dupes = [], [], []
    for pkg in STACK:
        dvs, ev = _deps_versions(pkg), _env_version(pkg)
        if not dvs:
            missing.append(f"{pkg}: not in .deps")
            continue
        if len(dvs) > 1:
            dupes.append(f"{pkg}: {', '.join(dvs)} all present in .deps")
            continue
        dv = dvs[0]
        if ev is None:
            # in .deps but not importable in the build env — fine, it ships from .deps
            print(f"[deps-check] {pkg}: .deps={dv} (not installed in build env — ok)")
            continue
        if dv != ev:
            skews.append(f"{pkg}: .deps={dv}  vs  build-env={ev}")
        else:
            print(f"[deps-check] {pkg}: {dv} (consistent)")

    if dupes:
        print("\n[deps-check] ERROR — DUPLICATE versions in .deps (a `pip install --upgrade\n"
              "             --target` left stale dist-info behind — the bundle is a coin-flip):")
        for d in dupes:
            print("    " + d)
        print("\n  Rebuild .deps from scratch:\n"
              "      rmdir /s /q .deps  &&  pip install --target .deps faster-whisper\n")
        return 1

    if missing:
        print("[deps-check] NOTE: " + "; ".join(missing)
              + " — collect_all will source these from the build environment.")

    if skews:
        # WARN, don't fail: this compares *.dist-info metadata, which can go stale
        # vs the actual module files (a manual robocopy of just av/ updates the .pyd
        # but leaves the old dist-info) — so a hard error here could block a build
        # whose bundled module is actually fine. The POST-BUILD `--selftest` is the
        # definitive gate: it imports the real frozen module and fails the build if
        # it's broken. This is the early heads-up.
        print("\n[deps-check] WARNING (TICKET-177) — .deps dist-info versions differ from the build env:")
        for s in skews:
            print("    " + s)
        print("  If the post-build --selftest fails, rebuild .deps clean:\n"
              "      rmdir /s /q .deps  &&  pip install --target .deps faster-whisper\n"
              "  (dist-info can also just be stale after a partial copy — --selftest is authoritative.)")
        return 0

    print("[deps-check] OK — .deps and the build environment agree on the AI stack.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
