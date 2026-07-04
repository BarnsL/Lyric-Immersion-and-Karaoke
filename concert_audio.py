# -*- coding: utf-8 -*-
"""
Offline CONCERT AUDIO ANALYSIS — "really cinch in" live/concert/show syncing.

WHY THIS EXISTS
───────────────
Real-time recognition of a multi-song 3D-live / concert is unreliable:

  • Shazam fingerprints a LIVE-arranged performance against STUDIO recordings,
    so it either misses or returns a wildly wrong offset (a real log line from a
    Phase Connect Offkai concert: matched 'Melt' but drift = **-748s**, because
    the studio LRC does not line up with the live arrangement's timing).
  • The live recognize child is GIL-heavy (WASAPI capture + fingerprint), so on
    a busy concert frame it gets killed by the smoothness backoff ("terminated
    recognize child (frame 3296ms)", "delaying auto identify 60s") and then the
    whole concert goes unidentified.
  • Applause / MC talk / cinematic intros mean the first ~30-60s of each song is
    NOT singing, so anchoring lyrics to the raw clock (or even the chapter start)
    shows them too early.

The fix, requested by the user: do the analysis OFFLINE, from a background
download of the video's own audio. With the WHOLE track in hand we can look
ahead and behind — impossible for a live listener — and:

  1. Decode the concert once to mono 16 kHz PCM.
  2. Build an ENERGY ENVELOPE (RMS + a tonality-gated 200-3000 Hz vocal-band
     ratio) over the whole thing — cheap, vectorised numpy, no live capture.
  3. SEGMENT it: songs are long runs of sustained tonal vocal energy; the gaps
     between them are quiet (MC talk) or broadband-loud (applause/cheer, which
     the tonality gate separates from singing).
  4. Find each song's VOCAL ONSET so lyrics start when the singing does, not
     during the intro/applause (exactly the "start when the waveform matches"
     behaviour the user described).
  5. IDENTIFY each segment by fingerprinting a ~12s slice at its vocal onset
     (recognize.identify_pcm — offline, so no jank, no device enumeration).
  6. Emit a CORRECTABLE per-video SETLIST PLAN: one entry per song with a start,
     an end, the vocal-onset anchor, and the Shazam title/artist. The engine
     drives per-song lyric loading off this plan; if a later by-ear read
     disagrees, that entry can be overridden (the plan is a strong default, not
     a lock).

This module is PURE analysis: it downloads to a temp dir, reads it, and DELETES
the audio in a ``finally`` — only the small plan (a list of dicts) survives. It
never touches Tk, the overlay, or global engine state; ``main.py`` owns the
threading and consumption.

INPUTS/OUTPUTS
──────────────
``analyze(url, chapters=…, …) -> list[Segment] | None``

  Segment = {
      "start":   float,   # segment start in VIDEO seconds
      "end":     float,   # segment end in VIDEO seconds
      "onset":   float,   # VIDEO seconds where singing begins (lyric anchor)
      "title":   str|None,# Shazam-identified song title (None if unmatched)
      "artist":  str|None,
      "chapter": str|None,# the YouTube chapter title, if chapters were given
      "source":  str,     # "chapters" | "energy" (how the boundary was found)
      "id_conf": float,   # 0..1 rough confidence in the Shazam id
  }

Everything degrades gracefully: no yt-dlp, no decoder, a download failure, or an
over-long video → returns None and the existing chapter/OCR/by-ear path stands.
"""
from __future__ import annotations

import logging
import tempfile
import shutil
from pathlib import Path

log = logging.getLogger("karaoke")

# ── tunables (kept module-level; the engine passes overrides from self._tune) ──
_SR = 16000               # analysis sample rate (mono) — matches the whisper decode
_HOP_S = 0.5              # energy-envelope frame hop / window, seconds
_VOCAL_LO_HZ = 200        # vocal band low edge (skip bass/kick rumble)
_VOCAL_HI_HZ = 3000       # vocal band high edge (skip cymbal/hiss)
_FLATNESS_TONAL = 0.65    # spectral flatness at/below this = tonal (voice/music)
_FLATNESS_RAMP = 0.30     # flatness ramp width for the tonality gate
_ID_SLICE_S = 12.0        # seconds of audio fingerprinted per segment
_MIN_SONG_S = 45.0        # a song region must sustain at least this long
_MIN_GAP_S = 6.0          # a quiet gap this long separates two songs
_MAX_DUR_S = 4800         # >80 min ⇒ almost certainly not a single concert upload

