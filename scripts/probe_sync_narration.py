"""TICKET-205 — prove every sync correction narrates its REAL outcome.

The bug this guards against: `_note_event` was called from the sync CALLERS, and
only 3 of the 18 `_smooth_offset` call sites ever did it. A correction from the
energy correlator, the sync tier, Shazam or the fine-tuner moved the lyrics on
screen and told the console nothing. Narration now lives in the funnel
(`_smooth_offset` + the deferred-commit site), so a caller cannot forget.

Two kinds of check here, and the second is the one with a long shelf life:

  1. BEHAVIOUR — exec the real `_smooth_offset` / `_note_sync` / `_commit_offset`
     against a stub and assert what lands in the narrative ring for an applied
     nudge, a big jump, a deferred commit, and a sub-deadband discard.

  2. COVERAGE — every `reason=` string passed to `_smooth_offset` anywhere in
     main.py has an entry in `_SYNC_CAUSE`, and every `_SYNC_CAUSE` entry is
     still reachable. This is what stops the regression from coming back: add a
     new sync path without a cause phrase and this fails, instead of the path
     silently narrating a raw machine tag at the user.

Extracts sources with `ast` so main.py is never imported (importing it builds a
Tk app). Exits non-zero on any failure.
"""
import ast
import sys
import time
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "main.py"
TEXT = SRC.read_text(encoding="utf-8-sig")
tree = ast.parse(TEXT)

WANT = {"_smooth_offset", "_note_sync", "_note_event", "_commit_offset", "_sync_event"}
fns = {}
cause_node = None
for node in ast.walk(tree):
    if isinstance(node, ast.FunctionDef) and node.name in WANT and node.name not in fns:
        fns[node.name] = node
    if (isinstance(node, ast.Assign) and node.targets
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "_SYNC_CAUSE"):
        cause_node = node

missing = WANT - set(fns)
assert not missing, f"not found in main.py: {sorted(missing)}"
assert cause_node is not None, "_SYNC_CAUSE not found in main.py"

ns = {"time": time, "log": type("L", (), {"info": staticmethod(lambda *a, **k: None)})()}
exec(compile(ast.fix_missing_locations(ast.Module(body=[cause_node], type_ignores=[])),
             str(SRC), "exec"), ns)
for name in ("_note_event", "_sync_event", "_commit_offset", "_note_sync", "_smooth_offset"):
    exec(compile(ast.fix_missing_locations(ast.Module(body=[fns[name]], type_ignores=[])),
                 str(SRC), "exec"), ns)

_SYNC_CAUSE = ns["_SYNC_CAUSE"]


class Media:
    def get(self): return {"title": "Some Video Title", "position": 42.0}


class Line:
    def __init__(self, start, end): self.start, self.end = start, end


class Stub:
    """Only what the funnel actually touches."""
    def __init__(self, scroll="off", **over):
        self.media = Media()
        self.offset = 0.0
        self.idx = 2
        self.lines = [Line(i * 10.0, i * 10.0 + 9.0) for i in range(8)]
        self._pending_offset = None
        self._pending_offset_t = 0.0
        self._pending_note = None
        self._ignored_streak = 0
        self._display_offset = 0.0
        self._display_offset_t = 0.0
        self._drift_integral = 0.0
        self._notable = []
        self._sync_events = []
        self._scroll = scroll
        self._tune = {}
        self.__dict__.update(over)

    def _effective_scroll(self): return self._scroll
    def _track_title(self): return "Some Video Title"

    # bind the extracted real implementations
    _note_event = ns["_note_event"]
    _sync_event = ns["_sync_event"]
    _commit_offset = ns["_commit_offset"]
    _note_sync = ns["_note_sync"]
    _smooth_offset = ns["_smooth_offset"]


FAILS = []


def check(label, cond, extra=""):
    if cond:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}  {extra}")
        FAILS.append(label)


print("TICKET-205 sync narration probe\n")
print("[1] behaviour")

# ── an applied nudge narrates, with cause + evidence + outcome ──────────────
# idx<0 forces the immediate-commit branch (no line on screen).
s = Stub(idx=-1)
s._smooth_offset(3.4, "energy-align", why="lift 0.31, score 0.88")
ev = s._notable[-1] if s._notable else {}
check("applied nudge narrates", ev.get("kind") == "sync-nudge", ev)
check("carries the mapped cause phrase",
      "audio-energy correlator" in ev.get("detail", ""), ev.get("detail"))
check("carries the caller's evidence", "lift 0.31" in ev.get("detail", ""), ev.get("detail"))
check("reports delta/to/frm", (ev.get("delta"), ev.get("to"), ev.get("frm")) == (3.4, 3.4, 0.0), ev)
check("info severity for a small move", ev.get("sev") == "info", ev.get("sev"))

# ── a big jump is a warning ────────────────────────────────────────────────
s = Stub(idx=-1)
s._smooth_offset(56.0, "ocr-sync", why="matched line 29 at 0.95 confidence")
ev = s._notable[-1]
check("big jump narrates as sync-jump", ev.get("kind") == "sync-jump", ev.get("kind"))
check("big jump is severity warn", ev.get("sev") == "warn", ev.get("sev"))

