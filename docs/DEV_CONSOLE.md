# Developer Console — how to use it

The console is a separate desktop app that reads the running Lyric Immersion app over
its localhost API. It is **read-mostly**: it shows what the engine is thinking, and
lets you change tuning knobs live without a rebuild.

Open it from the tray: **Developer Console**. The app must be running with **Local API
(agent control)** ticked — that is the `127.0.0.1:8765` server everything here reads.

---

## First, the thing that confused everyone

Panels here come in two kinds, and telling them apart is most of learning the console:

- **Live panels** always have data while a track is playing. The *now* strip, the
  identification sources, the sync ladder, the decision engine.
- **Event panels** are populated by something that *happened*: a banner-OCR pass, a
  decide-by-ear gate. These fire on concerts and song boundaries and are otherwise
  **empty — correctly so.** An empty gate ladder means no by-ear decision has had to
  run, which on a correctly identified track is the good outcome, not a fault.

Every event panel now says which it is and why it's empty, rather than showing a bare
dash. If a panel *does* hold data from an earlier video it is dimmed and badged
**previous track** (TICKET-194 — it used to render half-hour-old concert reads as
though they were current).

---

## The live "now" strip

Sits at the top of **Song finder** and **Decisions**. It is never empty while a track
is loaded, and the moving playhead is the quickest proof the console is talking to a
live app.

The headline badge is the one to read first. It answers **"how do we know these are
the right lyrics?"** — not "do the titles match", which is a question that cannot
fail (see below):

| badge | meaning |
|---|---|
| `library body` | a bundled/curated body from your own library — authoritative |
| `words verified` | the words actually being sung were transcribed and matched to this body. The strongest proof available |
| `timing only` | an energy or caption lock aligned the body to the audio. Proves **when** the lines land, not **what** they are |
| `title only` | nothing backs this body except the title that was searched for. The normal state for a freshly fetched song, and the state in which wrong lyrics look perfect |
| `nothing loaded` | no body for this track yet |

A second `title mismatch` badge appears alongside when the loaded body names a
different song from the one searched for — a switch in flight, or the wrong lyrics up.

**Why the badge is not "identified" (TICKET-200).** It used to be, and it was
derived from comparing the loaded body's title against the player's. That is
circular: a body fetched by title search is *filed under the title it was searched
with*, so it agrees with itself no matter which song it actually contains. The
console duly showed a green ✓ **identified** over lyrics for an entirely different
song. `title only` is the honest label for that state.

**`searched for …`** — under the track title, shown only when the title reduction
changed something. This is what the lyric providers were actually asked for, after
`clean_title()` stripped the credits and decorations. When a match goes wrong with
everything else looking healthy, check this first: `IA & ONE / てるみい (石風呂)
【MUSIC VIDEO】` reducing to `IA & ONE` is a whole bug visible in one line.

Tiles: lines loaded · lyric source · language (`+rm`/`+en` mark romaji and translation
availability) · sync offset · drift · renderer · overlay state.

**On `renderer`:** with the GPU overlay on, the app-side Tk frame timer is idle, so
there is no meaningful fps to report and the tile reads `GPU`. That is not a stall —
the overlay renders in its own process. It shows a number only when Tk is drawing.

---

## The views

### Overview
Now-playing, sync offset/drift, render FPS, and the app's own success scorecard.
Start here to answer "is it alive and roughly working".

### Song finder — *what it can see*
The answer to "why did it pick **that**?" Ordered live-first: the *now* strip, then the
identification sources, then the setlist, then the banner reader.

- **Identification sources** *(live)* — SMTC, Shazam, the loaded body (with a warning
  if it is a thin stub), lock state, and mode. On an ordinary track this is where
  identification actually comes from, and it is the panel to read.
- **What the screen reader sees** *(event)* — every line the concert banner OCR read,
  each tagged with its verdict:
  - `used as the song` — this cleared the accept bar (0.85) and set the song
  - `window chrome` — matched an open window/tab title, so it is the browser, not a banner
  - `not on setlist` — this video's setlist doesn't contain it (ads, search box, page copy)
  - `needs 2nd read` — held until it reads identically again; real banners persist, junk doesn't
  - `seen, unused` — read, but nothing acted on it
