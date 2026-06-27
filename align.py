"""
Sync by listening — align the cached lyrics to the ACTUALLY HEARD audio.

When Shazam can't identify the exact thing playing (a fan MV, a remix, an
"anniversary special ver." with a longer intro), there's no catalog offset to
calibrate against, so the cached LRC timestamps don't line up. This fixes that a
different way: it listens to a few seconds of the live vocals, transcribes them
locally with **faster-whisper**, and fuzzy-matches the transcript against the
song's *already-cached* lyric lines to work out WHERE in the song we actually are
— then returns the sync offset to apply. No catalog and no reference audio needed;
it matches the heard words to the lyrics you already have.

Deliberately **opt-in and on-demand** (transcription is CPU-heavy): it runs only
when you trigger it (tray "Sync by listening" / POST /align), never continuously.
If faster-whisper isn't installed it degrades gracefully (``available()`` is False).

    from align import available, capture_and_align
    if available():
        off = capture_and_align(lines, lang="ja", get_pos=lambda: app.media_pos())

Only the model size and a tiny clip of audio are processed locally; nothing is
uploaded. The model is cached under the app's data dir.
"""
from __future__ import annotations

import difflib
import re
import time

_SR = 16000          # faster-whisper wants 16 kHz mono
_CAP = 9             # seconds of audio to listen to
_MODEL = "base"      # tiny=fastest/weakest … base is a good CPU balance for anchoring
_MIN_RATIO = 0.42    # reject a match this unsure (avoid setting a bogus offset)

_GEN_MODEL = "small"  # generation transcribes for DISPLAY → bigger model = better JP
_models = {}          # cached WhisperModel per size
_FURI = re.compile(r"\(([ぁ-ゖァ-ヺ゛゜ーゝゞ]+)\)")     # half-width furigana readings
_PUNCT = re.compile(r"[\s,.!?;:'\"…、。！？「」『』（）()・，．]+")


_last_error = None      # why available() last returned False (for logging)

# Keep the os.add_dll_directory handles alive: the returned handle REMOVES the dir
# from the DLL search path when garbage-collected, so a discarded handle registers
# nothing. The set dedupes repeated _ensure_deps_path calls. (The CUDA libraries
# ALSO need the dir on PATH — see _ensure_deps_path — because CTranslate2 loads
# cuBLAS/cuDNN by BARE name, a load that does not consult add_dll_directory dirs.)
_dll_dir_handles = []
_dll_dirs_added = set()


def _ensure_deps_path():
    """faster-whisper is heavy and NOT bundled in the lean app. If the user has
    vendored it into `<data_dir>/deps` (next to the .exe, or the repo's `.deps`
    when running from source), make it importable. Appended — so the app's own
    bundled stdlib/numpy keep priority. Also register the C-extension DLL
    directories (ctranslate2, av's FFmpeg libs, tokenizers) so they load inside
    the frozen app, where Windows won't search a vendored package's own folder."""
    import os
    import sys
    try:
        from appdata import data_dir
    except Exception:
        return
    for cand in (data_dir() / "deps", data_dir() / ".deps"):
        if not cand.is_dir():
            continue
        p = str(cand)
        if p not in sys.path:
            sys.path.append(p)
        if hasattr(os, "add_dll_directory"):
            for sub in ("", "ctranslate2", "ctranslate2.libs", "av", "av.libs",
                        "tokenizers", "onnxruntime/capi", "numpy.libs",
                        # CUDA runtime (cuBLAS / cuDNN / nvRTC) for GPU transcription
                        # — present only when the GPU extras were vendored or fetched
                        # on demand by gpu_setup.
                        "nvidia/cublas/bin", "nvidia/cudnn/bin",
                        "nvidia/cuda_nvrtc/bin"):
                d = cand / sub if sub else cand
                try:
                    ds = str(d)
                    if d.is_dir() and ds not in _dll_dirs_added:
                        _dll_dir_handles.append(os.add_dll_directory(ds))
                        # CTranslate2 loads cuBLAS/cuDNN by BARE name (LoadLibraryW),
                        # which does NOT search add_dll_directory dirs — so also put the
                        # dir on PATH (the legacy DLL search order DOES include PATH).
                        # Without this the GPU model loads but the first encode raises
                        # "Library cublas64_12.dll is not found or cannot be loaded".
                        os.environ["PATH"] = ds + os.pathsep + os.environ.get("PATH", "")
                        _dll_dirs_added.add(ds)
                except Exception:
                    pass


