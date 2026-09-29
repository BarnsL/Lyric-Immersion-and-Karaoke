"""
Tests for the concert runtime changes in main.py (spec 001-concert-mc-awareness).

Run:  python -m pytest tests/test_concert_sync.py -v
      (Linux CI runs it under xvfb-run, like the other tests that import main)

These drive the REAL `main.Overlay` methods in their real module context. The
engine is created with `Overlay.__new__` (no Tk app is built), given the REAL
tune-knob defaults parsed out of main.py, and only side-effecting collaborators
are stubbed (canvas, Tk root, media session, fetch/identify recorders). No
network, no audio device, no lyric text (placeholder strings only).

What each group protects:
  * MC gate      — the playhead-in-talk flag, its edges, staleness and knobs.
  * Bug A        — resync-by-listening judged the ABSOLUTE concert offset
                   (≈ -song start) and died after minute 10; stale results.
  * Bug B        — the vocal-onset release compared VIDEO time with song time.
  * Bug D        — a Shazam/decide switch in a concert wrote offset 0.0 (raw
                   video time → the new song's lyrics minutes away, blank).
  * Bug E        — a chapter skipped by the sound-lock hold was never re-run.
  * Bug F        — plan install: partial/final ordering, targeted re-anchor.
  * MC chapters  — a chapter that is mostly talk is a non-song segment.
"""

import ast
import sys
import time
from pathlib import Path

import pytest

_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import main  # noqa: E402
import align  # noqa: E402

PLAYING = main.PLAYING


def _real_tune_defaults():
    """The literal `self._tune = {...}` dict from Overlay.__init__, parsed —
    so the tests run against the shipped defaults, not copies that can drift."""
    tree = ast.parse((_ROOT / "main.py").read_text(encoding="utf-8-sig"))
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Attribute)
                and node.targets[0].attr == "_tune"
                and isinstance(node.value, ast.Dict)):
            return ast.literal_eval(node.value)
    raise AssertionError("tune dict not found in main.py")


TUNE = _real_tune_defaults()


class Media:
    def __init__(self, pos=0.0, status=PLAYING, dur=3600.0):
        self.state = {"status": status, "position": pos, "duration": dur, "rate": 1.0}

    def get(self):
        return dict(self.state)


class Root:
    def __init__(self):
        self.after_calls = []

    def after(self, ms, fn=None, *a):
        self.after_calls.append((ms, fn))
        return len(self.after_calls)


class Canvas:
    def __init__(self):
        self.items = set()

    def delete(self, tag):
        self.items.discard(tag) if tag != "all" else self.items.clear()

    def find_withtag(self, tag):
        return (1,) if tag in self.items else ()


class Index:
    def __init__(self, match=None):
        self._match = match

    def match(self, *a, **k):
        return self._match

    def candidates(self, *a, **k):
        return []


def engine(**attrs):
    """A real Overlay without __init__: real methods, stubbed side effects."""
    e = main.Overlay.__new__(main.Overlay)
    e._tune = dict(TUNE)
    e.smooth_calls, e.hints, e.events, e.recal = [], [], [], []
    e.fetches, e.identifies, e.ticks = [], [], []

    def _smooth(new, reason="", why="", kind=None):
        e.smooth_calls.append((round(new, 2), reason))
        e.offset = round(new, 2)
        e._pending_offset = None

    def _hint(msg):
        e.hints.append(msg)
        e.cv.items.add("hint")
        e._hint_owner = None
        e._hint_t = time.time()

    defaults = dict(
        _live_mode=True, _live_arrangement=False, _mv_mode=False, _is_cover=False,
        offset=0.0, _pending_offset=None, _pending_note=None, _display_offset=None,
        lines=[], idx=-1, meta={}, _lyrics_path=None, _track=("Band", "Concert"),
        _concert_mc=(), _in_mc=False, _mc_idx=-1, _mc_playing=False, _mc_flag_t=0.0,
        _mc_enter_t=0.0, _mc_exit_pos=-1.0, _hint_t=0.0, _hint_owner=None, _mc_cur=None,
        _chapter_intro_engage_pos=None, _chapter_vocal_pos=None, _chapter_crude_off=None,
        _identify_user_pending=None, _gen_token=0, _gen_lines=[], _pending_swap=None,
        _chapter_intro_hold=False, _chapter_intro_pos0=0.0, _chapter_intro_t0=0.0,
        _intro_anchored=True, _sound_song=None, _concert_plan=None,
        _concert_plan_seq=-1, _concert_plan_final=False, _concert_setlist=None,
        _setlist_idx=None, _setlist_deferred_idx=None, _track_seq=7,
        _applause_for=0.0, _applause_armed=False, _concert_song_t=0.0,
        _last_decision=None, _fast_calib=0, _vocal_onset_reject_t=0.0,
        _identify_user=False, _identify_clip_s=0.0, _fetching=False,
        _generating=False, _verified=False, _last_sound_lock_t=0.0,
        _title_locked=False, _sound_fail_streak=0, _last_heard_contra=None,
        _concert_candidates=[], _ocr_song=None, _aligning=False,
        _auto_align_silent=True, _fine_active=False, _live_resync_inflight=False,
        _align_tpvr_active=False, _align_tpvr=None, _align_tpvr_until=0.0,
        _verified_meta=False, _subs_on_flag=False,
    )
    for k, v in defaults.items():
        setattr(e, k, v)
    e.media, e.root, e.cv, e.index = Media(), Root(), Canvas(), Index()
    e._smooth_offset = _smooth
    e._hint = _hint
    e._note_event = lambda kind, detail="", **k: e.events.append(kind)
    e._arm_recal = lambda s: e.recal.append(s)
    e._start_fetch = lambda *a, **k: e.fetches.append(a)
    e._start_identify = lambda *a, **k: e.identifies.append(k)
    e._subs_suppresses_sound = lambda: False
    e._sync_match_floor = lambda: 0.62
    e._note_live_resync = lambda ok: None
    e._fine_exit = lambda reason: None
    e._file_valid = lambda *a, **k: True
    e.load = lambda p: e.fetches.append(("load", p))
    e._maybe_translate = lambda: None
    e._set_verified = lambda *a, **k: None
    for k, v in attrs.items():
        setattr(e, k, v)
    return e


def L(*spans):
    return [main.Line(start=s, end=e, jp="placeholder") for s, e in spans]