import re as _re
_ID_NORM_RE = _re.compile(r"[^0-9a-z぀-ヿ一-鿿]+")


def _norm_for_id(s):
    """Normalize a title for the two-probe corroboration check (case + spaces
    + punctuation + 'feat.' stripped, CJK kept)."""
    return _ID_NORM_RE.sub("", (s or "").lower())


def _ensure_deps():
    """faster-whisper (and its PyAV decoder) live in the bundled ``.deps`` dir
    that align.py adds to sys.path. Reuse that so this module works no matter
    which code path imported first."""
    try:
        import align
        align._ensure_deps_path()
    except Exception:
        pass


def available() -> bool:
    """True when the offline decode + numpy are importable (the yt-dlp download
    reuses deep_transcribe, which has its own availability check)."""
    _ensure_deps()
    try:
        import numpy  # noqa: F401
        from faster_whisper.audio import decode_audio  # noqa: F401
        return True
    except Exception:
        return False


# ── ENERGY ENVELOPE ───────────────────────────────────────────────────────────
def _envelope(pcm, sr=_SR, hop_s=_HOP_S):
    """Return (times, rms, vocal) frame arrays for the whole PCM signal.

    Mirrors songchange.py's live per-block math (RMS + a tonality-gated
    200-3000 Hz band ratio) but VECTORISED over the whole file and computed in
    blocks so a 45-minute concert never allocates a giant FFT at once.

      rms[i]   — loudness of frame i (song vs. quiet/MC).
      vocal[i] — fraction of energy in the vocal band, SCALED by tonality so
                 broadband applause/cheer (high spectral flatness) reads LOW and
                 real singing reads HIGH. This is what separates "the crowd is
                 roaring" from "someone is singing".
    """
    import numpy as np
    hop = max(1, int(sr * hop_s))
    n = len(pcm) // hop
    if n < 2:
        return None, None, None
    frames = np.asarray(pcm[: n * hop], dtype="float32").reshape(n, hop)
    times = (np.arange(n) * hop_s).astype("float32")
    rms = np.sqrt(np.mean(np.square(frames, dtype="float64"), axis=1) + 1e-12).astype("float32")

    freqs = np.fft.rfftfreq(hop, 1.0 / sr)
    band = (freqs >= _VOCAL_LO_HZ) & (freqs <= _VOCAL_HI_HZ)
    vocal = np.zeros(n, dtype="float32")
    BLK = 256                                   # frames per FFT block (bounds memory)
    for s in range(0, n, BLK):
        e = min(n, s + BLK)
        spec = np.abs(np.fft.rfft(frames[s:e], axis=1)) ** 2 + 1e-12
        total = spec.sum(axis=1)
        band_e = spec[:, band].sum(axis=1)
        ratio = band_e / total
        # spectral flatness = geometric mean / arithmetic mean of the power
        # spectrum; ~1.0 for white/broadband noise (applause), low for tonal.
        flat = np.exp(np.mean(np.log(spec), axis=1)) / np.mean(spec, axis=1)
        tonal = np.clip((_FLATNESS_TONAL - flat) / _FLATNESS_RAMP, 0.0, 1.0)
        vocal[s:e] = (ratio * tonal).astype("float32")
    return times, rms, vocal


def _sustained_onset(times, rms, vocal, seg_start, seg_end,
                     floor, hop_s=_HOP_S, sustain_s=1.2):
    """VIDEO-seconds where singing first SUSTAINS inside [seg_start, seg_end).

    Walks the vocal-energy frames from the segment start and returns the first
    time a run of ≥ ``sustain_s`` stays above the adaptive ``floor``. This is the
    lyric anchor — it skips the intro / applause / MC dead-space at the top of a
    song so the first line lands ON the first sung word. Falls back to seg_start
    when nothing clears the floor (instrumental-heavy or mis-thresholded)."""
    import numpy as np
    need = max(1, int(sustain_s / hop_s))
    lo = int(np.searchsorted(times, seg_start))
    hi = int(np.searchsorted(times, seg_end))
    run = 0
    for i in range(lo, min(hi, len(vocal))):
        if vocal[i] >= floor:
            run += 1
            if run >= need:
                return float(times[i - need + 1])
        else:
            run = 0
    return float(seg_start)


