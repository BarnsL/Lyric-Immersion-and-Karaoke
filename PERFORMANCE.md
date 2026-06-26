# Desktop Karaoke — Performance Tickets

Dedicated log for rendering / CPU / audio-stutter performance work.
Status: 🔴 open · 🟡 in-progress · 🟢 fixed (deployed)

**The core constraint:** the overlay is a **Tkinter canvas** (software/CPU
rasterization — no GPU). "Enable GPU acceleration" in the tray menu is for
**Whisper transcription (CUDA)**, NOT the scroll. The scroll is CPU-bound by
Tkinter's design. Real GPU rendering needs a different window toolkit (see PERF-100).

**How to measure:** `curl 127.0.0.1:8765/diag` → `fps` block
(`render`, `frame_ms`, `worst_ms`, `jitter_ms`, `recent_ms`). A healthy scroll is
`frame_ms ≈ 16`, `jitter < 3`, `worst < 30`. Also watch app CPU in Task Manager
and check for stray `python`/`node` (yt-dlp) processes.

---

## PERF-001 — Hung yt-dlp/test processes saturated CPU → stuttering audio 🟢
Two stray `python` processes at ~110% CPU each (leftover hung yt-dlp/test runs)
were saturating cores, which stutters audio system-wide. Killed them. (Watch for
this: a yt-dlp caption fetch that hangs keeps a thread/process busy.)

## PERF-002 — Caption-fetch pileup 🟢
On a fast playlist each track spawned a new heavy yt-dlp caption fetch (network +
node JS runtime) while previous ones still ran to completion → CPU pile-up.
Fix: single-flight guard (`_captions_fetching`) — one yt-dlp fetch at a time;
lighter yt-dlp (retries 1, socket_timeout 15).

## PERF-003 — Windows timer granularity (the 16/30 ms stutter) 🟢
Windows' default ~15.6 ms timer made Tk's `after(16)` fire at 16 OR 31 ms
unpredictably. `ctypes.windll.winmm.timeBeginPeriod(1)` at startup → steady ~16 ms
frames, jitter 10 → 1 ms.

## PERF-004 — CPU affinity / priority (static audio stutter) 🟢
Windows runs the audio engine + device interrupts on core 0; the app sharing
core 0 caused a static stutter in playing audio. Pinned the process to the upper
half of cores (off core 0) + BELOW_NORMAL priority. App CPU also dropped from
32.8% → ~2-8%.

## PERF-005 — Karaoke fill re-rendered every glyph each fill-step 🟢
The sung-fill re-rendered every glyph WITH stroke outlines (~8-9 draws/char) ~5×/s
per singing line — 27-44 ms spikes. Fix: render base + sung LAYERS once, composite
per-fill via a cheap rectangle mask (no glyph render). Sung layer built lazily at
first-sing so it doesn't double the spawn cost.

## PERF-006 — Skip energy correlation on caption songs 🟢
YouTube captions are already video-locked, so running the correlator against them
is wasted CPU. Skipped when `source == youtube-captions`.

## PERF-007 — Periodic Whisper auto-align stuttered scroll + audio 🟢
After faster-whisper was bundled, the **periodic** auto-align (every 15 s) started
using Whisper — a ~1-2 s 100%-core CPU transcription — instead of the light energy
correlation. `render` fell to 22 fps, `worst` 168 ms. Fix: the periodic /
track-start checks use the cheap energy correlation; Whisper is reserved for a
confirmed persistent drift (`reason in {drift, drift-integral}`) or the explicit
"Sync by listening" button.

---

## PERF-100 — GPU-accelerated overlay (future, big win, big effort) 🔴
**Research (June 2026):** Tkinter canvas is CPU-rasterized and cannot be GPU
accelerated; `canvas.move()` walks ALL items each frame (dirty-rect repair model,
O(n)) and re-rasterizes any touching the damaged band. For a true 60-144 fps,
near-zero-CPU scroll, the smoothest Python path is **moderngl + GLFW + pywin32**:
GLFW gives a transparent framebuffer (`GLFW_TRANSPARENT_FRAMEBUFFER`),
always-on-top (`GLFW_FLOATING`), and Win32 `WS_EX_LAYERED|WS_EX_TRANSPARENT` gives
click-through; text becomes a GPU texture atlas, scroll = shift UV/vertex X on the
GPU. DirectComposition is technically ideal but Python-only via hand-written COM
(very high effort). PySide6 QOpenGLWidget works but its GL-translucency is finicky
on Windows. Dear PyGui / Kivy can't do per-pixel-alpha click-through overlays.
Sources: GLFW window guide; moderngl; Tk Performance wiki; swharden scrolling.
**Decision:** only pursue if the quick wins (PERF-101) can't hold 60 fps.

## PERF-101 — Single-strip Tkinter render (quick win, medium effort) 🔴
Research's top low-effort win: pre-render each lyric line to ONE wide PhotoImage
and move that single canvas item, instead of a belt of many per-block images
(many items flicker + cost O(n)/frame; `PhotoImage.paste()` re-uploads when the
image is displayed). Karaoke fill becomes a moving clip-x over the single strip,
not a per-frame paste. Also: shrink the overlay window to the lyric band (smaller
dirty-rect = cheaper repaint), keep integer offsets, hide off-screen items rather
than delete (avoid canvas-ID churn). Candidate next step if PERF-007 isn't enough.