- **Matched against** — whether the pool was *this video's setlist* or *the whole
  library*. A `wide pool` warning means arbitrary screen text can score a match.
- **Refused strings** — the live list of what was rejected and how often. The log
  rate-limits these to once per 10 minutes; this panel is the current truth.
- **Setlist** — the parsed songs and which already have lyrics cached.

> Real example: a concert showed the wrong song for ten minutes because the OCR read
> `breaking dimensions` — the text still sitting in the YouTube **search box** — and
> matched it against the whole library. This panel would have shown that in one row.
>
> The same panel later caught the bare word `posts` being accepted at 0.856 against the
> whole 373-title library (TICKET-195, still open) — found by reading the console, which
> is what it is for.

### Decisions — *why it kept or changed the song*
Ladders, ordered live-first: **Sync** and **Concert identification** run continuously,
so they lead; the by-ear **Gate ladder** is an event panel and sits below them. The lit
path is what actually ran; greyed steps were never reached, and the step that **ended**
the evaluation pulses so your eye lands on the answer.

- **Sync** *(live)* — arrangement → video-locked? → drift measured → correction deadband
  → **how it may commit** → offset. That commit step is worth reading: it shows whether
  a single read can move sync or whether two agreeing reads are required.
- **Concert identification** *(live)* — live mode → setlist parsed → OCR gate → banner
  read → refused text → outcome. On a normal track it stops at the first step and says
  so.
- **Gate ladder, decide-by-ear** *(event)* — transcript length → candidate vs loaded →
  is the loaded body worth protecting → cross-artist block → absolute score bar → margin
  → lopsided override → outcome. Every threshold is the *effective* one (including the
  +15 title-lock bump), sent by the server rather than duplicated in the UI.
  Empty until a decision runs, and badged **previous track** once the song changes under
  it — deciding by ear means running Whisper, so it is deliberately rare.
- **Decision engine** *(live)* — the four dimensions, strike count, and state.

> Real example: the correct 32-line body scored **67** against a loaded stub scoring
> **0**, and was refused three ways — a title-lock bar of 75, a lopsided margin of 84,
> and a cross-artist block. The ladder renders exactly that.

### Activity — *the narrated decision stream*
The newest view, and the one to watch during a session. Two things in one:

- **Toast notifications** fire when a notable decision happens: the song
  changed, a body was rejected or restored, a sync correction committed or was
  refused, lyrics arrived (especially late), the decision engine escalated, or
  you overrode the title. Each toast slides in from the right (cubic-bezier
  ease-out), is severity-coloured (green = good, amber = warn, blue = info), and
  the warn icon pulses so your eye lands on it. Auto-dismisses after 6s
  (info/good) or 10s (warn).

- **The log panel** stays for the session. Each row carries the full context:
  the video name, the event kind, a human-readable detail line, and the time
  metadata — video position, lyric line time, the **gap** between them (the
  number that tells a sync bug from a correct run; >3s is amber, >10s is red),
  the sync offset in force, the OCR match ratio, and how late lyrics arrived.

This is the view that would have made the hololive Unchained runaway visible in
real time: the runaway showed `pos=16.6` with `lyric_t=72.6` — a 56s gap that
was invisible until someone read `karaoke.log` by hand. The Activity view shows
that gap as a red number on a sliding toast the moment it happens.

#### Every sync correction, whoever made it (TICKET-205)

Sync narration comes from **one place**: `_smooth_offset` in `main.py`, the
funnel all seventeen correction paths go through. Fifteen of those are automatic
(the energy correlator, the OCR sync, the tier and fine-tune paths, align-by-ear,
the live-follow and reset paths, and so on). The other two are the tray's manual
`nudge` and `reset_offset`, deliberately routed through the same funnel so a hand
correction glides like an automatic one and is narrated like one.

Narration used to come from the callers, and only two of them bothered, so a song
that visibly came into sync mid-play produced no toast and no log row. Reporting
from the funnel means a new sync path cannot forget, because reporting is not its
job.

