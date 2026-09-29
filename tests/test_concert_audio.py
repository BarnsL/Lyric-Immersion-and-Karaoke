"""
Tests for the offline concert analyzer (concert_audio.py), spec 001-concert-mc-awareness.

Run:  python -m pytest tests/test_concert_audio.py -v

Hermetic by design (constitution Principles I and VI):
  * no network — fingerprinting and speech probability are injected fakes;
  * no copyrighted audio — every signal is synthesised here: a "band song" (kick
    drum on every beat, bass, pad and a sung-like melody) and "talk" (irregular
    voiced syllables with phrase pauses, the timing statistics of real speech);
  * the only optional dependency, PyAV/faster-whisper, is needed solely by the
    decoder parity tests, which skip cleanly when it is absent (as in CI).

What is protected, and why it matters:
  * MC talk is flagged, songs are not — even when the speech model says
    "speech" over a song (the rap trap: the beat must veto it).
  * Chapters that open with talk anchor lyrics AFTER the talk (was 3/3 wrong).
  * Chapterless concerts split into one segment per song at the talk (was one
    243 s blob).
  * The envelope is numerically unchanged for float input and never allocates a
    whole-concert float64 copy (was a 586 MiB transient on 80 minutes).
  * Fingerprinting votes 0.85 / 0.60 / 0.45, runs playhead-first, stops early,
    and the partial plan arrives before any fingerprint request.
"""

import sys
import threading
import tracemalloc
from pathlib import Path

import numpy as np
import pytest

_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import concert_audio as ca  # noqa: E402

SR = ca._SR


# ── synthetic signals ────────────────────────────────────────────────────────
def band_song(seconds, bpm=120, seed=0):
    """A band playing: kick on every beat, snare on 2 and 4, a bass line and a
    pad per bar, and a sung-like harmonic melody (vibrato) one note per beat.
    Steady beat → high pulse clarity; continuous → no talk-like pauses."""
    rng = np.random.default_rng(seed)
    n = int(seconds * SR)
    out = np.zeros(n)
    beat = 60.0 / bpm
    for k in range(int(seconds / beat)):
        s = int(k * beat * SR)
        L = min(n - s, int(0.18 * SR))
        tt = np.arange(L) / SR
        out[s:s + L] += 0.9 * np.sin(2 * np.pi * (55 + 60 * np.exp(-tt * 30)) * tt) * np.exp(-tt * 18)
        if k % 2 == 1:
            out[s:s + L] += 0.25 * rng.standard_normal(L) * np.exp(-tt * 25)
    roots = [110.0, 87.3, 98.0, 82.4]
    for k in range(int(seconds / (4 * beat)) + 1):
        s = int(k * 4 * beat * SR)
        L = min(n - s, int(4 * beat * SR))
        if L <= 0:
            break
        tt = np.arange(L) / SR
        r = roots[k % 4]
        out[s:s + L] += 0.35 * np.sin(2 * np.pi * r / 2 * tt)
        for f in (r * 2, r * 2.52, r * 3):
            out[s:s + L] += 0.08 * np.sin(2 * np.pi * f * tt)
    mel = [392, 440, 494, 523, 587, 523, 494, 440]
    ph = 0.0
    for k in range(int(seconds / beat)):
        s = int(k * beat * SR)
        L = min(n - s, int(beat * SR))
        tt = np.arange(L) / SR
        f0 = mel[k % len(mel)] * (1 + 0.012 * np.sin(2 * np.pi * 5.5 * tt))
        phase = ph + 2 * np.pi * np.cumsum(f0) / SR
        ph = phase[-1]
        for h in range(1, 8):
            out[s:s + L] += (0.3 / h) * np.sin(h * phase)
    return (0.15 * out / np.max(np.abs(out))).astype(np.float32)


def talk(seconds, seed=0):
    """MC-like talk: voiced syllables with log-normal durations and gaps,
    gliding pitch, a moving first formant, and 0.3-1.5 s phrase pauses — no
    beat, deep pauses, no bass (the statistics measured on real speech)."""
    rng = np.random.default_rng(seed)
    n = int(seconds * SR)
    out = np.zeros(n)
    i = int(rng.uniform(0, 0.3) * SR)
    while i < n:
        for _ in range(int(rng.integers(2, 12))):
            L = int(rng.lognormal(np.log(0.16), 0.45) * SR)
            if i + L >= n:
                break
            tt = np.arange(L) / SR
            f0 = rng.uniform(95, 200) * (1 + rng.uniform(-0.3, 0.3) * tt / max(tt[-1], 1e-3))
            phase = 2 * np.pi * np.cumsum(f0) / SR
            F1 = rng.uniform(450, 950)
            v = sum((1.0 / h) * np.sin(h * phase) / (1 + ((h * f0 - F1) / 250) ** 2)
                    for h in range(1, 24))
            out[i:i + L] += v * np.hanning(L) * rng.uniform(0.4, 1.0)
            i += L + int(rng.lognormal(np.log(0.08), 0.8) * SR)
        i += int(rng.uniform(0.3, 1.5) * SR)
    out /= np.max(np.abs(out))
    out += 0.003 * rng.standard_normal(n)
    return (0.12 * out).astype(np.float32)