def _segments_from_energy(times, rms, vocal, floor, min_song_s=_MIN_SONG_S,
                          min_gap_s=_MIN_GAP_S, hop_s=_HOP_S):
    """Derive song regions from the envelope alone (NO chapters).

    A song = a run of frames whose vocal energy is mostly above ``floor``,
    lasting ≥ ``min_song_s``, separated from the next by a quiet/low-vocal gap of
    ≥ ``min_gap_s``. Short dips inside a song (a breath, a quiet bridge) are
    bridged; short blips of energy in a gap (a stray cheer) are ignored. Returns
    a list of (start_s, end_s)."""
    import numpy as np
    active = vocal >= floor
    # bridge short gaps WITHIN a song (fill False runs shorter than min_gap)
    gap_n = max(1, int(min_gap_s / hop_s))
    i, n = 0, len(active)
    filled = active.copy()
    while i < n:
        if not filled[i]:
            j = i
            while j < n and not filled[j]:
                j += 1
            if (j - i) < gap_n and i > 0 and j < n:
                filled[i:j] = True
            i = j
        else:
            i += 1
    # collect runs of True lasting >= min_song
    song_n = max(1, int(min_song_s / hop_s))
    segs, i = [], 0
    while i < n:
        if filled[i]:
            j = i
            while j < n and filled[j]:
                j += 1
            if (j - i) >= song_n:
                # end = end of the LAST frame in the run, not its start —
                # otherwise the final ~hop_s of the song falls outside the
                # segment and _plan_for_pos returns None there.
                end_i = min(j, n - 1)
                segs.append((float(times[i]),
                             float(times[end_i]) + hop_s))
            i = j
        else:
            i += 1
    return segs


