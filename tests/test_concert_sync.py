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

    defaults = dict(
        _live_mode=True, _live_arrangement=False, _mv_mode=False, _is_cover=False,
        offset=0.0, _pending_offset=None, _pending_note=None, _display_offset=None,
        lines=[], idx=-1, meta={}, _lyrics_path=None, _track=("Band", "Concert"),
        _concert_mc=(), _in_mc=False, _mc_idx=-1, _mc_playing=False, _mc_flag_t=0.0,
        _mc_enter_t=0.0, _mc_exit_pos=-1.0, _mc_hint_t=0.0, _hint_owner=None,
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
        e.lines = L((10, 14))
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