def available() -> bool:
    """True if faster-whisper can be imported (the optional feature is installed
    or vendored into the app's `deps` folder). On failure, the reason is stashed
    in `_last_error` so the caller can log why."""
    global _last_error
    _ensure_deps_path()
    try:
        import faster_whisper  # noqa: F401
        return True
    except Exception as e:
        _last_error = f"{type(e).__name__}: {e}"
        return False


def _plain(jp: str) -> str:
    """A cached line's bare text for matching: drop furigana readings and
    punctuation/spacing so it compares cleanly to an ASR transcript."""
    return _PUNCT.sub("", _FURI.sub("", jp or "")).strip()


def _data_models_dir():
    try:
        from appdata import data_dir
        d = data_dir() / "models"
        d.mkdir(parents=True, exist_ok=True)
        return str(d)
    except Exception:
        return None


_device = {}          # which device each cached model actually loaded on
_last_gen_lang = None  # language Whisper auto-detected on the most recent generation chunk


def _get_model(size=_MODEL):
    if size not in _models:
        import os
        _ensure_deps_path()
        md = _data_models_dir()
        if md:                                       # keep all model cache off C:
            os.environ.setdefault("HF_HOME", md)
            os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
        from faster_whisper import WhisperModel
        m, used = None, None
        # Prefer the GPU (CUDA) — transcription is ~5-10x faster — but ONLY when
        # the CUDA math libs are actually loadable. ctranslate2 builds a CUDA
        # model object even without cuBLAS, then fails on the first encode with
        # "cublas64_12.dll not found" — so a construct-time try/except isn't
        # enough. Probe the DLL first (the GPU extras are an optional ~1.5 GB
        # download via gpu_setup; without them we use CPU, which is fine for the
        # short align/generation clips — a 16 s clip in ~2 s).
        devices = [("cpu", "int8")]
        try:
            import ctypes
            ctypes.CDLL("cublas64_12.dll")          # raises if not on the DLL path
            devices = [("cuda", "float16"), ("cpu", "int8")]
        except Exception:
            pass
        # Bound CPU inference to a FEW threads. CTranslate2 otherwise defaults to
        # every physical core, and on CPU that saturated the render cores (8-15)
        # during a transcribe — the audio/scroll stutter. 4 threads keep short
        # int8 clips fast while leaving headroom for the render + audio. Ignored
        # on CUDA. This protects the periodic sync-tier listen (and the applause
        # resync / "sync by listening" button) from hitching the overlay.
        for device, ctype in devices:
            try:
                m = WhisperModel(size, device=device, compute_type=ctype,
                                 download_root=md, cpu_threads=4)
                used = device
                break
            except Exception:
                continue
        _models[size] = m or WhisperModel(size, device="cpu", compute_type="int8",
                                          download_root=md, cpu_threads=4)
        _device[size] = used or "cpu"
        try:        # surface GPU-vs-CPU once per model so it's visible in the log
            import logging
            logging.getLogger("karaoke").info(
                "whisper model %r loaded on %s", size, _device[size])
        except Exception:
            pass
    return _models[size]


def _capture(seconds=_CAP):
    """Record `seconds` of system audio output (WASAPI loopback) as a mono
    float32 numpy array at 16 kHz, or None if no loopback device."""
    import numpy as np
    import soundcard as sc

    spk = sc.default_speaker()
    mics = sc.all_microphones(include_loopback=True)
    loop = next((m for m in mics if getattr(m, "isloopback", False)
                 and spk and spk.name in m.name), None) \
        or next((m for m in mics if getattr(m, "isloopback", False)), None)
    if loop is None:
        return None
    with loop.recorder(samplerate=_SR, channels=1) as rec:
        data = rec.record(numframes=_SR * seconds)
    arr = data[:, 0] if getattr(data, "ndim", 1) > 1 else data
    return np.asarray(arr, dtype="float32")


