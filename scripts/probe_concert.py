"""Exercise main.App.get_concert and the concert-verdict explainer (TICKET-218).

Same technique as probe_insight.py: pull the functions out with `ast` and exec
just those, so main.py is never imported (importing it builds a Tk app).

Two things are being protected here.

1. `get_concert` is a DIAGNOSTICS accessor, so the failure that matters is not
   "wrong number" but "raised, and took /concert down with it". Every case below
   therefore runs against a deliberately hostile stub: missing attributes, a
   half-initialised engine, chapters in the legacy tuple shape, and a plan entry
   with a null onset.

2. TICKET-215 was a shape bug that produced an EMPTY list rather than an error,
   and the console rendered that as "no setlist parsed for this video" — a
   plausible-looking lie. So the chapter assertions check for real content, not
   merely for "did not raise".
"""
import ast
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

SRC = Path(r"D:\Desktop-Karaoke\main.py")
tree = ast.parse(SRC.read_text(encoding="utf-8-sig"))

WANT = {"get_concert", "_chapter_fields", "explain_live_or_compilation",
        "is_live_or_compilation"}
found = {}
for node in ast.walk(tree):
    if isinstance(node, ast.FunctionDef) and node.name in WANT and node.name not in found:
        found[node.name] = node
missing = WANT - set(found)
assert not missing, f"not found in main.py: {sorted(missing)}"

# The regexes and helpers the explainer closes over, lifted from main.py by name
# so this probe tests the REAL patterns rather than a copy that can drift.
NEEDED_NAMES = ["_LOOP_VER_RE", "_LIVE_RE", "_GENERIC_TITLE_RE", "_FROM_EVENT_RE"]
NEEDED_FNS = ["_live_cue_is_parenthetical_aside", "_has_single_song_at_event",
              "_is_generic_title", "_norm_title"]
body = []
for node in tree.body:
    if isinstance(node, ast.Assign) and any(
            isinstance(tgt, ast.Name) and tgt.id in NEEDED_NAMES for tgt in node.targets):
        body.append(node)
    elif isinstance(node, ast.FunctionDef) and node.name in NEEDED_FNS:
        body.append(node)
body += [found["_chapter_fields"], found["explain_live_or_compilation"],
         found["is_live_or_compilation"], found["get_concert"]]

mod = ast.Module(body=body, type_ignores=[])
ast.fix_missing_locations(mod)
ns = {"re": __import__("re"), "time": __import__("time"),
      "unicodedata": __import__("unicodedata"),
      "_s": lambda v, n=40: (str(v)[:n] if v else ""),
      "log": type("L", (), {"info": staticmethod(lambda *a, **k: None)})()}
exec(compile(mod, str(SRC), "exec"), ns)
get_concert = ns["get_concert"]
explain = ns["explain_live_or_compilation"]
chapter_fields = ns["_chapter_fields"]

# The REAL skip set, parsed out of the class body rather than retyped — a copy
# would let the probe pass while the engine used a different set.
SKIP = None
for node in ast.walk(tree):
    if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "_SETLIST_SKIP" for t in node.targets):
        SKIP = ast.literal_eval(node.value)
        break
assert SKIP and "mc" in SKIP and "intermission" in SKIP, "could not read _SETLIST_SKIP"


class Media:
    def __init__(self, d): self._d = d
    def get(self): return self._d


class Stub:
    """Only what get_concert reads. Deliberately MINIMAL: anything the method
    touches that is not defined here must be reached through getattr with a
    default, which is the property we want to hold."""
    _SETLIST_SKIP = SKIP

    def __init__(self, **over):
        self.media = Media({"position": 1000.0})
        self.lines = [object()] * 20
        self._aligning = False
        self.concert_ocr = True
        self._tune = {
            "applause_min_s": 2.5, "mv_intro_timeout": 20.0, "concert_setlist_on": 1,
            "ocr_setlist_gate": 1, "setlist_gen_deadline_s": 45.0,
            "chapter_override_min_score": 0.70, "chapter_no_song_reject": 0,
            "concert_pool_scoped": 1, "concert_pool_prefetch_max": 20,
            "concert_audio_on": 1, "concert_audio_identify": 1,
            "concert_audio_max_dur_s": 4800, "concert_audio_min_song_s": 45.0,
            "concert_audio_floor_frac": 0.40, "concert_audio_id_slice_s": 12.0,
            "agree_live": 4.0, "live_max_jump_s": 45.0, "live_song_max_s": 900.0,
            "concert_tpvr_gap_s": 1.2, "live_tpvr_gap_s": 2.5, "tpvr_gap_s": 1.5,
            "concert_single_shot_max_s": 1.8, "live_single_shot_max_s": 1.2,
            "single_shot_max_s": 2.0, "concert_first_read_max_s": 1.8,
            "sync_live_follow_alpha": 0.35, "live_resync_listen_s": 4.0,
            "live_resync_fast_gap_s": 1.0, "live_resync_mid_gap_s": 6.0,
            "live_resync_slow_gap_s": 14.0, "live_resync_relax_n": 3,
            "live_energy_apply_min": 0.15, "live_energy_lift_floor": 0.025,
            "live_energy_peak_margin": 0.035, "live_sync_match_min": 0.62,
            "energy_max_offset_live": 120.0, "ocr_sync_in_live": 1,
            "ocr_sync_min": 0.66, "ocr_sync_min_live": 0.58,
            "ocr_sync_single_shot_max": 4.0, "ocr_when_gaming": 0,
            "onset_max_intro_s": 90.0,
        }
        self._live_mode = True
        self._live_arrangement = False
        self._live_arr_why = ""
        self._live_why = {"rule": "duration", "detail": "43.4 min is over the 10 min concert threshold",
                          "by": "title+duration", "title": "Some Concert", "duration": 2604.0,
                          "at": 1.0}
        self._setlist_idx = 1
        for k, v in over.items():
            setattr(self, k, v)


