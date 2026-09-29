# Architecture Overview

| Module | Role |
|---|---|
| `main.py` | `Overlay` engine + Tk renderer: track changes, matching, sync, concerts, tray, `/tune` |
| `MediaWatcher` (main.py) | picks the session to follow — see [[Media Sources]] |
| `fetch_lyrics.py` | provider search + verification + annotation — see [[Lyric Sourcing]] |
| `align.py` | Whisper transcription, resync by listening, decide-by-ear — [[faster-whisper]] |
| `recognize.py` | fingerprint identify (live capture + `identify_pcm` offline) — [[Shazam]] |
| `songchange.py` | cheap live loudness / vocal-band detector (boundaries, onsets) |
| `concert_audio.py` | [[Offline Concert Analysis]] |
| `concert_ocr.py` | on-screen song-banner OCR (chapterless concerts) |
| `yt_description.py` | chapters / setlists / candidate songs from the video page |
| `deep_transcribe.py` | downloads ([[yt-dlp]]), captions, generation by ear |
| `api.py` | local HTTP API (`/status`, `/concert`, `/tune`, …) used by the [[Dev Console]] |
| `tune_docs.py` | documentation for every [[Tune Knobs]] entry |
| `gpu_renderer.py` | optional GPU renderer child process |

Data flow: [[Media Sources]] → track change → [[Lyric Sourcing]] → load → [[Sync Engine]]
every frame; the [[Decision Engine]] keeps checking the choice was right.