def _transcribe(audio, lang, size=_MODEL):
    """Return [(start_s, text), …] segments from faster-whisper, ASR-noisy."""
    lang = {"ja-romaji": "ja"}.get(lang, lang)
    if lang not in ("ja", "ko", "zh", "es", "de", "ru", "en", "fr", "it", "pt"):
        lang = None                                  # let Whisper auto-detect
    model = _get_model(size)
    segments, _info = model.transcribe(
        audio, language=lang, beam_size=1, vad_filter=False,
        condition_on_previous_text=False)
    return [(seg.start, seg.text) for seg in segments if seg.text.strip()]


def transcribe_vocals(lang="ja", seconds=12, size=_GEN_MODEL):
    """Transcribe a few seconds of the LIVE vocals → one plain string (furigana /
    punctuation stripped), or None if too little was sung. Uses the ~250 MB
    faster-whisper *small* model (better than *base* for sung Japanese). Separated
    from scoring so we can transcribe ONCE and then match the same heard text
    against several candidate pools (title-similar first, the whole library if
    needed)."""
    _ensure_deps_path()
    audio = _capture(seconds)
    if audio is None:
        return None
    import numpy as np
    if float(np.sqrt(np.mean(np.square(audio)) + 1e-12)) < 4.0e-3:
        return None                                   # essentially silence
    segs = _transcribe(audio, lang, size=size)
    heard = _plain(" ".join(t for _, t in segs))
    return heard if len(heard) >= 6 else None


def score_candidates(heard, candidates):
    """Rank ``(key, lyric_text)`` candidates by how well the HEARD singing matches
    each one's lyrics — best first. ``partial_ratio`` is char-level so it works for
    Japanese (no word breaks) and matches the short heard window against the full
    lyric body; ``token_set_ratio`` helps romaji / English lines (word reorder, ASR
    slips). This is the local "Shazam by lyrics": the candidate pool IS the
    accumulated knowledge (every song we've cached), so a high match identifies the
    song from what's actually being sung."""
    if not heard or not candidates:
        return []
    from rapidfuzz import fuzz
    ranked = []
    for key, body in candidates:
        b = _plain(body or "")
        if len(b) < 6:
            continue
        score = max(fuzz.partial_ratio(heard, b), fuzz.token_set_ratio(heard, b))
        ranked.append((round(float(score), 1), key))
    ranked.sort(reverse=True)
    return ranked


def decide_song_by_lyrics(candidates, lang="ja", seconds=12, size=_GEN_MODEL):
    """Convenience one-shot (transcribe + score) — used by the ``/decide`` API.
    Returns ``{"heard": …, "ranked": [(score, key), …]}`` or None. The main loop
    uses transcribe_vocals + score_candidates directly so it can escalate from the
    title-similar pool to the WHOLE library on one transcription."""
    if not candidates:
        return None
    heard = transcribe_vocals(lang, seconds, size)
    if not heard:
        return None
    return {"heard": heard, "ranked": score_candidates(heard, candidates)}