def run(label, stub):
    out = get_concert(stub)
    json.dumps(out)              # must survive the HTTP layer
    print(f"--- {label}: mode={out['mode']!r} chapters={len(out['chapters'])} "
          f"mc={len(out['mc_segments'])} plan={len(out['plan'])} "
          f"applause={out['applause']['progress']}")
    return out


# ── 1. the bare minimum: a stub with almost nothing set must not raise ───────
o = run("empty engine", Stub(_live_mode=False, _live_why={}, _setlist_idx=None))
assert o["mode"] == "studio", o["mode"]
# An unevaluated engine must SAY it is unevaluated. An empty reason renders as a
# dash in the panel, which is indistinguishable from the provenance feature being
# broken — the exact ambiguity TICKET-215 taught us to avoid.
assert o["why"]["rule"] == "not evaluated", o["why"]
assert o["why"]["detail"], "the not-evaluated case still needs a human-readable detail"
assert o["chapters"] == [] and o["plan"] == []
assert o["applause"]["running"] is False        # not a live cut
assert o["sync_profile"] == "studio"

# ── 2. TICKET-215: chapters must actually appear, in BOTH shapes ─────────────
DICTS = [{"start": 0.0, "title": "Opening"}, {"start": 61.5, "title": "Real Song"},
         {"start": 240.0, "title": "MC"}, {"start": 400.0, "title": "Another Song"},
         {"start": 900.0, "title": "Intermission"}, {"start": 1000.0, "title": "アンコール"}]
o = run("dict chapters", Stub(_concert_setlist=DICTS))
assert len(o["chapters"]) == 6, o["chapters"]
assert o["chapters"][1]["title"] == "Real Song" and o["chapters"][1]["skip"] is False
# Opening / MC / Intermission / アンコール are all non-song segments.
assert len(o["mc_segments"]) == 4, [m["title"] for m in o["mc_segments"]]
assert {m["title"] for m in o["mc_segments"]} == {"Opening", "MC", "Intermission", "アンコール"}
assert o["chapters"][1]["current"] is True, "chapter_idx 1 must be flagged current"

TUPLES = [(0.0, "Opening"), (61.5, "Real Song"), (240.0, "MC")]
o = run("legacy tuple chapters", Stub(_concert_setlist=TUPLES))
assert len(o["chapters"]) == 3, o["chapters"]
assert o["chapters"][1]["title"] == "Real Song"
assert [m["title"] for m in o["mc_segments"]] == ["Opening", "MC"]

# A malformed entry must drop ITSELF, not the whole list — the exact failure
# mode of TICKET-215, where one bad entry blanked everything.
o = run("one malformed chapter", Stub(_concert_setlist=[
    {"start": 0.0, "title": "Good One"}, None, 42, {"start": 9.0, "title": "Also Good"}]))
assert len(o["chapters"]) == 2, o["chapters"]
assert [c["title"] for c in o["chapters"]] == ["Good One", "Also Good"]

# ── 3. the applause integrator ──────────────────────────────────────────────
o = run("applause mid-count", Stub(_applause_for=1.25, _applause_armed=False))
assert o["applause"]["progress"] == 0.5, o["applause"]["progress"]   # 1.25 / 2.5
assert o["applause"]["armed"] is False
assert o["applause"]["running"] is True
assert o["applause"]["on_gap"] == "re-identify the next song", o["applause"]["on_gap"]

o = run("applause armed, live arrangement", Stub(
    _live_mode=False, _live_arrangement=True, _applause_for=9.0, _applause_armed=True))
