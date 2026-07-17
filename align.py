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
    # v1.1.77 (TICKET-176): FROZEN APP — register the app's OWN bundled native-lib
    # dirs. faster-whisper imports PyAV, whose C-extension `av._core` loads the
    # FFmpeg DLLs from `av.libs`. PyAV's delvewheel shim adds that dir via a path
    # computed RELATIVE TO av/__init__.py's __file__, which does NOT resolve in the
    # PyInstaller runtime — so `import av` died with "DLL load failed while importing
    # _core", which made available() return False and silently disabled ALL whisper
    # features (generate-by-ear, sync-by-listening, decide-by-ear reject) in EVERY
    # packaged release. The DLLs are present in _internal/av.libs; we just have to
    # register that dir ourselves, BEFORE `import faster_whisper` runs (in available()).
    if getattr(sys, "frozen", False) and hasattr(os, "add_dll_directory"):
        base = getattr(sys, "_MEIPASS", None) or os.path.dirname(sys.executable)
        for sub in ("av.libs", "ctranslate2.libs", "ctranslate2", "tokenizers",
                    "onnxruntime/capi", "numpy.libs",
                    "nvidia/cublas/bin", "nvidia/cudnn/bin", "nvidia/cuda_nvrtc/bin"):
            d = os.path.join(base, sub)
            if os.path.isdir(d) and d not in _dll_dirs_added:
                try:
                    _dll_dir_handles.append(os.add_dll_directory(d))
                    os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
                    _dll_dirs_added.add(d)
                except Exception:
                    pass
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


def model_ready(size=_GEN_MODEL) -> bool:
    """True when the Whisper weights for `size` are already ON DISK, i.e. no
    first-run download is needed. On a NEW INSTALL the library is bundled but the
    MODEL is not — the first WhisperModel() call silently downloads ~200 MB from
    HuggingFace, which on a slow/blocked network hangs or fails for minutes and
    looked like 'stuck generating lyrics'. The caller uses this to show a
    '⬇ downloading' hint and pick a longer stall deadline."""
    try:
        from pathlib import Path
        md = _data_models_dir()
        if not md:
            return True          # no managed dir → default HF cache; just try
        p = Path(md)
        # huggingface_hub snapshot layout: models--Systran--faster-whisper-small/
        # snapshots/<rev>/model.bin (weights may also be *.safetensors)
        for d in p.glob(f"models--*{size}*"):
            if next(d.rglob("model.bin"), None) or next(d.rglob("*.safetensors"), None):
                return True
        d2 = p / size            # bare converted-model dir is also accepted
        if d2.is_dir() and next(d2.glob("model.bin"), None):
            return True
        return False
    except Exception:
        return True              # never block generation on the probe itself


def _plain(jp: str) -> str:
    """A cached line's bare text for matching: drop furigana readings and
    punctuation/spacing so it compares cleanly to an ASR transcript."""
    return _PUNCT.sub("", _FURI.sub("", jp or "")).strip()


# Whisper's stock NON-SPEECH hallucinations — the YouTube outro phrases and bracket
# tags it emits on quiet / instrumental / noisy clips. These poisoned decide-by-ear:
# a verse-gap clip transcribed as "ご視聴ありがとうございました" once scored 100 against a
# library song whose cache contained that very outro, switching away from a correct
# title (Suisei 綺麗事 → Tip Taps Tip). Drop them before matching/generation.
_HALLUCINATION_RE = re.compile(
    r"ご(?:視聴|清聴)\s*ありがとうございま|ご覧いただきありがと|チャンネル登録|高評価|"
    r"次回もお楽しみ|お(?:疲|つか)れ様でした|"
    r"thank(?:s| you)\s*(?:you\s*)?(?:all\s*)?(?:so much\s*)?(?:for\s*)?(?:watch|listen|view)|"
    r"please\s+(?:like|subscribe)|subscribe\s+(?:to|for|now)|like\s+and\s+subscribe|"
    r"subtitles?\s+(?:by|provided)|transcription\s+by|amara\.org|"
    r"\[\s*(?:music|applause|laughter|silence|sound|noise|cheering)\s*\]|"
    r"^\s*[\(（]?\s*(?:music|applause|laughter)\s*[\)）]?\s*$",
    re.I,
)