def concert(blocks):
    """Concatenate (label, audio) blocks → (pcm, [(label, start_s, end_s), ...])."""
    pcm, spans, t = [], [], 0.0
    for label, audio in blocks:
        pcm.append(audio)
        spans.append((label, t, t + len(audio) / SR))
        t += len(audio) / SR
    return np.concatenate(pcm).astype(np.float32), spans


def speech_from_spans(spans, inside=0.95, outside=0.05, labels=("talk",)):
    """A fake speech model that 'hears speech' exactly inside the given labelled
    spans (it receives the chunk start ``t0`` in video seconds)."""
    def fn(x, t0=0.0):
        nF = len(x) // int(SR * ca._HOP_S)
        mid = t0 + (np.arange(nF) + 0.5) * ca._HOP_S
        p = np.full(nF, outside, dtype=np.float32)
        for label, s, e in spans:
            if label in labels:
                p[(mid >= s) & (mid < e)] = inside
        return p
    return fn


def overlap_s(intervals, s, e):
    return sum(max(0.0, min(b, e) - max(a, s)) for a, b in intervals)


@pytest.fixture(scope="module")
def song_talk_song():
    return concert([("song", band_song(60, seed=1)), ("talk", talk(40, seed=2)),
                    ("song", band_song(60, bpm=100, seed=3))])


# ── envelope: unchanged numbers, bounded memory ──────────────────────────────
def _envelope_reference(pcm, sr=SR, hop_s=0.5):
    """The pre-spec-001 implementation, verbatim, as the numeric reference."""
    hop = max(1, int(sr * hop_s))
    n = len(pcm) // hop
    frames = np.asarray(pcm[: n * hop], dtype="float32").reshape(n, hop)
    times = (np.arange(n) * hop_s).astype("float32")
    rms = np.sqrt(np.mean(np.square(frames, dtype="float64"), axis=1) + 1e-12).astype("float32")
    freqs = np.fft.rfftfreq(hop, 1.0 / sr)
    band = (freqs >= ca._VOCAL_LO_HZ) & (freqs <= ca._VOCAL_HI_HZ)
    vocal = np.zeros(n, dtype="float32")
    for s in range(0, n, 256):
        e = min(n, s + 256)
        spec = np.abs(np.fft.rfft(frames[s:e], axis=1)) ** 2 + 1e-12
        ratio = spec[:, band].sum(axis=1) / spec.sum(axis=1)
        flat = np.exp(np.mean(np.log(spec), axis=1)) / np.mean(spec, axis=1)
        tonal = np.clip((ca._FLATNESS_TONAL - flat) / ca._FLATNESS_RAMP, 0.0, 1.0)
        vocal[s:e] = (ratio * tonal).astype("float32")
    return times, rms, vocal


def test_envelope_matches_reference_for_float_input():
    x = (np.random.default_rng(0).standard_normal(SR * 70) * 0.1).astype(np.float32)
    for got, want in zip(ca._envelope(x), _envelope_reference(x)):
        np.testing.assert_allclose(got, want, rtol=1e-6, atol=1e-7)


def test_envelope_int16_matches_scaled_float():
    x = (np.random.default_rng(1).standard_normal(SR * 30) * 0.1).clip(-1, 1)
    x16 = (x * 32767).astype(np.int16)
    for got, want in zip(ca._envelope(x16), ca._envelope(x16.astype(np.float32) / 32768.0)):
        np.testing.assert_allclose(got, want, rtol=1e-6, atol=1e-7)


def _peak_bytes(fn, *args):
    tracemalloc.start()
    try:
        fn(*args)
        return tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


