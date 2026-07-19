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

### Deploying

`scripts/deploy_local.py` exists because hand-deploying kept going wrong. It stops
every process running from the target first (copying over a running exe
half-succeeds), mirrors `_internal\` only — the target also holds `lyrics\`,
`models\` (2.1 GB) and `deps\` (1.9 GB), so a top-level `/MIR` would delete your
cache and weights — and treats robocopy's exit code as the bitmask it is (`>=8` is
an error; non-zero is not). `--dry-run` shows the plan without touching anything.