def transcribe_for_generation(pos_cap, lang=None, seconds=16, size=_GEN_MODEL):
    """LAST-RESORT lyric generation: capture `seconds` of the live audio and
    transcribe it into timed lyric lines (for songs no provider has). Returns
    ``[{"t":[start,end], "jp": text}, …]`` on the SONG clock (offset by the player
    position `pos_cap` at capture start), or ``[]`` on silence/failure.

    ``lang=None`` (the default) lets Whisper AUTO-DETECT the sung language, so an
    English / Korean cover isn't force-fit into Japanese gibberish; the detected
    language is stashed in ``_last_gen_lang`` for the caller to pin on later chunks.

    Uses a **bigger model** than sync-by-listening (this text is *shown*, not just
    matched) and in-chunk context for the best transcription quality feasible. VAD
    is OFF on purpose: Silero VAD treats SUNG vocals as non-speech and would drop
    whole clips (no lyrics generated); Whisper's own no_speech_threshold still skips
    the instrumental gaps. Still imperfect — the caller marks every generated line
    so the user knows it's machine-made, not official."""
    global _last_gen_lang
    _ensure_deps_path()
    lang = {"ja-romaji": "ja"}.get(lang, lang)
    hint = lang if lang in ("ja", "ko", "zh", "es", "de", "ru", "en",
                            "fr", "it", "pt") else None   # None → Whisper auto-detects
    audio = _capture(seconds)
    if audio is None:
        return []
    import numpy as np
    if float(np.sqrt(np.mean(np.square(audio)) + 1e-12)) < 4.0e-3:
        return []                                    # essentially silence
    try:
        model = _get_model(size)
        # vad_filter=False: Silero VAD classifies SUNG vocals as non-speech and drops
        # the whole clip → 0 generated lines for most music (verified live: VAD on
        # gave 0 segments on the same audio where VAD off transcribed real lyrics).
        segs, _info = model.transcribe(
            audio, language=hint, beam_size=5, vad_filter=False,
            condition_on_previous_text=True)
        _last_gen_lang = getattr(_info, "language", None) or _last_gen_lang
    except Exception:
        return []
    out = []
    for s in segs:
        t = (s.text or "").strip()
        if len(t) < 2:
            continue
        out.append({"t": [round(pos_cap + float(s.start), 2),
                          round(pos_cap + float(s.end), 2)], "jp": t})
    return out


def _best_anchor(segments, lines):
    """Find the (segment_time_in_clip, cached_line) pair that matches best.
    Returns (seg_t, line, ratio) or None."""
    plains = [(_plain(ln.jp), ln) for ln in lines]
    plains = [(p, ln) for p, ln in plains if len(p) >= 3]
    if not plains:
        return None
    best = None
    for seg_t, text in segments:
        t = _PUNCT.sub("", text)
        if len(t) < 3:
            continue
        for p, ln in plains:
            r = difflib.SequenceMatcher(None, t, p).ratio()
            # reward a strong partial hit (ASR clip often = part of a line)
            if len(t) < len(p):
                r = max(r, difflib.SequenceMatcher(None, t, p[:len(t) + 4]).ratio())
            if best is None or r > best[2]:
                best = (seg_t, ln, r)
    return best


def capture_and_align(lines, lang="ja", get_pos=None, seconds=_CAP):
    """Listen, transcribe, and return the sync OFFSET (seconds) to set so the
    lyrics line up with what's heard — or None if it can't tell confidently.

    `get_pos()` must return the player's CURRENT position (seconds); it's read at
    capture start so we can map the heard line's cached time back to a correction.
    """
    if not lines:
        return None
    _ensure_deps_path()
    pos_cap = float(get_pos() or 0.0) if get_pos else 0.0
    audio = _capture(seconds)
    if audio is None:
        return None
    segs = _transcribe(audio, lang)
    if not segs:
        return None
    anchor = _best_anchor(segs, lines)
    if not anchor or anchor[2] < _MIN_RATIO:
        return None
    seg_t, line, ratio = anchor
    # The heard line's real song-time is line.start; in the clip it occurred at
    # pos_cap + seg_t. The offset makes displayed (position+offset) == song time.
    offset = round(line.start - (pos_cap + seg_t), 2)
    if abs(offset) > 600:                            # absolute sanity guard
        return None
    # A LARGER correction must clear a HIGHER confidence bar. A weak ASR match just
    # over the floor that implies a big jump is almost always a mis-anchor on a
    # noisy transcript (observed live: a 0.44 match yanking the offset to -95s),
    # not a real long intro — so scale the required ratio with the jump size. A
    # genuinely large offset (a cinematic intro) still passes if the match is
    # strong; a small drift correction keeps the lenient floor.
    if ratio < _MIN_RATIO + min(0.30, abs(offset) / 200.0):
        return None
    return offset, round(ratio, 2), line.start


if __name__ == "__main__":      # quick manual test (needs something playing)
    print("faster-whisper available:", available())