Each sync row reads as *what happened → why → evidence → outcome*:

> **Sync nudge** — Moved the lyrics +3.40s (sync now +3.40s) because the
> audio-energy correlator re-aligned the lyrics to the music. Applied at the end
> of the line, so it did not cut mid-lyric.
> `video 1:12` `lyric 1:15` `gap +3.4s` `moved +0.00s → +3.40s (Δ +3.40s)`

The `moved A → B` chip is the offset either side of the change, so you never
have to do the arithmetic. Corrections are narrated **when they actually land** —
a deferred correction waits for the line boundary and is reported there, and one
that gets cancelled first is never reported at all.

**Sync held back** (`sync-ignored`) is the other new event, and the one worth
understanding. `_smooth_offset` drops corrections below `sync_apply_min_s`
(0.22s, or `sync_apply_min_s_scroll` 0.25s in a scroll layout). A single dropped
correction is meaningless wobble, so this fires only when the same fix has been
proposed and discarded **five times in a row**
(`notable_sync_ignored_streak`) — which means the lyrics are visibly off and
nothing in the engine is able to correct them. Its chip says `wanted A → B`, not
`moved`, because nothing moved.

Every correction that *applied* is narrated, with no additional threshold:
`sync_apply_min_s` has already filtered the noise, and if the engine judged a
move big enough to shift the lyrics on screen then it is big enough to explain.
Set `notable_sync_min_s` above 0.0 if you want it quieter.

The **Wrong Song** panel at the bottom of the Activity view lets you correct a
title the engine reduced badly. It shows the raw video title, what `clean_title`
reduced it to, and any active override. Click **Pick correct title** to see
every title string the engine has seen for this video (player title, cleaned
title, OCR reads, prior overrides) as one-click corrections, or type your own.
The bad string and the good one are logged for diagnostics. No lyric body text
is logged — only titles and metadata, keeping the repo clear of copyright
material.

> Real example: a title `IA & ONE / てるみい (石風呂)【MUSIC VIDEO】` reduced to
> `IA & ONE` — the artist, not the song. Every downstream panel looked healthy.
> The Wrong Song panel would show that reduction in one line, and picking the
> correct title from the seen-strings list would re-fetch immediately.

### Concerts — *the whole live pipeline in one page*
New in v1.1.92 (TICKET-218). Concert handling is the most stateful thing the engine
does, and almost none of it used to be observable: the mode verdict was a bare
boolean whose reason lived only in a log line, the applause integrator counted in
silence, and the chapter list was hard-wired to come back empty. A concert that
behaved oddly gave you a blank panel and no way to tell a real parse failure from a
bug.

The organising principle is **mechanism, live state and its knobs together**. A
threshold means nothing without the value currently being measured against it, which
is why applause is drawn as a meter rather than printed as two numbers. It polls on
the usual 2.5s cadence, and it reads `/concert` directly rather than `/insight`.

**Mode** *(live)* — the verdict, and the rule that produced it. Four outcomes:
`concert` (the title names the **event**, so the title is refused outright and songs
are found by sound and by setlist), `live arrangement` (the title is still trusted,
but the timing differs from the studio cut, so the offset is **followed** instead of
reset to zero), `non-music video`, and `studio`. Two rows carry the provenance:
*Verdict came from* gives the decider and its rule tag, *Because* gives the
specifics. Rules are `duration` (over 10 minutes), `keyword` (and it names which
title keyword matched), the three vetoes `loop-veto` (a looped or extended single
song, so long but not a concert), `aside-veto` and `single-song-veto` (one song
performed at an event, not the event itself), `no-cue`, plus `subs-demote`,
`category-flip` and `nonmusic-demote` recorded when something later in the run
overturns the first reading.

> **The one that surprises people.** Any **cover** sets live-arrangement mode, with
> no live, short or acoustic cue anywhere in the title. That is deliberate: a
> cover's timing differs from the studio original exactly as a live take's does, so
> following the offset is right. But it means the badge is frequently lit on a
> studio-quality cover, and it quietly swaps in the `live` sync profile. The *Live
> arrangement because* row names which of the three inputs actually fired (cleaned
> title, raw player title, or cover), so you never have to assume the title said
> "LIVE" somewhere.