# ── MC interval helpers ──────────────────────────────────────────────────────
def test_mc_normalize_merges_shrinks_and_drops_junk():
    raw = [[10, 40], [35, 60], [100, 103], ["x", 5], [50, 20], [float("nan"), 3], None]
    assert main._mc_normalize(raw, edge_s=2.0, min_len_s=4.0) == ((12.0, 58.0),)
    assert main._mc_normalize([], 2.0) == ()


def test_mc_find_and_overlap():
    ivs = ((12.0, 58.0), (80.0, 90.0))
    assert main._mc_find(ivs, 12.0) == 0
    assert main._mc_find(ivs, 57.9) == 0
    assert main._mc_find(ivs, 58.0) == -1
    assert main._mc_find(ivs, 85.0) == 1
    assert main._mc_find(ivs, 70.0) == -1
    assert main._mc_find(ivs, "junk") == -1
    assert main._mc_find((), 5.0) == -1
    assert main._mc_overlap(((0.0, 10.0),), 5.0, 15.0) == pytest.approx(0.5)
    assert main._mc_overlap(((0.0, 10.0),), 20.0, 30.0) == 0.0


# ── the MC gate and its edges ────────────────────────────────────────────────
def test_mc_gate_enter_and_exit_edges():
    e = engine(_concert_mc=((100.0, 160.0),), _chapter_intro_hold=True, _intro_anchored=False,
               _last_decision={"t": time.time(), "ranked": [(10.0, "x")]})
    now = time.time()
    e.media.state["position"] = 120.0
    e._mc_update(e.media.get(), now)
    assert e._mc_gate_active() and "mc-enter" in e.events
    e.media.state["position"] = 170.0
    e._mc_update(e.media.get(), now + 50)
    assert not e._mc_gate_active() and "mc-exit" in e.events
    assert e._mc_exit_pos == 170.0
    assert e._chapter_intro_pos0 == 170.0          # hold backstop re-based to the talk end
    assert e._fast_calib >= 2 and 1 in e.recal
    assert e._last_decision.get("inconclusive") and e._last_decision.get("mc")


@pytest.mark.parametrize("change", ["paused", "stale", "knob", "not_concert"])
def test_mc_gate_is_off_when(change):
    e = engine(_concert_mc=((100.0, 160.0),))
    e.media.state["position"] = 120.0
    if change == "paused":
        e.media.state["status"] = 5
    if change == "knob":
        e._tune["concert_mc_gate"] = 0
    if change == "not_concert":
        e._live_mode = False
    e._mc_update(e.media.get(), time.time())
    if change == "stale":
        e._mc_flag_t -= 5.0                       # the frame loop stopped evaluating
    assert not e._mc_gate_active()


def test_mc_card_only_when_no_line_is_due():
    e = engine(_concert_mc=((100.0, 160.0),), lines=L((10, 14), (15, 20)), offset=-50.0)
    e.media.state["position"] = 120.0
    e._mc_update(e.media.get(), time.time())
    # display time 70 s: the song (lines 10-20 s) is over → no line expected
    assert not e._mc_line_expected(120.0)
    e._mc_show_hint()
    e._mc_show_hint()                              # already showing → no redraw
    assert len(e.hints) == 1 and e._hint_owner == "mc"
    # 2 s before the first line under the current offset → a line IS expected
    assert e._mc_line_expected(58.0)


# ── Bug A: resync-by-listening in a concert ──────────────────────────────────
def _fake_align(monkeypatch, heard_line_start, seg_t=1.0, ratio=0.80):
    monkeypatch.setattr(align, "_ensure_deps_path", lambda: None)
    monkeypatch.setattr(align, "_capture", lambda seconds: object())
    monkeypatch.setattr(align, "_transcribe", lambda audio, lang: ["seg"])
    line = main.Line(start=heard_line_start, end=heard_line_start + 3.0, jp="placeholder")
    monkeypatch.setattr(align, "_best_anchor", lambda segs, lines: (seg_t, line, ratio))


def test_capture_and_align_judges_the_correction_not_the_offset(monkeypatch):
    _fake_align(monkeypatch, heard_line_start=12.0)
    lines = L((12.0, 15.0))
    # 30 min into a concert: true offset ≈ 12 - (1800 + 1) = -1789
    assert align.capture_and_align(lines, get_pos=lambda: 1800.0) is None          # old behaviour
    res = align.capture_and_align(lines, get_pos=lambda: 1800.0, ref_offset=-1790.0)
    assert res is not None and res[0] == pytest.approx(-1789.0)
    # a large CORRECTION still needs a strong match
    _fake_align(monkeypatch, heard_line_start=12.0, ratio=0.50)
    assert align.capture_and_align(lines, get_pos=lambda: 1800.0, ref_offset=-1700.0) is None


def test_apply_align_concert_applies_relative_correction():
    lines = L((10, 14))
    e = engine(offset=-1800.0, lines=lines)
    e._apply_align((-1803.0, 0.80, 12.0), lines=lines, ref=-1800.0)
    assert e.smooth_calls == [(-1803.0, "align-by-ear")]
    assert "-3.0" in e.hints[-1]                   # the CORRECTION is shown, not -1803


@pytest.mark.parametrize("case", ["lines_changed", "anchor_moved", "heard_in_mc"])
def test_apply_align_drops_stale_results(case):
    lines = L((10, 14))
    e = engine(offset=-1800.0, lines=lines, _concert_mc=((1810.0, 1820.0),))
    ref, res = -1800.0, (-1803.0, 0.80, 12.0)       # heard line at video 12+1803 = 1815
    if case == "lines_changed":
        e.lines = L((30, 34))                       # a different song's timings
        e._concert_mc = ()
    elif case == "anchor_moved":
        e.offset = -1790.0
        e._concert_mc = ()
    e._apply_align(res, lines=lines, ref=ref)
    assert e.smooth_calls == []


def test_apply_align_never_resets_a_concert_to_zero():
    lines = L((10, 14))
    e = engine(offset=-1800.0, lines=lines)
    e._auto_align_silent = False                    # a manual align
    e._apply_align((-1850.0, 0.50, 12.0), lines=lines, ref=-1800.0)
    assert e.offset == -1800.0 and e.smooth_calls == []
    assert "keeping current sync" in e.hints[-1]