def _is_hallucination(text: str) -> bool:
    """True if an ASR segment is one of Whisper's non-speech stock hallucinations
    (so it must not drive a song decision or land in generated lyrics)."""
    t = (text or "").strip()
    return (not t) or bool(_HALLUCINATION_RE.search(t))


def _is_degenerate(text: str) -> bool:
    """True if a transcript is a REPETITION-dominated Whisper hallucination — the
    kind a quiet/instrumental intro produces ("me me me me me", "んmememememe",
    "la la la la"). These are not stock outro phrases (so _is_hallucination misses
    them) but they are equally worthless, and worse: a repeated token fuzzy-matches
    a WRONG song at a spuriously high score and switches away from the correct one
    (kamone was title-matched at 112 then lost to feelingradation/ReGLOSS at 98 on
    exactly such a transcript). Two independent signatures:
      • very low character diversity (few distinct chars over a long string), and
      • a single whitespace token making up most of the transcript.
    Real lyrics — Japanese kana/kanji especially — are far more diverse than either.
    """
    t = (text or "").strip()
    collapsed = t.replace(" ", "")
    if len(collapsed) >= 10:
        uniq = len(set(collapsed)) / float(len(collapsed))
        if uniq < 0.30:
            return True
    toks = t.split()
    if len(toks) >= 4:
        from collections import Counter
        most = Counter(toks).most_common(1)[0][1]
        if most / float(len(toks)) >= 0.6:
            return True
    return False


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
_GPU_AVOID_WHEN_GAMING = True  # during a fullscreen game, fall off the GPU (idle 2nd GPU, else CPU)
_GPU_SOLO_OVERRIDE = False     # TICKET-103: single-GPU stays on CPU unless this is True
_CUBLAS_OK = None


def set_gpu_gaming_guard(on: bool):
    """Toggle the 'don't use the game's GPU' behaviour (default ON)."""
    global _GPU_AVOID_WHEN_GAMING
    _GPU_AVOID_WHEN_GAMING = bool(on)


def set_gpu_solo_override(on: bool):
    """TICKET-103: toggle whether single-GPU machines may use the GPU at all.
    Default OFF (the policy stays on CPU on a single-GPU box). Flip to True
    via the gpu_solo_override tune knob to opt back into the speed-up."""
    global _GPU_SOLO_OVERRIDE
    _GPU_SOLO_OVERRIDE = bool(on)


def current_device_choice():
    """Read-only snapshot of the device gpu_setup would pick RIGHT NOW.
    Returns ``(device, index, reason, gpu_count)`` for /diag + tray label."""
    try:
        import gpu_setup
        n = gpu_setup.cuda_device_count()
        if not _cuda_runtime_ok():
            return ("cpu", 0, "no CUDA runtime", n)
        dev, idx, reason = gpu_setup.pick_inference_device(
            _GPU_AVOID_WHEN_GAMING, _GPU_SOLO_OVERRIDE)
        return (dev, idx, reason, n)
    except Exception:
        return ("cpu", 0, "gpu probe failed", 0)


def _cuda_runtime_ok() -> bool:
    """CUDA is usable only when its math libs actually load. ctranslate2 builds a CUDA
    model even without cuBLAS, then fails on the first encode ("cublas64_12.dll not
    found") — so probe the DLL (the GPU extras are an optional ~1.5 GB gpu_setup
    download). Memoized; presence can't change mid-session."""
    global _CUBLAS_OK
    if _CUBLAS_OK is None:
        try:
            import ctypes
            _ensure_deps_path()
            ctypes.CDLL("cublas64_12.dll")
            _CUBLAS_OK = True
        except Exception:
            _CUBLAS_OK = False
    return _CUBLAS_OK


