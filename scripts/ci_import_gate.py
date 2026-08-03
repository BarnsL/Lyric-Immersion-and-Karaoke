"""Cross-platform import gate (PORTING.md Phase 0).

Every module below must IMPORT WITHOUT RAISING on every OS. Windows-only
functionality is expected to degrade behind its guards/lazy imports, but the
import itself must always succeed — that is the invariant that keeps the
Linux/macOS ports startable. Run from the repo root:

    python scripts/ci_import_gate.py

Exit 0 when every module imports; exit 1 with a per-module report otherwise.
"""
import importlib
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

MODULES = [
    # pure engine
    "version", "appdata", "updater", "metrics", "confidence", "gairaigo",
    "fetch_lyrics", "align", "songchange", "recognize",
    "llm_disambiguate", "deep_transcribe", "api",
    # windows-functionality modules — must import (and no-op) everywhere
    "audible_sessions", "window_titles", "discord_rpc",
    "concert_ocr", "ocr_lyrics", "gpu_setup",
    # UI stack
    "character",
    # the whole app
    "main",
]
if sys.platform == "linux":
    MODULES.append("media_mpris")     # dbus-next is a linux-marked requirement

failures = []
for name in MODULES:
    try:
        importlib.import_module(name)
        print(f"  ok    {name}")
    except Exception:
        print(f"  FAIL  {name}")
        failures.append((name, traceback.format_exc(limit=6)))

print()
if failures:
    for name, tb in failures:
        print(f"── {name} " + "─" * max(1, 60 - len(name)))
        print(tb)
    print(f"IMPORT GATE: FAIL ({len(failures)}/{len(MODULES)} modules)")
    sys.exit(1)
print(f"IMPORT GATE: PASS ({len(MODULES)} modules on {sys.platform})")