def test_apply_align_studio_behaviour_unchanged():
    lines = L((10, 14))
    e = engine(offset=0.0, lines=lines, _live_mode=False)
    e._auto_align_silent = False
    e._apply_align((-50.0, 0.50, 12.0), lines=lines, ref=None)
    assert e.offset == 0.0 and "reset to 0" in e.hints[-1]


# ── Bug B: the between-songs hold released by the singing ────────────────────
def _held(pos, **kw):
    e = engine(offset=-1800.0, lines=L((10, 14), (15, 20)), _chapter_intro_hold=True,
               _intro_anchored=False, _chapter_intro_pos0=1800.0, **kw)
    e.media.state["position"] = pos
    return e


def test_vocal_onset_in_a_concert_anchors_relative_to_the_song():
    e = _held(1830.0)
    e._on_vocal_onset()
    assert e.smooth_calls == [(-1820.0, "vocal-onset-concert")]
    assert e._intro_anchored


def test_vocal_onset_earlier_than_expected_keeps_the_anchor():
    e = _held(1805.0)
    e._on_vocal_onset()
    assert e.smooth_calls == [] and e._intro_anchored


def test_vocal_onset_implausible_is_rate_limited_and_ignored(capsys):
    e = _held(1800.0 + 200.0)
    e._on_vocal_onset()
    first = e._vocal_onset_reject_t
    e._on_vocal_onset()
    assert e.smooth_calls == [] and not e._intro_anchored
    assert first > 0 and e._vocal_onset_reject_t == first   # second call did not re-log


def test_vocal_onset_ignored_during_talk_and_measured_from_the_talk_end():
    e = _held(1830.0, _concert_mc=((1810.0, 1850.0),))
    e._mc_update(e.media.get(), time.time())
    e._on_vocal_onset()
    assert e.smooth_calls == []                     # the host talking is not the song
    e.media.state["position"] = 1851.0
    e._mc_update(e.media.get(), time.time())        # exit at 1851
    e._on_vocal_onset()
    assert e.smooth_calls == []                     # still the tail of the talk
    e.media.state["position"] = 1871.0
    e._on_vocal_onset()
    assert e.smooth_calls == [(-1861.0, "vocal-onset-concert")]


def test_vocal_onset_knob_off_restores_the_old_rejection():
    e = _held(1830.0)
    e._tune["concert_relative_sync"] = 0
    e._on_vocal_onset()
    assert e.smooth_calls == [] and not e._intro_anchored


def test_vocal_onset_outside_a_hold_does_nothing():
    e = _held(1830.0)
    e._chapter_intro_hold = False
    e._on_vocal_onset()
    assert e.smooth_calls == []


# ── Bug D: a song switched in mid-concert lands on its own timeline ──────────
def test_switch_offset_prefers_plausible_shazam_timing():
    e = engine()
    e.media.state["position"] = 1800.0
    off, how = e._concert_switch_offset(12.0, time.time() - 2.0)
    assert how == "shazam" and off == pytest.approx(14.0 - 1800.0, abs=0.1)


def test_switch_offset_falls_back_to_the_concert_anchor():
    e = engine(_concert_plan=[{"start": 1700.0, "end": 2000.0, "onset": 1720.0}])
    e.media.state["position"] = 1800.0
    assert e._concert_switch_offset(5000.0, time.time()) == (-1720.0, "anchor")
    e2 = engine(_concert_setlist=[{"start": 0.0, "title": "A"}, {"start": 1690.0, "title": "B"}])
    e2.media.state["position"] = 1800.0
    assert e2._concert_switch_offset() == (-1690.0, "anchor")
    e3 = engine()
    e3.media.state["position"] = 1800.0
    assert e3._concert_switch_offset() == (-1800.0, "now")


def test_set_switch_offset_studio_keeps_zero():
    e = engine(_live_mode=False, offset=-4.0)
    e._set_switch_offset(12.0, time.time())
    assert e.offset == 0.0


def test_set_switch_offset_concert_is_never_zero():
    e = engine(offset=-100.0, _pending_offset=-99.0)
    e.media.state["position"] = 1800.0
    e._set_switch_offset(12.0, time.time() - 1.0, why="test")
    assert e.offset == pytest.approx(13.0 - 1800.0, abs=0.1)
    assert e._pending_offset is None                # old song's deferred fix dropped


# ── Bug F: installing the offline plan ───────────────────────────────────────
def _plan(*segs):
    return [dict(start=s, end=e, onset=o, title=t, artist="Band", id_conf=c,
                 source="energy", chapter=None, mc_frac=0.0) for (s, e, o, t, c) in segs]


def test_partial_then_final_and_a_late_partial_is_ignored():
    e = engine()
    e._apply_concert_plan(7, _plan((0, 200, 5, None, 0.0), (260, 500, 262, None, 0.0)),
                          mc=[[200.0, 260.0]], partial=True)
    assert e._concert_mc == ((202.0, 258.0),) and not e._concert_plan_final
    assert e._concert_setlist is None               # no ids yet → no setlist
    final = _plan((0, 200, 5, "Song A", 0.85), (260, 500, 262, "Song B", 0.85))
    e._apply_concert_plan(7, final, mc=[[200.0, 260.0]], partial=False)
    assert e._concert_plan_final and [c["title"] for c in e._concert_setlist] == ["Song A", "Song B"]
    e._apply_concert_plan(7, _plan((0, 200, 99, None, 0.0)), mc=[], partial=True)
    assert e._concert_plan is final and e._concert_mc == ((202.0, 258.0),)


def test_plan_for_another_track_is_ignored_and_mc_installs_without_songs():
    e = engine()
    e._apply_concert_plan(6, _plan((0, 200, 5, "Song A", 0.85)), mc=[[1.0, 50.0]])
    assert e._concert_plan is None and e._concert_mc == ()
    e._apply_concert_plan(7, [], mc=[[100.0, 150.0]], partial=False)
    assert e._concert_mc == ((102.0, 148.0),)


def test_chapterless_setlist_thresholds_are_knobs():
    plan = _plan((0, 200, 5, "Song A", 0.85), (260, 266, 261, "Short", 0.85),
                 (300, 500, 302, "Song C", 0.85))
    e = engine()
    e._apply_concert_plan(7, plan, mc=[], partial=False)
    assert [c["title"] for c in e._concert_setlist] == ["Song A", "Song C"]
    e = engine()
    e._tune["concert_plan_min_seg_s"] = 5.0
    e._apply_concert_plan(7, plan, mc=[], partial=False)
    assert [c["title"] for c in e._concert_setlist] == ["Song A", "Short", "Song C"]