def _select_device():
    """(device, index, compute_type, reason) for a transcription RIGHT NOW: the GPU by
    default, but an idle 2nd GPU or the CPU while a fullscreen game runs, so the AI
    never fights the game for the card (see gpu_setup.pick_inference_device)."""
    if not _cuda_runtime_ok():
        return ("cpu", 0, "int8", "no CUDA runtime")
    try:
        import gpu_setup
        dev, idx, reason = gpu_setup.pick_inference_device(
            _GPU_AVOID_WHEN_GAMING, _GPU_SOLO_OVERRIDE)
    except Exception:
        dev, idx, reason = ("cuda", 0, "default")
    return (dev, idx, "float16" if dev == "cuda" else "int8", reason)


def _affinity_cpu_count():
    """How many logical CPUs THIS process is actually allowed to run on. Under
    the single-core pin policy this is ~2 (one physical core's two SMT threads),
    so we throttle Whisper to keep the audio render thread from being starved."""
    try:
        import psutil
        n = len(psutil.Process().cpu_affinity())
        if n > 0:
            return n
    except Exception:
        pass
    try:
        import ctypes
        k = ctypes.windll.kernel32
        proc = ctypes.c_size_t()
        sysm = ctypes.c_size_t()
        if k.GetProcessAffinityMask(k.GetCurrentProcess(),
                                    ctypes.byref(proc), ctypes.byref(sysm)):
            return max(1, bin(proc.value).count("1"))
    except Exception:
        pass
    import os as _os
    return _os.cpu_count() or 4


def _get_model(size=_MODEL):
    # Re-evaluated each call (cheap: game state is cached): the device can change when
    # a game starts/ends. Models are cached per (size, device) so flipping back never
    # reloads — both the CUDA and CPU copies stay warm.
    dev, idx, ctype, reason = _select_device()
    key = (size, dev, idx)
    if key not in _models:
        import os
        _ensure_deps_path()
        md = _data_models_dir()
        if md:                                       # keep all model cache off C:
            os.environ.setdefault("HF_HOME", md)
            os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
        from faster_whisper import WhisperModel
        # Bound CPU inference threads. CTranslate2 otherwise grabs every core.
        # CRITICAL (audio-glitch fix): under the DEFAULT single-core pin policy
        # (main.py cpu_dedicate_last_core=1 pins the WHOLE process to ONE physical
        # core), 4 Whisper threads SATURATE that shared core and can starve the
        # user's music player's audio render thread → dropouts/"clipping". So cap
        # threads to the process's ACTUAL affinity width: 1 when pinned to a single
        # core, else a few with headroom. Ignored on CUDA.
        nproc = _affinity_cpu_count()
        cput = 1 if nproc <= 2 else min(4, nproc - 1)
        kw = dict(compute_type=ctype, download_root=md, cpu_threads=cput)
        if dev == "cuda":
            kw["device_index"] = idx
        try:
            m = WhisperModel(size, device=dev, **kw)
        except Exception:                            # GPU load failed → CPU fallback
            dev, idx, key = "cpu", 0, (size, "cpu", 0)
            m = _models.get(key) or WhisperModel(size, device="cpu", compute_type="int8",
                                                 download_root=md, cpu_threads=cput)
        _models[key] = m
        _device[key] = dev
        try:        # surface the device + reason once per (model, device) — visible in the log
            import logging
            logging.getLogger("karaoke").info(
                "whisper model %r on %s (%s)", size,
                "cpu" if dev == "cpu" else f"cuda:{idx}", reason)
        except Exception:
            pass
    return _models[key]


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
    try:
        # capture can RAISE (not just return None) on unusual audio devices —
        # soundcard throws COM errors when the default speaker is mid-switch or
        # loopback is unavailable. Never let that kill the caller's thread.
        audio = _capture(seconds)
    except Exception:
        return None
    if audio is None:
        return None
    import numpy as np
    if float(np.sqrt(np.mean(np.square(audio)) + 1e-12)) < 4.0e-3:
        return None                                   # essentially silence
    segs = _transcribe(audio, lang, size=size)
    # Drop Whisper's non-speech hallucinations so a quiet/instrumental clip can't
    # turn "thanks for watching" into a confident (wrong) song match.
    heard = _plain(" ".join(t for _, t in segs if not _is_hallucination(t)))
    if len(heard) < 6 or _is_degenerate(heard):
        return None                                   # silence / repetition-hallucination
    return heard


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
    try:
        # capture can RAISE on unusual audio devices (soundcard COM errors on a
        # fresh machine / device switch) — the generation loop must survive it.
        audio = _capture(seconds)
    except Exception:
        return []
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
        if len(t) < 2 or _is_hallucination(t):       # skip the "thanks for watching" outros
            continue
        out.append({"t": [round(pos_cap + float(s.start), 2),
                          round(pos_cap + float(s.end), 2)], "jp": t})
    return out


