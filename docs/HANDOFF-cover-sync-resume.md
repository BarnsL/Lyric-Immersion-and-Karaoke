# Resume Handoff — Cover (歌ってみた) sync-reliability work

**Date:** 2026-06-29. **Why this file exists:** the "Lyric Immersion 3 handover" session was
mid-investigation into why **cover / 歌ってみた highlighting breaks**, running an ultracode design
workflow (`sync-reliability-batch`), when the app/preview crashed on a Windows GDI memory error.
This doc captures what was established before the crash so a fresh session can pick it up without
re-deriving it. Read this together with `AGENT_HANDOFF.md` (canonical released-state, v1.1.17) and
`docs/sync-by-sound/`.

---

## 0. The crash that ended the session (read first)

**Dialog:** `Tk_GetPixmap: Error from CreateDIBSection` → *"Not enough memory resources are
available to process this command."* Then the Claude Code preview pane: *"The preview server
stopped."*

**What it is:** Windows error **1450, ERROR_NO_SYSTEM_RESOURCES**. `CreateDIBSection` is the Win32
call Tk uses to allocate an image pixmap. It failed because the box ran out of a **GDI / commit
resource**, not RAM-in-general. This is the same **commit/handle-exhaustion class** logged for the
Squad crash on this laptop (page file already set to Windows-managed).

**Most likely cause here:** the **Tk CPU renderer** path was the one drawing (the GPU renderer is
default-on but falls back to Tk when its child dies or is disabled), and the Tk renderer churns
GDI pixmaps (per-character canvas surfaces, karaoke fill, furigana ruby). Running that Tk overlay
**inside the Claude Code preview server** while an **ultracode workflow** spawned many concurrent
agent processes pushed GDI handles / commit charge past the limit, so the next `CreateDIBSection`
returned 1450 and took the preview down with it.

**Recover:** close + reopen the preview (as the dialog says); kill any stray overlay process
(`Get-Process -Name Lyric-Immersion-and-Karaoke | Stop-Process -Force`); then continue.

**Prevent (do this when resuming):**
- Do **not** run the Tkinter overlay inside the Claude Code preview during a heavy workflow. Test
  against the **deployed exe** at `D:\DesktopKaraoke` instead, driven via the control API
  (`http://127.0.0.1:8765`) — that path uses the GPU renderer and the existing live-verify endpoints.
- Confirm the **GPU renderer is on** (`/tune gpu_renderer_on=1`, persisted `gpu_renderer`) so the
  GDI-heavy Tk CPU pixmap path is not the one rendering.
- Don't co-run the overlay GUI and a large fan-out workflow at the same time on this box. Run the
  workflow, let it finish, then exercise the overlay.
- If the Tk path must be exercised, raise the per-process GDI cap (registry
  `HKLM\...\Windows\GDIProcessHandleQuota`, default 10000, max 65536) and watch handle count in
  Task Manager (GDI Objects column).

---

## 1. The problem under investigation

**Covers / 歌ってみた highlight badly** even when the right lyrics load. The 2-point and energy
checks "pass" but the karaoke fill sits ahead/behind or hops around. The session was producing a
grounded root-cause list plus a 4-part fix design via the `sync-reliability-batch` workflow.

### Root causes established before the crash (6 of the planned 10 were on screen)
1. **Original LRC ≠ cover timing.** The fetch is title-first, so for a cover it pulls the *original*
   song's LRC. The cover singer (e.g. Isekaijoucho) phrases with different rubato, so the original
   per-line timestamps are wrong **before any correction even fires**.
2. **Cover = live-arrangement = FOLLOW, not freeze.** `_is_cover` routes into `_live_arrangement`,
   so the engine continuously chases the measured offset. On a cover whose drift is large and
   changing, every correction repositions the line at the boundary, so the highlight keeps hopping
   to a "corrected" spot.
3. **Shazam locks the original at a chorus.** The cover fingerprints near the original recording; on
   a repeated chorus Shazam can return an offset pointing at a *different instance* of that chorus,
   and the two-point check still passes because both reads land on the same repeated section.
4. **Energy-align onto the wrong section.** `_auto_align_by_energy` correlates vocal energy against
   the original LRC's expected positions; a cover's different backing makes the peaks line up in the
   wrong place, locking a confidently-wrong offset.
5. **MV-intro dead-space.** A cover MV opens with a cinematic/instrumental intro, so the original
   LRC's time 0 lands tens of seconds in; until onset detection catches up the highlight sits far
   ahead.
6. **`display_lead` fights the slower cover.** The lead + asymmetric window assume the highlight
   sits just ahead of the audio; a cover sung *slower* makes the LRC chronically early, so the lead
   pushes the highlight even further off.

> **Reasons 7–10 and the full workflow synthesis were lost in the crash** (the workflow
> `sync-reliability-batch` had been launched and was still running when the preview died). To
> recover them, **re-run that workflow** (it was an ultracode investigation+design pass). The 4
> design tracks it was asked to produce are below.

### The 4 fix tracks the design workflow was scoped to deliver
1. **CC-for-covers** — when a cover is detected, prefer the **video's own captions**
   (`_maybe_fetch_captions` / the caption path) over the original-song LRC, because the cover's own
   captions match the cover's singing. This is the core fix: the original LRC fundamentally does not
   fit a re-phrased cover.
2. **Drift-recovery** — detect when sync has locked confidently-wrong (causes 3/4) and break out of
   the lock instead of chasing it.
3. **Better force-sync** — make `/forcesync` actually re-seat a cover that the auto path can't.
4. **Diagnostics API** — quantify shown-vs-should line + lag so cover failures are measurable live
   (extends the existing `GET /measure_sync`).

---

## 2. Where to resume

1. **Re-run the design workflow** to regenerate the full 10 reasons + the synthesized fix plan that
   the crash ate (it was an ultracode multi-agent pass named `sync-reliability-batch`). Do it
   **without** the Tk overlay running in the preview (see §0).
2. **Key code paths** (all in `main.py` unless noted):
   - `_maybe_fetch_captions(...)` / caption-apply path — the CC-for-covers lever.
   - `_is_cover` / `cover_signal()` (and `_live_arrangement`) — cover detection + the FOLLOW mode.
   - `_auto_align_by_energy(...)` — energy correlation that mis-locks on covers.
   - `display_lead` + the asymmetric lead/window — the "slower cover reads early" effect.
   - `GET /measure_sync` (api.py) — the diagnostics surface to extend.
3. **Relevant docs:** `docs/sync-by-sound/` (energy-correlation, shazam-two-point, whisper-align),
   `docs/lyric-sourcing/youtube-captions.md` (the CC-for-covers source), `docs/ISSUES.md`
   (open tickets — file the cover-sync work as a new ticket).
4. **Verify on the deployed exe**, not the Tk preview: load a known 歌ってみた cover (the
   Isekaijoucho case from the investigation is a good repro), then watch `/measure_sync` and
   `/diag` while toggling the caption-first path.

## 3. Guardrails (carry over from AGENT_HANDOFF.md)
- **Commit identity = `BarnsL <barnsl@pm.me>`** (author + committer), **no `Co-Authored-By: Claude`
  trailer**. `git fetch` first (multi-machine), push to `master`.
- **No bundled lyrics** ever (TICKET-124, product/legal). `SALES_CONSIDERATIONS.md` stays local-only.
- Build/deploy recipe and the `Remove-Item` deletion guard are in `AGENT_HANDOFF.md` — follow exactly.
- App-launch etiquette: launch minimized, never steal focus, never a visible terminal window
  (the user games fullscreen).