assert o["applause"]["progress"] == 1.0, "progress must clamp at 1.0"
assert o["applause"]["on_gap"] == "two-point resync by ear", o["applause"]["on_gap"]
assert o["mode"] == "live arrangement" and o["sync_profile"] == "live"

# TICKET-214: a completed gap must survive the reset and be reportable.
o = run("last gap reported", Stub(_applause_last_gap_s=4.75, _applause_gaps=3,
                                  _applause_last_action="re-identify the next song"))
assert o["applause"]["last_gap_s"] == 4.75, o["applause"]["last_gap_s"]
assert o["applause"]["gaps_this_run"] == 3

# ── 4. OCR suppression must be EXPLAINED, not merely off ────────────────────
o = run("ocr blocked by chapters", Stub(_concert_setlist=DICTS))
assert o["ocr"]["running"] is False
assert "chapters are present" in o["ocr"]["blocked_because"], o["ocr"]["blocked_because"]

o = run("ocr running", Stub())
assert o["ocr"]["running"] is True and o["ocr"]["blocked_because"] == ""

o = run("ocr switched off", Stub(concert_ocr=False))
assert o["ocr"]["running"] is False
assert "switched off" in o["ocr"]["blocked_because"], o["ocr"]["blocked_because"]

# ── 5. the offline plan, including a null onset ─────────────────────────────
PLAN = [{"start": 0.0, "end": 200.0, "onset": 12.5, "title": "One", "artist": "A",
         "source": "fingerprint", "id_conf": 0.91},
        {"start": 200.0, "end": 1200.0, "onset": None, "title": "Two", "artist": "",
         "source": "chapter", "id_conf": 0.0}]
o = run("plan with a null onset", Stub(_concert_plan=PLAN))
assert len(o["plan"]) == 2, o["plan"]
assert o["plan"][0]["onset"] == 12.5 and o["plan"][1]["onset"] is None
assert o["plan_current"] == 1, o["plan_current"]        # position 1000 lands in seg 2

# ── 6. knob groups must be populated and grouped ────────────────────────────
o = run("knobs", Stub())
assert "Applause detection" in o["knobs"] and "Live sync thresholds" in o["knobs"]
assert o["knobs"]["Applause detection"][0]["key"] == "applause_min_s"
assert o["knobs"]["Applause detection"][0]["value"] == 2.5
_all = [r["key"] for g in o["knobs"].values() for r in g]
assert len(_all) == len(set(_all)), "a knob is listed in two groups"
# A knob absent from _tune must be omitted, not rendered as a null row.
o = run("knob absent from tune", Stub(_tune={"applause_min_s": 2.5}))
assert list(o["knobs"]) == ["Applause detection"], o["knobs"]

# ── 7. the verdict explainer, and its agreement with the bool wrapper ───────
CASES = [
    ("Some Band LIVE TOUR 2024 Full Concert", 3600, True, "duration"),
    ("Some Band ワンマンライブ", None, True, "keyword"),
    ("Seamless 30min Ver", 3600, False, "loop-veto"),
    ("作業用BGM 3時間耐久", 10800, False, "loop-veto"),
    ("Just A Normal Song", 210, False, "no-cue"),
    ("Just A Normal Song", None, False, "no-cue"),
]
print("--- verdict explainer")
for title, dur, want_v, want_rule in CASES:
    v, rule, detail = explain(title, dur)
    print(f"    {want_rule:18} {v!s:5} {title[:44]!r}  {detail[:60]}")
    assert v is want_v, f"{title!r}: verdict {v}, wanted {want_v}"
    assert rule == want_rule, f"{title!r}: rule {rule!r}, wanted {want_rule!r}"
    assert detail, f"{title!r}: empty detail"
    # The wrapper must never disagree with the explainer it delegates to.
    assert ns["is_live_or_compilation"](title, dur) is v, f"{title!r}: wrapper disagrees"

# The loop veto must beat the duration rule, which is the whole point of it
# being checked first — a 3-hour loop of one song is not a concert.
assert explain("Seamless 30min Ver", 3600)[0] is False
assert explain("耐久", 99999)[1] == "loop-veto"

# ── 8. every verdict path must leave a usable reason ────────────────────────
for rule in ("subs-demote", "category-flip", "nonmusic-demote"):
    o = get_concert(Stub(_live_why={"rule": rule, "detail": "x", "by": "y", "at": 1.0}))
    assert o["why"]["rule"] == rule
    assert "at" not in o["why"], "wall-clock must be stripped from the UI payload"

# ── 9. the cover surprise must be stated, not implied ───────────────────────
o = run("cover counts as live arrangement", Stub(
    _live_mode=False, _live_arrangement=True,
    _live_arr_why="this is a COVER — covers follow the offset like a live take, "
                  "even with no live cue in the title"))
assert "COVER" in o["live_arrangement_why"], o["live_arrangement_why"]

print("\nALL PROBES PASSED")