**Applause** *(live)* — a live cut pauses for applause while the player clock keeps
running, so the lyrics drift ahead by the length of the pause. The detector looks for
audio that is loud but **not** tonal singing, and acts when the singing returns. The
meter is the current gap accumulating toward `applause_min_s`; at 100% it arms, and
the pill goes `idle` → `watching` → `armed`. *When a gap completes* is the row to
read, because the same detector has two completely different consequences: in a
concert a gap means **re-identify the next song**, in a single live arrangement it
means **two-point resync by ear**. *Last gap* reports the measured length of the last
one; before TICKET-214 that value was reset one line before it was logged, so every
applause message in the app's history claimed `~0.0s`. If a resync is being confirmed
by a second listen, the two-point row shows the held offset and when it expires.

**Setlist** *(event)* — chapters, from the video's own chapter marks or from
timestamps parsed out of its description. When they exist they drive song changes
deterministically: entering a song chapter fetches that title and anchors the offset
to the chapter start, or to the measured vocal onset if the offline pass found one.
The currently playing chapter is highlighted, and non-song chapters are badged.
Empty means no chapters, which is a real and common state, not a fault: songs then
come from banner OCR and by ear.

**MC, talk and intermissions** *(event)* — the chapters treated as non-song segments.
The engine does not fetch lyrics for these, and the previous song's lyrics stay up
until the next real song chapter starts. **Read the caveat on this card carefully:**
these are recognised by chapter **title** only, against a fixed skip list (intro,
outro, MC, talk, opening, ending, encore, intermission, break, credits, staff roll
and their Japanese equivalents). There is no audio-based MC detector, so a talk block
with a song-like chapter name will be missed, and an **unchaptered concert cannot
report non-song segments at all**. An empty card means "none identified", never "none
present".

**Between songs** *(live)* — the hold. When a new chapter starts and the offline pass
has no measured onset for it, the lyrics are held back until vocals are actually
heard, so they do not run during the applause and the introduction. The card shows
whether it is holding, whether the offset has been anchored past the intro yet, how
long the hold has run, and the point at which it releases regardless. That release
timeout is `mv_intro_timeout`, which is **20.0s**; a stale comment and in-code
fallback claimed 75s until TICKET-216, and were never the real value because the knob
is always registered.

**Offline audio plan** *(event)* — the offline pass downloads the audio once and
measures each segment's true vocal **onset**, so lyrics anchor past the applause and
intro rather than to the chapter mark. Each segment shows `onset` with the measured
time, or `no onset` meaning the chapter start is used instead. The segment covering
the current position is highlighted.

**Watchdogs** *(live)* — two backstops for the characteristic concert failure, where
the video moved on and the overlay did not.

- **Unconfirmed switch**: a song heard but not yet confirmed by a second read.
  It escalates after 15s when nothing is on screen and 90s when lyrics are already
  up, because a blank overlay is the worse state to sit in.
- **Same song showing for**: a concert song still up after 6.5 minutes (390s) almost
  certainly changed and the boundary was missed, so a re-identify is forced.

**Live resync cadence** *(live)* — a live arrangement drifts continuously, so the
engine re-listens on a loop and backs off as confidence grows: fast while it is still
catching up, slow once it has been in sync for several passes running. The card shows
the in-sync streak, how many passes it takes to relax, the current gap between
listens, and the three tier values.

**Banner OCR** *(event)* — many concert streams caption the current song on screen,
and the engine reads that banner as an identification source. The important row is
**why it is blocked**, because this is the most misunderstood part of concert
handling. OCR is suppressed entirely, in this order of precedence:

| shown reason | meaning |
|---|---|
| chapters are present | a chapter setlist exists, so it drives song changes instead. The whole documented OCR pipeline never runs on a chaptered concert |
| the video is not a concert | studio or live-arrangement mode; OCR is concert-only |
| this video was judged non-music | the non-music demotion fired |
| banner OCR is switched off | the `concert_ocr` toggle is off |