def test_plan_reanchors_a_held_chapter_to_its_measured_onset():
    e = engine(_concert_setlist=[{"start": 0.0, "title": "One"}, {"start": 1000.0, "title": "Two"}],
               _setlist_idx=1, _chapter_intro_hold=True, _intro_anchored=False,
               offset=-1000.0, lines=L((10, 14)))
    e.media.state["position"] = 1010.0
    plan = [dict(start=0.0, end=1000.0, onset=3.0, title=None, id_conf=0.0, source="chapters",
                 chapter="One", mc_frac=0.0),
            dict(start=1000.0, end=1300.0, onset=1030.0, title=None, id_conf=0.0,
                 source="chapters", chapter="Two", mc_frac=0.1)]
    e._apply_concert_plan(7, plan, mc=[], partial=True)
    assert e.smooth_calls == [(-1030.0, "concert-plan-onset")]
    assert not e._chapter_intro_hold and e._intro_anchored


def test_plan_reticks_only_when_nothing_is_loaded_or_in_flight():
    base = dict(_concert_setlist=[{"start": 0.0, "title": "One"}, {"start": 1000.0, "title": "Two"}],
                _setlist_idx=1)
    e = engine(**base)
    e._concert_setlist_tick = lambda pos: e.ticks.append(pos)
    e.media.state["position"] = 1010.0
    e._apply_concert_plan(7, _plan((1000, 1300, 1010, "Two", 0.85)), mc=[], partial=False)
    assert e.ticks == [1010.0]
    e2 = engine(lines=L((1, 2)), **base)
    e2._concert_setlist_tick = lambda pos: e2.ticks.append(pos)
    e2._apply_concert_plan(7, _plan((1000, 1300, 1010, "Two", 0.85)), mc=[], partial=False)
    assert e2.ticks == []                           # lyrics loaded → no blind re-tick


# ── Bug E + MC chapters: the per-chapter tick ────────────────────────────────
SETLIST = [{"start": 0.0, "title": "Opening Theme Song"},
           {"start": 300.0, "title": "Distinctive Second Song"}]


def test_chapter_skipped_by_the_sound_lock_is_re_evaluated_later():
    e = engine(_concert_setlist=list(SETLIST), _setlist_idx=0, _verified=True,
               _last_sound_lock_t=time.time(), meta={"title": "Opening Theme Song"})
    e._concert_setlist_tick(310.0)
    assert e.fetches == [] and e._setlist_deferred_idx == 1   # held by the recent lock
    e._concert_setlist_tick(314.0)
    assert e.fetches == []                                  # lock still fresh
    e._last_sound_lock_t = time.time() - 120.0
    e._verified = False
    e._concert_setlist_tick(320.0)
    assert e.fetches and e.fetches[-1][1] == "Distinctive Second Song"
    assert e.offset == -300.0                               # anchored to the chapter


def test_talk_dominated_chapter_is_a_non_song_segment():
    plan = [dict(start=0.0, end=300.0, onset=2.0, title=None, id_conf=0.0, source="chapters",
                 chapter="A", mc_frac=0.1),
            dict(start=300.0, end=500.0, onset=305.0, title=None, id_conf=0.0,
                 source="chapters", chapter="B", mc_frac=0.92)]
    e = engine(_concert_setlist=list(SETLIST), _setlist_idx=0, _concert_plan=plan)
    e._concert_setlist_tick(310.0)
    assert e.fetches == [] and e._setlist_idx == 1


def test_generic_chapter_title_is_anchored_while_waiting_for_shazam():
    sl = [{"start": 0.0, "title": "One"}, {"start": 600.0, "title": "Song 3"}]
    e = engine(_concert_setlist=sl, _setlist_idx=0, offset=-5.0, _pending_offset=-4.0)
    e._concert_setlist_tick(610.0)
    assert e.fetches == [] and e.offset == -600.0 and e._pending_offset is None


# ── /concert diagnostics (Bug C) ─────────────────────────────────────────────
def test_get_concert_reports_mc_and_never_raises():
    e = engine(_concert_setlist=list(SETLIST), _setlist_idx=1, _concert_mc=((100.0, 160.0),),
               _concert_plan=_plan((0, 200, 5, "Song A", 0.85)), _concert_plan_final=True,
               _live_why={}, _pending_switch=("Some Song", "Band"), _pending_switch_t=time.time(),
               _boundary=None, concert_ocr=True)
    e.media.state["position"] = 120.0
    e._mc_update(e.media.get(), time.time())
    out = e.get_concert()
    assert out["mc_audio"]["in_mc"] is True and out["mc_audio"]["count"] == 1
    assert out["chapters"] and out["chapters"][1]["title"] == "Distinctive Second Song"
    assert out["plan"] and out["plan"][0]["mc_frac"] == 0.0
    assert out["watchdogs"]["pending_switch"] == "Some Song"
    assert "MC talk awareness" in out["knobs"]


# ════════════════════════════════════════════════════════════════════════════
# Review fixes (spec 001, independent review of the first version)
# Each test below failed on that version; the scenario is in its docstring.
# ════════════════════════════════════════════════════════════════════════════
class _Char:
    def set_playing(self, v):
        pass


