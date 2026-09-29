# -*- coding: utf-8 -*-
"""
Offline CONCERT AUDIO ANALYSIS — "really cinch in" live/concert/show syncing.

WHY THIS EXISTS
───────────────
Real-time recognition of a multi-song 3D-live / concert is unreliable:

  • Shazam fingerprints a LIVE-arranged performance against STUDIO recordings,
    so it often misses. (A logged "drift = -748s" on a Phase Connect Offkai
    concert was long blamed on live timing; spec 001 found that concert offsets
    were being handled as ABSOLUTE video time — see docs/CONCERT_AUDIO_SYNC.md.)
  • The live recognize child is GIL-heavy (WASAPI capture + fingerprint), so on
    a busy concert frame it gets killed by the smoothness backoff and the whole
    concert goes unidentified for a while.
  • Applause / MC talk / cinematic intros mean the first ~30-60s of each song is
    NOT singing, so anchoring lyrics to the raw clock (or even the chapter start)
    shows them too early.

So we do the analysis OFFLINE, from one background download of the video's own
audio. With the WHOLE track in hand we can look ahead and behind — impossible
for a live listener:

  1. DECODE the concert once to mono 16 kHz int16 PCM (streaming, bounded memory).
  2. Build an ENERGY ENVELOPE (RMS + a tonality-gated 200-3000 Hz vocal-band
     ratio) over the whole thing — cheap, blockwise numpy, no live capture.
  3. Detect MC / TALK stretches (spec 001-concert-mc-awareness). The envelope's
     "vocal" feature cannot tell speech from singing — measured, MC talk scored
     HIGHER than singing — so a dedicated detector fuses three cues:
       · speech probability from the Silero VAD bundled with faster-whisper,
       · NO steady beat (pulse clarity of the onset envelope; rap has a beat),
       · talk-like pauses (low-energy-frame ratio / dB modulation depth).
     Without the speech model a strict acoustic-only rule is used instead.
  4. SEGMENT: songs are long runs of sustained tonal energy; talk, quiet and
     broadband applause separate them. YouTube chapters seed the boundaries when
     present.
  5. Find each song's VOCAL ONSET (skipping leading talk) so lyrics start on the
     first sung word, not during the intro, applause or MC.
  6. IDENTIFY each segment by fingerprinting slices (recognize.identify_pcm —
     offline, so no jank): up to three probes (onset, +25 s, midpoint), majority
     vote, run in a small thread pool with the segment under the playhead first.
  7. Emit a CORRECTABLE per-video PLAN (song segments + MC intervals). A PARTIAL
     plan (onsets + MC, no ids yet) is delivered through ``on_partial`` before the
     slow fingerprinting starts, so the runtime can use onsets within seconds.

This module is PURE analysis: it downloads to a temp dir, reads it, and DELETES
the audio in a ``finally`` — only the small plan survives. It never touches Tk,
the overlay, or global engine state; ``main.py`` owns threading and consumption.

INPUTS / OUTPUTS
────────────────
``analyze_concert(url, chapters=…, …) -> dict | None``::

    {
      "segments": [Segment, ...],   # one per song, in time order
      "mc":       [[start, end], ...],  # MC / talk intervals, VIDEO seconds
      "stats":    {...},            # timings, detector used, counts (diagnostics)
      "partial":  False,            # True only for the on_partial snapshot
    }

    Segment = {
        "start":   float,    # segment start in VIDEO seconds
        "end":     float,    # segment end in VIDEO seconds
        "onset":   float,    # VIDEO seconds where singing begins (lyric anchor)
        "title":   str|None, # fingerprint-identified title (None if unmatched)
        "artist":  str|None,
        "chapter": str|None, # the YouTube chapter title, if chapters were given
        "source":  str,      # "chapters" | "energy" (how the boundary was found)
        "id_conf": float,    # 0..1: 0.85 corroborated, 0.60 lone hit, 0.45 conflict
        "mc_frac": float,    # 0..1 share of the segment covered by MC talk
    }

``analyze(...)`` is kept for backward compatibility and returns just the list of
segments (or None), exactly as before spec 001.

Everything degrades gracefully: no PyAV (falls back to faster-whisper's decoder),
no speech model (strict acoustic MC rule), no yt-dlp / a download failure / an
over-long video → returns None and the existing chapter/OCR/by-ear path stands.

DEBUGGING TIPS
──────────────
* Every run logs one ``concert-audio: …`` summary line with the decoder used,
  the MC detector used, segment onsets and ids. Grep the karaoke log for it.
* ``scripts/eval_concert_mc.py`` rebuilds a labelled mini-concert from
  permissively-licensed clips and prints MC recall / false-MC per song.
* All stages take plain numpy arrays, so they can be exercised from a REPL:
  ``times, rms, vocal = _envelope(pcm)``; ``_mc_intervals(pcm, len(times))``.
"""
from __future__ import annotations

import gc
import io
import itertools
import logging
import re as _re
import shutil
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

log = logging.getLogger("karaoke")

# ── tunables (module-level defaults; the engine overrides them via self._tune) ──
_SR = 16000               # analysis sample rate (mono) — matches the whisper decode
_HOP_S = 0.5              # energy-envelope frame hop / window, seconds
_VOCAL_LO_HZ = 200        # vocal band low edge (skip bass/kick rumble)
_VOCAL_HI_HZ = 3000       # vocal band high edge (skip cymbal/hiss)
_FLATNESS_TONAL = 0.65    # spectral flatness at/below this = tonal (voice/music)
_FLATNESS_RAMP = 0.30     # flatness ramp width for the tonality gate
_ID_SLICE_S = 12.0        # seconds of audio fingerprinted per probe
_MIN_SONG_S = 45.0        # a song region must sustain at least this long
_MIN_GAP_S = 6.0          # a quiet gap this long separates two songs
_MAX_DUR_S = 4800         # >80 min ⇒ almost certainly not a single concert upload