def test_envelope_never_copies_the_whole_signal_as_float64():
    """Self-calibrating: the old implementation's transient scales with the
    concert (a float64 square of the whole thing); the blockwise one must stay
    well under half of it on a 10-minute signal (and is flat in length)."""
    x = (np.random.default_rng(2).standard_normal(SR * 600) * 0.05).astype(np.float32)
    new_peak = _peak_bytes(ca._envelope, x)
    old_peak = _peak_bytes(_envelope_reference, x)
    assert new_peak < 0.5 * old_peak, (f"blockwise transient {new_peak/2**20:.1f} MiB vs "
                                       f"reference {old_peak/2**20:.1f} MiB")
    assert new_peak < 0.6 * x.size * 8           # never a whole-signal float64 copy


def test_envelope_too_short_returns_nones():
    assert ca._envelope(np.zeros(SR // 2, np.float32)) == (None, None, None)


# ── MC detection ─────────────────────────────────────────────────────────────
def test_mc_detects_talk_between_songs(song_talk_song):
    pcm, spans = song_talk_song
    n = len(pcm) // int(SR * ca._HOP_S)
    mask, mc, info = ca._mc_intervals(pcm, n, speech_fn=speech_from_spans(spans))
    assert info["detector"] == "speech+acoustic"
    _, ts, te = spans[1]
    assert overlap_s(mc, ts, te) >= 0.8 * (te - ts)
    for label, s, e in spans:
        if label == "song":   # allow 2 s of edge blur at each boundary
            assert overlap_s(mc, s + 2.0, e - 2.0) == 0.0
    assert len(mask) == n and mask.dtype == bool


def test_beat_vetoes_speech_probability_the_rap_trap(song_talk_song):
    """A speech model that says 'speech' EVERYWHERE (it does, over rap) must not
    turn songs into MC: the steady beat vetoes it; the talk is still found."""
    pcm, spans = song_talk_song
    n = len(pcm) // int(SR * ca._HOP_S)
    _, mc, _ = ca._mc_intervals(pcm, n, speech_fn=lambda x, t0=0.0: np.full(len(x) // 8000, 0.95))
    for label, s, e in spans:
        if label == "song":
            assert overlap_s(mc, s + 2.0, e - 2.0) == 0.0
    _, ts, te = spans[1]
    assert overlap_s(mc, ts, te) >= 0.8 * (te - ts)


def test_acoustic_fallback_when_speech_model_unavailable(song_talk_song):
    pcm, spans = song_talk_song
    n = len(pcm) // int(SR * ca._HOP_S)
    _, mc, info = ca._mc_intervals(pcm, n, speech_fn=lambda x, t0=0.0: None)
    assert info["detector"] == "acoustic-fallback"
    for label, s, e in spans:
        if label == "song":
            assert overlap_s(mc, s + 2.0, e - 2.0) == 0.0
    _, ts, te = spans[1]
    assert overlap_s(mc, ts, te) >= 0.5 * (te - ts)   # stricter rule, lower recall


def test_short_talk_is_not_mc_and_a_cheer_does_not_split_a_block():
    pcm, spans = concert([("song", band_song(40, seed=4)), ("talk", talk(6, seed=5)),
                          ("song", band_song(40, seed=6)), ("talk", talk(20, seed=7)),
                          ("gap", np.zeros(int(1.5 * SR), np.float32)),
                          ("talk", talk(20, seed=8)), ("song", band_song(40, seed=9))])
    n = len(pcm) // int(SR * ca._HOP_S)
    speech = speech_from_spans(spans, labels=("talk", "gap"))
    _, mc, _ = ca._mc_intervals(pcm, n, speech_fn=speech)
    _, s0, e0 = spans[1]
    assert overlap_s(mc, s0, e0) == 0.0                   # 6 s < concert_mc_min_s
    assert len([iv for iv in mc if iv[0] > spans[2][1]]) == 1   # 20+1.5+20 s → ONE block


def test_mc_min_duration_knob_is_honoured(song_talk_song):
    pcm, spans = song_talk_song
    n = len(pcm) // int(SR * ca._HOP_S)
    _, mc, _ = ca._mc_intervals(pcm, n, speech_fn=speech_from_spans(spans),
                                tune={"concert_mc_min_s": 60.0})
    assert mc == []


# ── onsets and segmentation ──────────────────────────────────────────────────
def test_chapter_that_opens_with_talk_anchors_after_the_talk():
    pcm, spans = concert([("song", band_song(50, seed=10)), ("talk", talk(30, seed=11)),
                          ("song", band_song(50, seed=12))])
    chapters = [{"start": 0.0, "title": "One"}, {"start": spans[1][1], "title": "Two"}]
    res = ca.analyze_concert(pcm=pcm, chapters=chapters, want_ids=False,
                             speech_fn=speech_from_spans(spans))
    two = res["segments"][1]
    assert two["onset"] >= spans[1][2] - 1.0, "onset landed on the MC talk"
    assert two["onset"] <= spans[1][2] + 4.0
    assert 0.3 < two["mc_frac"] < 0.7


def test_chapterless_concert_splits_at_talk(song_talk_song):
    pcm, spans = song_talk_song
    res = ca.analyze_concert(pcm=pcm, want_ids=False, speech_fn=speech_from_spans(spans))
    segs = res["segments"]
    assert len(segs) == 2
    assert segs[0]["end"] <= spans[1][1] + 3.0
    assert segs[1]["start"] >= spans[1][2] - 3.0
    assert all(s["source"] == "energy" for s in segs)
    assert len(res["mc"]) == 1


def test_mc_detection_can_be_switched_off(song_talk_song):
    pcm, spans = song_talk_song
    res = ca.analyze_concert(pcm=pcm, want_ids=False, tune={"concert_mc_detect": 0},
                             speech_fn=speech_from_spans(spans))
    assert res["mc"] == [] and res["stats"]["mc_detector"] == "off"


def test_same_id_neighbours_merge_and_their_inner_mc_is_dropped():
    segs = [{"start": 0.0, "end": 60.0, "onset": 2.0, "title": "Song A", "artist": "X",
             "id_conf": 0.85, "source": "energy", "chapter": None},
            {"start": 80.0, "end": 150.0, "onset": 81.0, "title": "song a", "artist": "X",
             "id_conf": 0.60, "source": "energy", "chapter": None},
            {"start": 200.0, "end": 260.0, "onset": 201.0, "title": "Song B", "artist": "Y",
             "id_conf": 0.85, "source": "energy", "chapter": None}]
    mc = [[62.0, 78.0], [155.0, 195.0]]
    out, kept = ca._merge_same_id(segs, mc)
    assert [s["title"] for s in out] == ["Song A", "Song B"]
    assert out[0]["end"] == 150.0 and out[0]["onset"] == 2.0 and out[0]["id_conf"] == 0.85
    assert kept == [[155.0, 195.0]]
    assert out[0]["mc_frac"] == 0.0


def test_chapter_segments_are_never_merged():
    segs = [{"start": 0.0, "end": 60.0, "onset": 0.0, "title": "Same", "id_conf": 0.85,
             "source": "chapters"},
            {"start": 60.0, "end": 120.0, "onset": 60.0, "title": "Same", "id_conf": 0.85,
             "source": "chapters"}]
    out, _ = ca._merge_same_id([dict(s) for s in segs], [])
    assert len(out) == 2


# ── fingerprinting ───────────────────────────────────────────────────────────
@pytest.mark.parametrize("hits, want", [
    ([], (None, None, 0.0)),
    ([("Melt", "A")], ("Melt", "A", 0.60)),
    ([("Melt", "A"), ("melt!", "A")], ("Melt", "A", 0.85)),
    ([("Melt", "A"), ("Other", "B")], ("Melt", "A", 0.45)),
    ([("Other", "B"), ("Melt", "A"), ("MELT", "A")], ("Melt", "A", 0.85)),
])
def test_vote(hits, want):
    assert ca._vote(hits) == want


def test_probe_positions_stay_inside_the_segment():
    seg = {"start": 100.0, "end": 130.0, "onset": 104.0}
    ps = ca._probe_positions(seg, 12.0, 1000.0)
    assert ps and all(100.0 <= p <= 118.0 for p in ps)
    assert all(abs(a - b) >= 3.0 for i, a in enumerate(ps) for b in ps[i + 1:])
    assert ca._probe_positions({"start": 0.0, "end": 3.0, "onset": 0.0}, 12.0, 1000.0) == []


def test_id_order_is_playhead_first():
    segs = [{"start": s, "end": s + 100.0} for s in (0.0, 100.0, 200.0, 300.0, 400.0)]
    assert ca._id_order(segs, 250.0) == [2, 3, 4, 1, 0]
    assert ca._id_order(segs, None) == [0, 1, 2, 3, 4]
    assert ca._id_order(segs, 9999.0) == [4, 3, 2, 1, 0]


def test_identify_early_exit_and_playhead_order():
    pcm = np.zeros(SR * 400, np.float32)
    segs = [{"start": s, "end": s + 100.0, "onset": s} for s in (0.0, 100.0, 200.0, 300.0)]
    calls, lock = [], threading.Lock()

    def fake(x, sr=SR, attempts=2):
        with lock:
            calls.append(len(calls))
        return ("Same Title", "Artist", 0.0)

    ca._identify_segments(pcm, segs, 12.0, 400.0, 1, fake, lambda: True, 250.0)
    assert len(calls) == 8                        # 2 agreeing probes per segment, no 3rd
    assert all(s["id_conf"] == 0.85 for s in segs)


def test_identify_parallel_fills_every_segment():
    pcm = np.zeros(SR * 400, np.float32)
    segs = [{"start": s, "end": s + 100.0, "onset": s} for s in (0.0, 100.0, 200.0, 300.0)]

    def fake(x, sr=SR, attempts=2):
        return (None, None, None)

    ca._identify_segments(pcm, segs, 12.0, 400.0, 3, fake, lambda: True, None)
    assert all(s["title"] is None and s["id_conf"] == 0.0 for s in segs)


def test_partial_plan_arrives_before_any_fingerprint_request(song_talk_song):
    pcm, spans = song_talk_song
    events = []

    def on_partial(plan):
        events.append(("partial", plan))

    def fake(x, sr=SR, attempts=2):
        events.append(("id", None))
        return ("A Song", "An Artist", 0.0)

    res = ca.analyze_concert(pcm=pcm, want_ids=True, on_partial=on_partial,
                             identify_fn=fake, speech_fn=speech_from_spans(spans),
                             tune={"concert_audio_id_workers": 1})
    assert events[0][0] == "partial"
    assert sum(1 for kind, _ in events if kind == "partial") == 1
    partial = events[0][1]
    assert partial["partial"] is True and all(s["title"] is None for s in partial["segments"])
    assert partial["mc"] == res["mc"] or len(res["mc"]) <= len(partial["mc"])
    assert res["partial"] is False and all(s["title"] == "A Song" for s in res["segments"])


def test_no_ids_means_no_partial_and_no_network(song_talk_song):
    pcm, spans = song_talk_song

    def boom(*a, **k):
        raise AssertionError("fingerprinting must not run")

    res = ca.analyze_concert(pcm=pcm, want_ids=False, on_partial=boom, identify_fn=boom,
                             speech_fn=speech_from_spans(spans))
    assert res and res["segments"]


def test_legacy_analyze_wrapper_returns_the_segment_list(monkeypatch):
    fake = {"segments": [{"start": 0.0, "end": 1.0}], "mc": [[5.0, 20.0]],
            "stats": {}, "partial": False}
    monkeypatch.setattr(ca, "analyze_concert", lambda *a, **k: fake)
    assert ca.analyze("https://example.invalid/v") == fake["segments"]
    monkeypatch.setattr(ca, "analyze_concert", lambda *a, **k: dict(fake, segments=[]))
    assert ca.analyze("https://example.invalid/v") is None
    monkeypatch.setattr(ca, "analyze_concert", lambda *a, **k: None)
    assert ca.analyze("https://example.invalid/v") is None


def test_too_short_audio_returns_none():
    assert ca.analyze_concert(pcm=np.zeros(SR * 10, np.float32), want_ids=False) is None


# ── decoding (needs PyAV + faster-whisper; skipped when absent, e.g. CI) ─────
def _write_wav(path, x, sr):
    import wave
    x16 = (np.clip(x, -1, 1) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(x16.shape[1] if x16.ndim > 1 else 1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(x16.tobytes())


def test_decode_pcm16_is_bit_identical_to_decode_audio(tmp_path):
    pytest.importorskip("av")
    fw_audio = pytest.importorskip("faster_whisper.audio")
    rng = np.random.default_rng(7)
    stereo = (rng.standard_normal((44100 * 20, 2)) * 0.1).astype(np.float32)
    path = tmp_path / "clip.wav"
    _write_wav(path, stereo, 44100)
    got = ca._decode_pcm16(path)
    ref = fw_audio.decode_audio(str(path), sampling_rate=SR)
    assert got.dtype == np.int16 and len(got) == len(ref)
    np.testing.assert_array_equal(got.astype(np.float32) / 32768.0, ref)


def test_audio_path_is_analysed_and_never_deleted(tmp_path):
    pytest.importorskip("av")
    pcm, spans = concert([("song", band_song(50, seed=13)), ("talk", talk(20, seed=14)),
                          ("song", band_song(50, seed=15))])
    path = tmp_path / "concert.wav"
    _write_wav(path, pcm, SR)
    res = ca.analyze_concert(audio_path=path, want_ids=False,
                             speech_fn=speech_from_spans(spans))
    assert path.exists()
    assert res and res["stats"]["decode"] in ("pyav-int16", "decode_audio-f32")
    assert len(res["segments"]) == 2
