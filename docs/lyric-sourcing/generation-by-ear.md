# Method: Generation by ear (rung 3)

**Source:** `deep_transcribe.py` / `main.py:_begin_generation` / `_generate_loop`,
using **faster-whisper** (ctranslate2 + PyAV + onnxruntime), bundled on the D drive
under `.deps` and loaded with `PYTHONPATH=.deps` in the frozen build.

When no caption track and no provider LRC verify, the app transcribes the **actual
audio** to produce timed lines. This is the only rung that's guaranteed to match the
audio (it IS the audio) but it's the least accurate text and the most expensive.

## Confidence / handling
- Output is marked **`***`** (AI-generated) and `meta.source` starts `generated`.
- A generated cache is treated as **stale**: on the next play of that song the app
  runs a background **upgrade-fetch** to replace it with a real LRC if one now
  exists (`stale = gen or romaji_only` in `_on_track_change`).
- Heavy: a transcription is a ~1–2 s 100%-core burst, so it runs OFF the render
  thread and is NEVER on the automatic sync path (that uses cheap energy
  correlation — [PERF-007]).

**Gate before acting:** only entered when rungs 1–2 returned nothing that verified;
the result is provisional and self-replacing.