# ── MC / talk detection (spec 001) ────────────────────────────────────────────
# A short-time analysis runs next to the 0.5 s envelope: 32 ms windows every
# 15.625 ms, i.e. exactly 32 short frames per envelope frame, so the two grids
# line up without resampling.
_MC_N = 512               # short STFT window (samples) — 32 ms at 16 kHz
_MC_HOP = 250             # short STFT hop (samples) — 15.625 ms
_MC_FPS = _SR / _MC_HOP   # 64 short frames per second
_MC_PER = int(round(_HOP_S * _MC_FPS))   # 32 short frames per envelope frame
_MC_WIN_S = 3.0           # context window for the pause cues (lefr / depth / lowr)
_MC_PULSE_WIN_S = 6.0     # context window for pulse clarity (>= 4 beats at 40 BPM)
_MC_SPEECH_MIN = 0.50     # Silero speech probability needed (fused rule)
_MC_PULSE_MAX = 0.30      # talk has no steady beat: pulse clarity must stay below
_MC_LEFR_MIN = 0.45       # talk-like pauses: share of low-energy short frames …
_MC_DEPTH_MIN = 16.0      # … OR dB spread (p90 - p10) of the vocal-band energy
_MC_SMOOTH_K = 11         # majority-vote smoothing, in envelope frames (5.5 s)
_MC_MIN_S = 10.0          # a talk run shorter than this is not "MC"
_MC_JOIN_GAP_S = 4.0      # talk runs closer than this are ONE block (a cheer mid-talk)
_MC_PULSE_DETREND_S = 0.75  # moving average removed from the onset envelope first
# MC edges are only known to about ±half the pulse window: when a song sits
# right next to the talk, its beat enters the 6 s pulse window 3 s before the
# song does, so the talk block is detected up to ~3 s short at that side. The
# onset search therefore treats a margin of half the pulse window around each
# block as talk too, so the first line can never anchor on the host's first or
# last words (a song that starts right after talk anchors ≤ ~0.5 s late, which
# the live resync corrects).
_MC_ONSET_PAD_BEFORE_S = _MC_PULSE_WIN_S / 2.0
_MC_ONSET_PAD_AFTER_S = _MC_PULSE_WIN_S / 2.0
# Strict acoustic-only fallback used when the speech model is unavailable.
# Measured 0 s of false MC over 79 min of real songs (27 tracks incl. rap),
# at ~60 % recall on clean talk — precision first, it only ever PAUSES things.
_MC_FB_PULSE_MAX = 0.22
_MC_FB_LEFR_MIN = 0.52
_MC_FB_DEPTH_MIN = 18.0
_MC_FB_LOWR_MAX = 0.12
_MC_CAND_PAD_S = 4.0      # the speech model runs on candidate regions ± this
_MC_VAD_BLOCK_S = 30.0    # longest single speech-model call: its activations scale
                          # with length (120 s blocks peaked ~27 MiB higher; 30 s
                          # blocks moved MC edges by <=1 s on a 79-min concert)
_MC_VAD_WARM_S = 1.024    # context prepended to each call so its RNN state is warm

# ── fingerprinting ────────────────────────────────────────────────────────────
_ID_WORKERS = 3           # segments fingerprinted concurrently
_ID_PROBE2_S = 25.0       # the second probe sits this far after the onset
_ID_MERGE_GAP_S = 90.0    # same-id ENERGY neighbours closer than this are one song

_ID_NORM_RE = _re.compile(r"[^0-9a-z぀-ヿ一-鿿]+")


def _norm_for_id(s):
    """Normalize a title for probe corroboration (case + spaces + punctuation
    stripped, CJK kept) so 'Melt (Live)' and 'melt' vote together."""
    return _ID_NORM_RE.sub("", (s or "").lower())


def _ensure_deps():
    """faster-whisper / PyAV / onnxruntime live in the bundled ``.deps`` dir
    that align.py adds to sys.path. Reuse that so this module works no matter
    which code path imported first."""
    try:
        import align
        align._ensure_deps_path()
    except Exception:
        pass


def available() -> bool:
    """True when numpy and at least one decoder (PyAV directly, or
    faster-whisper's ``decode_audio``) are importable. The yt-dlp download reuses
    deep_transcribe, which has its own availability check."""
    _ensure_deps()
    try:
        import numpy  # noqa: F401
    except Exception:
        return False
    try:
        import av  # noqa: F401
        return True
    except Exception:
        pass
    try:
        from faster_whisper.audio import decode_audio  # noqa: F401
        return True
    except Exception:
        return False


# ── DECODING ──────────────────────────────────────────────────────────────────
# faster-whisper's decode_audio returns float32 and, on the way, holds the int16
# buffer plus TWO float32 copies (astype, then "/ 32768"): measured 801 MiB peak
# RSS for a 79-minute concert. The concert pass only needs int16, so decode to
# int16 in a stream and convert small blocks to float32 on demand (197 MiB).
def _ignore_invalid_frames(frames):
    """Yield decoded frames, silently skipping corrupt ones (same policy as
    faster-whisper: one bad packet must not abort a 60-minute decode)."""
    import av
    it = iter(frames)
    while True:
        try:
            yield next(it)
        except StopIteration:
            return
        except av.error.InvalidDataError:
            continue


def _group_frames(frames, num_samples):
    """Batch small codec frames into ~``num_samples`` chunks before resampling —
    far fewer resampler calls (mirrors faster-whisper's decoder)."""
    import av
    fifo = av.audio.fifo.AudioFifo()
    for frame in frames:
        frame.pts = None                 # ignore timestamp gaps in live uploads
        fifo.write(frame)
        if fifo.samples >= num_samples:
            yield fifo.read()
    if fifo.samples > 0:
        yield fifo.read()


