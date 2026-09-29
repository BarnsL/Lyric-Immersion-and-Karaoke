"""
Scan the cached lyrics library for likely-wrong entries and (optionally)
purge them so they get re-fetched correctly next time the song plays.

Detects: language mismatches (e.g. a Japanese title with non-Japanese
lyrics), empty/too-short files, and unreadable JSON.

    python scripts/validate.py            # report only
    python scripts/validate.py --purge    # delete the bad ones
"""

import os
import sys

# The script lives in scripts/, and fetch_lyrics is in the repo root. Put the
# root on the path so the AGENTS.md command `python scripts/validate.py` works
# from a clean checkout (as ci_import_gate.py does).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fetch_lyrics import validate_file, LYRICS_DIR  # noqa: E402


def main():
    purge = "--purge" in sys.argv
    files = sorted(LYRICS_DIR.glob("*.json"))
    bad = 0
    for p in files:
        ok, reason = validate_file(p)
        if not ok:
            bad += 1
            print(f"  BAD  {p.name}: {reason}")
            if purge:
                p.unlink(missing_ok=True)
    print(f"\n{len(files)} files checked, {bad} suspect"
          + (" (purged)" if purge else ""))
    if bad and not purge:
        print("Re-run with --purge to remove them; they'll re-fetch on next play.")


if __name__ == "__main__":
    main()