def _init_literal_defaults():
    """Every ``self.<name> = <literal>`` in Overlay.__init__ (numbers, strings,
    None, bools, empty containers) — the REAL starting values of the plain
    state the frame function reads, parsed like the tune dict above."""
    tree = ast.parse((_ROOT / "main.py").read_text(encoding="utf-8-sig"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "Overlay":
            init = next(f for f in node.body
                        if isinstance(f, ast.FunctionDef) and f.name == "__init__")
            out = {}
            for st in ast.walk(init):
                if (isinstance(st, ast.Assign) and len(st.targets) == 1
                        and isinstance(st.targets[0], ast.Attribute)
                        and isinstance(st.targets[0].value, ast.Name)
                        and st.targets[0].value.id == "self"):
                    try:
                        out.setdefault(st.targets[0].attr, ast.literal_eval(st.value))
                    except Exception:
                        pass
            return out
    raise AssertionError("Overlay.__init__ not found")


INIT_DEFAULTS = _init_literal_defaults()


def _tickable(e, title, artist="", pos=45.0):
    """Stub what `_tick_body` touches beyond the engine, so the REAL frame
    function can run one frame (it re-schedules itself through root.after)."""
    for k, v in INIT_DEFAULTS.items():
        if not hasattr(e, k):
            setattr(e, k, v)
    # settings-derived layout (read from settings.json in __init__); drawing
    # itself is out of scope — record which line the frame would render
    e.scroll_dir, e.pos_x, e.pos_y = "none", "center", "bottom"
    e.rendered = []
    e._render = lambda ln: e.rendered.append(ln)
    e.media.state.update({"title": title, "artist": artist, "source": "chrome",
                          "position": pos, "status": PLAYING})
    for name in ("_consume_async", "_decision_engine_tick", "_check_applause_gap",
                 "_check_game_focus", "_check_gaming_hard_drift", "_check_monitors",
                 "_update_smtc_pause_state"):
        setattr(e, name, lambda *a, **k: None)
    e._trusted_duration = lambda st: None
    e._gpu_send_song = lambda: None
    e._cancel_pending_swap = lambda reason: None
    e.character = _Char()
    e._render_frame, e._last_tick_t, e._offset_hist, e._offset_hist_last = (
        False, None, [], e.offset)
    e._now_url, e._music_source_last_t = None, 0.0
    e._idx_minus_one_since = 0.0
    e._tick = lambda: None
    e._last_raw_title, e._last_src, e._last_artist = title, "chrome", artist
    e._clean_artist_cache, e._clean_title_cache = artist, title
    return e


def test_same_timing_helper():
    a = L((10, 14), (15, 20))
    assert main._same_timing(a, a)
    assert main._same_timing(a, L((10, 14), (15, 20)))       # a same-song reload
    assert not main._same_timing(a, L((10, 14), (16, 20)))
    assert not main._same_timing(a, L((10, 14)))
    assert not main._same_timing(a, None)


@pytest.mark.parametrize("live", [False, True])
def test_align_result_survives_a_same_song_reload(live):
    """A translation backfill reloads the SAME song as a new list mid-capture
    (load(keep_idx=True)); a studio "Sync by listening" result was dropped as
    "lyrics changed" — the one non-concert regression the review found."""
    lines = L((10, 14), (15, 20))
    e = engine(_live_mode=live, offset=-1800.0 if live else 0.0, lines=lines)
    e._auto_align_silent = False
    e.lines = L((10, 14), (15, 20))                 # same timings, new list
    ref = -1800.0 if live else None
    e._apply_align(((-1802.5 if live else -2.5), 0.90, 12.0), lines=lines, ref=ref)
    assert e.smooth_calls == [((-1802.5 if live else -2.5), "align-by-ear")]


CH2 = [{"start": 0.0, "title": "Opening Theme Song"},
       {"start": 1800.0, "title": "Distinctive Second Song"}]


def _cached_engine(pos):
    e = engine(_concert_setlist=list(CH2), _setlist_idx=None,
               index=Index(match=Path("/nonexistent/second.json")))

    def _load(p):
        e.lines = L((10, 14), (15, 20), (21, 25))    # first line at song time 10 s
        e._lyrics_path = p
    e.load = _load
    e.media.state["position"] = pos
    return e


def test_late_hold_keeps_the_anchor_and_the_plan_onset_still_applies():
    """Resuming 60 s into a chapter engaged the hold with the singing already
    going; its first held frame counted as the ONSET and re-timed the song so
    its first line played "now" (50 s late) — and the plan's measured onset
    could no longer repair it."""
    e = _cached_engine(1860.0)
    e._concert_setlist_tick(1860.0)
    assert e._chapter_intro_hold and e.offset == -1800.0
    e._chapter_vocal_pos = 1860.1                   # the tick's first held frame
    e._on_vocal_onset()
    assert e.smooth_calls == [] and e.offset == -1800.0 and e._intro_anchored
    plan = [dict(start=1800.0, end=2100.0, onset=1812.0, title=None, id_conf=0.0,
                 source="chapters", chapter="B", mc_frac=0.0)]
    e._apply_concert_plan(7, plan, mc=[], partial=True)
    assert e.smooth_calls == [(-1812.0, "concert-plan-onset")]
    e._apply_concert_plan(7, plan, mc=[], partial=False)      # final: no double anchor
    assert e.smooth_calls == [(-1812.0, "concert-plan-onset")]


def test_plan_onset_never_overrides_a_refined_offset():
    e = _cached_engine(1860.0)
    e._concert_setlist_tick(1860.0)
    e._chapter_vocal_pos = 1860.1
    e._on_vocal_onset()
    e.offset = -1803.5                              # resync refined it since
    plan = [dict(start=1800.0, end=2100.0, onset=1812.0, title=None, id_conf=0.0,
                 source="chapters", chapter="B", mc_frac=0.0)]
    e._apply_concert_plan(7, plan, mc=[], partial=True)
    assert e.smooth_calls == [] and e.offset == -1803.5


def test_bug_e_late_reevaluation_keeps_the_anchor():
    e = _cached_engine(1805.0)
    e._setlist_idx, e._verified = 0, True
    e._last_sound_lock_t = time.time()
    e.meta = {"title": "Opening Theme Song"}
    e._concert_setlist_tick(1805.0)
    assert e._setlist_deferred_idx == 1
    e._last_sound_lock_t, e._verified = time.time() - 95.0, False
    e.media.state["position"] = 1885.0
    e._concert_setlist_tick(1885.0)
    e._chapter_vocal_pos = 1885.1
    e._on_vocal_onset()
    assert e.smooth_calls == [] and e.offset == -1800.0


def test_on_time_hold_still_calibrates_from_the_heard_onset():
    e = _cached_engine(1800.5)
    e._concert_setlist_tick(1800.5)
    e.media.state["position"] = 1832.0
    e._chapter_vocal_pos = 1830.0                   # vocals first heard at 1830
    e._on_vocal_onset()
    assert e.smooth_calls == [(-1820.0, "vocal-onset-concert")]   # 10 - (1830-1800)


def test_onset_heard_while_fetching_anchors_the_song_through_the_tick():
    """The hold engaged on time, but the lyrics were still being fetched when
    the singing started; the onset used to be the frame the fetch LANDED on."""
    sl = [{"start": 0.0, "title": "Opening Theme Song"},
          {"start": 1800.0, "title": "Distinctive Second Song"}]
    e = engine(_concert_setlist=sl, _setlist_idx=0)
    e.media.state["position"] = 1800.5
    e._concert_setlist_tick(1800.5)                 # uncached distinctive → fetch
    assert e._chapter_intro_hold and e.lines == [] and e.fetches
    _tickable(e, "Concert", "Band", pos=1805.0)
    e._vocals_active_now = lambda *a, **k: True
    main.Overlay._tick_body(e)                      # no lyrics yet: remember WHEN
    assert e._chapter_vocal_pos == pytest.approx(1805.0)
    e.lines = L((2, 6), (7, 11))                    # the fetch lands at 1812 ...
    e.media.state["position"] = 1812.0
    e._vocals_active_now = lambda *a, **k: False    # ... between two phrases
    main.Overlay._tick_body(e)
    assert e.smooth_calls == [(-1803.0, "vocal-onset-concert")]   # 2 - (1805-1800)
    assert not e._chapter_intro_hold


def _gen_engine(offset):
    e = engine(offset=offset, _gen_token=1, _gen_lines=[], _gen_lang="ja",
               _cur_duration=7200.0, meta={}, _deep_token=0)
    e._row_visible = lambda row: False
    e._mc_gate_active = lambda: False
    return e


def test_generation_run_stops_when_the_song_clock_moves(monkeypatch):
    """A chapter change mid-generation re-anchored the offset; the run kept
    going, stamping the next chunk on the NEW clock and re-installing the old
    song's lines on it."""
    import fetch_lyrics
    e = _gen_engine(-1500.0)
    monkeypatch.setattr(fetch_lyrics, "annotate", lambda *a, **k: None)
    seen = []

    def fake_transcribe(pos, lang=None, seconds=16):
        seen.append(pos)
        if len(seen) == 1:
            e.offset = -1720.0                      # the next chapter's anchor
            e.media.state["position"] = 1724.0
            return [{"t": [pos + 1.0, pos + 5.0], "jp": "placeholder-a"}]
        if len(seen) >= 3:                          # a regression must FAIL, not hang
            e._gen_token += 100
            return []
        return [{"t": [pos + 1.0, pos + 5.0], "jp": "placeholder-b"}]

    monkeypatch.setattr(align, "transcribe_for_generation", fake_transcribe)
    e.media.state["position"] = 1700.0
    e._generate_loop(1)
    assert seen == [200.0]                          # never transcribed on the new clock
    for _, fn in list(e.root.after_calls):
        if fn is not None:
            try:
                fn()
            except Exception:
                pass
    assert e._gen_token == 2 and e._gen_lines == [] and not e._generating


def test_new_song_chapter_cancels_an_in_flight_generation():
    e = engine(_concert_setlist=list(SETLIST), _setlist_idx=0, _generating=True,
               _gen_token=3, _gen_lines=[{"t": [1.0, 2.0], "jp": "placeholder"}])
    e._concert_setlist_tick(310.0)
    assert e._gen_token == 4 and not e._generating and e._gen_lines == []


@pytest.mark.parametrize("knob,kept", [(1, [(121.0, 124.0)]),
                                       (0, [(121.0, 124.0), (132.0, 135.0)])])
def test_generation_talk_filter_follows_the_gate_knob(monkeypatch, knob, kept):
    """With `concert_mc_gate` off the generation loop still dropped segments
    inside (possibly false) MC — and saved that hole into the cache."""
    import fetch_lyrics
    segs = [{"t": [121.0, 124.0], "jp": "placeholder-a"},
            {"t": [132.0, 135.0], "jp": "placeholder-b"}]
    monkeypatch.setattr(align, "transcribe_for_generation",
                        lambda pos, lang=None, seconds=8: [dict(s) for s in segs])
    monkeypatch.setattr(fetch_lyrics, "annotate", lambda *a, **k: None)
    e = engine(_concert_mc=((130.0, 300.0),), _gen_token=5, _gen_lines=[], _gen_lang=None,
               _cur_duration=100.0)
    e._row_visible = lambda row: False
    e._tune["concert_mc_gate"] = knob
    e._tune["concert_relative_sync"] = 0            # generated t == video t
    e.media.state["position"] = 120.0
    main.Overlay._generate_loop(e, 5)
    assert [tuple(d["t"]) for d in e._gen_lines] == kept


def test_concert_switch_that_fetches_drops_the_old_body():
    e = engine(offset=-1500.0, lines=L((10, 14), (15, 20)), idx=1)
    e._drop_old_body_for_switch("test")
    assert e.lines == [] and e.idx == -1
    s = engine(_live_mode=False, lines=L((10, 14)), idx=0)
    s._drop_old_body_for_switch("test")
    assert s.lines and s.idx == 0                   # studio: unchanged


def _report_wrong_engine():
    e = engine(_concert_mc=((100.0, 200.0),), lines=L((10, 14)), _lyrics_path=None)
    for name in ("_blacklist_current_lyrics", "_rotate_provider_order",
                 "_escalate_to_captions", "_maybe_escalate_ocr"):
        setattr(e, name, lambda *a, **k: None)
    e._bump_wrong_streak = lambda: False
    e._yt_metadata_fetching, e._yt_metadata, e._cover_original_artist = False, None, None
    e._fetch_key = None
    return e


def test_automatic_report_wrong_is_a_background_identify():
    e = _report_wrong_engine()
    main.Overlay.report_wrong(e, user=False)        # load()'s language check
    assert e.identifies == [{"user": False}]
    u = _report_wrong_engine()
    main.Overlay.report_wrong(u)                    # the tray / user button
    assert u.identifies == [{"user": True}]


def test_mc_card_yields_to_a_fresh_hint_then_takes_over():
    e = engine(_concert_mc=((100.0, 200.0),))
    e.media.state["position"] = 150.0
    e._mc_update(e.media.get(), time.time())
    e._hint("user feedback")
    e._mc_show_hint()
    assert e.hints[-1] == "user feedback"           # stays readable
    e._hint_t = time.time() - 5.0
    e._mc_show_hint()
    assert e.hints[-1].startswith("\U0001f3a4 MC") and e._hint_owner == "mc"


def test_plan_replace_inside_talk_is_not_a_new_talk_block():
    e = engine(_concert_mc=((100.0, 200.0),),
               _last_decision={"t": time.time(), "ranked": [(50.0, "x")]})
    now = time.time()
    e.media.state["position"] = 150.0
    e._mc_update(e.media.get(), now)
    e._apply_concert_plan(7, [], mc=[[3.0, 62.0], [98.0, 202.0]], partial=False)
    e._mc_update(e.media.get(), now + 0.1)          # same talk, now index 1
    assert e.events == ["mc-enter"] and e.recal == [] and e._mc_gate_active()
    assert not e._last_decision.get("inconclusive")


def test_final_plan_dropping_an_earlier_talk_block_is_not_an_edge():
    part = [[98.0, 162.0], [398.0, 462.0], [898.0, 1002.0]]
    e = engine()
    e._apply_concert_plan(7, [], mc=part, partial=True)
    now = time.time()
    e.media.state["position"] = 950.0
    e._mc_update(e.media.get(), now)
    e._apply_concert_plan(7, [], mc=[part[0], part[2]], partial=False)
    e._mc_update(e.media.get(), now + 0.1)
    assert e.events == ["mc-enter"] and e.recal == []


def test_seek_between_two_talk_blocks_is_exit_plus_enter():
    e = engine(_concert_mc=((100.0, 200.0), (900.0, 1000.0)))
    now = time.time()
    e.media.state["position"] = 150.0
    e._mc_update(e.media.get(), now)
    e.media.state["position"] = 950.0
    e._mc_update(e.media.get(), now + 1)
    assert e.events == ["mc-enter", "mc-exit", "mc-enter"]


@pytest.mark.parametrize("how", ["knob", "empty_final_plan"])
def test_gate_release_without_an_exit_edge_still_exits(how):
    e = engine(_concert_mc=((100.0, 200.0),), lines=L((10, 14)), offset=-50.0,
               _chapter_intro_hold=True, _intro_anchored=False)
    now = time.time()
    e.media.state["position"] = 150.0
    e._mc_update(e.media.get(), now)
    e._mc_show_hint()
    if how == "knob":
        e._tune["concert_mc_gate"] = 0
    else:
        e._apply_concert_plan(7, [], mc=[], partial=False)
    e._mc_update(e.media.get(), now + 0.1)
    assert "mc-exit" in e.events and e.recal == [1]
    assert e._hint_owner is None and not e.cv.find_withtag("hint")   # card gone
    assert e._chapter_intro_pos0 == 150.0           # backstop re-based


def test_user_identify_during_a_background_read_is_parked_then_run():
    """"/identify" or "Wrong lyrics" pressed while a background read was in
    flight was a silent no-op; that read then heard MC talk and was dropped."""
    e = engine(_concert_mc=((100.0, 200.0),))
    now = time.time()
    e.media.state["position"] = 130.0
    e._mc_update(e.media.get(), now)
    e._identifying, e._identify_user, e._identify_clip_s = True, False, 8.0
    main.Overlay._start_identify(e, seconds=6, attempts=2, user=True)
    assert e._identify_user_pending == (6, 2)
    e._fetch_result = e._translate_result = None
    e._last_tr_check_t = time.time()
    e._user_identify_pending, e._null_read_streak, e._pending_switch = False, 0, None
    e._identify_result = ("done", ("Some Song", "Band", 30.0, time.time() - 12.0))
    main.Overlay._consume_async(e)                  # dropped: recorded during talk
    for _, fn in list(e.root.after_calls):
        if fn is not None:
            fn()
    assert {"seconds": 6, "attempts": 2, "user": True} in e.identifies
    assert e._identify_user_pending is None


def test_junk_page_title_drops_the_concert_talk_intervals():
    e = engine(_concert_mc=((30.0, 90.0),), lines=L((10, 14)), _lyrics_path="x")
    e._gpu_send_song = lambda: None
    e._cancel_pending_swap = lambda reason: None
    e.media.state["position"] = 45.0
    main.Overlay._on_track_change(e, ("", "Instagram"))
    assert e._concert_mc == () and e._mc_cur is None
    e._mc_update(e.media.get(), time.time())
    assert not e._mc_gate_active()


# ════════════════════════════════════════════════════════════════════════════
# Force Sync, OCR-assisted sync and the energy auto-align in a concert
# (spec 001 follow-ups: all three assumed a baseline offset of 0.0, which in a
# concert is raw VIDEO time — the song's lyrics sit minutes away)
# ════════════════════════════════════════════════════════════════════════════
CONCERT_2 = [{"start": 0.0, "title": "Opening Theme Song"},
             {"start": 1790.0, "title": "Distinctive Second Song"}]


def _wait_for(pred, timeout=3.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if pred():
            return True
        time.sleep(0.01)
    return False


def test_rank_offsets_judges_the_correction_not_the_offset(monkeypatch):
    monkeypatch.setattr(align, "_ensure_deps_path", lambda: None)
    monkeypatch.setattr(align, "_capture", lambda seconds: object())
    monkeypatch.setattr(align, "_transcribe", lambda audio, lang: ["seg"])
    line = main.Line(start=12.0, end=15.0, jp="placeholder")
    monkeypatch.setattr(align, "_rank_anchors",
                        lambda segs, lines, top_n=12: [(1.0, line, 0.80)])
    lines = [line]
    # 30 min into a concert the true offset is about 12 - (1800 + 1) = -1789
    assert align.rank_offsets(lines, get_pos=lambda: 1800.0) == []        # old behaviour
    got = align.rank_offsets(lines, get_pos=lambda: 1800.0, ref_offset=-1790.0)
    assert got and got[0][0] == pytest.approx(-1789.0)
    assert align.rank_offsets(lines, get_pos=lambda: 12.0) == [(-1.0, 0.8, 12.0)]  # studio


def _force_sync_engine(**kw):
    e = engine(lines=L((10, 14), (15, 20)), offset=-37.0, _display_offset=-37.0,
               _concert_setlist=list(CONCERT_2), meta={"lang": "ja"}, **kw)
    e.media.state["position"] = 1800.0
    e._feat_ok = lambda what, real: True
    e.ticks_fs = []
    e._force_sync_tick = lambda: e.ticks_fs.append(True)
    return e


def test_force_sync_in_a_concert_starts_from_the_song_anchor():
    e = _force_sync_engine()
    e.force_sync()
    assert e._fs_ref == -1790.0 and e.offset == -1790.0 and e._display_offset == -1790.0
    e._force_sync_apply([(-1793.0, 0.80, 12.0)], 1800.0)
    assert e.offset == -1793.0
    assert "-3.0s" in e.hints[-1]                   # the correction, not "-1793.0s"


def test_force_sync_studio_still_starts_from_zero():
    e = _force_sync_engine(_live_mode=False)
    e.force_sync()
    assert e._fs_ref == 0.0 and e.offset == 0.0
    e._force_sync_apply([(-1.5, 0.80, 12.0)], 30.0)
    assert e.offset == -1.5 and "-1.5s" in e.hints[-1]


def test_force_sync_listens_with_the_concert_baseline(monkeypatch):
    seen = {}

    def fake_rank(lines, lang="ja", get_pos=None, seconds=8.0, top_n=6, ref_offset=0.0):
        seen["ref"] = ref_offset
        return []

    monkeypatch.setattr(align, "rank_offsets", fake_rank)
    e = _force_sync_engine(_force_sync_active=True, _fs_ref=-1790.0, _fs_tries=0)
    e._align_pos = lambda: 1800.0
    main.Overlay._force_sync_tick(e)
    assert _wait_for(lambda: "ref" in seen) and seen["ref"] == -1790.0


def _ocr_engine(monkeypatch, offset, ocr_text):
    import ocr_lyrics
    monkeypatch.setattr(ocr_lyrics, "available", lambda: True)
    monkeypatch.setattr(ocr_lyrics, "read_lyric_lines", lambda **k: [ocr_text])
    lines = [main.Line(start=10.0, end=14.0, jp="placeholder alpha words"),
             main.Line(start=15.0, end=20.0, jp="placeholder beta words"),
             main.Line(start=200.0, end=204.0, jp="placeholder gamma words")]
    e = engine(lines=lines, offset=offset, _concert_setlist=list(CONCERT_2),
               meta={"source": "lrclib/get"}, _last_src="chrome", _last_ocr_sync_t=0.0)
    e.media.state["position"] = 1805.0
    e._ocr_gpu_safe = lambda: True
    e._source_window_hwnd = lambda: None
    e.root.after = lambda ms, fn=None, *a: fn() if fn else None
    return e


def test_ocr_sync_in_a_concert_measures_from_the_song_anchor(monkeypatch):
    """Read line 2 (starts at song time 15) at video 1805: offset -1790, 2 s
    from the current -1792. The absolute 120 s cap threw this away."""
    e = _ocr_engine(monkeypatch, -1792.0, "placeholder beta words")
    e._ocr_assisted_sync("test")
    assert e.smooth_calls == [(-1790.0, "ocr-sync")]


def test_ocr_sync_revert_goes_to_the_song_anchor_not_zero(monkeypatch):
    """TICKET-201's revert: two reads far from the baseline while an OCR commit
    is in force back that commit out — to the song's anchor, not to 0.0."""
    e = _ocr_engine(monkeypatch, -1600.0, "placeholder gamma words")
    e._ocr_sync_applied = -1600.0                   # a wrong OCR commit is in force
    # line 3 (200 s) read at video 1805 implies -1605: 185 s from the -1790 anchor
    for _ in range(2):
        e._last_ocr_sync_t = 0.0
        e._ocr_assisted_sync("test")
    assert e.smooth_calls == [(-1790.0, "ocr-sync-revert")]


class _Boundary:
    def __init__(self, hist):
        self._h = hist

    def vocal_history(self, secs):
        return list(self._h)


def _vocal_history(lines, offset_true, video_now, n=150):
    import numpy as np
    rng = np.random.default_rng(5)
    now_wall = time.time()
    hist = []
    for k in range(n):                              # 0.2 s blocks, newest last
        t = now_wall - (n - 1 - k) * 0.2
        song = video_now - (now_wall - t) + offset_true
        on = any(ln.start <= song < ln.end for ln in lines)
        hist.append((t, (0.6 if on else 0.15) + float(rng.normal(0, 0.03))))
    return hist


def _aperiodic_lines():
    import numpy as np
    rng = np.random.default_rng(3)
    spans, t0 = [], 10.0
    while t0 < 70.0:
        d = float(rng.uniform(1.0, 2.5))
        spans.append((t0, t0 + d))
        t0 += d + float(rng.uniform(1.5, 4.0))
    return L(*spans)


def test_energy_align_in_a_concert_corrects_on_the_song_clock():
    lines = _aperiodic_lines()
    e = engine(offset=-1800.0, lines=lines, _last_audio_off=None, _last_audio_off_t=0.0)
    applied = []
    e._apply_energy_align = lambda new_off, score, lift: applied.append(new_off)
    e._note_energy_verdict = lambda v: None
    e.root.after = lambda ms, fn=None, *a: fn() if fn else None
    e._energy_reason = "test"
    video_now = 50.0 + 1803.0                        # song time 50 s now, true offset -1803
    hist = _vocal_history(lines, -1803.0, video_now)
    st = {"position": video_now, "status": PLAYING}
    e._run_energy_correlation(hist, st, -1800.0)
    assert applied == [pytest.approx(-1803.0, abs=0.21)]
    applied.clear()
    e._run_energy_correlation(hist, st)             # the old video-clock mapping: nothing
    assert applied == []


def _energy_caller(**kw):
    lines = _aperiodic_lines()
    e = engine(offset=-1800.0, lines=lines, **kw)
    e._boundary = _Boundary(_vocal_history(lines, -1800.0, 1850.0))
    e.media.state["position"] = 1850.0
    e.calls = []
    e._run_energy_correlation = lambda h, st, off=None: e.calls.append(off)
    return e


def test_energy_align_caller_passes_the_concert_offset_and_respects_talk():
    e = _energy_caller()
    e._auto_align_by_energy("test")
    assert _wait_for(lambda: e.calls) and e.calls == [-1800.0]
    k = _energy_caller()
    k._tune["concert_energy_align"] = 0             # knob off: historical mapping
    k._auto_align_by_energy("test")
    assert _wait_for(lambda: k.calls) and k.calls == [None]
    s = _energy_caller(_live_mode=False)            # a normal track: unchanged
    s._auto_align_by_energy("test")
    assert _wait_for(lambda: s.calls) and s.calls == [None]
    m = _energy_caller(_concert_mc=((1830.0, 1845.0),))
    m._auto_align_by_energy("test")                 # the last 30 s overlap talk
    time.sleep(0.05)
    assert m.calls == []
    t = _energy_caller(_concert_mc=((1840.0, 1900.0),))
    t._mc_update(t.media.get(), time.time())        # inside talk right now
    t._auto_align_by_energy("test")
    time.sleep(0.05)
    assert t.calls == []
