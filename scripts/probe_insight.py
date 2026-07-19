"""Exercise main.App.get_insight against a duck-typed stub.

Extracts the method source with `ast` and execs just that function, so main.py is
never imported (importing it would build a Tk app). Verifies the TICKET-194 `now`
block is present, JSON-serialisable and correctly shaped.
"""
import ast, json, sys, types
from pathlib import Path

SRC = Path(r"D:\Desktop-Karaoke\main.py")
tree = ast.parse(SRC.read_text(encoding="utf-8-sig"))

fn = None
for node in ast.walk(tree):
    if isinstance(node, ast.FunctionDef) and node.name == "get_insight":
        fn = node
        break
assert fn is not None, "get_insight not found"

mod = ast.Module(body=[fn], type_ignores=[])
ast.fix_missing_locations(mod)
ns = {"time": __import__("time"), "PLAYING": 4, "Path": Path}
exec(compile(mod, str(SRC), "exec"), ns)
get_insight = ns["get_insight"]


# The REAL line type, copied from main.py. The first version of this probe used
# plain dicts, which quietly encoded my own wrong assumption about self.lines —
# so `isinstance(ln, dict)` looked correct here and was dead in production. A stub
# that does not match the real type tests nothing.
import dataclasses


@dataclasses.dataclass
class Line:
    start: float
    end: float
    jp: str = ""
    rm: str = ""
    en: str = ""


class Media:
    def __init__(self, d): self._d = d
    def get(self): return self._d


class Index:
    entries = [{"title": "Some Song"}]
    def match(self, *a, **k): return None


class Stub:
    """Only the attributes get_insight actually reads."""
    def __init__(self, **over):
        self.media = Media({"title": "ReGLOSS - Hour Time Yellow OFFICIAL MV",
                            "artist": "hololive DEV_IS ReGLOSS",
                            "source": "brave", "playing": True,
                            "status": 4, "position": 91.5, "duration": 253.0})
        self.meta = {"title": "Hour Time Yellow", "artist": "ReGLOSS",
                     "source": "lrclib/search", "lang": "ja"}
        self._clean_title_cache = "Hour Time Yellow"
        self.lines = [Line(10.0, 12.5, "x", "y", "z")] * 40
        self.idx = 12
        self.index = Index()
        self._tune = {"ocr_setlist_gate": 1, "sync_tier_ok_drift": 0.6,
                      "single_shot_max_s": 2.0, "tpvr_gap_s": 1.5,
                      "sync_apply_min_s_scroll": 0.25}
        self._track = ("ReGLOSS", "Hour Time Yellow")
        self._cur_duration = 253.0
        self._sync_events = []
        self._frame_ms = 0.0
        self.perf = "smooth"
        self.tauri_overlay_on = True
        self.offset = 0.0
        self._deciding = False
        self._aligning = False
        for k, v in over.items():
            setattr(self, k, v)

    def _same_song_title(self, a, b, *args, **kw):
        return a.lower().find(b.lower()) >= 0 or b.lower().find(a.lower()) >= 0

    def _subs_on(self): return False

    def get_title_identification(self):
        # TICKET-204. Shape-only stub: get_insight just embeds the result, so the
        # probe asserts it is present and serialisable, not what it contains.
        return {"raw_title": "", "clean_title": "", "search_title": "",
                "artist": "", "overridden": False, "override_title": None,
                "seen_strings": []}

    def _autoresearch_state(self): return {"exists": False}


def run(label, stub):
    out = get_insight(stub)
    json.dumps(out)                      # must be serialisable for the HTTP layer
    n = out["now"]
    print(f"--- {label}")
    for k in ("player_title", "search_title", "loaded_title", "agree", "evidence",
              "idx", "line_count", "overlay", "renderer", "render_fps", "busy",
              "position", "playing"):
        print(f"    {k:14} {n[k]!r}")
    return out


o = run("normal playback (titles agree)", Stub())
assert o["now"]["agree"] == "match", o["now"]["agree"]
assert o["now"]["overlay"] == "lyrics"
assert o["now"]["renderer"] == "gpu"
assert o["now"]["render_fps"] is None, "gpu overlay must not report a fake tk fps"
assert o["now"]["line_t"] == [10.0, 12.5], o["now"]["line_t"]
assert o["now"]["has_romaji"] and o["now"]["has_english"], (o["now"]["has_romaji"], o["now"]["has_english"])

# a body with NO romaji/english layer must report false, not crash
o = run("no romaji/english layers", Stub(lines=[Line(1.0, 2.0, "x", "", "")], idx=0))
assert o["now"]["has_romaji"] is False and o["now"]["has_english"] is False
assert o["now"]["line_t"] == [1.0, 2.0]

o = run("wrong lyrics loaded", Stub(meta={"title": "Unchained", "artist": "hololive EN",
                                          "source": "lrclib", "lang": "ja"}))
assert o["now"]["agree"] == "mismatch", o["now"]["agree"]

o = run("nothing loaded", Stub(meta={}, lines=[], idx=-1))
assert o["now"]["agree"] == "none"
assert o["now"]["overlay"] == "idle"
assert o["now"]["line_count"] == 0
assert o["now"]["idx"] == -1

o = run("busy deciding by ear", Stub(_deciding=True))
assert o["now"]["busy"] == "deciding by ear"

o = run("tk renderer, real fps", Stub(tauri_overlay_on=False, _frame_ms=16.0))
assert o["now"]["renderer"] == "tk" and o["now"]["render_fps"] == 62

# idx out of range must not raise or index past the end
o = run("idx past end of body", Stub(idx=999))
assert o["now"]["idx"] == -1 and o["now"]["overlay"] == "idle"

# ── TICKET-200: the evidence ladder ──────────────────────────────────────────
# The whole point is that `agree` and `evidence` are INDEPENDENT. A body fetched
# by title searching is filed under the title it was searched with, so it always
# agrees with itself; the wrong-song case that prompted this reported agree ==
# "match" with no evidence whatsoever behind it. If these two ever collapse into
# one signal again, the console goes back to showing full confidence for a body
# nothing has checked.
o = run("title-only body (agrees, but unproven)", Stub())
assert o["now"]["agree"] == "match", o["now"]["agree"]
assert o["now"]["evidence"] == "title", o["now"]["evidence"]

o = run("timing corroborated", Stub(_body_corroborated=True))
assert o["now"]["evidence"] == "timing", o["now"]["evidence"]

# word verification outranks a timing lock — an energy lock proves WHEN, not WHAT
o = run("words verified", Stub(_body_corroborated=True, _body_word_verified=True))
assert o["now"]["evidence"] == "words", o["now"]["evidence"]

o = run("bundled library body", Stub(meta={"title": "Hour Time Yellow", "artist": "ReGLOSS",
                                           "source": "bundled", "lang": "ja"}))
assert o["now"]["evidence"] == "library", o["now"]["evidence"]

o = run("no body at all", Stub(meta={}, lines=[], idx=-1))
assert o["now"]["evidence"] == "none", o["now"]["evidence"]

# The searched title must be published even when it differs sharply from the
# player's — that difference IS the TICKET-200 diagnosis.
o = run("reduction ate the song name", Stub(
    _clean_title_cache="IA & ONE",
    meta={"title": "IA & ONE", "artist": "IA", "source": "syncedlyrics", "lang": "ja"}))
assert o["now"]["search_title"] == "IA & ONE", o["now"]["search_title"]
assert o["now"]["evidence"] == "title", o["now"]["evidence"]

print("\nALL PROBES PASSED")
