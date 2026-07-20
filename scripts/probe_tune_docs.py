"""TICKET-212 — every tunable knob has documentation, and every doc has a knob.

The dev console exposes ~230 runtime parameters. A knob with no doc renders as a
bare key name and a number, which is the state this ticket existed to fix; a doc
with no knob is a lie that survives because nothing contradicts it. Both drift
silently, because neither breaks anything at runtime.

So the 1:1 mapping is enforced here rather than trusted:

  * parse `Overlay._tune` out of main.py with `ast` (no import — importing main
    builds a Tk app), and
  * compare it against `tune_docs.TUNE_DOC`.

Adding a knob without a doc line now fails the build instead of quietly shipping
an undocumented control.

Also checks the properties the console's tooltip rendering depends on: ASCII
only (so a tooltip, a terminal and a log all show the same thing) and enough
text to actually be a description rather than a restated key name.

    python scripts/probe_tune_docs.py      # exits non-zero on any mismatch
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

MIN_LEN = 150          # shorter than this is a label, not documentation
MAX_LEN = 700

src = (ROOT / "main.py").read_text(encoding="utf-8-sig")
lines = src.split("\n")

# ── pull the knob names straight out of the tune dict ─────────────────────────
try:
    start = next(i for i, l in enumerate(lines) if l.strip().startswith("self._tune = {"))
except StopIteration:
    print("FAIL: could not find `self._tune = {` in main.py")
    sys.exit(1)

depth, end = 0, None
for i in range(start, len(lines)):
    depth += lines[i].count("{") - lines[i].count("}")
    if depth == 0 and i > start:
        end = i
        break
if end is None:
    print("FAIL: `self._tune` dict is not closed")
    sys.exit(1)

kpat = re.compile(r'^\s*"([a-z0-9_]+)"\s*:')
knobs = {m.group(1) for m in (kpat.match(l) for l in lines[start:end + 1]) if m}

try:
    from tune_docs import TUNE_DOC
except Exception as e:
    print(f"FAIL: cannot import tune_docs: {e}")
    sys.exit(1)

print(f"main.py tune dict : {len(knobs)} knobs  (main.py:{start + 1}..{end + 1})")
print(f"tune_docs.TUNE_DOC: {len(TUNE_DOC)} entries\n")

problems = []

missing = sorted(knobs - set(TUNE_DOC))
if missing:
    problems.append(f"{len(missing)} knob(s) with NO documentation: {missing}")

orphan = sorted(set(TUNE_DOC) - knobs)
if orphan:
    problems.append(f"{len(orphan)} doc(s) for a knob that no longer exists: {orphan}")

for k, v in sorted(TUNE_DOC.items()):
    if not isinstance(v, str):
        problems.append(f"{k}: doc is {type(v).__name__}, expected str")
        continue
    bad = [c for c in v if ord(c) > 127]
    if bad:
        problems.append(f"{k}: non-ASCII character(s) {sorted(set(bad))!r} in the doc")
    if len(v) < MIN_LEN:
        problems.append(f"{k}: doc is only {len(v)} chars (min {MIN_LEN}) — not a description")
    if len(v) > MAX_LEN:
        problems.append(f"{k}: doc is {len(v)} chars (max {MAX_LEN})")
    if "\n" in v:
        problems.append(f"{k}: doc contains a newline (tooltips are single-paragraph)")

if problems:
    print(f"FAILED — {len(problems)} problem(s):")
    for p in problems[:40]:
        print("  ! " + p)
    if len(problems) > 40:
        print(f"  ... and {len(problems) - 40} more")
    sys.exit(1)

print(f"OK — all {len(knobs)} knobs documented, 1:1, ASCII, {MIN_LEN}..{MAX_LEN} chars.")