def _rank_anchors(segments, lines, top_n=6):
    """Rank EVERY (segment_time_in_clip, cached_line) pair by match ratio and
    return the best ``top_n`` as ``[(seg_t, line, ratio), …]`` (best first).

    Unlike a single best-anchor, this deliberately surfaces the runner-up
    matches. When the heard window is a CHORUS hook that legitimately recurs at
    several timestamps in the song, each occurrence is a separate ``line`` with
    a near-equal ratio — so they all appear here, letting the caller derive a
    candidate offset for each and try them in turn (instead of locking onto one
    arbitrary occurrence: the "chorus trap")."""
    plains = [(_plain(ln.jp), ln) for ln in lines]
    plains = [(p, ln) for p, ln in plains if len(p) >= 3]
    if not plains:
        return []
    scored = []
    for seg_t, text in segments:
        t = _PUNCT.sub("", text)
        if len(t) < 3:
            continue
        for p, ln in plains:
            r = difflib.SequenceMatcher(None, t, p).ratio()
            # reward a strong partial hit (ASR clip often = part of a line)
            if len(t) < len(p):
                r = max(r, difflib.SequenceMatcher(None, t, p[:len(t) + 4]).ratio())
            scored.append((seg_t, ln, r))
    scored.sort(key=lambda x: x[2], reverse=True)
    return scored[:max(1, top_n)]


def _best_anchor(segments, lines):
    """Find the (segment_time_in_clip, cached_line) pair that matches best.
    Returns (seg_t, line, ratio) or None."""
    ranked = _rank_anchors(segments, lines, top_n=1)
    return ranked[0] if ranked else None


def rank_offsets(lines, lang="ja", get_pos=None, seconds=_CAP, top_n=6):
    """Listen ONCE and return a ranked list of candidate sync offsets,
    ``[(offset, ratio, line_start), …]`` best-first, deduped so near-identical
    offsets collapse to one.

    This is the multi-hypothesis cousin of :func:`capture_and_align` (which
    commits to a single best anchor). Each top-ranked (segment, line) match
    yields one candidate offset; a recurring chorus phrase therefore produces
    SEVERAL offsets — one per occurrence in the song. Force Sync tries them in
    rank order and forward-verifies each against later reads, so a wrong
    occurrence (whose lyrics stop matching once the song moves on) is dropped in
    favour of the one that keeps lining up."""
    if not lines:
        return []
    _ensure_deps_path()
    pos_cap = float(get_pos() or 0.0) if get_pos else 0.0
    audio = _capture(seconds)
    if audio is None:
        return []
    segs = _transcribe(audio, lang)
    if not segs:
        return []
    cands = []
    for seg_t, line, ratio in _rank_anchors(segs, lines, top_n=max(top_n * 3, 12)):
        if ratio < _MIN_RATIO:
            continue
        offset = round(line.start - (pos_cap + seg_t), 2)
        if abs(offset) > 600:                        # absolute sanity guard
            continue
        # Same jump-vs-confidence gate capture_and_align uses: a weak match that
        # implies a big correction is almost always a mis-anchor, not a real
        # long intro, so a larger offset must clear a higher ratio bar.
        if ratio < _MIN_RATIO + min(0.30, abs(offset) / 200.0):
            continue
        cands.append((offset, round(ratio, 2), line.start))
    # Collapse near-identical offsets (keep the strongest ratio of each cluster).
    cands.sort(key=lambda x: (-x[1], abs(x[0])))
    deduped = []
    for off, r, ls in cands:
        if any(abs(off - d[0]) <= 1.0 for d in deduped):
            continue
        deduped.append((off, r, ls))
    return deduped[:max(1, top_n)]


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
