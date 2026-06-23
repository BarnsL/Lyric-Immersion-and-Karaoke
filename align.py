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
                        # CUDA runtime (cuBLAS / cuDNN) for GPU transcription —
                        # present only when the GPU extras were vendored.
                        "nvidia/cublas/bin", "nvidia/cudnn/bin"):
                d = cand / sub if sub else cand
                try:
                    if d.is_dir():
                        os.add_dll_directory(str(d))
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
        # Prefer the GPU (CUDA) — transcription is ~5-10x faster, so generation
        # keeps up with the song. Falls back to CPU when there's no GPU or the
        # CUDA/cuDNN libraries aren't vendored (any load error → CPU).
        for device, ctype in (("cuda", "float16"), ("cpu", "int8")):
            try:
                m = WhisperModel(size, device=device, compute_type=ctype,
                                 download_root=md)
                used = device
                break
            except Exception:
                continue
        _models[size] = m or WhisperModel(size, device="cpu", compute_type="int8",
                                          download_root=md)
        _device[size] = used or "cpu"
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


def _transcribe(audio, lang):
    """Return [(start_s, text), …] segments from faster-whisper, ASR-noisy."""
    lang = {"ja-romaji": "ja"}.get(lang, lang)
    if lang not in ("ja", "ko", "zh", "es", "de", "ru", "en", "fr", "it", "pt"):
        lang = None                                  # let Whisper auto-detect
    model = _get_model()
    segments, _info = model.transcribe(
        audio, language=lang, beam_size=1, vad_filter=False,
        condition_on_previous_text=False)
    return [(seg.start, seg.text) for seg in segments if seg.text.strip()]


def transcribe_for_generation(pos_cap, lang="ja", seconds=16, size=_GEN_MODEL):
    """LAST-RESORT lyric generation: capture `seconds` of the live audio and
    transcribe it into timed lyric lines (for songs no provider has). Returns
    ``[{"t":[start,end], "jp": text}, …]`` on the SONG clock (offset by the player
    position `pos_cap` at capture start), or ``[]`` on silence/failure.

    Uses a **bigger model** than sync-by-listening (this text is *shown*, not just
    matched) with **VAD** (skip the instrumental gaps) and in-chunk context for the
    best transcription quality feasible on CPU. Still imperfect — the caller marks
    every generated line so the user knows it's machine-made, not official."""
    _ensure_deps_path()
    lang = {"ja-romaji": "ja"}.get(lang, lang)
    hint = lang if lang in ("ja", "ko", "zh", "es", "de", "ru", "en",
                            "fr", "it", "pt") else None
    audio = _capture(seconds)
    if audio is None:
        return []
    import numpy as np
    if float(np.sqrt(np.mean(np.square(audio)) + 1e-12)) < 4.0e-3:
        return []                                    # essentially silence
    try:
        model = _get_model(size)
        segs, _info = model.transcribe(
            audio, language=hint, beam_size=5, vad_filter=True,
            condition_on_previous_text=True)
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
    if abs(offset) > 600:                            # sanity guard
        return None
    return offset, round(ratio, 2), line.start


if __name__ == "__main__":      # quick manual test (needs something playing)
    print("faster-whisper available:", available())