def _decode_pcm16(path, sr=_SR):
    """Stream-decode ``path`` to MONO int16 PCM at ``sr`` Hz with PyAV.

    Output is bit-identical to ``faster_whisper.audio.decode_audio`` × 32768 (the
    same resampler settings) but never materialises a float copy of the whole
    file. The int16 buffer is PRE-SIZED from the container duration (+1 s), so a
    long concert is written in place instead of being re-copied as it grows; it
    grows ×1.5 only if the duration was unknown or understated.
    Returns an ``np.int16`` array, or None when nothing decoded. Raises on a
    missing PyAV / unreadable file — ``_decode`` handles the fallback."""
    import numpy as np
    import av
    resampler = av.audio.resampler.AudioResampler(format="s16", layout="mono", rate=sr)
    buf, n = None, 0
    try:
        with av.open(str(path), mode="r", metadata_errors="ignore") as container:
            est = 0
            try:
                if container.duration:                  # microseconds (AV_TIME_BASE)
                    est = int(container.duration / 1_000_000 * sr) + sr
            except Exception:
                est = 0
            buf = np.empty(max(est, sr * 60), dtype=np.int16)
            frames = _group_frames(_ignore_invalid_frames(container.decode(audio=0)), 500000)
            for frame in itertools.chain(frames, [None]):       # None flushes
                for out in resampler.resample(frame):
                    a = out.to_ndarray().reshape(-1)
                    if n + len(a) > len(buf):
                        grown = np.empty(max(len(buf) * 3 // 2, n + len(a)), dtype=np.int16)
                        grown[:n] = buf[:n]
                        buf = grown
                    buf[n:n + len(a)] = a
                    n += len(a)
    finally:
        # PyAV resampler objects are only freed by a GC pass (faster-whisper #390)
        del resampler
        gc.collect()
    if buf is None or n < 1:
        return None
    return buf[:n]


def _decode(path):
    """Decode ``path`` for analysis → ``(pcm, how)``. Prefers the streaming int16
    decoder; falls back to faster-whisper's float32 ``decode_audio`` so a PyAV
    quirk can never cost us the whole analysis. ``how`` is logged."""
    try:
        pcm = _decode_pcm16(path)
        if pcm is not None and len(pcm):
            return pcm, "pyav-int16"
    except Exception as e:
        log.info("concert-audio: int16 decode failed (%s) — falling back", str(e)[:120])
    try:
        from faster_whisper.audio import decode_audio
        return decode_audio(str(path), sampling_rate=_SR), "decode_audio-f32"
    except Exception as e:
        log.info("concert-audio: decode failed: %s", str(e)[:120])
        return None, "failed"


def _as_f32(x):
    """A float32 copy of a PCM block in [-1, 1] — int16 is scaled by 2^-15 (an
    exact power of two, so results match ``/ 32768`` bit for bit), float input
    is passed through as float32."""
    import numpy as np
    if getattr(x, "dtype", None) == np.int16:
        out = x.astype(np.float32)
        out *= (1.0 / 32768.0)
        return out
    return np.asarray(x, dtype=np.float32)


# ── ENERGY ENVELOPE ───────────────────────────────────────────────────────────
def _envelope(pcm, sr=_SR, hop_s=_HOP_S):
    """Return (times, rms, vocal) frame arrays for the whole PCM signal.

    Mirrors songchange.py's live per-block math (RMS + a tonality-gated
    200-3000 Hz band ratio), vectorised over the whole file. Everything —
    including RMS — is computed in 256-frame blocks, so an 80-minute concert
    never allocates a full-length float copy (the previous version built a
    586 MiB float64 square of the whole concert for the RMS alone).

    Accepts int16 or float PCM. For float32 input the output is identical to the
    pre-spec-001 implementation.

      rms[i]   — loudness of frame i (song vs. quiet/MC).
      vocal[i] — fraction of energy in the vocal band, SCALED by tonality so
                 broadband applause/cheer (high spectral flatness) reads LOW and
                 tonal sound reads HIGH. NOTE: speech is tonal too — this
                 feature alone cannot tell MC talk from singing (see
                 ``_mc_intervals``).
    """
    import numpy as np
    pcm = np.asarray(pcm)
    hop = max(1, int(sr * hop_s))
    n = len(pcm) // hop
    if n < 2:
        return None, None, None
    view = pcm[: n * hop].reshape(n, hop)          # a VIEW — no copy
    times = (np.arange(n) * hop_s).astype("float32")
    rms = np.empty(n, dtype="float32")
    vocal = np.empty(n, dtype="float32")
    freqs = np.fft.rfftfreq(hop, 1.0 / sr)
    band = (freqs >= _VOCAL_LO_HZ) & (freqs <= _VOCAL_HI_HZ)
    BLK = 128                                   # frames per block (~4 MB of float32)
    for s in range(0, n, BLK):
        e = min(n, s + BLK)
        fr = _as_f32(view[s:e])
        rms[s:e] = np.sqrt(np.mean(np.square(fr, dtype="float64"), axis=1) + 1e-12)
        spec = np.abs(np.fft.rfft(fr, axis=1)) ** 2 + 1e-12
        total = spec.sum(axis=1)
        band_e = spec[:, band].sum(axis=1)
        ratio = band_e / total
        # spectral flatness = geometric mean / arithmetic mean of the power
        # spectrum; ~1.0 for white/broadband noise (applause), low for tonal.
        flat = np.exp(np.mean(np.log(spec), axis=1)) / np.mean(spec, axis=1)
        tonal = np.clip((_FLATNESS_TONAL - flat) / _FLATNESS_RAMP, 0.0, 1.0)
        vocal[s:e] = (ratio * tonal).astype("float32")
    return times, rms, vocal


# ── MC / TALK DETECTION ───────────────────────────────────────────────────────
def _mc_short_feats(pcm):
    """Per SHORT frame (32 ms window, 15.625 ms hop), computed blockwise:

      Ev — power in 300-3400 Hz (the speech band; drives the pause cues)
      El — power in 30-95 Hz (kick / bass: a band is playing)
      Et — total power
      On — onset strength: half-wave-rectified log-magnitude spectral flux
           (the input to pulse clarity)

    Returns four float64 arrays of equal length (0 when the clip is shorter than
    one window)."""
    import numpy as np
    from numpy.lib.stride_tricks import sliding_window_view
    N, H = _MC_N, _MC_HOP
    n = 1 + (len(pcm) - N) // H if len(pcm) >= N else 0
    Ev, El, Et, On = (np.zeros(max(n, 0)) for _ in range(4))
    if n <= 0:
        return Ev, El, Et, On
    win = np.hanning(N).astype("float32")
    f = np.fft.rfftfreq(N, 1.0 / _SR)
    vb = (f >= 300) & (f <= 3400)
    lb = (f >= 30) & (f <= 95)
    prev = None
    B = 1024                                  # short frames per block: ~15 MB of FFT
                                              # temporaries (results identical to 2048)
    for s in range(0, n, B):
        e = min(n, s + B)
        seg = _as_f32(pcm[s * H:(e - 1) * H + N])
        fr = sliding_window_view(seg, N)[::H] * win          # (e-s, N)
        M = np.abs(np.fft.rfft(fr, axis=1))
        P = M * M
        Ev[s:e] = P[:, vb].sum(axis=1)
        El[s:e] = P[:, lb].sum(axis=1)
        Et[s:e] = P.sum(axis=1)
        L = np.log1p(100.0 * M)
        first = L[:1] if prev is None else prev[None, :]
        On[s:e] = np.maximum(np.diff(np.concatenate([first, L], axis=0), axis=0),
                             0.0).sum(axis=1)
        prev = L[-1]
    return Ev, El, Et, On


def _mc_window(x, w, n_frames):
    """Strided VIEW of shape (≤n_frames, w): row i holds the ``w`` short-frame
    values centred on envelope frame i (edge-padded at both ends)."""
    import numpy as np
    from numpy.lib.stride_tricks import sliding_window_view
    pad = max(0, w // 2 - _MC_PER // 2)
    xp = np.pad(np.asarray(x, dtype="float64"), (pad, w), mode="edge")
    return sliding_window_view(xp, w)[::_MC_PER][:n_frames]


def _mc_pulse(On, n_frames):
    """Pulse clarity per envelope frame: the largest normalised autocorrelation
    of the onset envelope over beat-period lags 0.25-1.5 s (40-240 BPM), in a
    6 s window. Songs — rap included — keep a steady beat (≈0.3-0.7); talk does
    not (≈0.1-0.2, measured across every talk condition incl. talk over music).

    The onset envelope is DETRENDED first (a centred 0.75 s moving average is
    subtracted): a slow level change inside the window — e.g. a hard mute or
    silence next to talk — otherwise reads as strong short-lag "periodicity"
    (measured 0.42-0.47 around a 1.5 s silence, vs 0.11-0.18 detrended). Beat
    spikes survive the high-pass untouched; on real songs and talk the two
    variants measured within 1 s of false MC and 1 % of recall.
    Computed in frame blocks so memory stays bounded."""
    import numpy as np
    k = max(3, int(round(_MC_PULSE_DETREND_S * _MC_FPS)) | 1)
    On = np.asarray(On, dtype="float64")
    if len(On):
        cs = np.concatenate([[0.0], np.cumsum(On)])
        idx = np.arange(len(On))
        lo = np.clip(idx - k // 2, 0, len(On))
        hi_i = np.clip(idx + k // 2 + 1, 0, len(On))
        On = On - (cs[hi_i] - cs[lo]) / np.maximum(1, hi_i - lo)
    wp = int(round(_MC_PULSE_WIN_S * _MC_FPS))
    lo, hi = int(round(0.25 * _MC_FPS)), int(round(1.5 * _MC_FPS))
    rows = _mc_window(On, wp, n_frames)
    nfft = 1 << (2 * wp - 1).bit_length()
    out = np.zeros(rows.shape[0], dtype="float32")
    for s in range(0, rows.shape[0], 512):
        ow = np.array(rows[s:s + 512], dtype="float64")
        ow -= ow.mean(axis=1, keepdims=True)
        X = np.fft.rfft(ow, nfft, axis=1)
        ac = np.fft.irfft(X * np.conj(X), nfft, axis=1)[:, :hi]
        ac0 = ac[:, 0]
        ok = ac0 > 1e-12
        vals = np.zeros(len(ac0))
        vals[ok] = ac[ok, lo:hi].max(axis=1) / ac0[ok]
        out[s:s + len(vals)] = vals
    return out


def _mc_features(pcm, n_frames):
    """Acoustic talk cues per envelope frame → dict of float32 arrays
    ``lefr``, ``depth``, ``lowr``, ``pulse`` (or None for a too-short clip).

      lefr  — share of short frames whose speech-band energy is < 0.5× the
              3 s window mean (speech pauses between words/phrases; high = talk)
      depth — p90 − p10 of speech-band energy in dB over 3 s (deep = talk)
      lowr  — 30-95 Hz share of total energy over 3 s (a band is playing)
      pulse — beat clarity over 6 s (see ``_mc_pulse``; low = talk)
    """
    import numpy as np
    Ev, El, Et, On = _mc_short_feats(pcm)
    if len(Ev) < _MC_PER:
        return None
    w = int(round(_MC_WIN_S * _MC_FPS))
    ev_rows = _mc_window(Ev, w, n_frames)
    el_rows = _mc_window(El, w, n_frames)
    et_rows = _mc_window(Et, w, n_frames)
    nF = ev_rows.shape[0]
    lefr = np.zeros(nF, "float32")
    depth = np.zeros(nF, "float32")
    lowr = np.zeros(nF, "float32")
    for s in range(0, nF, 1024):                 # frame blocks bound memory
        ev = np.asarray(ev_rows[s:s + 1024])
        lefr[s:s + len(ev)] = np.mean(ev < 0.5 * ev.mean(axis=1, keepdims=True), axis=1)
        p10, p90 = np.percentile(10.0 * np.log10(ev + 1e-10), [10, 90], axis=1)
        depth[s:s + len(ev)] = p90 - p10
        lowr[s:s + len(ev)] = (np.asarray(el_rows[s:s + 1024]).sum(axis=1)
                               / (np.asarray(et_rows[s:s + 1024]).sum(axis=1) + 1e-12))
    return {"lefr": lefr, "depth": depth, "lowr": lowr, "pulse": _mc_pulse(On, nF)[:nF]}


_VAD_MODEL = None
_VAD_FAILED = False
_VAD_LOCK = threading.Lock()


def _vad_model():
    """The Silero VAD model bundled with faster-whisper (loaded once), or None
    when faster-whisper / onnxruntime / the asset are unavailable — in which
    case MC detection uses the strict acoustic fallback."""
    global _VAD_MODEL, _VAD_FAILED
    with _VAD_LOCK:
        if _VAD_MODEL is None and not _VAD_FAILED:
            try:
                _ensure_deps()
                try:
                    # This is the app's first onnxruntime session. Microsoft's official
                    # Windows builds log usage telemetry (ETW TraceLogging) unless it
                    # is turned off. The app sends no telemetry (constitution
                    # Principle III), so it is switched off before the session is
                    # created. The call is process-wide and idempotent.
                    import onnxruntime
                    onnxruntime.disable_telemetry_events()
                except Exception:
                    pass
                from faster_whisper.vad import get_vad_model
                _VAD_MODEL = get_vad_model()
            except Exception as e:
                _VAD_FAILED = True
                log.info("concert-audio: speech model unavailable (%s) — acoustic "
                         "MC fallback", str(e)[:120])
        return _VAD_MODEL


def _silero_frame_probs(x, t0=0.0):
    """Mean Silero speech probability per ENVELOPE frame (0.5 s) of float32 mono
    16 kHz audio ``x`` → float32 array of ``len(x) // 8000`` values, or None when
    the model is unavailable. ``t0`` (the chunk's start in video seconds) is part
    of the ``speech_fn`` contract but unused here. Calls are split into ≤30 s blocks (bounded
    memory); each block is fed ~1 s of preceding audio that is discarded, so the
    recurrent state is warm at the block edge."""
    import numpy as np
    model = _vad_model()
    if model is None:
        return None
    CH = 512                                   # Silero window (samples)
    hop = int(_SR * _HOP_S)
    n_frames = len(x) // hop
    if n_frames <= 0:
        return np.zeros(0, "float32")
    total_ch = -(-(n_frames * hop) // CH)      # ceil
    xp = np.zeros(total_ch * CH, dtype="float32")
    m = min(len(x), total_ch * CH)
    xp[:m] = x[:m]
    probs = np.zeros(total_ch, dtype="float32")
    blk = max(1, int(_MC_VAD_BLOCK_S * _SR) // CH)
    warm = int(round(_MC_VAD_WARM_S * _SR)) // CH
    for c0 in range(0, total_ch, blk):
        c1 = min(total_ch, c0 + blk)
        w0 = max(0, c0 - warm)
        out = np.asarray(model(xp[w0 * CH:c1 * CH])).reshape(-1)
        probs[c0:c1] = out[c0 - w0:c0 - w0 + (c1 - c0)]
    # average the chunks whose START lies inside each 0.5 s frame
    edges = (np.arange(n_frames + 1) * hop) // CH
    cs = np.concatenate([[0.0], np.cumsum(probs, dtype="float64")])
    cnt = np.maximum(1, edges[1:] - edges[:-1])
    return ((cs[np.minimum(edges[1:], total_ch)] - cs[edges[:-1]]) / cnt).astype("float32")


def _runs(mask):
    """[(start, end), ...] index pairs of the True runs of a 1-D bool array."""
    import numpy as np
    m = np.asarray(mask, dtype=bool)
    if not m.any():
        return []
    d = np.diff(np.concatenate([[0], m.astype(np.int8), [0]]))
    return list(zip(np.flatnonzero(d == 1).tolist(), np.flatnonzero(d == -1).tolist()))


def _majority(raw, k):
    """Centred majority vote over ``k`` frames (edge-padded): a frame is True
    when more than half of its neighbourhood is True. Removes 1-2 frame flicker
    in both directions."""
    import numpy as np
    from numpy.lib.stride_tricks import sliding_window_view
    raw = np.asarray(raw, dtype=bool)
    k = max(1, int(k) | 1)                      # odd window
    if len(raw) == 0 or k == 1:
        return raw.copy()
    rp = np.pad(raw.astype(np.int8), k // 2, mode="edge")
    return sliding_window_view(rp, k).sum(axis=1) > k // 2


def _keep_runs(mask, min_len):
    """Copy of ``mask`` keeping only True runs of at least ``min_len`` frames."""
    import numpy as np
    out = np.zeros(len(mask), dtype=bool)
    for a, b in _runs(mask):
        if b - a >= min_len:
            out[a:b] = True
    return out


def _join_gaps(mask, max_gap):
    """Copy of ``mask`` with False gaps shorter than ``max_gap`` frames filled
    when they sit BETWEEN two True runs (a cheer in the middle of a talk block
    must not split it into two blocks with a fake 'song' in between)."""
    import numpy as np
    out = np.asarray(mask, dtype=bool).copy()
    runs = _runs(out)
    for (a0, b0), (a1, b1) in zip(runs, runs[1:]):
        if a1 - b0 < max_gap:
            out[b0:a1] = True
    return out


def _dilate(mask, before, after):
    """Copy of ``mask`` with every True run widened by ``before`` frames to the
    left and ``after`` frames to the right (clipped to the array)."""
    import numpy as np
    out = np.zeros(len(mask), dtype=bool)
    for a, b in _runs(mask):
        out[max(0, a - before):min(len(out), b + after)] = True
    return out


def _speech_prob(pcm, cand, speech_fn):
    """Speech probability per envelope frame, evaluated ONLY on candidate
    frames (dilated by ``_MC_CAND_PAD_S`` and merged) — 0 elsewhere. Songs with
    a beat are never candidates, which keeps the model's CPU cost to the few
    minutes of a concert that could be talk. Returns None if ``speech_fn`` ever
    reports the model unavailable (caller falls back to the acoustic rule)."""
    import numpy as np
    nF = len(cand)
    sp = np.zeros(nF, dtype="float32")
    pad = int(round(_MC_CAND_PAD_S / _HOP_S))
    hop = int(_SR * _HOP_S)
    spans = []
    for a, b in _runs(cand):
        a, b = max(0, a - pad), min(nF, b + pad)
        if spans and a <= spans[-1][1]:
            spans[-1][1] = max(spans[-1][1], b)
        else:
            spans.append([a, b])
    for a, b in spans:
        p = speech_fn(_as_f32(pcm[a * hop:b * hop]), t0=a * _HOP_S)
        if p is None:
            return None
        p = np.asarray(p, dtype="float32")
        m = min(len(p), b - a)
        sp[a:a + m] = p[:m]
    return sp


def _mc_intervals(pcm, n_frames, speech_fn=None, tune=None):
    """Detect MC / talk stretches → ``(mask, intervals, info)``.

    mask       bool per envelope frame (True = talk), length ``n_frames``
    intervals  [[start_s, end_s], ...] in VIDEO seconds, sorted, disjoint,
               each ≥ ``concert_mc_min_s``
    info       {"detector": "speech+acoustic" | "acoustic-fallback" | "none",
                "frames": int, "speech_evaluated_s": float}

    FUSED RULE (speech model available)::

        speech ≥ concert_mc_speech_min
        AND pulse < concert_mc_pulse_max          (no steady beat)
        AND (lefr ≥ 0.45 OR depth ≥ 16 dB)         (talk-like pauses)

    then an 11-frame majority vote, a minimum run of ``concert_mc_min_s``, and
    runs closer than 4 s joined (a cheer in the middle of a talk block).
    Measured on 23 real songs: 59.5 s flagged in 4880 s (Silero alone: 498 s —
    it calls rap "speech"; the beat cue removes that) at 92-93 % recall on
    talk (scripts/eval_concert_mc.py --songs).

    FALLBACK (no speech model): pulse < 0.22 AND lefr ≥ 0.52 AND depth ≥ 18 dB
    AND lowr ≤ 0.12 — 0 s false MC on the same songs, ~60 % recall.

    ``speech_fn(x_f32, t0=start_s) -> per-frame probs | None`` overrides the
    model (tests, evaluation); by default the bundled Silero VAD is used."""
    import numpy as np
    t = tune or {}
    none = (np.zeros(max(0, n_frames), dtype=bool), [],
            {"detector": "none", "frames": 0, "speech_evaluated_s": 0.0})
    feats = _mc_features(pcm, n_frames)
    if feats is None:
        return none
    nF = len(feats["lefr"])
    lefr, depth, lowr, pulse = feats["lefr"], feats["depth"], feats["lowr"], feats["pulse"]
    speech_min = float(t.get("concert_mc_speech_min", _MC_SPEECH_MIN))
    pulse_max = float(t.get("concert_mc_pulse_max", _MC_PULSE_MAX))
    min_len = max(1, int(round(float(t.get("concert_mc_min_s", _MC_MIN_S)) / _HOP_S)))
    fn = speech_fn if speech_fn is not None else (
        _silero_frame_probs if _vad_model() is not None else None)
    raw, detector, evaluated = None, "acoustic-fallback", 0.0
    if fn is not None:
        cand = (pulse < pulse_max) & ((lefr >= _MC_LEFR_MIN) | (depth >= _MC_DEPTH_MIN))
        sp = _speech_prob(pcm, cand, fn)
        if sp is not None:
            raw = cand & (sp >= speech_min)
            detector = "speech+acoustic"
            evaluated = float(np.count_nonzero(sp)) * _HOP_S
    if raw is None:
        raw = ((pulse < _MC_FB_PULSE_MAX) & (lefr >= _MC_FB_LEFR_MIN)
               & (depth >= _MC_FB_DEPTH_MIN) & (lowr <= _MC_FB_LOWR_MAX))
    mask = _keep_runs(_majority(raw, _MC_SMOOTH_K), min_len)
    mask = _join_gaps(mask, int(round(_MC_JOIN_GAP_S / _HOP_S)))
    full = np.zeros(max(0, n_frames), dtype=bool)
    full[:min(nF, len(full))] = mask[:len(full)]
    intervals = [[round(a * _HOP_S, 2), round(b * _HOP_S, 2)] for a, b in _runs(full)]
    return full, intervals, {"detector": detector, "frames": nF,
                             "speech_evaluated_s": round(evaluated, 1)}


def _mc_frac(intervals, start, end):
    """Share of ``[start, end)`` covered by MC ``intervals`` (0..1)."""
    span = float(end) - float(start)
    if span <= 0:
        return 0.0
    cov = sum(max(0.0, min(float(e), end) - max(float(s), start)) for s, e in intervals)
    return round(min(1.0, cov / span), 3)


# ── ONSETS & SEGMENTATION ─────────────────────────────────────────────────────
def _sustained_onset(times, rms, vocal, seg_start, seg_end,
                     floor, hop_s=_HOP_S, sustain_s=1.2, skip=None):
    """VIDEO-seconds where singing first SUSTAINS inside [seg_start, seg_end).

    Walks the vocal-energy frames from the segment start and returns the first
    time a run of ≥ ``sustain_s`` stays above the adaptive ``floor``. This is the
    lyric anchor — it skips the intro / applause dead-space at the top of a song
    so the first line lands ON the first sung word.

    ``skip`` (bool per frame, optional) marks frames that can never be the onset
    — MC talk. Talk is tonal and in the vocal band, so without it a chapter that
    opens with the host introducing the song anchored the lyrics to the TALK
    (measured: 3 of 3 cases). Falls back to seg_start when nothing qualifies."""
    import numpy as np
    need = max(1, int(sustain_s / hop_s))
    lo = int(np.searchsorted(times, seg_start))
    hi = int(np.searchsorted(times, seg_end))
    run = 0
    for i in range(lo, min(hi, len(vocal))):
        if skip is not None and i < len(skip) and skip[i]:
            run = 0
            continue
        if vocal[i] >= floor:
            run += 1
            if run >= need:
                return float(times[i - need + 1])
        else:
            run = 0
    return float(seg_start)


def _segments_from_energy(times, rms, vocal, floor, min_song_s=_MIN_SONG_S,
                          min_gap_s=_MIN_GAP_S, hop_s=_HOP_S, gap_mask=None):
    """Derive song regions from the envelope alone (NO chapters).

    A song = a run of frames whose vocal energy is mostly above ``floor``,
    lasting ≥ ``min_song_s``, separated from the next by a quiet/low-vocal gap of
    ≥ ``min_gap_s``. Short dips inside a song (a breath, a quiet bridge) are
    bridged; short blips of energy in a gap (a stray cheer) are ignored.

    ``gap_mask`` (bool per frame, optional) forces frames to count as a gap —
    MC talk. Without it, talk (tonal, vocal-band) glued neighbouring songs into
    one: a measured 5-block mini-concert came back as a single 243 s "song".
    Returns a list of (start_s, end_s)."""
    import numpy as np
    active = np.asarray(vocal) >= floor
    if gap_mask is not None:
        gm = np.zeros(len(active), dtype=bool)
        m = min(len(gm), len(gap_mask))
        gm[:m] = np.asarray(gap_mask[:m], dtype=bool)
        active = active & ~gm
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


def _merge_same_id(segs, mc):
    """Join adjacent ENERGY segments that fingerprint to the same song (both ids
    ≥ 0.60) and sit ≤ ``_ID_MERGE_GAP_S`` apart. They are one song that a
    spoken-style passage split — so the MC intervals inside the joined span are
    dropped too (never pause listening in the middle of a song).
    Returns ``(segments, mc)``; chapter segments are never merged."""
    out = []
    for seg in segs:
        prev = out[-1] if out else None
        if (prev is not None and seg.get("source") == "energy"
                and prev.get("source") == "energy"
                and seg.get("title") and prev.get("title")
                and _norm_for_id(seg["title"]) == _norm_for_id(prev["title"])
                and min(seg.get("id_conf", 0.0), prev.get("id_conf", 0.0)) >= 0.60
                and seg["start"] - prev["end"] <= _ID_MERGE_GAP_S):
            prev["end"] = seg["end"]
            prev["id_conf"] = max(prev["id_conf"], seg["id_conf"])
            prev["merged"] = int(prev.get("merged", 1)) + 1
            continue
        out.append(seg)
    kept = [iv for iv in mc
            if not any(s.get("merged") and s["start"] <= iv[0] and iv[1] <= s["end"]
                       for s in out)]
    for s in out:
        s["mc_frac"] = _mc_frac(kept, s["start"], s["end"])
    return out, kept


# ── IDENTIFICATION (offline fingerprinting) ───────────────────────────────────
def _probe_positions(seg, slice_s, dur_s):
    """Up to three probe START times (VIDEO seconds) inside one segment: the
    vocal onset, onset + 25 s, and the segment midpoint (choruses fingerprint
    best). Clamped so each slice stays inside the segment (and the file);
    probes closer than 3 s to an earlier one are dropped."""
    s = float(seg["start"])
    e = min(float(seg["end"]), float(dur_s))
    if e - max(s, 0.0) < 4.0:
        return []
    on = float(seg.get("onset", s))
    last = max(s, e - slice_s)
    out = []
    for p in (on, on + _ID_PROBE2_S, on + 0.5 * (e - on)):
        p = min(max(p, s), last)
        if all(abs(p - q) >= 3.0 for q in out):
            out.append(p)
    return out


def _vote(hits):
    """Collapse probe hits ``[(title, artist), ...]`` (probe order) into
    ``(title, artist, id_conf)``:

      ≥ 2 hits agree on the title → 0.85 (corroborated — clears the 0.70
                                     override gate downstream)
      exactly 1 hit              → 0.60 (a hint; below the gate)
      ≥ 2 hits, all different    → 0.45 (conflict; earliest probe's title)
      no hits                    → (None, None, 0.0)"""
    if not hits:
        return None, None, 0.0
    groups = {}
    for t, a in hits:
        groups.setdefault(_norm_for_id(t), []).append((t, a))
    best = max(groups.values(), key=len)       # ties → earliest probe
    if len(best) >= 2:
        return best[0][0], best[0][1], 0.85
    if len(hits) == 1:
        return hits[0][0], hits[0][1], 0.60
    return hits[0][0], hits[0][1], 0.45


def _identify_one(pcm, seg, slice_s, dur_s, identify_fn, alive):
    """Fingerprint one segment with early exit: stop as soon as two probes
    agree. Returns ``(title, artist, id_conf)``."""
    hits = []
    n = int(slice_s * _SR)
    for k, p in enumerate(_probe_positions(seg, slice_s, dur_s)):
        if not alive():
            break
        a = int(p * _SR)
        b = min(len(pcm), a + n)
        if b - a < _SR * 4:
            continue
        t, art, _off = identify_fn(_as_f32(pcm[a:b]), sr=_SR,
                                   attempts=2 if k < 2 else 1)
        if t:
            hits.append((t, art))
            if _vote(hits)[2] >= 0.85:
                break
    return _vote(hits)


def _id_order(segs, pos):
    """Segment indices in fingerprinting priority: the one under the playhead,
    then the ones after it (what the viewer will reach next), then the earlier
    ones latest-first. ``pos`` None → plain time order."""
    if pos is None:
        return list(range(len(segs)))
    cur = next((i for i, s in enumerate(segs)
                if s["start"] - 1.0 <= pos < s["end"]), None)
    if cur is None:
        cur = next((i for i, s in enumerate(segs) if s["start"] >= pos), len(segs))
    return list(range(cur, len(segs))) + list(range(cur - 1, -1, -1))


def _identify_segments(pcm, segs, slice_s, dur_s, workers, identify_fn, alive, pos):
    """Fill ``title`` / ``artist`` / ``id_conf`` on every segment, fingerprinting
    up to ``workers`` segments at once in playhead-first order (a pool executes
    submissions FIFO). Worker exceptions leave that segment unidentified."""
    order = _id_order(segs, pos)

    def _store(i, res):
        segs[i]["title"], segs[i]["artist"], segs[i]["id_conf"] = res

    if workers <= 1:
        for i in order:
            if not alive():
                break
            try:
                _store(i, _identify_one(pcm, segs[i], slice_s, dur_s, identify_fn, alive))
            except Exception as e:
                log.info("concert-audio: id failed for segment %d: %s", i, str(e)[:120])
        return
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="concert-id") as ex:
        futs = {ex.submit(_identify_one, pcm, segs[i], slice_s, dur_s,
                          identify_fn, alive): i for i in order}
        for fut in as_completed(futs):
            i = futs[fut]
            try:
                _store(i, fut.result())
            except Exception as e:
                log.info("concert-audio: id failed for segment %d: %s", i, str(e)[:120])


# ── main entry points ─────────────────────────────────────────────────────────
def analyze_concert(url=None, chapters=None, lang=None, max_dur=_MAX_DUR_S,
                    want_ids=True, tune=None, is_seq_current=None, on_partial=None,
                    pos_now=None, speech_fn=None, identify_fn=None,
                    audio_path=None, pcm=None):
    """Download (or read) the concert audio, analyse it offline, and return the
    plan dict described in the module docstring — or None on any failure.

    Parameters
    ----------
    url : str            exact video URL (a title search would land on a
                         different upload whose timing differs — always pass the
                         browser-pushed URL). Ignored when ``audio_path`` or
                         ``pcm`` is given.
    chapters : list[{"start", "title"}] | None
                         YouTube chapters. When ≥ 2 are given they seed the
                         segment boundaries; otherwise segments come from the
                         energy envelope, split at MC talk.
    lang : str | None    reserved for a future per-segment transcription pass.
    max_dur : int        reject downloads longer than this (not one concert).
    want_ids : bool      fingerprint each segment. Off = onsets/MC only, no
                         network, and no partial callback.
    tune : dict | None   engine ``self._tune`` — knobs read here:
                         concert_audio_min_song_s, concert_audio_id_slice_s,
                         concert_audio_floor_frac, concert_audio_id_workers,
                         concert_mc_detect, concert_mc_min_s,
                         concert_mc_speech_min, concert_mc_pulse_max.
    is_seq_current : callable | None
                         ``() -> bool``; abort early when the track changed.
    on_partial : callable | None
                         ``(plan_dict) -> None``, called ONCE with onsets + MC
                         (no ids, ``partial=True``) before fingerprinting.
    pos_now : callable | None
                         ``() -> float`` current playback position; the segment
                         under the playhead is fingerprinted first.
    speech_fn : callable | None
                         ``(x_f32, t0=start_s) -> per-0.5 s speech probs | None``;
                         default: the Silero VAD bundled with faster-whisper.
    identify_fn : callable | None
                         ``(pcm_f32, sr=, attempts=) -> (title, artist, offset)``;
                         default ``recognize.identify_pcm``. Both are dependency
                         injection points for tests and evaluation.
    audio_path : path | None
                         analyse a local file instead of downloading (the file is
                         NOT deleted).
    pcm : array | None   analyse already-decoded 16 kHz mono PCM (int16/float).

    The downloaded audio (never ``audio_path``) is ALWAYS deleted.
    """
    t = tune or {}
    min_song_s = float(t.get("concert_audio_min_song_s", _MIN_SONG_S))
    id_slice_s = float(t.get("concert_audio_id_slice_s", _ID_SLICE_S))
    workers = max(1, int(t.get("concert_audio_id_workers", _ID_WORKERS)))
    mc_on = bool(int(t.get("concert_mc_detect", 1)))

    def _alive():
        try:
            return is_seq_current() if is_seq_current else True
        except Exception:
            return True

    def _pos():
        try:
            return float(pos_now()) if pos_now else None
        except Exception:
            return None

    tmp = None
    try:
        import numpy as np
        stats = {}
        t0 = time.perf_counter()
        if pcm is None:
            if not available():
                return None
            if audio_path is None:
                # 1) DOWNLOAD the exact video's audio (audio-only; deep_transcribe
                #    has the anti-bot resilience + the duration cap we lift).
                import deep_transcribe
                tmp = Path(tempfile.mkdtemp(prefix="dk_concert_"))
                audio, _canon = deep_transcribe._download_audio(url, tmp, max_dur=max_dur)
                if not audio or not _alive():
                    return None
                stats["download_s"] = round(time.perf_counter() - t0, 1)
            else:
                audio = Path(audio_path)
            # 2) DECODE to mono 16 kHz int16 (streaming; float fallback).
            t1 = time.perf_counter()
            pcm, how = _decode(audio)
            if pcm is None:
                return None
            stats["decode"], stats["decode_s"] = how, round(time.perf_counter() - t1, 1)
        else:
            pcm, stats["decode"] = np.asarray(pcm), "given"
        dur_s = len(pcm) / float(_SR)
        stats["dur_s"] = round(dur_s, 1)
        if dur_s < min_song_s or not _alive():
            return None
        t2 = time.perf_counter()
        # 3) ENERGY ENVELOPE over the whole concert.
        times, rms, vocal = _envelope(pcm, sr=_SR)
        if times is None:
            return None
        n = len(times)
        # 4) MC / TALK detection (feeds the floor, onsets and segmentation).
        if mc_on:
            mc_mask, mc, mc_info = _mc_intervals(pcm, n, speech_fn=speech_fn, tune=t)
        else:
            mc_mask, mc, mc_info = np.zeros(n, dtype=bool), [], {"detector": "off"}
        stats["mc_detector"] = mc_info.get("detector")
        # adaptive vocal floor keyed off the LOUD level (90th percentile), NOT
        # the median: in a concert the singing is the MAJORITY of the runtime.
        # Computed from NON-talk frames — talk reads as "vocal" and would
        # otherwise inflate the loud level.
        base = vocal[~mc_mask] if (~mc_mask).sum() >= 20 else vocal
        hi = float(np.percentile(base, 90))
        if not np.isfinite(hi):
            hi = 0.0                    # never let a nan floor silently no-op
        floor = max(1e-3, float(t.get("concert_audio_floor_frac", 0.40)) * hi)

        # 5) SEGMENT: refine chapters if given, else derive from energy. Onsets
        #    skip MC plus an edge margin (MC edges are only known to ±1-2 s).
        skip = _dilate(mc_mask, int(round(_MC_ONSET_PAD_BEFORE_S / _HOP_S)),
                       int(round(_MC_ONSET_PAD_AFTER_S / _HOP_S)))
        segs = []
        ch = [c for c in (chapters or []) if isinstance(c, dict) and "start" in c]
        if len(ch) >= 2:
            ch = sorted(ch, key=lambda c: float(c["start"]))
            for i, c in enumerate(ch):
                s = float(c["start"])
                e = float(ch[i + 1]["start"]) if i + 1 < len(ch) else dur_s
                if e - s < 8.0:
                    continue
                onset = _sustained_onset(times, rms, vocal, s, e, floor, skip=skip)
                segs.append({"start": s, "end": e, "onset": onset,
                             "chapter": str(c.get("title") or "") or None,
                             "source": "chapters"})
        else:
            for (s, e) in _segments_from_energy(times, rms, vocal, floor,
                                                min_song_s=min_song_s,
                                                gap_mask=mc_mask):
                onset = _sustained_onset(times, rms, vocal, s, e, floor, skip=skip)
                segs.append({"start": s, "end": e, "onset": onset,
                             "chapter": None, "source": "energy"})
        for seg in segs:
            seg.setdefault("title", None)
            seg.setdefault("artist", None)
            seg.setdefault("id_conf", 0.0)
            seg["mc_frac"] = _mc_frac(mc, seg["start"], seg["end"])
        stats.update({"analysis_s": round(time.perf_counter() - t2, 1),
                      "mc_count": len(mc),
                      "mc_total_s": round(sum(e - s for s, e in mc), 1),
                      "segments": len(segs),
                      "source": "chapters" if len(ch) >= 2 else "energy",
                      "floor": round(floor, 4)})
        if not _alive():
            return None

        # 6) PARTIAL delivery: onsets + MC are usable NOW; ids take minutes.
        if on_partial and want_ids and (segs or mc):
            try:
                on_partial({"segments": [dict(s) for s in segs],
                            "mc": [list(iv) for iv in mc],
                            "stats": dict(stats), "partial": True})
            except Exception as e:
                log.info("concert-audio: on_partial callback failed: %s", str(e)[:120])

        # 7) IDENTIFY each segment offline (parallel, playhead-first, voted).
        if want_ids and segs:
            t3 = time.perf_counter()
            if identify_fn is None:
                import recognize
                identify_fn = recognize.identify_pcm
            _identify_segments(pcm, segs, id_slice_s, dur_s, workers,
                               identify_fn, _alive, _pos())
            stats["id_s"] = round(time.perf_counter() - t3, 1)
            if stats["source"] == "energy":
                segs, mc = _merge_same_id(segs, mc)
                stats["segments"] = len(segs)
                stats["mc_count"] = len(mc)

        stats["total_s"] = round(time.perf_counter() - t0, 1)
        if not segs:
            log.info("concert-audio: decoded %.0fs of audio but produced NO song "
                     "segments (floor=%.4f, p90=%.4f, mc=%d) — leaving chapters as-is",
                     dur_s, floor, hi, len(mc))
        else:
            log.info("concert-audio: %d segment(s) over %.0fs (%s, %s): %s",
                     len(segs), dur_s, stats["source"], stats.get("decode"),
                     " | ".join(f"{int(s['onset'])}s "
                                f"{(s.get('title') or s.get('chapter') or '?')[:20]}"
                                for s in segs[:10]))
        log.info("concert-audio: MC %d interval(s), %.0fs total (%s)%s",
                 len(mc), stats["mc_total_s"], stats["mc_detector"],
                 (": " + " | ".join(f"{int(a)}-{int(b)}s" for a, b in mc[:8])) if mc else "")
        return {"segments": segs, "mc": mc, "stats": stats, "partial": False}
    except Exception as e:
        log.info("concert-audio: analysis error: %s", str(e)[:160])
        return None
    finally:
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)   # ALWAYS delete the source audio


def analyze(url, chapters=None, lang=None, max_dur=_MAX_DUR_S,
            want_ids=True, tune=None, is_seq_current=None):
    """Backward-compatible wrapper: returns just the list of song segments (or
    None), exactly like the pre-spec-001 API. New callers should use
    ``analyze_concert`` to also receive the MC intervals and a partial plan."""
    res = analyze_concert(url, chapters=chapters, lang=lang, max_dur=max_dur,
                          want_ids=want_ids, tune=tune, is_seq_current=is_seq_current)
    if not res:
        return None
    return res["segments"] or None