# ── a manual nudge reads as the user's own action ──────────────────────────
s = Stub(idx=-1)
s._smooth_offset(0.4, "manual-nudge")
ev = s._notable[-1]
check("manual nudge is severity good", ev.get("sev") == "good", ev.get("sev"))
check("manual nudge says 'you'", "you nudged" in ev.get("detail", ""), ev.get("detail"))

# ── an unmapped reason still narrates (falls back, never silent) ───────────
s = Stub(idx=-1)
s._smooth_offset(2.0, "some-future-path")
check("unmapped reason still narrates", len(s._notable) == 1 and
      "some-future-path" in s._notable[-1].get("detail", ""), s._notable)

# ── a DEFERRED correction does not narrate until it commits ───────────────
s = Stub(idx=2)                      # a line is on screen -> defer
s._smooth_offset(1.5, "sync-tier", why="two reads agreed")
check("deferred correction is silent while queued", s._notable == [], s._notable)
check("deferred correction stashed its narration", isinstance(s._pending_note, dict), s._pending_note)
check("deferred correction did not move the offset", s.offset == 0.0, s.offset)

# ── ...and a cancelled one never narrates at all ──────────────────────────
s2 = Stub(idx=2)
s2._smooth_offset(1.5, "sync-tier")
s2._pending_offset = None            # what the line-timing-change path does
s2._pending_note = None
check("cancelled correction never narrates", s2._notable == [], s2._notable)

# ── sub-deadband discards stay quiet until the streak trips ───────────────
s = Stub(idx=-1)
for _ in range(4):
    s.offset = 0.0
    s._smooth_offset(0.1, "fine-tune-catchup")
check("single sub-deadband discard is silent", s._notable == [], s._notable)
for _ in range(1):
    s.offset = 0.0
    s._smooth_offset(0.1, "fine-tune-catchup")
ev = s._notable[-1] if s._notable else {}
check("5th consecutive discard narrates", ev.get("kind") == "sync-ignored", s._notable)
check("discard explains the floor", "discarded each time" in ev.get("detail", ""), ev.get("detail"))

# ── an applied correction resets the streak ──────────────────────────────
s = Stub(idx=-1)
for _ in range(3):
    s.offset = 0.0
    s._smooth_offset(0.1, "fine-tune-catchup")
s.offset = 0.0
s._smooth_offset(2.0, "energy-align")
check("applied correction resets the discard streak", s._ignored_streak == 0, s._ignored_streak)

# ── the ring is capped and never raises ──────────────────────────────────
s = Stub(idx=-1, _tune={"notable_events_size": 5})
for i in range(20):
    s.offset = 0.0
    s._smooth_offset(float(i + 2), "energy-align")
check("ring honours notable_events_size", len(s._notable) == 5, len(s._notable))

print("\n[2] coverage — every _smooth_offset reason has a cause phrase")

# Every literal reason passed to _smooth_offset anywhere in main.py.
#
# Walk the AST rather than regexing the text: reasons like "sync(live)-follow"
# contain parentheses, and call sites like _smooth_offset(round(c, 2), "sync-tier")
# nest them, so any brace-counting regex silently truncates and under-reports —
# which would make this guard quietly pass while missing real call sites.
called = set()
for node in ast.walk(tree):
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == "_smooth_offset"):
        continue
    reason = None
    if len(node.args) >= 2:                       # positional: (new_off, reason, ...)
        reason = node.args[1]
    for kw in node.keywords:                      # or keyword: reason="..."
        if kw.arg == "reason":
            reason = kw.value
    if isinstance(reason, ast.Constant) and isinstance(reason.value, str) and reason.value:
        called.add(reason.value)

unmapped = sorted(c for c in called if c not in _SYNC_CAUSE)
unused = sorted(k for k in _SYNC_CAUSE if k not in called)

print(f"  {len(called)} distinct reasons at call sites, {len(_SYNC_CAUSE)} mapped")
check("no _smooth_offset reason is missing a cause phrase", not unmapped, unmapped)
check("no _SYNC_CAUSE entry is stale", not unused, unused)

print("\n[3] no caller narrates sync directly any more")
# The regression guard: a _note_event with a sync-* kind outside _note_sync means
# someone went back to narrating intent from a call site.
stray = []
for node in ast.walk(tree):
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == "_note_event"):
        continue
    first = node.args[0] if node.args else None
    if isinstance(first, ast.Constant) and str(first.value).startswith("sync-"):
        stray.append((first.value, node.lineno))
# sync-rejected is legitimately a caller-side event: it is a REFUSAL that never
# reaches _smooth_offset at all, so the funnel would never see it.
stray = [(k, n) for k, n in stray if k != "sync-rejected"]
check("no sync-* _note_event outside the funnel", not stray, stray)

print()
if FAILS:
    print(f"FAILED: {len(FAILS)} check(s) — {FAILS}")
    sys.exit(1)
print("all checks passed")
