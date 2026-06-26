# Desktop Karaoke — Lyric-Rendering Performance Tickets

Dedicated ticketing file for **lyric display performance** (the scrolling karaoke
overlay's frame rate / smoothness). General CPU / audio-stutter / build tickets live
in [PERFORMANCE.md](PERFORMANCE.md); this file is only the on-screen lyric render.

Status: 🔴 open · 🟡 in-progress · 🟢 fixed (deployed) · 🔵 needs-measurement

---

## The core constraint
The overlay is a **Tkinter canvas** — CPU/software rasterised, **no GPU**. Each scroll
line is a pre-rendered RGBA bitmap ("block"). A horizontal scroll dirties the whole
lane band every frame, so Tkinter **re-composites every on-screen block each frame**.
Per-frame cost ≈ **lanes × block_width × block_height**, and "fast" perf mode targets
**30 fps (33 ms/frame)** — that is the ceiling, spikes drag the average below it.

**The 1.5× multiplier:** the user's `font_scale` is **1.5**, so every block is
~**2.25×** the pixels of default. This is the single biggest cost multiplier and the
backdrop to every ticket below.

## How to measure
`curl 127.0.0.1:8765/diag` → `fps` block: `render`, `frame_ms`, `worst_ms`,
`jitter_ms`, `recent_ms`. Healthy = `frame_ms ≈ 33`, few entries > 60 ms.
The tell-tale of a spike problem: steady 33 ms frames with periodic 150–450 ms
outliers (not a uniformly low rate).

## Live tuning knobs (`POST /tune`, no rebuild)
`scroll_max_lanes` · `scroll_spawn_margin` · `fill_skip` · `repaint_budget` ·
`spawn_budget` · `heavy_budget_ms`.

---

## LP-001 — Recurring 150–450 ms spikes: blocks re-rendered every re-entry 🟢
**Measured:** render 9 fps; `recent_ms = [32,33,148,32,33,454,33,32]` — steady belt is
30 fps, killed by periodic spikes. Each spike = one long 1.5×-scale furigana block
(~2500×420 px, hundreds of stroked glyphs ≈ 450 ms) being **re-rendered** every time
the line re-enters (repeated choruses, edge despawn/respawn, lane churn).
**Fix:** per-line **bitmap cache** (`_block_cache`, idx + scale-sig keyed, LRU 32,
cleared on song load). A re-entering line is now a cheap PhotoImage wrap.
**Result:** 9 → 13 fps, worst 473 → 182 ms. Helped, but exposed LP-002 as the
remaining spike.

## LP-002 — Karaoke fill recomposited the whole block every step 🟢
**Measured:** with LP-001 in, spikes persisted every 3rd frame (= `fill_skip`).
Live experiment cutting fill frequency (`repaint_budget 1`, `fill_skip 6`) →
**22 fps**, which proved the **karaoke fill** is the remaining cost: every fill step
did `base.copy()` + full-size mask + full recomposite + full upload (~85 ms).
**Fix (building, v1.0.56c):** **sliver fill** — keep a persistent composited surface
and paste only the *newly-sung strip* into it (PIL `Image.paste` with box+mask, a few
chars wide), then one blit. Drops the recomposite; a fill step falls to ~one frame.
NOTE: Pillow 12's `PhotoImage.paste` has no box, so the *blit* is still whole-image —
the win is removing the PIL recomposite, not the upload.
**Expected:** recurring fill spikes gone → ~30 fps ceiling on steady playback.
**Needs-measurement after deploy.**

## LP-003 — Lane count vs clutter 🟢
**Resolved with the user:** keep **up to 3 lanes** (`scroll_max_lanes` default 3,
capped to what fits on screen by `_relayout_song`). 1 lane did NOT fix performance
(proving lane count was never the real bottleneck — LP-005 was), and the user
prefers the 3-line context. Lanes stay live-tunable.
**Still open (cosmetic):** long lines overflow both screen edges, which reads as
"duplicated". A wrap/shrink-to-fit pass would declutter — separate from fps.

## LP-004 — font_scale 1.5 is a 2.25× cost multiplier 🔴
The biggest single lever. Options: (a) a "performance" toggle that caps the scroll
font even when the static font is large; (b) auto-reduce scale when many rows
(furigana+romaji+EN) push block height up; (c) just inform the user that 1.0–1.2
roughly doubles fps. No code yet — needs a product call.

## LP-005 — First-appearance spawn spike (cache miss) 🟢 SOLVED (glyph atlas)
**Fix that worked:** a **glyph atlas** (`_atlas_tile` + `_glyph_cache`). Each
(glyph, font, colour, stroke) is rasterised ONCE into a tiny cached tile; a line is
composed by `alpha_composite`-ing tiles instead of re-rasterising ~180 stroked
glyphs. Benchmarked **8× faster** (30 → 3.5 ms/line) and verified **pixel-identical**
to the old renderer before shipping (0-pixel diff). Warm-up is now per-unique-glyph
(a few hundred per session), not per-line.
**Measured live (v1.0.56f):** render **57 fps**, frames a steady **16-17 ms**, 1
frame >60 ms in 60 (warm-up). The scroll is no longer render-bound at all — it hits
the loop's full frame rate. This is the fix that makes it smooth on most computers.
Earlier history (for the record):
The cache (LP-001) makes repeats free, but the FIRST time each unique line appears it
renders once (~150–290 ms at 1.5×). This — not lane count — is what made "single lane
still has poor performance". Post-warmup (cache full) the scroll is a smooth 30 fps at
3 lanes (measured), so the whole problem is *paying* for the first render.
**Tried & REVERTED — background prewarm thread:** rendering every line's bitmap on a
daemon thread. **Measured 7 fps during warm-up** — `recent_ms` alternated 33 ↔ 290 ms.
Lesson: **Pillow text rendering holds the GIL**, so a render thread stalls the single
Tk scroll loop. Total render time is *conserved*; the prewarm just front-loads it into
a 20–30 s bad start. (My web-research assumption that Pillow yields the GIL was wrong —
verified empirically.) Net-negative → reverted.
**Shipped instead:** minimise the per-render COST so the unavoidable inline first-pass
spike is small — `_stroke_w()` caps the glyph outline at 2 px (was 3 at 1.5×; stroke is
the dominant cost per python-pillow #6618). Modest (~20–30 %).
**The two real levers remain open:**
1. **Lower `font_scale`** (user set 1.5) — render cost ∝ scale², so 1.0–1.2 roughly
   halves every spike. Biggest immediate win; needs the user's OK (their text shrinks).
2. **Cheaper outline** — render glyphs flat once, build the outline from the alpha via
   a few `alpha_composite` offsets (GIL-releasing C op) instead of per-glyph stroke.
   Could cut render 2–4×. Medium effort, risks fill-alignment — not yet done.
3. Else **LP-100 / LP-101** (single-strip / PyGame-SDL2). The honest ceiling fix.

## LP-006 — 30 fps ceiling in "fast" mode 🔴
Even spike-free, "fast" mode targets 30 fps (33 ms `after`). Smoother 60 fps needs a
higher target AND headroom from LP-002/003 — only worth raising once spikes are gone,
else it just busy-loops. Revisit after LP-002 deploys.

## LP-007 — Rapid radio/playlist song changes → lag & perceived wrong lyrics 🔵
**Symptom:** a YouTube **radio/mix** autoplays a new song every ~3 min; while the
overlay is fps-starved (12 fps, 347 ms worst) it lags catching the change, so for a
few seconds the previous song's lyrics or a desynced line shows → reads as "wrong
song". Verified the lyrics themselves were correct (overlay romaji matched the
video's own captions). Likely eased once LP-002 restores headroom.
**Needs-measurement:** hold on ONE song and confirm sync; re-check after LP-002.

---

## LP-100 — Single composited strip per lane (medium effort) 🔴
Pre-render each visible line into ONE wide strip and move that single item; karaoke
fill becomes a moving clip-x. Removes per-block item churn. See
[PERFORMANCE.md PERF-101](PERFORMANCE.md). The real structural win short of GPU.

## LP-101 — GPU overlay (large effort) 🔴
True 60–144 fps near-zero-CPU scroll needs a GPU toolkit (text → texture atlas,
scroll = shift UV on GPU). **Research found a lower-effort path than moderngl/GLFW:**
a **PyGame/SDL2 window made into a layered click-through overlay via pywin32**
(`SetLayeredWindowAttributes` + `WS_EX_LAYERED|WS_EX_TRANSPARENT`) — SDL2 blits
textures on the GPU, and there's a working
[pywin32+pygame click-through gist](https://gist.github.com/ahmed-shariff/dc7de26423659f1de01430f74f8b0927).
Caveat from research: a layered window can't overlay a game in **exclusive**
full-screen (needs borderless-window mode). moderngl-window is the step up if SDL2
isn't enough. See [PERFORMANCE.md PERF-100]. Only pursue if LP-001/002/005 + LP-100
can't hold 30 fps.

## Research sources
- [python-pillow #6618 — ImageDraw.text() perf](https://github.com/python-pillow/Pillow/issues/6618) — stroked text is slow; cache it (validates LP-001/005).
- [Tk Performance — Tcler's wiki](https://wiki.tcl-lang.org/page/Tk+Performance) — many canvas items ≈ 1 CPU core; cull to viewport.
- [swharden — Tkinter vs PyGame scrolling](https://swharden.com/blog/2010-03-05-smoothly-scroll-an-image-across-a-window-with-tkinter-vs-pygame/) — Tkinter's scroll ceiling vs PyGame.
- [pywin32 + PyGame click-through overlay gist](https://gist.github.com/ahmed-shariff/dc7de26423659f1de01430f74f8b0927) — the LP-101 path.

---

## Measurement log
| Build | Change | render fps | worst ms | note |
|---|---|---|---|---|
| 1.0.56  | lane cap 2 + cull | — | — | baseline still spiked |
| 1.0.56b | + block cache (LP-001) | 9 → 13 | 473 → 182 | repeats fixed, fill spikes remain |
| (live)  | fill_skip 6, repaint 1 | 22 | 113 | proved LP-002 is the fill |
| 1.0.56c | + sliver fill (LP-002) | 27 | 121 | steady 33ms; first-pass spikes remain |
| 1.0.56d | + bg prewarm (LP-005) | 7 (warmup) | 371 | REVERTED — GIL stalls scroll during prewarm |
| 1.0.56e | revert prewarm + stroke cap 3→2 | 22 (warmup) | 144 | smaller first-pass spikes |
| 1.0.56e | **+ font_scale 1.5→1.1, 3 lanes, warm** | **30** | **104** | **steady 30fps, 0 frames >60ms — GOAL** |

**Outcome:** post-warmup is a clean **30 fps at 3 lanes** (the fast-mode ceiling), zero
spikes. The brief per-song first-pass (~15 s while each new line renders once) is now
~22 fps with small ≤144 ms blips (was 7–13 fps with 350–470 ms spikes). The journey:
block cache (LP-001) + sliver fill (LP-002) + cheaper stroke + **font_scale 1.1** (the
scale² lever) did it; background prewarm (LP-005) was the one dead end (GIL).