For what the reader actually *read*, and how each line was judged, use **Song
finder → What the screen reader sees**. This card answers only whether it is running.

**Knobs that steer concert behaviour** — every concert-relevant knob, grouped by the
mechanism it steers (applause detection, setlist and chapters, offline audio
analysis, live sync thresholds, resync cadence, the live energy correlator, banner
OCR, the between-songs hold) rather than as a flat alphabetical wall. Click a value
to edit it, hover the name for its registered documentation. Same contract as
**Parameters**: immediate, runtime-only, gone on restart. A knob the engine
*refuses* now says so inline instead of appearing to have been accepted (TICKET-220).

Validated by `scripts/probe_concert.py` (20 checks).

### Runtime map
Static architecture diagram — how audio, metadata, lyric sourcing and rendering fit
together.

### Parameters
Every live-tunable knob, grouped. Changes apply **immediately** and are **not
persisted** unless you ask — restart restores defaults. Safe to experiment.

### AutoResearch
Status of the offline knob-research loop, and the prompt template for driving it.
The **Loop status** card reports reality: whether the worktree exists, how many
`experiment:` commits it has, whether it is ahead of master, and whether the skill is
installed. If it says *has never run*, it has never run.

See [AUTORESEARCH.md](AUTORESEARCH.md) for the runner (`scripts/autoresearch.py`) and,
importantly, the list of metrics that are **not** safe to optimise against.

### Library — *making a fresh install testable*
Added in v1.1.91. Both halves exist for the same reason: a developer machine has
already cached everything and has every optional component installed, which is
exactly the machine on which first-run bugs are invisible.

**Lyric cache** (TICKET-210) — every lyric body the app has saved, with the count and
the bytes on disk. Each row gives the title, artist, language, source, line **count**,
file size and how long ago it was written; the currently loaded song is badged
`playing`, and a show transcript is badged `subtitle`. Filter by title, artist or
filename. **Refresh** re-reads the directory.

**Clear cache** asks first, then offers **Delete all** or **Keep current** (everything
except the song playing right now). Clearing makes the next play of each song go
through the full find-and-fetch path, which is the only way to see what a new user
sees. Cached lyrics come back automatically as songs play, but anything that was
generated by ear has to be regenerated by listening again, so that is the one
genuinely expensive thing to throw away.

> **Copyright.** The table shows metadata and a line count, never lyric text. That is
> enforced at the engine, not here: `/lyric_cache` excludes the body, romanisation and
> translation fields outright rather than the console merely declining to render them.

**Optional components** (TICKET-211) — the heavy optional pieces, and what the app
would do without each. Availability is decided purely by "is it on disk", so
exercising the without-it paths used to mean deleting several gigabytes and
re-downloading them. The switch makes the engine **act** as though a piece is missing.

| component | what its absence changes |
|---|---|
| `whisper` | generate-by-ear, sync-by-listening and the wrong-lyrics reject path all decline with a hint. The single biggest difference between a lean install and a full one |
| `model` | the weights (about 2 GB) download on first use, so simulating absence shows the first-run download message and a longer stall timeout, not a failure |
| `gpu` | the CUDA and cuBLAS libraries (about 1.9 GB). Everything still works on the CPU, just slower, and full-episode subtitle transcription is skipped |
| `ytdlp` | deep generation quietly does nothing, and pulling a video's caption track shows a needs-yt-dlp hint |
| `node` | a JS runtime on PATH, which yt-dlp needs to get past YouTube's anti-bot checks on audio downloads. **Detected only**, not simulatable |

A component that is genuinely not installed is badged `absent` and its switch is
disabled, since there is nothing to pretend about. One being simulated is badged
`simulated missing`, and the card header shows `simulating a lean install` so you
cannot forget the machine is lying to you. The row also reports real GPU status and
any whisper import error. Simulating absence does not re-run the first-time download.

Note that these switches are **controlled** inputs bound to `/insight`. Until
TICKET-221 the view was not on the insight poll list at all, so arriving here
directly left the panel unrendered, and arriving via Activity showed it frozen:
toggling a switch POSTed successfully and then snapped back, which read as broken
rather than stale. If you ever see that again, the poll list in `App.tsx` is the
place to look.