# ── main entry ────────────────────────────────────────────────────────────────
def analyze(url, chapters=None, lang=None, max_dur=_MAX_DUR_S,
            want_ids=True, tune=None, is_seq_current=None):
    """Download the concert audio, analyse it offline, and return a setlist PLAN.

    Parameters
    ----------
    url : str            exact video URL (a title search would land on a
                         different upload whose timing differs — always pass the
                         browser-pushed URL).
    chapters : list[{"start", "title"}] | None
                         YouTube chapters if the video has them. When present
                         they seed the segment boundaries and each segment's
                         onset+id REFINE the chapter; when absent the segments
                         are derived from the energy envelope alone.
    lang : str | None    song language hint (unused today; reserved for a future
                         per-segment transcription pass).
    max_dur : int        reject videos longer than this (not a single concert).
    want_ids : bool      fingerprint each segment (offline Shazam). Off = onset
                         refinement only, no network.
    tune : dict | None   engine ``self._tune`` overrides for the module knobs.
    is_seq_current : callable | None
                         optional ``() -> bool`` the caller supplies to abort
                         early when the track changed mid-analysis.

    Returns the plan (list of Segment dicts) or None on any failure. ALWAYS
    deletes the downloaded audio.
    """
    if not available():
        return None
    try:
        import numpy as np
        from faster_whisper.audio import decode_audio
        import deep_transcribe
        import recognize
    except Exception:
        return None
    t = tune or {}
    min_song_s = float(t.get("concert_audio_min_song_s", _MIN_SONG_S))
    id_slice_s = float(t.get("concert_audio_id_slice_s", _ID_SLICE_S))

    def _alive():
        try:
            return is_seq_current() if is_seq_current else True
        except Exception:
            return True

    tmp = Path(tempfile.mkdtemp(prefix="dk_concert_"))
    try:
        # 1) DOWNLOAD the exact video's audio (audio-only; deep_transcribe has
        #    the anti-bot resilience + the duration cap we lift for concerts).
        audio, _canon = deep_transcribe._download_audio(url, tmp, max_dur=max_dur)
        if not audio or not _alive():
            return None
        # 2) DECODE to mono 16 kHz float32 (PyAV via faster-whisper — already a
        #    dependency; no ffmpeg binary required).
        try:
            pcm = decode_audio(str(audio), sampling_rate=_SR)
        except Exception as e:
            log.info("concert-audio: decode failed: %s", str(e)[:120])
            return None
        dur_s = len(pcm) / float(_SR)
        if dur_s < min_song_s or not _alive():
            return None
        # 3) ENERGY ENVELOPE over the whole concert.
        times, rms, vocal = _envelope(pcm, sr=_SR)
        if times is None:
            return None
        # adaptive vocal floor keyed off the LOUD level (90th percentile), NOT
        # the median: in a concert the singing is the MAJORITY of the runtime,
        # so a median/spread floor lands inside the song level and finds nothing.
        # A fraction of the loud level sits between "singing" (~p90) and
        # "quiet/applause" (~0) whether songs are 50% or 95% of the video.
        hi = float(np.percentile(vocal, 90))
        if not np.isfinite(hi):
            hi = 0.0                    # never let a nan floor silently no-op
        floor = max(1e-3, float(t.get("concert_audio_floor_frac", 0.40)) * hi)

        # 4) SEGMENT: refine chapters if given, else derive from energy.
        segs = []
        ch = [c for c in (chapters or []) if isinstance(c, dict) and "start" in c]
        if len(ch) >= 2:
            ch = sorted(ch, key=lambda c: float(c["start"]))
            for i, c in enumerate(ch):
                s = float(c["start"])
                e = float(ch[i + 1]["start"]) if i + 1 < len(ch) else dur_s
                if e - s < 8.0:
                    continue
                onset = _sustained_onset(times, rms, vocal, s, e, floor)
                segs.append({"start": s, "end": e, "onset": onset,
                             "chapter": str(c.get("title") or "") or None,
                             "source": "chapters"})
        else:
            for (s, e) in _segments_from_energy(times, rms, vocal, floor,
                                                min_song_s=min_song_s):
                onset = _sustained_onset(times, rms, vocal, s, e, floor)
                segs.append({"start": s, "end": e, "onset": onset,
                             "chapter": None, "source": "energy"})
        if not segs or not _alive():
            return segs or None

        # 5) IDENTIFY each segment offline (fingerprint a slice at the onset).
        #    id_conf is REAL SIGNAL, not a flat value: a match that survives BOTH
        #    the onset probe AND a chorus probe with the SAME title is high
        #    confidence (~0.85); a lone match is medium (~0.60) — below the
        #    override gate downstream so a stray live-arrangement mis-ID does not
        #    silently retitle a non-distinctive chapter. The threshold in
        #    _concert_setlist_tick is 0.70.
        if want_ids:
            slice_n = int(id_slice_s * _SR)
            for seg in segs:
                if not _alive():
                    break
                seg["title"] = seg.get("title")
                seg["artist"] = seg.get("artist")
                seg["id_conf"] = 0.0
                start_samp = int(seg["onset"] * _SR)
                probes = []
                for p in (start_samp, start_samp + int(25 * _SR)):
                    if p + slice_n > len(pcm):
                        p = max(0, len(pcm) - slice_n)
                    if len(pcm) - p < _SR * 4:
                        continue
                    if p not in probes:            # dedupe near-EOF collapse
                        probes.append(p)
                hits = []
                for p in probes:
                    sl = pcm[p:p + slice_n]
                    t, a, _off = recognize.identify_pcm(sl, sr=_SR, attempts=2)
                    if t:
                        hits.append((t, a))
                if hits:
                    t0, a0 = hits[0]
                    # Two probes with the SAME song title is corroborated
                    # evidence; a single hit is a hint that stays below the 0.70
                    # override gate.
                    corrob = len(hits) >= 2 and any(
                        _norm_for_id(h[0]) == _norm_for_id(t0) for h in hits[1:])
                    seg["title"], seg["artist"] = t0, a0
                    seg["id_conf"] = 0.85 if corrob else 0.60
        # default the id fields even when want_ids is off
        for seg in segs:
            seg.setdefault("title", None)
            seg.setdefault("artist", None)
            seg.setdefault("id_conf", 0.0)

        if not segs:
            log.info("concert-audio: decoded %.0fs of audio but produced NO "
                     "segments (floor=%.4f, p90=%.4f) — leaving chapters as-is",
                     dur_s, floor, hi)
        else:
            log.info("concert-audio: %d segment(s) over %.0fs (%s): %s",
                     len(segs), dur_s,
                     "chapters" if len(ch) >= 2 else "energy",
                     " | ".join(f"{int(s['onset'])}s "
                                f"{(s.get('title') or s.get('chapter') or '?')[:20]}"
                                for s in segs[:10]))
        return segs
    except Exception as e:
        log.info("concert-audio: analysis error: %s", str(e)[:160])
        return None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)   # ALWAYS delete the source audio