### Resources
Links to worktrees, docs, corpora and the API endpoints.

---

## Reading the numbers

**Sync profile** (`studio` / `live` / `concert`) decides which thresholds are in force.
They are genuinely different state machines:

| | studio | live arrangement | concert |
|---|---|---|---|
| single-read commit | ≤ 2.0s | ≤ 1.2s | ≤ 1.8s |
| confirm gap | 1.5s | 2.5s | 1.2s |

A concert is looser on purpose: it changes song every few minutes, so a slow
two-point verification often never completes — and being briefly wrong is recoverable
while being slow means no lyrics at all.

**`loaded_worthless`** — when the loaded body is under 8 lines *and* scores ≤ 8, the
protections that normally defend it (title-lock bump, cross-artist block) are lifted.
You cannot protect a right song you don't have.

**TPVR evidence** (`/insight.tpvr`) — two-point-verify agree/disagree tallies keyed by
the gap in force, e.g. `concert@1.20 → {agree: 14, disagree: 2}`. A short gap that
still agrees is evidence it can come down; one that starts disagreeing is evidence it
cannot. This is the honest objective for the gap knobs.

---

## Troubleshooting

**"Hmmm… can't reach this page — 127.0.0.1 refused to connect"**
The console binary was built without embedding its frontend, so it is trying to load a
Vite dev server that isn't running. **Cause:** it was built with `cargo build
--release` instead of the Tauri CLI. Plain cargo does not set the env the
`tauri-build` step needs, so it never embeds `frontendDist` and bakes in `devUrl`
instead — and it exits 0, so the build looks fine.

**Always build the console with:**

```
cd dev-console
npm run tauri:build          # runs `tsc -b && vite build`, then embeds ../dist
```

Verify before shipping (this is what `scripts/check_devconsole.py` does):

```
python scripts/check_devconsole.py
```

It compares the exe against the hashed `index-*.js` names currently in `dist/`, so it
catches both failure modes: **not embedded** (built with plain cargo) and **stale
embed** (the exe carries an older frontend than `dist/`).

It deliberately does *not* fail on the presence of `127.0.0.1:1420`. Tauri embeds the
whole `tauri.conf.json`, dev fields included, so that string is present in good builds
too — keying on it produced a false failure against a perfectly good binary.

**"app not running" in the sidebar**
The engine isn't up, or **Local API (agent control)** is unticked in the tray menu.

**"Idle / No SMTC session detected" while a track is playing**
Fixed in v1.1.87 (TICKET-197). The console was reading `status.title`; the API sends
`player_title`. Because `api.ts` *casts* the JSON rather than validating it, a wrong
name reads `undefined` and renders the fallback text — it compiles and typechecks and
is still wrong. If you see this again, the model and the API have drifted apart:

```
python scripts/check_console_contract.py
```

It diffs the field names declared in `dev-console/src/models.ts` against a live
response and names any the API does not send. Run it after touching either side.

**Two console windows open at once**
Shouldn't be possible from v1.1.87 (TICKET-198): the app focuses an existing window
instead of spawning, and the console itself refuses to start a second instance. If you
somehow get two, the giveaway is that they disagree — check the version in each
sidebar and close the older. A stale copy also used to linger at
`D:\DesktopKaraoke\dev-console\` beside the real one in `_internal\dev-console\`;
`scripts/deploy_local.py` now refreshes both.

**`npm run tauri:dev` starts and immediately exits**
The same guard, seen from the other side. A deployed console is already open, and it
shares a bundle identifier with the dev build, so the dev process hands off to the
existing window and quits. Close the deployed console and run it again. See
[Editing the frontend live](#editing-the-frontend-live).

**A view looks empty, but a song is playing**
Check the *now* strip at the top first — if the playhead is moving, the console is live
and you are looking at an **event panel** that simply has no event yet. See
[the two kinds of panel](#first-the-thing-that-confused-everyone). The banner reader
does not run for ordinary tracks, and a by-ear decision only fires at a song boundary,
a late load, an engine escalation, or the "Wrong lyrics" button.

If the *now* strip itself is missing while a track plays, the app is older than
v1.1.87 (no `now` block in `/insight`) — check the version in the sidebar.

**Numbers look stale**
Views poll every 2.5s, and only the visible view polls. Use the sidebar **refresh**.
Per-video evidence (banner reads, refused strings) is cleared on every track change, so
it can never be older than the current song; a gate snapshot that outlived its track is
dimmed and badged **previous track** rather than silently kept.

---

## Building it

```
cd dev-console
npm install                  # once
npm run tauri:dev            # live-reload development
npm run tauri:build          # release build (embeds the frontend)
cd ..
python scripts/check_devconsole.py        # the frontend is really embedded
python scripts/check_console_contract.py  # the models match the live API
python scripts/deploy_local.py            # copy over the live install
```

Run **both** checks before deploying. They catch different lies: `check_devconsole`
catches a binary that will show "refused to connect"; `check_console_contract` catches
a binary that renders perfectly and displays nothing, which is far harder to spot.

The app finds the exe at `_internal/dev-console/` in a frozen build, else
`dev-console/src-tauri/target/release/`. It also checks a sibling
`dev-console/` directory — keep that in step or delete it, because a stale exe there
is indistinguishable from the real one once it is on screen.

### Editing the frontend live

The two ways of running the console behave completely differently when you change a
`.tsx` file, and mistaking one for the other wastes a lot of time.

- **`npm run tauri:dev`** loads the frontend from the Vite dev server at
  `127.0.0.1:1420`, so an edit hot-reloads in place. This is how to work on a view.
- **The packaged console** bakes the built frontend into the exe via `frontendDist`.
  Nothing is loaded from disk at runtime, so a `.tsx` change is invisible until you
  run `npm run tauri:build` and redeploy. There is no way to patch a deployed
  console's UI in place, and no error to tell you that you tried.

HMR needs the websocket on the secondary port `127.0.0.1:1421` (set in
`vite.config.ts`), and Tauri's CSP has to permit it. The shipped `csp` deliberately
does not, and Tauri injects the production `csp` into dev builds when no `devCsp` is
declared, so HMR was silently blocked and edits appeared to need a full rebuild even
under `tauri:dev`. `tauri.conf.json` now carries a **`devCsp`** that adds the HMR
origin for **dev only**, leaving the shipped CSP unweakened. If HMR stops working,
check that pair first: any port change in `vite.config.ts` has to be mirrored there.

**The single-instance trap.** Only one console can exist at a time, guaranteed twice
over: `tauri-plugin-single-instance` in `src-tauri/src/lib.rs` (a second launch
un-minimises, shows and focuses the existing window instead of opening its own), and
the app's own check in `launch_dev_console`, which finds an already-open console by
window title and focuses it.

The trap is that the plugin keys on the **bundle identifier**, which a dev build and
a deployed build share. They therefore count as *the same instance*. So starting
`npm run tauri:dev` while a deployed console is already open makes the **dev**
process hand off to the deployed window and exit: your dev build appears to launch,
do nothing, and die, while the console on screen stubbornly shows the old UI. Close
the deployed console first. It is a healthy guard behaving exactly as designed, and
it looks precisely like a broken toolchain.

That title-based half has its own fragility: the tray matches
`main.py._DEVCONSOLE_TITLE` against the window title in
`src-tauri/tauri.conf.json`. Two files, two languages, nothing structurally tying
them together, so renaming the window would silently disable the guard.
`scripts/check_devconsole.py` now asserts the two strings match (TICKET-223).

### Deploying

`scripts/deploy_local.py` exists because hand-deploying kept going wrong. It stops
every process running from the target first (copying over a running exe
half-succeeds), mirrors `_internal\` only — the target also holds `lyrics\`,
`models\` (2.1 GB) and `deps\` (1.9 GB), so a top-level `/MIR` would delete your
cache and weights — and treats robocopy's exit code as the bitmask it is (`>=8` is
an error; non-zero is not). `--dry-run` shows the plan without touching anything.
