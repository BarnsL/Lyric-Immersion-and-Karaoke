# Desktop Karaoke — Issue Tickets

Numbered tickets for matching / sync / rendering / performance / features.
Status: 🔴 open · 🟡 in-progress · 🟢 fixed (pushed) · 🔵 needs-repro

**Verification rule:** always compare the app's line to the **video's on-screen
lyrics** at the same playback position — not just `/status`.

---

## v1.1.89 — 2026-07-19 (TICKET-201…204 — the runaway, the blank songs, the narrated console)

### TICKET-201 — hololive Unchained: OCR sync ran away to +56s and never recovered

**Report:** "hololive unchained starts off good, then for whatever reason syncs
to a bad part of the song then never recovers."

**Root cause (from the log):** the OCR-assisted sync (`_ocr_assisted_sync`)
committed a +55.99s offset correction from a single unverified read — the screen
reader matched lyric line 29 at 0.95 confidence on one pass, and the code had no
two-point verification gate. Every subsequent read was then measured against
that broken offset. A read landing 56s away was correctly identified as
out-of-range and discarded — but discarding a read from a broken baseline
*preserves* the broken baseline. The song never recovered because the evidence
needed to fix it was being thrown away for disagreeing with the thing that was
wrong.

The containment-scored match was also degenerate: the same LRC line matched on
nine consecutive passes across eight minutes. A caption cannot stay on one line
for eight minutes. The "match" was `ratio 0.95` from a 4-character OCR fragment
sitting inside a 40-character lyric line — the flat-containment bug the codebase
had already learned about in `_same_song_title` (`'ghost'` in `'ghosting'`) but
had not applied to the OCR path.

**Fix (three guards):**
1. **Length-gated containment.** Containment now requires the shorter string to
   cover >= 80% of the longer one, matching the `_same_song_title` rule. The 4-
   character fragment that scored 0.95 now scores 0.12 and is correctly rejected.
2. **Degenerate-read detection.** Two reads matching the SAME line while the
   video position moved >1s is a stale/generic read, not a measurement. The
   second is refused and logged as `sync-rejected` with the reason.
3. **Far-read streak revert.** Two out-of-range reads in a row while holding an
   OCR-committed offset backs the commit out: `offset → 0.0` with a
   `sync-revert` event. This is the fix the runaway needed: instead of
   discarding every correction for disagreeing with the broken offset, the
   broken offset itself is reverted when enough evidence accumulates against it.

Big corrections (> `ocr_sync_single_shot_max`) also now require a second,
independent agreeing read — the same rule the energy correlator already applies.

**Verified:** the containment fix alone rejects the 4-char fragment; the
degenerate fix refuses the repeated line-29 matches; the far-read streak reverts
the +56s commit within two verification passes.

**The log, verbatim** — every commit reports the SAME line at the SAME ratio, and
the offset slides by exactly the playback delta, which is the signature of
`line29.start − now` rather than of a measurement:

```
23:55:22 ocr-sync(energy-ambiguous): LRC line 29 ratio 0.95 → offset  51.25s (was  0.00)
23:55:46 ocr-sync(energy-ambiguous): LRC line 29 ratio 0.95 → offset  27.33s (was 51.25)
23:56:26 ocr-sync(energy-weak):      LRC line 29 ratio 0.95 → offset -12.67s (was 27.33)
23:57:06 ocr-sync(energy-ambiguous): LRC line 29 ratio 0.95 → offset  55.99s (was  0.00)
23:59:46 ocr-sync(energy-weak):      LRC line 29 ratio 0.95 → offset  23.57s (was  0.00)
00:01:46 ocr-sync(energy-weak):      LRC line 29 ratio 0.95 → offset   4.08s (was 23.57)
00:02:26 ocr-sync(energy-weak):      LRC line 29 ratio 0.95 → offset -35.92s (was  4.08)
00:03:06 ocr-sync(energy-weak):      line 29 but offset  -75.7s out of range (cap 60s) — skip
00:03:46 ocr-sync(energy-weak):      line 29 but offset -115.7s out of range (cap 60s) — skip
```

Note the `reason` on every line: `energy-weak` / `energy-ambiguous`. **The OCR path
only runs when the energy correlator declined to act** — and three lines above each
of these the correlator is logging `+12.40s is a BIG studio jump with no Shazam
corroboration — holding for confirmation`. The fallback that fires when confidence
is lowest was applying the *least* skepticism of any sync path in the app.

**Found by adversarial review, after the first implementation (fixed):** the
"second agreeing read" gate did not require the second read to be on a *different*
line, so a degenerate same-line pair could confirm itself — the exact failure the
degenerate-read guard exists to stop. Two reads of one line are one measurement seen
twice; confirmation now requires a different line.

### TICKET-202 — Lemonade (Love Live!) and All for One: cached body distrusted, replacement never arrived

**Report:** "NO LYRICS for lemonade love live mia teira" and "NO LYRICS SHOWING
FOR ALL FOR ONE a popular hololive song."

**Root cause (from the log, two independent cases):**

*Lemonade:* the YouTube channel name `(Love Live! series)ラ...` was used as the
"artist" for a language check. The cached body was English; the channel name
contains kana; the wrong-language guard (TICKET-062) fired and distrusted the
cache. The re-fetch came back empty. The overlay was left with **nothing** for
the rest of the song — distrust without a replacement is worse than a wrong
cache.

*All for One:* hololive English's song is genuinely in English, but the act is
classified Japanese. The English-only-body-for-a-Japanese-act guard fired,
distrusted the cache, and the re-fetch found nothing. Three minutes of no lyrics
before a deep pass fetched an equivalent body from the same provider.

**Fix:** distrust now DEMOTES a body instead of deleting it. The distrusted
cache is remembered in `_distrusted_cache` and restored by `_maybe_generate`
when the re-fetch comes back empty — so a wrong-language guard that fires on a
correct body degrades gracefully instead of blanking the song. The restore is
gated on `_track_seq` so a body set aside for a previous song is never carried
into a new one.

**Logged:** every distrust now emits a `body-rejected` notable event with the
reason, and every restore emits a `cache-restored` event. Both surface in the
new Activity view.

**Found by adversarial review, after the first implementation (all fixed):**

- **The restore re-entered `load()`'s own language guards.** The justification
  ("they already fired for this track") was only true for one of the two setters:
  when `_file_valid` sets `_distrusted_cache` (the kana-artist path), those guards
  have *not* run, so `load()` rejected the body again — a silent no-op that also
  kicked a duplicate re-identify chain racing the by-ear generation. A restore now
  suppresses them via a self-consuming `_restoring_distrusted` flag.
- **One of those guards `unlink()`s the file.** The restore therefore had a path
  that *permanently deleted* the cache this ticket exists to preserve, directly
  contradicting "demote, not delete". That guard is now skipped during a restore.
- **`_smtc_paused_takeover` bumps `_track_seq` without going through
  `_on_track_change`,** so it never cleared `_distrusted_cache`. A body set aside
  for the SMTC song could be restored into the Shazam-heard one — cross-song
  contamination, reported by the console as a legitimate `cache-restored`. It now
  clears the same per-track state.
- **The restore skipped `_maybe_translate()`,** which every other cache-load site
  pairs with `load()`, leaving the romaji and translation lanes blank for the rest
  of the song.

### TICKET-203 — Engine decisions were invisible until you read karaoke.log by hand

**Report:** "i want visible animated notifications in the developer console only
when decisions are made/song is rejected or switched or major sync happens …
lets also add a log panel that references video name, what it saw, time of
change in video compared to lyric time."

**Root cause:** the sync ring (`_sync_events`) was a firehose of raw telemetry
keyed for machine analysis. A human needed to know "the song changed", "a body
was rejected", "a +56s correction committed", "lyrics arrived 174s late" — and
none of those were surfaced anywhere readable. The hololive Unchained runaway
showed `pos=16.6` with `lyric_t=72.6` and nothing surfaced the 56s gap until
someone read the log by hand.

**Fix:** a separate narrative event ring (`_note_event`) records the handful of
moments a human would want narrated, stamped with the context needed to
reconstruct what happened:
- `title` — which video it happened on (events outlive their track)
- `pos` — where the VIDEO was, in seconds
- `lyric_t` — where the LYRICS were, in seconds
- `gap` — `lyric_t - pos`, the number that tells a sync bug from a correct run
- `sev` — good/info/warn, driving the notification colour

Exposed at `/insight.notable` and rendered in the new **Activity** view:
- **Animated toast notifications** that slide in when a new event arrives
  (cubic-bezier ease-out, 350ms), severity-coloured (green/amber/blue), with
  the warn icon pulsing to catch the eye. Auto-dismiss after 6s (info/good) or
  10s (warn).
- **A readable log panel** with video name, event kind, human detail, and the
  full time context (video position, lyric line time, gap, offset, ratio) —
  everything needed to understand a sync bug at a glance.

Wired at every decision point: song-change, lyrics-loaded, body-rejected,
cache-restored, sync-jump/nudge/revert/rejected, and decision-engine
escalation.

**Found by adversarial review, after the first implementation (all fixed):**

- **`_note_event` re-read `self.lines` three times in one expression** while
  running on the OCR/API threads, which the Tk thread can swap wholesale on a song
  change. The guard passes against the old body and the index then resolves against
  the **new** one — no `IndexError`, so the `except` never fires and the ring records
  a `lyric_t`/`gap` describing something that never happened. A fabricated gap is
  strictly worse than none in the one field whose entire purpose is to be trustworthy.
  The body and index are now bound once, exactly as `get_insight` already does.
- **"How late did the lyrics arrive" was measured against a `_track_t0` stamped
  after the cache-hit path had already loaded.** So the *fastest* possible path — an
  instant cache hit — was narrated as the slowest, using the *previous* track's start
  time, and the first load of a session reported a ~1.79-billion-second blank spell
  (`_track_t0` still held its `0.0` initial value). It now uses the player's own
  position, which is both honest and the number the user actually cares about.
- **The `title` field mixed two different strings for one song** — the cleaned name
  on `song-change`, the raw player title everywhere else — so per-track grouping split
  one song in two. All events now carry the raw video name.

### TICKET-204 — Title identification: Wrong Song button + correct-text picker

**Report:** "for the title identification, lets add a wrong song button to it
and allow for me to select the correct text from the text that the engine sees
in the event that its trying to find lyrics from the wrong string of text.
keep this logged as well as the full identified text and the 'bad' one for
future diagnostics."

**Root cause:** the engine's `clean_title()` reduces a video title by stripping
credits and decorations. This reduction is invisible from outside the process,
and when it strips the wrong part (e.g. `IA & ONE / てるみい (石風呂)【MUSIC
VIDEO】` reducing to `IA & ONE`), every downstream panel looks healthy while
the wrong song's lyrics are fetched. The user had no way to say "that's the
wrong string — search this instead."

**Fix:**
- Every title string the engine sees is now tracked in `_seen_title_strings`
  (raw player title, cleaned title, OCR reads, prior overrides) and exposed at
  `/insight.title_id.seen_strings`.
- The **Wrong Song** panel in the Activity view shows the raw title, what the
  engine reduced it to, and any active override. The **Pick correct title**
  button offers every seen string as a one-click correction, plus a free-text
  fallback.
- Selecting a title calls `POST /override_title`, which forces `clean_title` to
  the user's choice and re-fetches. The override is per-video (cleared on track
  change) and does not persist to settings.
- The bad string and the good one are logged (`TITLE OVERRIDE: engine had X,
  user chose Y`) for future diagnostics. The `_title_id_log` ring captures
  every override with timestamps.

**Copyright:** no lyric body text is logged — only titles and metadata. Corrections
are appended to `title_corrections.jsonl` in the **app data folder** (gitignored,
never shipped) plus the in-process `_title_id_log` ring — never to a file in the repo.

**Found by adversarial review, after the first implementation (all fixed):**

`override_title` had been defined **twice on the same class**, so the second silently
shadowed the first. The `title_corrections.jsonl` write and the success event were
unreachable dead code, the endpoint's `{ok: …}` contract was never honoured, and a
successful user correction was painted as a `warn`. Consolidated into one method.

The surviving implementation also had to clear `self.lines` to force a real re-fetch —
which defeats `_on_track_change`'s same-song early return, so the full per-track reset
ran and wiped the override **the instant it was set**. The correction is now carried
through exactly one re-identify by an explicit, self-consuming flag.

`_smtc_paused_takeover` changes song without going through `_on_track_change`, so it
now clears the override and the candidate strings too — otherwise a correction made for
one song would force its search string onto a different one.

---

## v1.1.88 — 2026-07-19 (TICKET-200 — the wrong song, reported as identified)

### TICKET-200 — A title reduction searched the ARTIST, and the console called it identified 🟢

**Report:** "poor lyric matching with this song… i can see that the proper wasnt
received and it got lyrics for another song that the artists IA and ONE sing",
playing **IA & ONE / てるみい (石風呂)【MUSIC VIDEO】** (channel *IA PROJECT*). The
overlay showed a full, well-timed, 43-line Japanese body — for a different song.

Two independent defects, one that loaded the wrong lyrics and one that hid it.

**1. `clean_title()` reduced the title to the performers.** JP MV uploads use the
convention `Vocalists / 曲名`, and the slash branch picks whichever side is *not*
the artist:

```
parts = ["IA & ONE", "てるみい (石風呂)"]
_artistish("てるみい (石風呂)") → False      # not the channel name
_artistish("IA & ONE")        → False      # ← the bug
else: t = p0                               # default-to-first → "IA & ONE"
```

`_artistish` compares a side against the **SMTC artist**, which here is the
channel/label *IA PROJECT*, not the vocalists. It tries two ways and both miss:
the joined form `iaone` is not a substring of `iaproject`, and its token test has
a **≥4-character floor** — so `ia` (2) and `one` (3) were never even considered.
The tie-break then defaulted to the first part, which on this convention is
always the artist. The providers were searched for **"IA & ONE"**, a performer
name, and returned a real, correctly-timed body for a different song by those
performers. Nothing downstream was wrong: the body was genuine, the sync was
good, the language was right. The song name had been thrown away three steps
earlier.

*Fix:* `_artistish()` now also splits a side on `& ＆ × ✕ and` and tests each name
on its own, by **whole-token equality** against the artist (`ia` **==** the artist
token `ia`). No length floor is needed there: the floor guards against *substring*
collisions (`ia` inside `rain`), and whole-token equality has no such failure
mode — which is why the floor was excluding precisely the names this app sees
most, the vocal synths (IA, ONE, GUMI, RIN, LEN), all under four characters.

*Residual limitation, accepted:* if the channel is unrelated to the performers
(a label channel with no shared token), no element matches and the tie-break
still defaults to the first part. Fixing that needs a source of truth for the
performers — the video description's vocals credits (TICKET-112 already parses
them) is the obvious one. Not done here.

**2. The console reported this at full confidence.** The Now strip's badge read
`now.agree`: does the loaded body's title match the player's? That comparison is
**circular** — a body fetched by title search is filed under the title it was
searched with, so it agrees with itself no matter which song it contains. The
console showed a green ✓ **identified** on lyrics for the wrong song. The one row
that told the truth (`Locks: title locked · unverified · words unchecked`) was
three panels down and outvoted by the tick.

The same comparison was also wrong in the *other* direction. Measured live on the
next track: player `【MV】Unchained【hololive English -Advent- Original Song】`
vs loaded `Unchained` scored **`mismatch`** — a red warning on a perfectly correct
body, because the raw title carries 【…】 furniture the loaded title does not. A
badge that is wrong in both directions is worse than no badge.

**Fixes**

| | |
|---|---|
| `evidence` on `/insight.now` | The ladder the badge now reads: `library` (bundled body) → `words` (the sung words were matched) → `timing` (energy/caption lock: proves *when*, not *what*) → `title` (nothing but the search) → `none`. |
| Badge shows what was **checked** | "title only" (amber) instead of "identified" (green) for a title-searched body. That is the honest label for the overwhelmingly common case, and it is what the IA & ONE body would have shown. |
| `search_title` on `/insight.now` | The title actually sent to the providers. Rendered under the player title as **searched for …**, only when the reduction changed it. For this bug that single row reads `searched for IA & ONE` and the diagnosis is over. |
| `agree` compares against `search_title` | Not the raw player title, and now passes the artists through to `_same_song_title`. Kills the Unchained false mismatch. `agree` and `evidence` stay independent signals. |

**Verification**

* `scripts/probe_clean_title.py` — 10/10, running the real `clean_title` via `ast`
  extraction (no app import). The reported title now reduces to `てるみい`, and
  every case the tie-break was originally tuned for still passes: `Dunk/轟はじめ`
  → `Dunk`, `FLOW GLOW / LOAD` → `LOAD`, `幻界/V.W.P #30` → `幻界`. A genuine `&`
  in a *song* title is unaffected in either position (`Sugar & Spice / Reol` and
  `Reol / Sugar & Spice` both → `Sugar & Spice`) because no element of it is one
  of the artist's tokens.
* `scripts/probe_insight.py` — extended to assert `agree` and `evidence` stay
  independent: the title-only case reports `agree=match` **and** `evidence=title`
  simultaneously. If those ever collapse into one signal again, the probe fails.

---

## v1.1.87 — 2026-07-18 (TICKET-194…199 — the dev console shows the live state)

### TICKET-194 — Both new console views were blank during ordinary playback 🟢

**Report:** "dev tools kinda useless right now. song is playing and its not displaying
anything identified or live", with a track playing and lyrics correctly on screen.

The console was *running* — it rendered, the API answered, `/insight` returned a full
payload. It was showing the wrong things. Three separate defects:

**1. Every panel rendered an EVENT, and events are rare.** The Song-finder view leads
with the banner-OCR pass; the Decisions view leads with the decide-by-ear gate ladder.
OCR only runs in live/concert mode, and a by-ear decision only fires at a song
boundary, a late load, an engine escalation, or a "Wrong lyrics" press. During ordinary
playback of a correctly identified track — the overwhelmingly common case, and the one
the user was in — **both headline panels are empty by construction.** The data that
*was* live (`sources`, `sync`, `decision`) sat below the fold or on another view. The
views were designed around the two nights of debugging that produced them rather than
around the state the app is normally in.

**2. Stale evidence was rendered as current.** `_finder_ocr` and `_finder_ocr_drops`
were never cleared on track change, so the Song-finder view showed a *previous
concert's* banner pass — measured at **1847s (31 min) old** — as the headline for a
video that never ran a banner pass at all, complete with its accepted match and its
list of refused strings. The only hint was a small "27m ago" pill. This is worse than
an empty panel: it is confidently wrong, and it is the kind of thing that makes you
distrust the whole instrument.

**3. Half the rows had no CSS at all.** `.knob-row` is scoped to table cells
(`.knob-row td`) for the Parameters view. Finder/Decisions used it on a `<div>`, which
matched *nothing* — no flex, no spacing. That is why the screenshot reads
"Matched against**the whole library**" and "Accepted**posts**" jammed together. Fixed
with a div-based `.stat-row`.

**Fixes**

| | |
|---|---|
| `now` block in `/insight` | Always populated while a track is loaded: player vs loaded title, **agreement between them**, position/duration, line index and count, lyric source, language, sync offset/drift, renderer, overlay state, and what the engine is busy doing. |
| `NowPane` at the top of both views | The panel that is never empty. The playhead moving is itself the proof the console is live. |
| Clear on track change | `_on_track_change` drops `_finder_ocr`/`_finder_ocr_drops`; the gate snapshot survives (it usually explains what is loaded now) but is stamped `stale` + `track` so the UI labels it "previous track" and dims it instead of implying it is current. |
| Re-ordered both views | Always-live panels first, event panels last. |
| Honest empty states | "Not running for this video — which is normal", plus *why*, instead of a bare dash. |
| `renderer` field | The Tk frame timer reads 0 while the GPU overlay draws, so `render_fps` is `null`. Reporting "0 fps" would libel a renderer that is running fine in another process; the tile shows `GPU` instead. |

**Verification:** `get_insight` was exercised against a duck-typed stub via `ast`
extraction (no app import, no rebuild) across 6 cases — titles agreeing, wrong lyrics
loaded, nothing loaded, busy deciding, Tk renderer with real fps, and `idx` past the
end of the body. All pass and the payload stays JSON-serialisable.

### TICKET-197 — The console's API model invented field names 🟢

The Overview's now-playing card showed **"Idle / No SMTC session detected"** while a
track was playing — with `POSITION 57s` and `DURATION 3m 32s` rendered correctly
directly beneath it. Data was arriving; the title was being read from a field that does
not exist.

`api.ts` casts the response straight to the model type:

```ts
return (await resp.json()) as T;     // a cast is not a check
```

Every field in `StatusPayload` is optional, so a wrong name reads `undefined`, renders
as a dash or a fallback string, and nothing throws. `tsc` cannot help: it is checking
the code against a *declaration*, and the declaration was fiction.

| console read | actually sent by `api.py._status` |
|---|---|
| `title` / `artist` | `player_title` / `player_artist` |
| `offset` | `sync_offset` |
| `now_line` | `current_line` |
| `source`, `status`, `matched`, `subs_mode`, `live_arrangement`, `mv_mode` | **never sent at all** |

Ten declared fields, all phantom. The now-playing card and the current-line card have
therefore been dead since they were written, and the sidebar has always said "no track".

**Fixed** by rewriting `StatusPayload` against `api.py` line by line and correcting
every read site.

**Guard:** `scripts/check_console_contract.py` parses the field names out of
`models.ts` and diffs them against a **live** response from the running app, for
`StatusPayload`, `Health` and `NowBlock`. Verified it flags all ten of the old names
and passes the corrected model. A type declaration is a claim about a remote system;
it needs a test, not a cast.

### TICKET-198 — Two dev consoles could be open at once 🟢

Two console windows side by side, showing different builds and different numbers, with
no way to tell which was live. `launch_dev_console` guarded on `self._devconsole_child`
— a handle to a console *this app instance* spawned. It misses both real cases: the app
restarted while a console stayed open, and the console started by hand.

Fixed at both ends:

- **App side:** enumerate top-level windows for the console's title and, if one exists,
  restore and focus it instead of spawning a second (`_focus_dev_console_window`).
  Catches consoles this process never started.
- **Console side:** `tauri-plugin-single-instance`. A second launch hands its argv to
  the running instance and exits; the primary un-minimises and focuses. Registered
  first, so no setup work happens in a process that is about to exit.

### TICKET-199 — A valid cache was distrusted, then the re-fetch found nothing 🔴

**Report:** "poor lyric matching with this song… I would just see a little bit of lyric
cross the left side but not during the bulk of the song" — on a Lyric Video whose lyrics
are burned into the frame.

From the log, two independent faults:

**1. The channel name is used as the artist for a language check.**

```
19:12:12 title-match 'Lemonade' -> lemonade.json (score 112)
19:12:12 cache lemonade.json is English but artist is kana-Japanese
         ('(Love Live! series)ラ') → distrust, re-fetch
19:12:47 no lyrics after the grace window (lookup came up empty) → OCR / generating by ear
```

The "artist" is the **YouTube channel name**, not the performer. The heuristic is
reasonable in isolation — an English body under a Japanese artist is often the wrong
song — but it fired on a 112-score title match and, when the re-fetch came back empty,
left the overlay with **nothing**. Distrusting a cache is only safe if the replacement
arrives; the fallback should be to keep the distrusted body rather than show none.

**2. The track flapped between two videos every ~15s.**

```
19:13:51 track change: '東方ストリングスアレンジ…' / 'SONICA_TOKYO'
19:13:53 track change: 'Lemonade' / '(Love Live! series)…'
19:14:06 track change: '東方ストリングスアレンジ…' / 'SONICA_TOKYO'
19:14:08 track change: 'Lemonade' / '(Love Live! series)…'
```

A second media session (a Touhou BGM video) kept winning the picker, and every flap
restarts identification — which is exactly "a bit of lyric, then nothing, for the bulk
of the song". Whisper generation was started twice and never got to finish.

Not fixed here. Two candidate fixes, independent: (a) when a distrust-and-re-fetch comes
back empty, restore the distrusted cache; (b) hysteresis in the session picker so a
background session cannot steal the track seconds after losing it.

### TICKET-196 — Building PyInstaller directly skipped the ABI guard 🟢

Self-inflicted, while building the fix above. `build.bat` prompts interactively, which
hangs a non-interactive shell, so I invoked PyInstaller directly:

```
python -m PyInstaller --noconfirm DesktopKaraoke.spec     # exit 0. Broken bundle.
```

On this machine the bare `python` on PATH is a **3.11** agent venv, while `.deps` is
**cp312**. That combination builds green and produces an app whose
`import numpy._core._multiarray_umath` fails at runtime — whisper silently dead. It is
precisely the failure TICKET-175/177 were written to prevent, and both of their guards
were bypassed: `scripts/check_build_deps.py` runs only from `build.bat`, and the
post-build `--selftest` likewise. Going around the front door went around the alarms.

**Fix:** move the backstop into `DesktopKaraoke.spec`. The spec is the one file *every*
build route must load, so it cannot be bypassed by choosing a different entry point. It
compares the `cpXY` tag on the vendored `.pyd`s against the running interpreter and
exits with the correct command line if they disagree.

Verified both ways: the 3.11 interpreter is refused, 3.12 proceeds.

**Lesson:** a guard attached to the *convenient* path is not a guard. It has to sit on
the path that everything shares.

### TICKET-195 — The OCR setlist gate is inert when there is no setlist 🔴

Found *using* the console above, which is the point of it. From a live `/insight`:

```
pool_kind : "library"     pool_size : 373
matched   : {"title": "posts", "score": 0.856}    accept_at : 0.85
```

The banner reader accepted the bare word **"posts"** against the whole 373-title
library. The surrounding reads ("with Docling and Granite", "Retraining a foundation
model or f", "Needs input YouTube video download") are page text from an unrelated
window, not a song banner.

TICKET-189 added the setlist gate for exactly this failure, but the gate is
`bool(setlist) and tune[...]` — **with no setlist parsed it evaluates false and the
code falls back to matching against the entire library**, which is the ungated
behaviour the ticket set out to remove. It closes the hole for chaptered concerts and
leaves it fully open for chapterless ones.

This is the third instance of the class: `breaking dimensions` (leftover search-box
text, hijacked a concert for ten minutes), `WALLET PORTFOLIO TRACKER` (a sidebar ad,
fetched as a song), now `posts`.

Not fixed here — it is a change to song *identification*, not to the console, and it
wants its own verification pass. Candidate fixes: require a minimum token count and
reject single dictionary words; raise `accept_at` when the pool is the whole library;
require the 2nd-read confirmation unconditionally in the ungated case.

---

## v1.1.86 — 2026-07-18 (TICKET-190/191/192 — dev-console insight, aggressive sync, concert TPVR)

### TICKET-191 — Sync made aggressive, because the reason for the caution is gone 🟢

Earlier tonight (TICKET-181) corrections were *damped* — a 1.0s scroll deadband, a
slower tier — because a correction re-derived the karaoke fill mid-line and the
highlight visibly jumped. LP-010 then decoupled the sweep from the sync clock
entirely: it runs on its own monotonic ramp and a correction **cannot** jolt it, while
the belt eases via `ease_slew_cap_s_scroll`. The smoothness is no longer paid for in
accuracy, so the damping was pure cost.

| knob | was | now | why |
|---|---|---|---|
| `sync_tier_ok_drift` | 1.2 | **0.6** | it declared "in sync" while up to 1.2s off — more than double the 0.5s goal |
| `sync_apply_min_s_scroll` | 1.0 | **0.25** | the deadband existed only to protect the fill, which is now insulated |
| `sync_tier_fast_s` / mid / slow | 20/40/60 | **12/25/40** | cadence bounds how fast drift can even be *noticed* |
| `tpvr_gap_s` / live | 2.5 / 4.0 | **1.5 / 2.5** | this delay *is* the lock latency |
| `live_single_shot_max_s` | **0.0** | 1.2 | live corrections could *never* commit on one read |
| `auto_align_cooldown` | 14 | 8 | must stay under the fast tier or it throttles it |
| `live_energy_apply_min` | 0.25 | 0.15 | live arrangements drift continuously |

### TICKET-192 — Concerts decide faster — and the first attempt was dead code 🟢

**The mistake, recorded because it is instructive.** The obvious change was a
concert-specific TPVR at the sync-tier path (`concert_tpvr_gap_s`,
`concert_single_shot_max_s`). Adversarial review caught that **neither can ever be
read in a concert**: `_tier_listen_now` returns early on `_live_mode`, so the entire
tier path is disabled during concert playback. The change compiled, looked correct,
and would have shipped as a **silent no-op** — the exact failure class as whisper
being broken and invisible for four releases.

**Where concerts actually sync:** the Shazam consume path, where a live correction
requires two reads agreeing within `agree_live`. That is the real two-point rule.

**Fix:** `concert_first_read_max_s` (1.8) — in a concert only, a *small* correction
commits on the **first** read instead of waiting for a second. Rationale: waiting
costs a whole Shazam cycle, and a concert can change song before it lands, so the
correction never applies at all. A chorus-repeat mismatch — the thing the pairing
exists to catch — shows up as a **large** offset and still requires two reads. Live
*arrangements* keep the strict pairing; they have the whole track to confirm against.

The two tier-path knobs are kept (a live arrangement with a duration mismatch can
still reach that path) but are commented as such so nobody tunes them expecting a
concert effect.

### TICKET-190 — Song-finder + decision introspection (`/insight`) 🟢

Every answer needed to debug tonight's bugs existed only inside function scope and was
written to a rate-limited log line. New `GET /insight` exposes: every line the banner
OCR read plus **why each was dropped** (`window-chrome` / `not-on-setlist` /
`awaiting-2nd-read`), the pool it matched against and its size, the setlist with
per-song cached state, SMTC/Shazam/lock state, the live decide-by-ear gate arithmetic
(best vs loaded, *effective* MIN incl. the title-lock bump, cross-artist block,
`loaded_worthless`), the live/concert sync profile, and the AutoResearch reality check.

Dev console gains **Song finder** and **Decisions** views (decide-by-ear, concert
identification and live-sync ladders, lit path for what ran, dimmed for never-reached).
Thresholds are rendered **from the payload**, never duplicated in the frontend — a
diagram that drifts from the code is worse than no diagram.

**AutoResearch:** see [AUTORESEARCH.md](AUTORESEARCH.md). Summary: the loop has never
run, and a naive optimiser could not produce trustworthy results — no knob→outcome
attribution exists, and the objective for `tpvr_gap_s` is discarded by log rotation.
Every candidate objective was rejected as perverse. The console now reports that
rather than describing the loop as operational.

---

## v1.1.85 — 2026-07-18 (TICKET-189 — concert OCR read the BROWSER, not the video)

### TICKET-189 — Concert showed a song from the search box 🟢

**Symptom.** A 3D live concert (【3DLIVE】Rise In Motion, 61 min) showed **no concert
lyrics at all**, and the app reported the song as **"Breaking Dimensions"** — a track
from an earlier session.

**It was not stale state.** "Breaking Dimensions" was the text still sitting in the
**YouTube search field**, and the banner OCR read it off the page:

```
09:33:24  concert OCR read uncached 'WALLET PORTFOLIO TRACKER' — awaiting a 2nd consistent read
09:33:28  concert OCR read uncached 'WALLET PORTFOLIO TRACKER' → fetching (cover-style)
09:33:44  title-match 'wallet portfolio tracker' -> wallet_portfolio_tracker.json (score 100)
09:33:52  concert OCR read 'breaking dimensions' (0.87) → breaking_dimensions.json
```

`WALLET PORTFOLIO TRACKER` is a **sidebar ad**. The banner reader captures the whole
media WINDOW, so it sees the search box, ads and page copy alongside the video.

**And the app already had the right answer:**

```
09:23:04  setlist: 8 description candidate songs — Dunk | ちゃちゃもにゃ | BANZAI |
          Countach | BANDAGE | 夜咄ディセイブ | 踊り子 | きゅうくらりん
09:25:53  concert-candidates prefetch: cached +4 (skipped 3 already-indexed, 1 failed) of 8/8
```

Four setlist songs were prefetched to disk (`dunk.json`, `夜咄ディセイブ.json`,
`踊り子.json`, `きゅうくらりん.json`) while the overlay showed a song scraped from the
search field.

**Root cause.** `_concert_ocr_check` matched OCR text against **the entire library**
(357 entries). Two existing guards were in place and neither could help:

* `_text_matches_window_title` catches window **chrome** — but a search box and an ad
  are page **content**, not a window title.
* the two-consecutive-reads rule (TICKET-171) is defeated by *persistent* screen text;
  a search box that just sits there re-reads identically every pass.

Against a 357-entry library, arbitrary page text will eventually clear 0.85 against
*something*.

**Fix.** When the video's setlist is known, that IS the candidate pool: match only
against its songs, and reject an uncached read that matches none of them. In a
concert, the song being performed is one of the setlist's songs; everything else on
screen is furniture. Falls back to the old whole-library behaviour when no setlist was
parsed, so nothing new is blocked on videos without one. Knob: `ocr_setlist_gate` (1).

**Verified** against the exact strings from this log: 'breaking dimensions' is no
longer in the gated pool (it was reachable before); the ad and the page copy are both
rejected as uncached; all 8 real setlist songs are still accepted, including with
trailing noise ("BANZAI!!"); with no setlist the pool and behaviour are unchanged.

**Related, still open:** `#5` — with a setlist parsed, the app should also *trust* it
for song boundaries rather than waiting on OCR/Shazam corroboration.

---

## v1.1.84 — 2026-07-18 (TICKET-188 — "where's the lyrics?" — the rescue was blocked 3 ways)

### TICKET-188 — Correct 32-line body on disk, app showed a 4-line stub 🟢

**Symptom.** A lyric video (`不可思議のカルテ` / Fukashigi no Carte, 236 s) showed no
usable lyrics. The app *had* identified the song — `title-lock: … → LOCKING` — and
then effectively dropped it.

**State at the time:** `line_count = 4`, `current_line = None`, every sync read
logging `line#-1@-1.0`.

**Three bodies for this song were already on disk:**

| file | lines | source | span |
|---|---|---|---|
| `fukashigi_no_karte.json` | **32** | syncedlyrics/musixmatch | 0.3 → 239.3 s ✅ |
| `fukashigi_no_carte_ver_lyrics.json` | 10 | generated (by-ear) | 66 → 154 s |
| `fukashigi_no_carte_live_2024.json` | 5 | generated (by-ear) | 45 → 68 s |

**Root cause — a chain, each link individually reasonable:**

1. **The title lookup missed.** SMTC reported the title in kana (`不可思議のカルテ`);
   the library file is romaji (`fukashigi_no_karte.json`, meta title
   "Fukashigi no KARTE"). `no confident title-match … (best 0)`.
2. So the app fetched/generated a **4-line by-ear stub** — and then **TITLE-LOCKED
   onto it**, because the *title* really was right. The lock protects the body.
3. `decide-by-ear` then did its job and found the truth:
   `heard '…' → best fukashigi_no_karte.json (67) vs loaded (0)`.
4. **And every guard blocked the rescue:**
   - short-transcript gate (TICKET-081): `only 15 chars heard — inconclusive, no action`
   - title-lock bump: `MIN` 60 → **75**; 67 fails
   - lopsided override: needs `3 × MARGIN` = **84**; 67 fails
   - cross-artist block: the real body credits the seiyuu cast, SMTC said the
     uploader "Shiina" → outright BLOCK off a title-locked song

Each guard was written for a real regression (Suisei 綺麗事, kamone, 名前のない怪物).
They share one unstated assumption: **that the loaded body is worth protecting.**
Against a 4-line stub scoring 0 they combine into a trap that cannot be escaped.

**Fix — name the assumption and check it.**

* `loaded_worthless` = body under `ear_thin_body_lines` (8) **and** transcript scores
  it under `ear_thin_body_score` (8). When true, the title-lock bump and the
  cross-artist block are both lifted — you cannot protect a right song you do not have.
* The short-transcript gate now distinguishes *deceptive tie* from *decisive*: a short
  read may act when best ≥ 55, loaded ≤ 8 and the margin ≥ 45. TICKET-081's actual
  failure was a near-tie at low score, which stays inconclusive.

**Verified** (gate arithmetic replayed): the real case now SWITCHes at MIN=60/MARGIN=12;
a title-locked *healthy* body still resists a mediocre match (MIN=75); the kamone
cross-artist block still holds on a healthy body; a short near-tie is still
inconclusive; and a short-but-decisive read against a *healthy* body still does not
switch.

**Follow-up (the primary miss, not yet fixed):** step 1. Title matching should connect
a kana title to a romaji-named library entry. The app already romanizes for its `rm`
row, so `不可思議のカルテ` → `fukashigi no karute` fuzzy-matches `fukashigi_no_karte`.
Fixing that removes the whole chain and the ~30 s of thrash before the rescue, instead
of relying on the by-ear safety net. Tracked separately.

**Also seen in this log:** the by-ear generator keeps writing near-duplicate stubs for
songs that already have a real body (`fukashigi_no_carte_ver_lyrics`,
`fukashigi_no_carte_live_2024`), which pollutes the library and gives later title
lookups more wrong things to match. Worth a dedupe pass.

---

## v1.1.82 — 2026-07-18 (TICKET-186 — "NO SUBTITLES"; LP-010 — highlights skipped)

### TICKET-186 — Overlay showed nothing: following a dead session from another app 🟢

**Symptom.** Subtitles mode on, a YouTube video playing in Brave with captions
visible in the player, and the app displayed **nothing at all**.

**Everything downstream looked healthy**, which is what made this confusing —
`/overlay` was serving a well-formed payload, `lyric-overlay.exe` was running and
polling it, and the renderer handles stationary mode correctly (`renderLine`). The
problem was one level up: the app was following the **wrong media session**.

```
/status  player_title: 東方ストリングスアレンジ… (a 3h09m BGM video)
         position: 2207.12   playing: FALSE   line_count: 0
```

**Enumerating SMTC directly settled it:**

```
2 sessions.  Windows' current session = 'Brave'

  com.squirrel.AnthropicClaude.claude   PAUSED   Touhou BGM   2207.1/11370.0   <-- followed
  Brave                                 PLAYING  (the user's video)
```

**Claude Desktop** was publishing a media session for a video paused an hour
earlier, and the app was locked onto it.

**Root cause.** `MediaWatcher._pick` step 3: when *nothing* is playing it keeps the
session it was already following, so the overlay holds the current song through a
gap between tracks. That is right for its intended case, but it had **no way to
expire a session** — an unrelated app publishing a long-paused session holds the
lock forever. The app then reports `playing=false` with 0 lines and the overlay
correctly draws nothing.

The signal to break the tie was available the whole time and unused:
`GlobalSystemMediaTransportControlsSessionManager.get_current_session()` — the
session Windows considers current, i.e. the app the user last interacted with. It
said **Brave** while we sat on Claude Desktop.

**Fix.** Each enumerated session now carries `is_current`. When nothing is playing:

* our session **is** current → keep it (this is the gap-between-tracks case, intact);
* nothing is current → keep it (Windows has no opinion, old behaviour);
* otherwise → **hand over to the session Windows calls current**.

Priority is unchanged otherwise: pin > audible-pref > playing > current > sticky.

**Verified** (unit test over the real `_pick`): stale paused session loses to the
current app; a gap between tracks still keeps our session; a PLAYING session still
beats a paused current one; with no `is_current` anywhere the old sticky behaviour
is preserved; a pin still overrides everything.

**Note for future debugging:** `window: []` in `/overlay` is *not* a bug in
stationary mode — the window array is only built for belt modes (`lr/rl/tb/bt`);
line modes render from the `line` field. Cost me a detour.

---

## v1.1.81 — 2026-07-17 (TICKET-184 — the app CRASHED twice in one evening: whisper/CUDA)

### TICKET-184 — Hard crash in ctranslate2 (CUDA/cuDNN) took the whole app down 🟢

**Symptom.** Mid-concert the app vanished. No traceback in `karaoke.log` — the log
just stops. Happened twice in one evening, on v1.1.79 **and** v1.1.80.

**Windows Event Log (both crashes, identical shape):**

```
Faulting module: KERNELBASE.dll  code 0xe06d7363   (MSVC C++ throw)
Faulting module: ucrtbase.dll    code 0xc0000409   (abort / __fastfail), ~8s later
```

**Crash-dump analysis** (stdlib minidump parse — exception stream + module list +
stack walk of the faulting thread):

```
THROWING MODULE : ctranslate2.dll
crashing thread : a WORKER thread, not main
stack           : ctranslate2 -> cudnn64_9.dll -> ctranslate2 -> nvcuda64.dll
```

A C++ exception thrown on a **native worker thread** never crosses back into
Python, so `std::terminate` → `abort()`. **No `try/except` anywhere in the app can
catch this.** That is the whole reason for the fix below.

**Root cause — two defects that compound:**

1. **`gpu_setup.pick_inference_device` picked a GPU by utilization % and never
   looked at free VRAM.** The only NVML call was `nvmlDeviceGetUtilizationRates`.
   Measured live during the investigation: `cuda:0` (RTX 2080 Super, 8 GB) sat at
   **0% utilization with 485 MiB free** while `cuda:1` had 5.7 GB free. The old
   logic ranks by utilization, so it chose the *starved* card — it looked idlest.
2. **`align._models` was append-only.** Keyed `(size, device, index)` with no
   eviction, so a session that touched `base`, `small` and `medium` across both
   cards kept every one resident in VRAM for the life of the process.

**The tell we missed, already in the log.** Before *each* crash there is a run of
GPU loads silently falling back:

```
19:39:41 whisper model 'small' on cpu (idle GPU 1 (16% vs cuda:0 41%))
19:40:23 whisper model 'small' on cpu (idle GPU 1 (24% vs cuda:0 38%))
19:41:48 whisper model 'small' on cpu (idle GPU 1 (14% vs cuda:0 32%))
19:42:15 *** CRASH ***
```

The *reason* says GPU, the *device* says cpu — i.e. `WhisperModel()` threw and hit
the CPU fallback, which swallowed the exception without logging it. Allocation was
already failing minutes before the fatal one.

**Fix (three layers).**

1. **VRAM is now a hard filter.** `_gpu_stats()` reads `nvmlDeviceGetMemoryInfo`;
   `pick_inference_device(..., need_mib=)` refuses any GPU without real headroom
   for that model (`align._MODEL_VRAM_MIB`, workspace included), falling to the
   other card then CPU. Verified against the live starved-card state: every model
   size now routes to `cuda:1` even though `cuda:0` reports lower utilization.
2. **Whisper runs in a CHILD PROCESS** (`whisper_worker.py`). This is the only real
   defence against an uncatchable native abort: the child dies, the parent logs it,
   flags the GPU unsafe for the session (`KARAOKE_WHISPER_FORCE_CPU`), respawns on
   CPU and carries on. Loopback socket + token (a windowed PyInstaller app has no
   usable stdout — the recognize child learned that the hard way). The model stays
   warm between requests. Knob: `whisper_child` (1).
3. **One CUDA model at a time.** `_evict_cuda_locked` frees other resident CUDA
   models before a new load, and `_model_for()` refcounts borrows so a model is
   never freed mid-transcribe (destroying a ctranslate2 model in use is itself a
   crash). Lazy `segments` generators are now drained *inside* the borrow.

**Also fixed along the way:** `CUDA_DEVICE_ORDER` is pinned to `PCI_BUS_ID` at
import. Without it CUDA enumerates FASTEST_FIRST while NVML uses bus order, so
`cuda:N` can be a *different card* than NVML index N — every utilization and VRAM
reading would be attributed to the wrong GPU. It happened to be set machine-wide on
the dev box, which is why the bug never showed there.

**Verified:** dump analysis identifies the exact throwing module; the VRAM guard
re-routes correctly against the real starved-card state; the child survives a
simulated `0xC0000409` abort and blacklists the GPU, while a clean `rc=1` kill does
not; the parent transparently recovers and respawns.

### TICKET-185 — Concert: correct lyrics fetched but never shown 🟢

Same session, same root cause. The Offkai Gen4 concert parsed its setlist and
**fetched the right body** (`deep real: saved 45 lines -> kton_boogie.json`), but
never displayed it. A bogus first Shazam read needed corroboration before a first
load in concert mode, and whisper's GPU work was producing frame times up to
**2637 ms** — so the smoothness governor cancelled identification over and over
(**4 cancelled, 4 recognize children killed, 17 skipped/delayed** in 3 minutes),
the bogus read was never displaced, and then the process died.

Fixed by TICKET-184: whisper's GIL/GPU load moves out of process, so the governor
stops firing and identification can actually complete. This is the same lesson as
TICKET-135 (identify-by-sound moved to a child to fix "highlight sticks then
jumps") — whisper was the last in-process offender.

---

## v1.1.80 — 2026-07-17 (TICKET-183 — "lots of songs never sync"; land within 0.5s of the sung lyric)

- **TICKET-183 — browser VTuber MVs never reliably synced; make caption timing the master → ≤0.5s** 🟡 implemented on master, pending rebuild/deploy. User: "this song never synced… we MUST fix syncing. IT must land within 0.5 second." RCA (5-agent workflow + live log, flagged songs CHIMERA/High Tide/Say My Name): browser hololive ORIGINAL MVs have **no video-locked timing anchor** and every fallback fails on this material. (1) The one reliable anchor — the video's OWN YouTube caption timestamps (YouTube force-aligns cue starts to the audio, ~0.3-0.5s) — was switched OFF on the song path (`deep_transcribe.py writeautomaticsub=False`; the auto-caption retry gated to subtitles-mode + English-only). (2) The energy correlator (`_run_energy_correlation`) structurally can't lock: produced MVs keep tonal synths/pads in the 200-3000 Hz "vocal" band, so the on/off mask has no contrast → flat agreement surface (`best 0.653 vs rival 0.653, margin 0.000`) → both gates fire → **"no change" forever** or rails to the ±15s window edge (the logged -10/-11/-15s). (3) An ungated one-shot vocal-onset handler misread a late onset as a huge intro → the catastrophic **-172s / -134s** offsets that then snap back to 0 and oscillate. FIX (per the user's chosen scope P0+P1+P2): **P0** `deep_transcribe.fetch_caption_timing(url, lang)` — fetch the video's OWN-language AUTO-caption cues as a TIMING GRID (native lang only; an `en` auto-track for a JP song is a re-timed machine translation, NOT force-aligned; exact URL/id only). **P1** `align.retime_to_captions()` — order-preserving text-match the nice provider/generated body onto that cue grid, each line adopting its cue start (interior gaps interpolate, ends shift by the edge delta, monotonic clamp); returns None below 30% matched-anchor coverage so a wrong/absent grid leaves the body untouched. Wired in `load()`: a browser provider/generated body schedules a background retime (once/track, exact `_now_url`), `_apply_caption_retime` rewrites the line times + zeroes the offset + sets `_caption_timed` so `_maybe_auto_align` skips the correlator (as it already does for `youtube-captions`). **Copyright:** the caption ASR text is consumed as a timestamp grid ONLY — matched, then discarded; never displayed, persisted, or indexed; the clean provider text stays. **P2** `_on_vocal_onset` guards: a top `_intro_anchored` gate (one anchor/track), a plausibility gate (reject onsets past `onset_max_intro_s`=90 after the 1st line / past half the video / past the LRC end), and tightened the -300/-120 caps to -90 — killing the -172s catastrophe. Precedence now: manual caption > provider body re-timed to the native auto-caption grid > Shazam catalog > (disambiguated) energy > decide-by-ear > guarded onset. Verified: `retime_to_captions` unit test 8/8 (exact ≤0.5s on a 12s-drifted body incl. interpolated gaps; safe None on an unrelated grid / low coverage); onset guards + retime adversarially checked against legit cases (already-synced bodies, Grimes-Genesis long intro, decision-engine interactions). Reaches ≤0.5s on any browser MV that has captions (nearly all); fails safe to today's behavior when captions are absent (where P3 correlator hardening — deferred — would help).

---

## v1.1.80 — 2026-07-17 (TICKET-182 — wrong song rode the whole track; "Wrong lyrics" button felt dead)

- **TICKET-182 — a late-loaded wrong body is never word-checked + the "⚑ Wrong lyrics" button deferred up to 3s** 🟡 implemented on master, pending rebuild/deploy. Live repro: Mori Calliope "Non-Fiction" (hololive EN Myth) showed a wrong Japanese song's lyrics the whole track. RCA (live `karaoke.log`): `no confident title-match for 'Non-Fiction' → sound → generate-by-ear`; deep generation then returned a WRONG same-title Japanese `syncedlyrics/cover` (30 ja lines) that **loaded ~104 s in**. The single track-start `decide-by-ear` had fired at `decide_at_s` (+12 s) and **bailed on `not self.lines`** (nothing loaded yet), and it is scheduled ONCE per track — so the late-loaded wrong body was **never word-checked** and rode the whole song. Meanwhile the decision engine's `source_agree` only flickers BAD when Shazam actively hears a conflicting song (VTuber originals give null reads → OK), and `ear_corrob` abstains for a title-locked-word-unverified body (the kamone probe guard) — so both identity dims were blind. **The decision tree** (for the record): `_decision_engine_tick` (2 s) scores 4 dims — `source_agree` (SMTC-vs-Shazam title), `sync_stable` (drift/timing only), `lyric_quality` (mojibake/*** only), `ear_corrob` (last by-ear word score) — sums strikes (BAD=2/DEGRADED=1), escalates ≥3 CAUTION / ≥5 SWITCH / ≥8 REGEN, but SWITCH/REGEN are heavily suppressed (drift-only, bundled/corroborated/caption immunity, 30 s cooldown). `decide-by-ear` is the only WORD-level check. FIX (per the user, the safest lever — make decide-by-ear RUN more, don't touch its scoring): (1) `load()` now schedules a fresh `decide-by-ear(reason="late-load")` when a re-checkable, not-word-verified body loads AFTER the track-start probe window (`_played >= decide_at_s`, guarded on `not _deciding / not live / not subs`, `decide_probe_late_load`/`decide_probe_load_delay_s` knobs) — so a late wrong body is caught and reject+reseeks via decide-by-ear's existing (guarded) switch/blacklist/refetch chain. (2) `report_wrong` (the "⚑ Wrong lyrics" tray item) now **drops the visible lyrics immediately** (`wrong_immediate_clear`, was a ≤3 s deferred swap) and escalates to the video's own caption track + OCR alongside Shazam, so "look for the next most likely" uses every signal. Adversarially verified (5-agent workflow) that the late-load probe cannot NEWLY tear down the documented correct-body cases (feelingradation / Suisei 綺麗事 / kamone cross-artist block / Tori no Uta / a slow-correct LRC) — it only extends WHEN decide-by-ear runs, not HOW. Compiles clean.

---

## v1.1.80 — 2026-07-17 (TICKET-181 — fine-syncing lurched the scroll belt ("stuck / jumping"))

- **TICKET-181 — small ongoing sync corrections lurch the scroll-through belt; "relax the fine syncing"** 🟡 implemented on master, pending rebuild/deploy. User (scroll-through / "rl" belt, hololive Advent "Rebellion"): the highlight keeps getting **stuck and jumping**; wants to "let it go and not use highlights with fine syncing." RCA (docs/PERFORMANCE.md + LYRIC_PERFORMANCE.md + code): the render itself is healthy (measured earlier this session: `render 60fps, worst 20ms, jitter 0.6`), so this is **not** low fps — it's the **sync clock lurching**. In a scroll-THROUGH belt the whole belt position rides `pos + offset`, so EVERY small offset correction visibly shifts all lines at once. Two sources: (1) the TICKET-085 **fine-tune** ±0.2s precision pass (`fine-tune-rewind` = re-scrolls shown text → "stuck"; `fine-tune-catchup` = "jumping"), and (2) the regular tier's **micro-nudges** (e.g. logged `sync-micro-nudge → -0.70s`) + energy-align / live-follow — all route through `_smooth_offset`, all ≥ the 0.22s line-mode floor, all lurch the belt. FIX (main.py, both scoped to scroll-through belts only; line mode unchanged): (a) `_smooth_offset` uses a wider deadband `sync_apply_min_s_scroll` (default **1.0s**) for AUTO corrections in lr/rl/tb/bt, so sub-second corrections are held (a <1s timing error is imperceptible on a continuously moving belt, but a <1s offset STEP is very visible); manual nudges/resets and big seeks still apply. (b) `_maybe_enter_fine_tune` no longer enters in scroll-through (`fine_tune_in_scroll` default 0) — its nudges wouldn't apply anyway and each 8s fine-tune Whisper listen briefly stalls the render (GIL, cf. PERF-007). Plus a `fine_tune_enabled` master switch. (c) `_eased_offset` uses a **gentler ease in scroll mode** (`ease_slew_cap_s_scroll` 1.0 / `ease_pull_per_sec_scroll` 1.5 vs the 3.0/3.5 line-mode default) so the rare ≥1s re-anchor that DOES pass the deadband glides smoothly instead of whooshing the belt at ~4× realtime; line mode keeps the snappy ease. All are live `/tune` knobs. Drift stays bounded (~1s) because the tier still re-anchors once |drift| exceeds the deadband. **Measured** (offline `belt_sim.py`, realistic 60s correction schedule): the deadband cuts visible-lurch frames (>1.5× belt velocity) **132→44** and the gentle ease drops obvious-lurch frames (>2×) **31→12** with per-frame velocity variance **0.233→0.137 (~40% smoother)**. **Render is NOT the bottleneck** (separate `render_bench.py`): the glyph-atlas warm re-render is ~5ms and the sliver fill ~1ms at font_scale 1.5 — easily 60fps; the only cost is the one-time ~39ms first-appearance per unique line (already cached after). Notably the documented-but-unbuilt LP-005 lever #2 (flat render + alpha-composite outline) was benchmarked and **regresses** at font_scale 1.5 (42ms vs 39ms) — do NOT build it; see docs/LYRIC_PERFORMANCE.md LP-008. Immediate relief on a running v1.1.79 (no rebuild): `POST /tune sync_apply_min_s=1.0` + `fine_tune_enter_after_s=999999`.

---

## v1.1.80 — 2026-07-17 (TICKET-180 — English song showed Japanese caption lyrics for the whole song)

- **TICKET-180 — "Use YouTube captions" showed a Japanese AUTO-TRANSLATION as the lyrics for an all-English song (hololive Advent "Genesis")** 🟡 implemented on master, pending rebuild/deploy. RCA (live, via `karaoke.log` + yt-dlp + web): the Genesis MV (`h1A76PvsqD4`) is sung entirely in English, `info.language='en'`, has NO manual caption track — only YouTube auto-captions (en ASR + 156 auto-translations). The app pulled the **ja** auto-translation (49 lines) and displayed Japanese for the whole song, `source='youtube-captions'`, never rejected. Three stacked defects were found (workflow RCA): **(A)** the MUSIC-mode caption picker ignored the video's own language; **(B)** a bare-title fuzzy match seeded `lang='ja'` by accepting Ado's "New Genesis" for "Genesis" (score 69); **(C)** caption bodies are auto-trusted for identity so the wrong-language body was never word-verified. Per the user, **only Defect A** is fixed here (the single change that stops the on-screen symptom); B and C are deferred. FIX (`deep_transcribe.py` `_captions_from_dir`): (1) un-gate the video's own language (`orig_lang` from `info.language`) so it leads track ranking in BOTH subtitles and music mode (was gated to subtitles only); (2) in MUSIC mode, decline a CJK track when the video's original language is confidently non-CJK (a `ja`/`zh`/`ko` render of an `en`/Latin original is a translation, not the lyrics) → returns None so the app falls back to provider LRC / by-ear (the real English). Fail-safe: when `info.language` is unknown/placeholder the old CJK-first behavior stands, so genuinely Japanese songs are unaffected; subtitles submode is untouched (a translated subtitle is often what's wanted there). Verified: 10/10 unit cases on `_captions_from_dir` (Genesis en+ja→None; genuine ja/zh kept; en-native kept; en+ja both present→en; unknown/placeholder→ja; subtitles ja kept; ko-of-en→None). NB: on a machine with a stale `new_genesis.json` cache, fully-correct lyrics also needs Defect B (else the fallback can land on the provisional wrong title-match); A alone guarantees the wrong-language caption track is never shown.

---

## v1.1.79 — 2026-07-17 (TICKET-179 — captions showed literal HTML entities `&gt;&gt;`)

- **TICKET-179 — subtitle overlay showed `&gt;&gt;` / `&amp;` / `&#39;` (raw HTML entities)** 🟢 v1.1.79. Once TICKET-178 made captions display on the Hanford talk video, the text read "**&gt;&gt;** All right, so I know **&gt;&gt;** what this image…". YouTube auto-caption VTT cues carry HTML entities (`&gt;&gt;` = the `>>` speaker-change marker, `&amp;`, `&#39;`, `&quot;`) and `_parse_vtt` stripped `<tags>` but never `html.unescape`d, so they rendered literally. Same gap in `movie_subs._parse_srt` (OpenSubtitles SRT). FIX: both parsers now `html.unescape()` after the tag strip, then drop the decoded `>>`/`>>>` speaker-change markers (caption convention, not spoken words — noise on a lyric-style overlay). `import html` added to deep_transcribe.py; `import html as _html` to movie_subs.py. Language-safe (only touches `&…;` sequences; JP/other text untouched). Verified: the cache body `…worse than Chernobyl or any &gt;&gt; Wrong. Today we're going…` now decodes to `…worse than Chernobyl or any Wrong. Today we're going…`.

---

## v1.1.78 — 2026-07-17 (TICKET-178 — Subtitles toggled ON mid-video showed nothing on a long talk video)

- **TICKET-178 — subtitles don't display when toggled ON mid-play on a long non-music video** 🟢 v1.1.78. User: a 33-min English reaction video ("No, the Hanford Site isn't Worse than Chernobyl" / T. Folse Nuclear), Subtitles toggled ON, overlay stayed empty. RCA (NOT the English language — captions fetched fine, 1764 en lines both times): the video-LENGTH heuristic flags any >~10-min video `_live_mode=True` ("live/concert mode via VIDEO LENGTH 1998s") at track change. The concert-vs-subtitles split that CLEARS that length-based `_live_mode` (so subtitles own the track) lives ONLY in `_on_track_change`, and only runs when subs are ALREADY on at track change. Toggling subs on AFTER the video loaded → the split never re-ran → `_live_mode` stayed True → `_subs_suppresses_sound()` (= subs_on AND NOT _live_mode) = False → `_apply_captions` refused the fetched captions with "subtitles no longer own this track — not applying show dialogue" → empty overlay. FIX (2 parts, main.py): (1) `_set_subtitle_active` (the toggle-ON chokepoint for menu + API) now re-runs the split for the CURRENT track before kicking the fetch — clears a length-based `_live_mode` + sets `_live_video_nonmusic` + spawns the bg category probe, UNLESS the title carries a real music-concert cue (`is_live_or_compilation(title, None)`) or the source is a music app. (2) belt-and-suspenders: the `_apply_captions` ownership gate now also passes when `_live_video_nonmusic` is True (a confirmed-non-music video's captions paint even if a stray `_live_mode` is set; a real concert keeps the flag False and still blocks). LIVE-VERIFIED on the exact video: before = "no longer own → not applying" (line_count 0); after v1.1.78 = "subtitles ON mid-video — cleared length-based live-mode" → "captions: applied 1764 lines" → /subtitles line_count=1764, subtitle_body=True, source=youtube-captions, lang=en, showing the real dialogue.

---

## v1.1.77 — build hardening (TICKET-177 — CI guards so a broken AI stack can't ship silently)

- **TICKET-177 — permanent guards against the TICKET-176 whisper-breakage class** 🟢. The av/`.deps` skew (see TICKET-176) that killed faster-whisper in v1.1.74→76 was invisible: the app just showed "needs faster-whisper" hints, nothing in the log, and `.deps` is gitignored so any build machine can reintroduce it. TWO layered guards, both wired into build.bat and verified end-to-end: **(1) pre-build** `scripts/check_build_deps.py` (step 1b) — compares `.deps` vs build-env versions of the native stack (av / ctranslate2 / faster-whisper / tokenizers). A plain version difference is a **WARNING** (dist-info metadata can lag the real module files after a partial copy, so a hard error there could block a good build); **DUPLICATE dist-info dirs** (a `pip install --upgrade --target` leaves the old one next to the new → the bundle becomes a coin-flip) are a **hard error** (exit 1, fails the build). **(2) post-build** `main.py --selftest --out FILE` (step 2b) — runs BEFORE any GUI init in the FROZEN exe, imports av + faster-whisper, checks `align.available()`, writes a one-line verdict, and `os._exit(0/1)`; build.bat runs it via `Start-Process -Wait -PassThru` (no window) and **fails the build** if the finished exe's AI stack is broken. This post-build gate is the definitive one — it exercises the real bundled module, catching ANY breakage regardless of cause. Also **rebuilt this machine's `.deps` clean** (nuked + `pip install --target .deps` pinned to env: av 18.0.0 / ct2 4.8.1 / fw 1.2.1 / tok 0.22.2) so module AND metadata agree (the earlier TICKET-176 robocopy fixed the module but left stale dist-info). Verified: pre-build check passes clean (exit 0), warns on skew, errors on dupes; frozen `--selftest` returns "av + faster-whisper import and align.available() is True" (exit 0) on the real bundle. docs/BUILD.md documents the invariant. v1.1.77 was already released (binary works); guards protect FUTURE builds — no re-release.

---

- **TICKET-176/177 follow-up — the guards that check the thing that actually breaks** 🟢
  v1.1.89. The two guards above are both *proxies*: `check_build_deps.py` compares
  **version strings**, and `--selftest` proves failure only by exit code. But this
  failure is about **DLL file identity** — PyAV's `av/_core.pyd` imports
  delvewheel-mangled FFmpeg DLLs (`avformat-62-<hash>.dll`) whose names embed a
  per-build hash, so two PyAV builds of the *same version* can still disagree, and
  dist-info metadata can lag the real module files. Three additions close that gap:
  **(1) `scripts/check_av_dlls.py`** parses the PE import table of every `av/*.pyd`
  and asserts each FFmpeg DLL it imports is present in `av.libs` — stdlib `struct`
  only, no `pefile`, so it cannot be silently skipped on a machine that lacks a
  dependency. Wired into build.bat **twice**: pre-build (step 1c, against the `.deps`
  that will be bundled) and post-build (step 2b, against the **shipped bundle**, which
  catches a PyInstaller collection that mixed sources even when the environment was
  clean). It runs without launching the exe, so it names the missing DLL instead of the
  selftest's bare exit code. Verified against the live `.deps`: *48 .pyd, 7 FFmpeg DLL
  imports, 7 present, 0 missing*. **(2) `requirements-deps.txt`** pins the exact
  known-good native set so `.deps` is reproducible and cannot drift back into a skew.
  **(3) A mixed-ABI check** in `check_build_deps.py`: the spec's TICKET-196 guard passes
  when the build tag is merely *present*, so a `.deps` vendored twice under two Pythons
  (holding **both** cp311 and cp312 — a live risk on a box with a 3.11 agent venv on
  PATH and a 3.12 build Python) sails straight through it, and the duplicate-dist-info
  check is blind because a dist-info name carries no ABI tag. This is the only guard
  that catches a mixed vendor tree. Plus **`/diag.whisper`** — the runtime counterpart,
  reporting `align.available()` and, when false, `align._last_error`: the build guards
  prove the bundle was assembled correctly, this proves the stack actually imports on
  the machine that has it.

---

## v1.1.77 — 2026-07-16 session (TICKET-174 — wrong lyrics never rejected by transcription)

- **TICKET-176 — WHISPER WAS DEAD IN EVERY PACKAGED RELEASE (v1.1.74→77): stale vendored `.deps\av` mismatched the bundled `av.libs`** 🟢 v1.1.77. The deepest root cause, found while chasing bvdiz. `align.available()` (`import faster_whisper`) failed in the FROZEN app with `ImportError: DLL load failed while importing _core` — so **generate-by-ear, sync-by-listening, AND decide-by-ear all silently returned False** (each degrades to "needs faster-whisper" / a no-op). Diagnosis: dumped `av/_core.pyd`'s PE import table — it needs `avformat-62-9c2d3ee….dll` etc., but the bundled `av.libs` had `avformat-62-b6d6bb16….dll` (different delvewheel hashes). The `_core.pyd` and the DLLs were from **two different av builds**: the spec bundles the whisper stack from a vendored `./.deps` (`pathex=[".deps"]`, `WHISPER = isdir('.deps')`), and `.deps\av` had gone STALE at **av 17.1.0** while the collected `av.libs` came from site-packages **av 18.0.0** → 17.1.0's `_core.pyd` + 18.0.0's DLLs = unresolved imports. FIX: re-synced `.deps\av` + `.deps\av.libs` to the current, consistent site-packages av 18.0.0 (robocopy /MIR) so `_core.pyd` and its DLLs match; verified the bundled `_core.pyd`'s required DLL is now present, and `import av 18.0.0 + av._core` loads. Also (belt-and-suspenders, `align._ensure_deps_path`) the FROZEN app now registers its OWN `_MEIPASS/av.libs` (+ ctranslate2/onnx/cuda dirs) with a PERSISTENT handle before importing faster-whisper — PyAV's delvewheel shim discards its `add_dll_directory` handle and computes the libs path relative to `__file__`, which doesn't resolve in the PyInstaller runtime. **Build-hygiene follow-up: keep `.deps` in sync with site-packages (re-run `pip install --target .deps --upgrade faster-whisper av ctranslate2 tokenizers`), or the mismatch recurs.** This unblocks TICKET-174/175 — decide-by-ear can now actually run and reject a wrong body.
- **TICKET-175 — decide-by-ear (reject-by-transcription) NEVER fired for browser videos (the real "why bvdiz stuck")** 🟢 v1.1.77. Live-caught while verifying TICKET-174: the whole reject-by-transcription path depends on `_decide_by_ear` running, but it has a gate `if position < decide_at_s-2: return` and browser/YouTube sources report an UNRELIABLE SMTC position (a YT-Music "…- Topic" bvdiz sat at `position=1.04s` the whole song). So the gate silently returned EVERY time and decide-by-ear (scheduled at track-start + on /decide) never reached transcription — no "listening among…" log ever. FIX: fall back to WALL-CLOCK elapsed since `_track_t0` — proceed when EITHER the SMTC position OR the seconds-actually-playing is ≥ decide_at_s-2 (status==PLAYING guards paused time). This is what makes TICKET-174's reject/blacklist/regen chain actually engage for browser videos (the common case). Diagnosed via /status position=1.04 + the missing pre-transcription log.
- **TICKET-174 — a wrong lyric body with the RIGHT title is never rejected ("bvdiz")** 🟢 v1.1.77. User: bvdiz "never got the right lyrics and didn't reject the one it thought it was — sync/reject by sound/transcription didn't work." RCA: `_body_corroborated` conflated TWO different proofs. The energy correlator (`_note_energy_verdict`) sets it True on an "insync"/"corrected" verdict — but energy only proves the line-**timing** grid matches the vocal on/off pattern, NOT that the **words** are right. A wrong lrclib body (title 'bvdiz', artist 'Re GLOSS', 54 lines, dur 167.7s — matches the song's length so it energy-aligns) got "corroborated", and `_decide_by_ear`'s immunity gate (`verified AND title_locked AND _body_corroborated`) then SKIPPED it — so the transcription word-check never ran and the wrong words were never caught. Reject-by-sound can't catch this either: Shazam heard 'bvdiz', the loaded TITLE was 'bvdiz', title matched → trusted. FIX (4 parts, all in main.py): (1) split the flag — new `_body_word_verified` is set ONLY by an actual transcript word-match (decide-by-ear: loaded_score ≥ decide_min_score, or LLM-confirm/switch) and is now the ONLY thing that grants decide-by-ear SKIP-immunity; `_body_corroborated` (still set by energy) keeps its sync-scoring role. Reset at all 5 per-song/body-swap sites (else a new wrong body inherits stale immunity); bundled = both True; `/sync` diag exposes both. (2) The `_score_ear_corrob` "kamone" anti-thrash guard re-keyed `not _body_corroborated` → `not _body_word_verified`, so a word-unverified title-locked body (wrong bvdiz OR a CORRECT energy-locked *generated* body — both newly exposed once decide-by-ear stopped skipping them) does NOT feed decision-engine strikes → no REGEN thrash. (3) decide-by-ear's own re-fetch (loaded_score < decide_wrong_floor AND best < MIN) now `_blacklist_current_lyrics(...)` BEFORE `_start_fetch` — WITHOUT this the provider returns the SAME poisoned body and it reloads unchanged (the bvdiz no-progress loop); blacklisting its sig makes the fetch skip it (reject_sigs) → next provider or generate-by-ear the actual sung words. (4) the previously-DEAD `block_cross_artist` (set but never read) wired into the switch guard — needed now that correct title-locked energy-locked songs route through decide-by-ear's switch path. Cost: ~1 whisper/song to earn immunity (bounded — decide-by-ear scheduled ~once/song + one probe retry; smoothness-backoff guarded). **Adversarially verified via a 3-agent workflow** (coverage=SOUND; recovery+thrash first flagged the fix INCOMPLETE — dead-end + generated-body thrash — which drove parts 2-4; re-verified after).
- **Docs (commercial honesty)**: README claimed the portable build LEAVES OUT Whisper (~120 MB) — but faster-whisper + ctranslate2 ARE bundled (789 MB `_internal`, 273 MB zip). Fixed the contradictory "Optional AI" box + feature-table rows: everything (incl. AI) ships in the Windows build out of the box; only NVIDIA CUDA is on-demand. GitHub About/description/topics refreshed.

---

## v1.1.76 — 2026-07-16 session (TICKET-171 → 172 — concert song-catching + katakana titles)

Driven live: ZAWA MAKE IT MV (post-ad resume), MKBHD 2026 desk-tour w/ Subtitles ON (435 ja caption lines, non-music category confirmed), ReGLOSS "Reach the top" full 3D LIVE (72 min), and 6 ReGLOSS MVs (Symmetry, feelingradation, SUPER DUPER, bvdiz, LAKI MODE, Lucky Loud). Sweep of all 261 cached lyric files: **0 mojibake** (no U+FFFD, no cp1252 signatures), and every ReGLOSS CJK-titled cache is fully JP+romaji+English.

- **TICKET-171 — concert never catches the right song fast; junk ad music loads over the setlist** 🟢 v1.1.76. Reach-the-top RCA: the opener was Shazam-heard exactly ONCE (at its intro — live audio fingerprints rarely, only faithful sections match), then ~30 consecutive NULL reads followed. Two failure modes stacked: **(a)** a YouTube pre-roll ad's music (a real MV Shazam matches perfectly — "Tokyo Midnight Cruising Club") loaded over the concert because the "nothing loaded yet → switch immediately" path trusted a single uncorroborated hearing; **(b)** the real opener then sat unconfirmed for ~4.5 min under the "await a 2nd agreeing read" gate (which never came) until the blunt 6.5-min watchdog rescued it. FIXES: **(1)** `_live_hearing_corroborated()` — in a concert, ONE read is enough to act on when the concert's own context corroborates it (heard ARTIST ∈ the live video's title/artist, or heard TITLE ∈ chapter setlist / description candidate pool); live covers by other artists still need the 2-read confirmation (OCR/setlist carry those). **(2)** In a concert with nothing loaded, an UNCORROBORATED hearing is held for a 2nd agreeing read before first-load (kills the ad-music-loads-over-concert case); corroborated songs load instantly. **(3)** Stale-capture discard: a Shazam read whose identity shares nothing with the player title AND arrives <20 s after a track flip overlapped the OLD audio (the ad→content boundary) — dropped, not "trusted over the stale session". **(4)** Pending-switch escalation watchdog: a held switch forces a longer re-identify after 15 s when nothing is showing (blank overlay = worst state) / 90 s when lyrics are already up, instead of only the 6.5-min catch-all. **(5)** OCR hardening: uncached banner reads now require TWO identical consecutive reads before a cover-style fetch ('Games and Software' one-off junk no longer fetched), and the window-title-chrome discard logs once per 10 min per string instead of every 8 s. **(6)** Diagnostics: `identify` null-read streaks are counted + logged (were invisible), and `/diag.sync` now surfaces `null_read_streak` / `pending_switch` / `pending_switch_age_s`.
- **TICKET-172 — katakana-loanword title = 5 wrong-song strikes + blacklisted the correct body** 🟢 v1.1.76. Shazam says the English loanword ('feelingradation') for a katakana-titled cache ('フィーリングラデーション') — the SAME word — but Hepburn romaji ('fīringuradēshon') is too far from the English spelling for the existing cross-script layers, so every hearing counted a wrong-song strike: 5 strikes broke the title-lock, blacklisted the CORRECT body (reason=decision-switch) and re-fetched the same song in a loop for the whole track. FIX: `_same_song_title` Layer 4 folds both sides to a loanword skeleton (l→r, -tion→shon, epenthetic-u dropped, long-vowel/double collapse) and fuzzy-compares, with a vowel-blind backstop for vowel-quality mismatches (サマー 'samaa' vs 'summer'). Gated tight: artist must corroborate AND ≥8 folded chars, so short generics ('ghost' vs 'ghosting') can't slip through. Empirically tuned against 27 positive/negative pairs across both ReGLOSS albums (ratio≥82, or vowel-blind ratio≥88).
- **TICKET-173 — subtitles showed a translated track for an original-language video** 🟢 v1.1.76. Big channels publish manual TRANSLATED caption tracks; a subtitles user watching an English video with a manual 'ja' track got Japanese subtitles because `_captions_from_dir`'s subs fallback is `ja`-led (VTuber-heavy default). FIX: `fetch_captions_only` reads the video's OWN spoken language from yt-dlp (`info.language`) and threads it to `_captions_from_dir(orig_lang=…)`, which now prefers the video's native track (full tag + base) over any translation of it.
- **FYI (not shipped, one-file heal): `feelingradation.json`** (stale Latin-named dupe of フィーリングラデーション, en 0/48) backfilled to 49/76 en. Also noted `usseewa.json` is mislabeled `lang="zh"` for a Japanese body → wrong translator source echoes 55/81 windows (226 s, 0 translated); latent, left as a known-issue for a lang-redetect pass.

---

## v1.1.75 — 2026-07-11 session (TICKET-170)

- **TICKET-170 — "none of the tray menu options work" → feedback/UX RCA (5-agent audit of all ~40 items)** 🟢 v1.1.75. User: menu options feel dead, esp. the song-finding ones. Full audit verdict: ~36/40 items are correctly wired and do their labeled thing; the "nothing works" perception came from **2 real bugs + 2 silent-no-ops**, all in the recovery actions reached for when lyrics are wrong. Audio capture confirmed healthy (right device, real vocal signal), whisper available, every POST endpoint returns ok — so nothing was fundamentally dead; it was missing feedback + two logic bugs. FIXES: **(1 BUG) Re-fetch lyrics** was a silent no-op — `refetch` cleared `_fetch_key/_lyrics_path/_title_locked` but NOT `self.lines`, so `_on_track_change`'s same-song early-return ate it exactly when wrong lyrics were on screen; now clears `self.lines/idx/_kara` + cancels pending swap + toasts "🔄 Re-fetching lyrics…". **(2 BUG) ±0.2s fine nudge** was swallowed by the `sync_apply_min_s=0.22` auto-wobble floor (manual nudges shared the auto path); `_smooth_offset` now exempts any `reason.startswith("manual")` (nudge AND reset), so the finest step always applies. **(3 SILENT) Identify by sound** ran real Shazam but the result consumer was `if res:` with no else — on a null match (the COMMON outcome for VTuber originals shazamio can't fingerprint) it left a stale "🎧 Listening…" toast; now `identify_by_sound` flags the user request + surfaces the `_identifying`/subs guards, and the result path toasts "🔍 Couldn't identify by sound — not in Shazam" on None. **(4 SILENT) Library backup "Back up now"** emitted nothing on any path (and no `.git` on a portable install = guaranteed no-op); now toasts "💾 Backing up…/backed up/already up to date/failed" and "No lyric-library repo here". **(5) Get captions** manual click now hints on its two silent guards (nothing playing / already pulling). **(6 doc) api.py /nudge** docstring said "(+ = lyrics later)" — OPPOSITE of the tray label + actual effect (+offset advances the highlight = earlier); corrected. Everything else (toggles, sync coarse nudges, position/display/opacity/font/scroll, presets, GPU overlay, import, dev console, API, startup, updates, quit) audited WORKS. Root theme: Shazam/captions genuinely can't ID original VTuber content — that's content-dependent, and the fix is honest "couldn't find it" toasts so a working action that found nothing no longer reads as a dead button.

---

## v1.1.74 — 2026-07-11 session (TICKET-168 → 169)

- **TICKET-168 — songs not IDENTIFIED or synced: loopback bound to a device that isn't carrying the music** 🟢 v1.1.74. User (仮谷せいら ZAWA MAKE IT and every other song of the session): lyrics loaded by title-fetch but zero Shazam IDs and `energy-align: low vocal contrast (on=150 off=0 spread=0.000)` on EVERY song — 30s of mask where all 150 samples are IDENTICAL. TICKET-167 covered a silent stale endpoint; this is its sibling: the bound endpoint carries a CONSTANT signal (the default speaker is not the device the browser renders to — multi-device setups: HDMI displays, headsets, virtual devices), which is not "silence" so the 60s-silence reopen never fired, and `recognize.py` bound the same wrong device so Shazam heard junk for the whole session. FIXES: **(1)** `_probe_best_loopback()` — records ~0.5s from EVERY loopback and binds the strongest varying signal; used on any junk-triggered reopen. **(2)** FLAT-MASK watchdog — 150 recent mask samples with <1e-4 spread → reopen via probe (catches constant junk in ~30s; the explicit-silence zeros make it catch dead-silent streams too). **(3)** the boundary detector publishes `active_device` (also in /diag live_audio) and `_start_identify` hands it to the recognize child via `KARAOKE_LOOPBACK_DEVICE`, so Shazam captures the same verified device. Residual: the FIRST bind after app start still prefers the default speaker (cheap); the probe corrects within ~30-60s of junk.
- **TICKET-169 — movie-site subtitles: fetch the same subs the site's own panel offers** 🟢 v1.1.74. User: "videos from f2movies have subs we can scrape … allow for work when subtitles are on." yt-dlp can't see inside the embed iframes, so Subtitles mode dead-ended into whisper for 110-min films. New `movie_subs.py` (stdlib-only): watch page (plain HTTP + browser UA — only bot fingerprints get blocked) → title/year/TMDB id → legacy OpenSubtitles REST (keyless, the SAME source the site's panel rips — filenames match the panel byte-for-byte) → ranked by downloads/year/format with simplified-query retries ("3-D X: Y" finds 0; "x y" finds 15) → gzipped SRT → parsed to caption-line shape (ad cues dropped). TWO entry points wired into the captions worker: exact URL when the browser pushed one, else the WINDOW TITLE ('Watch X Movie… - F2movies - Brave'). Live-verified: 1,121 English lines for the test film from a 107k-download srt, both paths. Hosts: f2movies/fmovies/sflix/solarmovie/myflixer/watchseries (extensible tuple). TV-series (season/episode) and per-release cut alignment = future work.

---

## v1.1.73 — 2026-07-11 session (TICKET-167)

- **TICKET-167 — live-arrangement single song NEVER synced (YOASOBI 祝福 ARENA TOUR cut)** 🟢 v1.1.73. User: whole 4-min live cut played with lyrics visibly un-synced; RCA from karaoke.log. Classification was CORRECT (`[live-arrangement]`, `title-match 祝福.json score 112`, `LRC 181s < video 247s → MV intro mode`) — but every sync engine was BLIND for the entire song: `auto-align: not enough vocal-mask history (0 blocks)` on every 20s pass, ZERO boundaries, ZERO vocal onsets (so the MV-intro anchor never fired), ZERO Shazam sync-reads. ROOT CAUSE: `SongChangeDetector.run()` binds the WASAPI loopback of the **default speaker at open time**; when the default output device changed (~12:00:31, last boundary event of the session), the recorder kept reading the stale endpoint = pure digital silence forever — and silent blocks (a) never appended to `_vocal_buf` (the vocal-ratio branch lived in the non-silent arm), (b) never fire boundaries/onsets, (c) starve everything downstream. One dead capture thread silently disabled the entire sound stack with no log signature. FIXES: **(1)** stale-device watchdog in the capture loop — every ~5s compare `sc.default_speaker().name` against the bound device (when the bind matched the default) and reopen on change; plus a 60s-continuous-silence reopen that catches same-name re-routes and endpoint invalidation. **(2)** silent blocks now append explicit `(t, 0.0)` to the vocal mask — the correlator needs the OFF samples and the ≥12s history minimum stays reachable through quiet live passages. **(3)** `energy_max_offset_live` tune knob (120s; studio keeps 60s) used by energy-align + ocr-sync when `_live_arrangement/_live_mode` — a live cut legitimately needs a larger offset than the studio cap allowed (247s video vs 181s LRC ⇒ up to ~66s drift). **(4)** the 0-blocks log line now says WHY (`loopback NOT capturing / hears only SILENCE while playing (device changed?)`) so this failure class is diagnosable at a glance. Residual (open, minor): a single onset anchor can still drift through long instrumental interludes of a live arrangement — energy-align + ocr-sync now stay alive to correct it, but per-interlude re-anchoring would be the deluxe fix.

---

## TICKET-FUTURE-001 — Animated bouncing lyrics (per-syllable / per-character) 🔵
**Future feature.** User intends to add animated lyrics that BOUNCE when sung. Per-character per-frame animation × multiple chars × multiple frames per syllable = drastically higher render budget than current static-color fill. Hard prerequisites BEFORE this can be attempted:
- True 60 fps with no spikes (currently ~33 ms baseline + 100-500 ms freezes during track-change per perf.log)
- Sub-millisecond per-character paint cost (Tk Canvas itemconfig probably can't hold this; need to evaluate Pygame/SDL2/Direct2D/OpenGL via moderngl as alternatives, OR cache pre-rendered per-character bitmaps and batch-blit)
- Tk thread CANNOT do LRC parse / first-block PIL render synchronously during playback (TICKET-082b subprocess refactor mandatory)
- Need an animation timeline format (offset per-char attack/release curve)
**Status: blocked behind TICKET-082b + TICKET-088 (smooth-transitions still snapping) + general perf headroom.** Re-open when the prereqs are unblocked.

---

## v1.1.x — 2026-06-29 session (TICKET-130 → 137)

- **TICKET-130 — title-only fetch accepts a WRONG same-title song** 🟡 (partial fix v1.1.29). User: shirobeats "Rainy Day" showed lyrics about *beer* not in the song ("IS IT CHECKING ARTISTS?"). ROOT (from cache `rainy_day.json`: `src=syncedlyrics/title`, meta artist=shirobeats but body is a DIFFERENT "Rainy Day"): the bare-title last-resort (`fetch_lrc` step 4) ran because `strict=False`, and it skipped `_lrc_artist_conflict`, so a same-length different-artist song passed `verify_lrc`+`_strict_ok` (duration coincidence) and got mislabelled with the *player's* artist. Same family as Play On!→Sixth Sense (TICKET-120). **v1.1.29:** title-only path now runs `_lrc_artist_conflict` (rejects when the LRC `[ar:]` clearly differs). STILL OPEN: (a) why `_clean_source()` is False for a Spotify track; (b) `_strict_ok` trusts a Latin-title duration coincidence; (c) decide-by-ear FALSE-corroborated the wrong body (ear_corrob=OK on a mismatch).
- **TICKET-131 — highlight FREEZES every few seconds on unfingerprintable songs** 🟢 v1.1.28. Recal ran Shazam every ~4s while "unconfirmed"; each recognize stalls the render via the GIL; the back-off required |offset|≤1s which such songs never meet. Now only CONFIRMED songs get the fast offset re-sync, and unconfirmed-with-lyrics songs back off to ~28s after 25s regardless of offset.
- **TICKET-132 — highlight "PERMANENT ON" / not a progressive karaoke fill** 🔴 User: "PERMANENT ON HIGHLIGHTS ARE NOT A SOLUTION." In lr scroll multiple lines appear fully gold rather than the current line sweeping. Needs repro: past/sung lines staying gold as they scroll off, or the fill not advancing? Distinct from the clock (fixed).
- **TICKET-133 — sync falsely JUMPS to the wrong part mid-song + slow re-lock** 🔴 User on 暧昧ライア (Hachioji P): "in sync at first then falsely syncs to the wrong part and took way too long to properly sync." A bad offset correction lands mid-song (energy-align / decide-by-ear picks a wrong alignment); the re-sync to undo it is slow. Relates to reset-first / deferred-commit.
- **TICKET-134 — GPU renderer: full port to match CPU "exactly but higher fps"** 🟡 IN SOURCE @ v1.1.31, **NOT deployed to local yet** (user: "finish the gpu but do not put it on local yet"). DONE + offline-verified (probe self-capture): (a) belt de-cluttered — dim context lines, active-only gold + translations, tighter window (was "whole screen full" / "permanent on"); (b) SETTINGS PARITY — opacity (dims glyph RGB, color-key-safe), pos_y/pos_x (cy from pos_y, verified cy=259@top/821@bottom/540@center), font_scale (rebuilds fonts + drops glyph cache), all pushed from the parent each tick; (c) GPU-side `_eff_pos()` extrapolation — belt glides during a parent stall (≤+1.2s cap), no freeze; (d) crash-hardening — the draw loop catches per-frame errors so one bad frame can't kill the child (a dead child is what left the black frame). STILL OPEN: full CPU `_ticker_update` spawn/fill-budget parity; the ROOT cause of "clicking kills the child" (not reproduced — hardening is defensive); a guaranteed dead-child→destroy-window→restore-Tk path. To ship: build + deploy + flip `settings.json gpu_renderer=true` (or tray). Verify OFFLINE via `scratchpad gpu/probe_live.py` (full-screen `--width 1920 --height 1080` + self-capture), NOT live screenshots.
- **TICKET-135 — run Shazam recognize in a SUBPROCESS (smooth highlights)** 🟢 v1.1.32. recognize was a thread → shared the GIL → stalled the render (~150-475ms frames) during each ~4s capture = the user's "highlight gets stuck then jumps to the 2nd/3rd line" (the v1.1.26 clock re-anchor only made the post-stall jump LAND accurately, it couldn't render during the blocked thread). FIX: `_start_identify` now spawns a child PROCESS (`--recognize-child <secs> <atts>` in main(), or `recognize.py --child` in dev) that captures+fingerprints in its own process and prints one JSON line; the parent's worker thread just waits + reads it. Verified the FROZEN child returns JSON over a piped stdout (a windowed PyInstaller app writes to a piped handle even though `& exe` from a console shows nothing). In-process fallback kept if the child can't spawn. This is THE root fix for the stick-then-jump on the CPU renderer.
- **TICKET-136 — highlight/belt STILL jumping after the subprocess fix → continuity made structural** 🟢 v1.1.34. User (scroll-belt view): "still jumping and skipping… FIX THE HIGHLIGHT SKIPPING ONCE AND FOR ALL." Subprocess-recognize (135) removed one stall but did not fix it, so the GIL-stall theory was incomplete. From the live `karaoke.log` + code: the belt's per-frame scroll is `v·(pos − _last_pos)`, so any single-frame jump in the timebase lurches the whole belt 2-3 lines. THREE structural causes, all fixed: **(a)** `_hi_pos` HARD-SET `_hi_clock = raw_pos` on every Tk-thread stall (>0.4s) and a non-PLAYING blip let the clock fall behind (logged Δ-7.77s) then SNAP forward — both are jumps. Rewrote as a TWO-LAYER clock: a ground-truth estimate that CREDITS real elapsed time across a stall (never loses time, never hard-jumps mid-song) + STICKY playback (a <0.6s non-PLAYING blip no longer freezes it) + a §3 OUTPUT SLEW LIMIT (`_hi_out`) that the belt/index/fill read — it chases the true clock at ≤`nominal_dt·(1+catchup)` per frame, so the visible position physically CANNOT lurch (zero steady-state lag, ~200ms smooth catch-up after a stall, clean cut only on a real seek/track change). Proven across stall/blip/seek/pause in an offline sim (worst non-seek frame delta ≤ cap in every case). **(b)** The scroll belt's MOTION + block placement were driven by the RAW choppy SMTC clock (`pos = state.position + eased_offset`), only the FILL used the smooth clock — so the belt lurched even with a perfect clock. Both belts (`_ticker_update` / `_ticker_update_v`) now ride `pos_hi` for motion AND fill. **(c)** Belt DISCONTINUITY GUARD: `_last_pos` is never reset on a track change, so the first belt frame of a new song computed `v·(0 − old_pos)`; the belt now reseeds (no move) when `|Δpos| > 0.5s`, immune to ANY pos_hi discontinuity. GPU stays OFF on local per the user. Verified by an adversarial audit. May also help TICKET-132's "fill not advancing" facet (motion + fill now share one clock).
- **TICKET-137 — refresh curated lyric-search data + fix substring false-positives** 🟢 v1.1.34. User: "DO WE HAVE A MODEL THAT DRIVES LYRIC SEARCH AN LLM? IMPROVE IT WITH NEWEST DATA" → chose "refresh curated data only" (no LLM; search is heuristic + Shazam + faster-whisper ASR). (1) Expanded `confidence._KNOWN_JA` with the current 2024-26 JP/V roster — hololive DEV_IS FLOW GLOW members, AZKi/Watame/Koyori/Kazama Iroha, VSPO, and mainstream acts (YOASOBI, Aimer, Yorushika, Zutomayo, Vaundy, Kenshi Yonezu, Mafumafu) — DISTINCTIVE tokens only. (2) `_ALWAYS_JA` += the unambiguous mainstream acts (full-JA certainty). (3) `fetch_lyrics._AGENCY_PREFIXES` += hololive Justice / English -Justice-, VSPO EN, ぶいすぽ. (4) **FP FIX:** `confidence.language_confidence` matched acts as a PLAIN SUBSTRING, so `rim`/`kaf`/`kobo` (理芽/KAF/Kobo) wrongly tagged **Grimes / Kafka / Kobold** as Japanese (→ wrong-language fetch). Now word-boundaried via a compiled regex, IDENTICAL to `fetch_lyrics._JP_VAGENCY_RE` (the two finally agree — the code comments had lamented they "drifted out of sync"). Verified: all new + existing acts score JA-lead; Grimes/Kafka/Kobold/Shadow/BTS/BLACKPINK now clean en=1.00; IU→ko, Jay Chou→zh.

---

## v1.1.48 — 2026-06-30 session (TICKET-167)
- **TICKET-167 — diagnose and soften sync/audio micro-stutters** 🟢 v1.1.48. User: "its still doing this pausing and going forward and backward a little... slight distortion in the music and audio for just a few MS." Live `/diag` showed frame spikes while `identifying=true` plus tiny Shazam follow corrections such as `-1.10s -> -0.99s` from ~0.18s drift. Fixes: recognition child now launches below-normal priority so sound-ID is less likely to compete with browser/audio playback; `/diag.smoothness` reports bad/critical frame counts, active work (`identifying`/`aligning`/`deciding`), likely causes, jank backoff, target vs displayed sync offset, ease delta, pending offset, and pending Shazam correction; automatic recalibration backs off briefly after analysis-correlated jank; Shazam micro-corrections under `sync_apply_min_s` (default 0.22s) are ignored instead of making the belt wobble; live-follow corrections are low-pass filtered with `sync_live_follow_alpha`; medium corrections commit to the target but continue to glide visually, with only seek-sized jumps snapping via `ease_snap_jump_s`.

---

## v1.1.48 — 2026-06-30 session (TICKET-165 → 166; GPU overlay parity)
- **TICKET-166 — "GPU overlay" tray button is now a clean GPU↔CPU toggle** 🟢 v1.1.48. User: "MAKE SURE THE GPU RENDER BUTTON ACTUALLY TOGGLES BETWEEN GPU AND CPU RENDER (TAURI AND CPU)." The button + its checkmark keyed off raw child-process liveness (`_tauri_active()`), which is muddy (a child can be alive-but-not-yet-confirmed, or briefly alive after toggle-off). Now both key off the user's INTENT (`tauri_overlay_on`): clicking flips intent → ON starts the Tauri overlay and the watchdog hides Tk only once it's CONFIRMED rendering; OFF stops the child and `deiconify()`s the Tk (CPU) overlay immediately (the "· GPU" indicator disappears, confirming the switch). Added a single-arm guard (`_tauri_wd_armed`) so startup + repeated toggles can't stack multiple watchdog loops. The watchdog still resets `tauri_overlay_on=False` on a child crash, so the checkmark stays honest. Never-blank guarantee from [[TICKET-163]] preserved.
- **TICKET-165 — GPU overlay made VISUALLY INDISTINGUISHABLE from the CPU overlay** 🟢 v1.1.48. User: "MAKE SURE THE LYRICS LOOK MORE LIKE THE CPU LYRICS AS IN WHITE UNTIL HIGHLIGHTED AND … FOLLOW ALL SETTINGS LIKE SCROLL THROUGH, POSITION, ETC. I WANT THEM INDISTINGUISHABLE." The Tauri overlay had hardcoded gold-ish colors, a single bottom-center line, a CSS gradient wipe, and honored NO settings. Mapped the CPU (Tk) renderer exactly and rebuilt the overlay to match: **(a) exact palette** — base #f8fafc (white) sweeping per-CHARACTER to #fcd34d (amber), JP+romaji+English all sweep in lockstep by `n=round(frac×chars)` (CPU `_highlight_block`/`_karaoke`), furigana fixed #7dd3fc and never swept; exact fonts (Yu Gothic UI 38 bold / Segoe UI 23·21) + 2px black outline. **(b) full settings parity** via a new `/overlay` `style` block (scroll, scroll_speed, font_scale×auto_scale, opacity, pos_x, pos_y, screen_w, work_h) + a time-windowed `window` list of nearby lines for the belt: position (top/center/bottom × left/center/right with the CPU's win_margin 28 / pad 64 / bottom_clear margins), font scale, opacity, scroll-in entrance (none/left/right/top/bottom — 320ms cubic ease-out, ±460 / ±0.9·blockH offsets, CPU `_animate_in`), and scroll-through belt (lr/rl/tb/bt — each line at `center + dir·speed·(mid−pos)`, CPU `_ticker_update(_v)`). **(c)** the overlay window now covers the whole monitor (Rust `set_size`/`set_position` to current monitor) and lays content out inside the work area (taskbar excluded via `screen.avail*`), the same full-work-area canvas the CPU overlay uses — so any position/belt works. Verified pixel-exact in a browser harness (line mode: 8 amber + 6 white at frac 0.55, furigana rgb(125,211,252), 38px, bottom gap = bottom_clear 76; belt: rl geometry lands current line at screen-center, past exiting left, future entering right). CPU fallback ([[TICKET-163]]) unchanged — Tk still owns the screen until the GPU overlay proves it renders.

---

## v1.1.47 — DisplayLink grace period + position-staleness free-running
- **DisplayLink display-lost grace + position-staleness free-running** 🟢 v1.1.47 (commit `2b65b2d`, landed from another machine; v1.1.48 rebased on top). `_resolve_monitor` waits a grace period (`display_lost_grace_s`, default 10s) before falling back to primary when the saved monitor fingerprint vanishes — so a DisplayLink USB monitor blinking off during sleep/wake, a resolution change, or a GPU-driver reload no longer slams the overlay onto the primary display; `_apply_display` honors a `None` ("don't move") return, the geometry watchdog stops re-asserting placement during the grace window, and pre-v1.1.46 settings without a `display_fp` auto-migrate one. Separately, when GSMTC stops updating `state["position"]` while status is PLAYING (background-tab media-session throttling / missed SMTC callbacks), the highlight clock free-runs from the last known position using wall-time (`pos_stale_thresh_s`, default 1.5s) so the lyrics don't freeze.

---

## v1.1.46 — 2026-06-30 session (TICKET-163 → 164)
- **TICKET-163 — GPU overlay "no lyrics" → GUARANTEED CPU fallback via a CONFIRMED-rendering gate** 🟢 v1.1.46. Hiding Tk the instant the GPU overlay is toggled on meant any non-rendering overlay (blank window, slow WebView init, frozen JS) = blank screen. FIX: the Tk (CPU) overlay now stays up until the GPU overlay is PROVEN rendering, and RETURNS the moment it stops. The watchdog (1.5s, Tk-thread) confirms three things every tick — process alive AND a **visible 'Lyric Overlay' top-level window** (`_overlay_window_visible()` via ctypes EnumWindows, the ground-truth check the process/heartbeat proxies miss) AND a fresh `/overlay` heartbeat — and only then withdraws Tk (`_gpu_rendering=True`); if any fails it `deiconify()`s Tk. The `_tick` fast-path now keys off `_gpu_rendering` (confirmed), not `tauri_overlay_on` (wanted), so Tk keeps drawing until the hand-off. Net: the user ALWAYS sees lyrics — GPU overlay when it works, CPU overlay otherwise — never blank. (Builds on the v1.1.44.1 GUI-subsystem fix.)
- **TICKET-164 — keep the repo generic about hardware + translation context confirmed** 🟢 v1.1.46. Per the user, genericized 31 specific-GPU references across docs + code COMMENTS (functional GPU selection still uses `cuda:N` indices, untouched). Local-path/identity sanitize re-confirmed clean. Translation context: `_translate_lines` already sends each line inside a 24-line block with ±2 overlap, so every line is translated with ample surrounding context (more than "2 prior + 2 after") — no change needed. NB the user asked: **don't cut GitHub releases unless asked or verified stable** ([[feedback-no-auto-release]]) — committed + pushed, NOT released.

---

## v1.1.45 — 2026-06-29 session (TICKET-160 → 162)
- **TICKET-160 — Chinese songs translated to garbage/original (the REAL "no English" cause)** 🟢 v1.1.45. The free Google endpoint with `source="auto"` SILENTLY FAILS on (Traditional) Chinese — it returns the INPUT UNCHANGED, which the code then stored as the "translation". Proven on the live cache: `karma_code.json` had **53/59 lines with en == the original Chinese**. (`source='zh-CN'` translates the exact same lines correctly — e.g. a common four-character proverb line came back as real English instead of an echo; lyric quote redacted.) FIX: `_make_translator(source_lang)` maps the SONG language to an EXPLICIT translator source (`_SRC_LANG`: zh/yue→`zh-CN`, ja→`ja`, ko→`ko`, ru→`ru`; else `auto`) for both Google and DeepL; `_translate_lines` passes `song_lang`. DEFENSE-IN-DEPTH: `_translate_window` + the single-line fallback now REJECT any result that just echoes the source (`en == original`), and `want()` treats an existing `en == original` as MISSING so the bad cached files **self-heal** on replay (verified: karma_code 53→0 echoes, all real English after). Covers zh, yue (jyutping songs), and any CJK that slipped through auto-detect.
- **TICKET-161 — GUARANTEED CPU fallback for the GPU overlay** 🟢 v1.1.45. Hiding the Tk overlay when the GPU overlay is on means a non-rendering GPU overlay = blank screen. The process-only watchdog missed "process alive but blank / no window / JS frozen". FIX: a HEARTBEAT — `get_overlay_state()` stamps `_overlay_ping_t` on every `/overlay` poll (the overlay polls ~4×/s only while its render loop runs). `_arm_tauri_watchdog` (2s, Tk-thread) restores the Tk (CPU) overlay if the child died OR the heartbeat is stale (>`overlay_heartbeat_stale_s`=6s). So the user ALWAYS ends with a working overlay, never blank. Pairs with the v1.1.44.1 GUI-subsystem fix (the overlay debug build was console-subsystem → `CREATE_NO_WINDOW` suppressed its WebView window → the engine-launched overlay had no window; `#![windows_subsystem="windows"]` unconditional fixed it, also killing the stray console/"terminal").
- **TICKET-162 — keep the HUD + docs generic about hardware** 🟢 v1.1.45. Per the user, the overlay HUD no longer prints a GPU model — it classifies `hardware`/`software` + the graphics API (e.g. "renderer: hardware · D3D11") only. Recent docs/release notes genericized (no specific GPU names). HUD stays OFF by default (`#fps`).

---

## v1.1.44 — 2026-06-29 session (TICKET-159)
- **TICKET-159 — GPU overlay = the renderer (hide Tk + indicator + HUD off), not a second overlay** 🟢 v1.1.44. User saw BOTH overlays at once ("both cpu and gpu are running and cpu are doing the lyrics"), the tray toggle "did nothing", and the debug HUD was ugly. Causes + fixes: (1) the Tauri overlay was ADDITIVE (never hid Tk) so two overlays drew the same lyrics — now `_apply_tauri_overlay_toggle(True)` **withdraws the Tk window** (and the `_tick` fast-path skips ALL Tk canvas work when `tauri_overlay_on`, computing only the active-line index to feed `/overlay`), so exactly ONE overlay renders and the CPU isn't drawing hidden lyrics; toggling off `deiconify()`s Tk. A 2s **watchdog** (`_arm_tauri_watchdog`, Tk-thread) restores Tk if the overlay child dies, so a crash/close can't leave a blank screen. Startup hook + the running engine now auto-launch the overlay engine-MANAGED (was being launched out-of-band, which is why the tray checkmark/toggle looked dead). (2) The on-screen HUD (fps/worst/GPU/D3D11) is now **OFF by default** (open the overlay URL with `#fps` to show it). (3) Restored the **"♪ <song> · GPU" indicator** in the lower-middle (`#meta`) as the persistent "GPU render is on" sign. (4) Confirmed via the HUD's WebGL probe the overlay IS hardware-accelerated (Direct3D11, 60 fps, ~17ms worst). NB: WebView2 picks the system's high-performance GPU; pinning WebView2 to a specific GPU isn't cleanly controllable (shared `msedgewebview2.exe` GPU process) — the reliable route is Windows Settings → Graphics → add lyric-overlay.exe → choose the GPU. Also hid a stray WindowsTerminal that was hosting the overlay exe.

---

## v1.1.43 — 2026-06-29 session (TICKET-156 → 158)
- **TICKET-156 — Cantonese songs get JYUTPING, not Mandarin pinyin (TICKET-096 done)** 🟢 v1.1.43. User: "if the lyric is sung in cantonese we get jyutping and not pinyin which is only for mandarin." Cantonese + Mandarin share Han characters, so `detect_lang` (script-only) tagged ALL Chinese as `zh` → `_zh_pinyin` (pinyin) for everything. FIX: added `ToJyutping` (pure-Python, ~484 KB, own trie, bundled in the spec + requirements) + `_zh_jyutping()` (falls back to pinyin if the lib is absent). New `_is_cantonese(body, artist, title)`: positive Cantonese evidence only — ≥2 distinct Cantonese-only colloquial markers (嘅唔喺咗佢冇睇嗰乜嘢啲哋嚟攞諗咁㗎… ), an explicit 粵語/Cantonese tag, or a Cantopop-artist allow-list (Beyond/Eason Chan/MIRROR/…). `annotate()` + `backfill_file()` decide the romanizer ONCE per song (jyutping if Cantonese, else pinyin) and `romanize(text,"yue")` returns jyutping. **meta.lang stays "zh"** (NOT "yue") so no downstream `=="zh"` check breaks — the dialect choice is local to romanization. Verified on real lines (quotes redacted): a marker-heavy Cantonese line (我哋/唔/嗰啲 forms) → correct jyutping; a plain Mandarin line → pinyin. Per-song dominant detection (mixed Canto/Mandarin songs take the dominant dialect); a `/tune` per-line toggle is a possible follow-up.
- **TICKET-157 — guarantee non-English → English (Chinese songs showed romaji but no translation)** 🟢 v1.1.43. User: "we aren't getting English translation for Chinese songs… ALL non-English must get translated." Root cause (found by scanning the live cache: 4/19 cached Chinese songs had `en=0` but `rm` filled): the free Google endpoint intermittently rate-limits/times out, and a SINGLE failure left those lines with NO English forever — the LOCAL romanizer (pinyin/jyutping) always succeeds, so the gap looked Chinese-specific. The pipeline itself was fine (zh is in `_LANGS`; `annotate`/`_translate_lines` translate it — verified end-to-end). FIX: `_translate_window` now RETRIES up to 3× with backoff (0.6/1.2 s) before giving up, so a transient throttle no longer permanently strands a window. Re-running translation on a stranded file fills it (confirmed: 孤勇者 en 0→44). (Setting `DEEPL_API_KEY` avoids the free-endpoint throttle entirely — better JP/CJK→EN too.)
- **TICKET-158 — audio dropouts/"clipping" while the app runs (CPU starvation, not distortion)** 🟢 v1.1.43. User: "make sure audio isn't clipping while using our app." Investigated: the app NEVER outputs or modifies audio — it only CAPTURES via WASAPI loopback in **shared mode** (never exclusive) + reads peak meters, so it CANNOT distort samples or seize playback. The only real risk is CPU scheduling starvation: the default `cpu_dedicate_last_core=1` pins the WHOLE process to ONE physical core at ABOVE_NORMAL, and in-process Whisper ran `cpu_threads=4` on that one core — a transcribe burst could saturate it and starve the user's player's audio render thread → dropouts. FIX: `align._affinity_cpu_count()` reads the process's actual affinity width; Whisper `cpu_threads` is now `1` when pinned to a single core (≤2 logical CPUs), else `min(4, n-1)`. So a transcribe can't saturate the shared core under the default policy; the legacy spread policy still gets up to 4. (Further hardening — moving Whisper/deep_transcribe to a wider-affinity child, render-thread-only ABOVE_NORMAL — noted for a later pass.)

---

## v1.1.42 — 2026-06-29 session (TICKET-154 → 155)
- **TICKET-154 — REGRESS the highlight clock to the Jun-26 build (the "perfect" one in the test video)** 🟢 v1.1.42. User: "AND NOW WE HAVE NO HIGHLIGHTS. OR THEY ARE REALLY SLOW AND SNAP… REGRESS THE LYRIC HIGHLIGHT LOGIC TO WHAT IT WAS HERE [youtu.be/Bwp8NBEXjso] IT WAS PERFECT HERE." Git archaeology: that "Lyric Immersion Test" video uploaded **2026-06-26 22:11:47 UTC (15:11 local), build ~v1.0.74**, which predates the entire `_hi_pos` two-layer clock (first shipped v1.1.23, 2026-06-28). On Jun-26 the active-line index + karaoke fill + scroll belt rode ONE simple timebase: `pos = state["position"] + _eased_offset() + lead`. My v1.1.23→1.1.41 rework (TICKET-136) replaced it with the slew-limited `_hi_pos`, whose §3 OUTPUT SLEW LIMIT caps forward motion to ≈`nominal_dt·(1+catchup)` per frame — so after any internal clock step the visible fill CRAWLS then JUMPS to catch up ("really slow and snap"), and a stuck/zeroed `_hi_out` shows "no highlights". FIX: in `_tick`, `pos_hi = pos` (was `self._hi_pos(state, _lead)`). Every visible consumer is back on the Jun-26 eased clock; sync still corrects the lyrics via boundary-deferred `self.offset` glided in by `_eased_offset` ("only the lyrics follow sync; the highlight just steadily fills"). `_hi_pos` is retained as dead code for reference. Also set **`display_lead_s` default 0.12→0.0** for true Jun-26 parity (the lead only ever masked the slew-clock lag, now retired — bump to ~0.1 if a residual systematic lag reappears). Verified by an adversarial review: `pos` is always defined before every consumer, the belt's own discontinuity guard (`_ticker_update`/`_v` reseed on |Δpos|>0.5s) absorbs the track-change snap so the belt can't lurch, index+fill are recoherent on one clock, and `_eased_offset` is rate-capped so the boundary-deferred offset can't inject a step. Cleaned up the stale "slew-limited clock" comments, the dead `_hi_clock/_hi_out/_hi_offset/_hi_corr` telemetry (now reports live `pos_hi` + `clock_model: eased-pos` in /syncdiag and the hi-skip log), and the `_karaoke` docstring.
- **TICKET-155 — make the tray "GPU renderer" slot a working Tauri-overlay toggle** 🟢 v1.1.42. User: "ALLOW TOGGLE OF TAURI OVERLAY WITH THAT OLD OPTION U GOT THERE IN MENU." Replaced the disabled "GPU renderer: retired" item with an enabled, checkmarked **"GPU overlay (Tauri · smooth, click-through)"** that launches/kills the standalone `lyric-overlay.exe` child. New methods `_tauri_overlay_cmd` (resolve exe: tune override → frozen-bundled `overlay\lyric-overlay.exe` → dev `target\release` → dev `target\debug`), `_start_tauri_overlay` (windowless spawn via `creationflags=_NO_WINDOW` + DEVNULL stdio; the overlay window is transparent/click-through/`focus:false`/`skipTaskbar` so it never steals focus from a fullscreen game), `_stop_tauri_overlay` (terminate→wait→kill), `_tauri_active` (poll + self-heal handle so the checkmark stays honest if the user closes the overlay), `_apply_tauri_overlay_toggle`. It is **ADDITIVE** — never hides the Tk overlay, never routes through `_gpu_active()` (which keys off the mothballed `_gpu_child`); it is fed purely over HTTP by `/overlay` on `127.0.0.1:8765`. The flag `tauri_overlay_on` persists and the overlay relaunches on startup if left on. **Adversarial review caught a real bug, fixed before release:** `_persist` wrote settings key `"tauri_overlay"` but `__init__` read `"tauri_overlay_on"` → the choice never survived a restart and the startup relaunch never fired; keys unified to `tauri_overlay_on`. Also reconciled the startup hook (`ov.tauri_overlay_on = ov._start_tauri_overlay()`) so a missing exe clears the flag instead of re-persisting "on", and removed the now-orphaned `_toggle_gpu_render` callback. The mothballed in-process pygame/moderngl child (TICKET-153) stays retired.

---

## v1.1.41 — 2026-06-29 session (TICKET-153)
- **TICKET-153 — MOTHBALL the Python pygame/moderngl GPU renderer; GPU path = the Tauri overlay** 🟢 v1.1.41. User toggled "GPU renderer (smooth · draws on the idle GPU)" ON and got a BLANK screen; the log confirmed the cause — `gpu_renderer: child PID=… started, Tk overlay hidden` then nothing drawn (the color-keyed GL child spawned + hid the Tk overlay but rendered no content). Toggling it OFF instantly restored the working Tk CPU renderer. Per the user ("MOTHBALL THE PYTHON GPU IMPLEMENTATION WE ARE USING TAURI"): retired the in-process GPU child. `_start_gpu_renderer()` is now a no-op stub (never spawns the child, never `root.withdraw()`s the Tk overlay) — every call site (tray toggle, startup, /tune flip) falls back to the working Tk CPU renderer via the existing `_apply_gpu_renderer_toggle` "spawn failed → gpu_renderer_on=False" path. Tray item disabled + relabelled "GPU renderer: retired → use the Tauri overlay". The new GPU path is the **Tauri overlay** at `<lyric-overlay-tauri>` (TICKET-147 PoC): built it for real (`cargo build`, tauri 2.11.3, 3m03s, `target\debug\lyric-overlay.exe` 12.5 MB), launched it (PID running, transparent click-through, fed by `/overlay`) — true per-pixel alpha + native `<ruby>` + a steady LOCAL fill animation, far better than the color-keyed Tk/pygame paths. `gpu_renderer.py` + the old spawn code remain in git history for reference, not deleted.

---

## v1.1.40 — 2026-06-29 session (TICKET-151)
- **TICKET-151 — suppress the Tk_GetPixmap/CreateDIBSection error DIALOG (no popped window while gaming)** 🟢 v1.1.40. User hit a "Tk_GetPixmap: Error from CreateDIBSection — Not enough memory resources" MessageBox. DIAGNOSED live: NOT system memory (8 GB RAM free, 9.5 GB commit free, page file Windows-managed, total GDI across all procs = 3784) and NOT a steady leak (the PrintWindow capture in `ocr_lyrics.capture_source_window` already DeleteObject/DeleteDC/ReleaseDC in a finally; OCR is single-flight + once-per-track + downscaled to 240px). It was a TRANSIENT per-process GDI bitmap-heap spike on a PRIOR instance during a brutal stretch (a 43.5-min concert in live mode + repeated OCR full-frame ~8 MB DIB captures + Whisper generation), and Tk's internal `Tk_GetPixmap` asked for a bitmap at the instant the heap was full. The current instance was healthy (GDI=30). Tk's DEFAULT behavior is to pop a focus-stealing MessageBox for such a background draw error — terrible while the user games fullscreen. FIX: at Tk root init, route BOTH `report_callback_exception` (Python callbacks) AND a Tcl `bgerror` override (the path Tk_GetPixmap surfaces on) to the LOG and drop the frame; the self-rescheduling render loop recovers next tick. Never pop a dialog. (Note: the python.exe "orphans" seen during diagnosis were the user's own unrelated background services — not karaoke leaks; left untouched.) See TICKET-152 for shrinking the peak itself.
- **TICKET-152 — shrink the OCR-capture GDI peak (so the heap never gets close to full)** 🔵 backlog (NOT blocking — v1.1.40 already made the failure non-disruptive). `ocr_lyrics.capture_source_window` allocates a FULL-resolution DIB (`CreateCompatibleBitmap(hdc, w, h)` at ~1920×1080 ≈ 8 MB) and only downscales to 240 px AFTER (`_OCR_DOWNSCALE_H`, the resize at ocr_lyrics.py ~326). Under a long concert in live mode the repeated full-frame captures are the dominant contributor to the transient per-process GDI bitmap-heap peak that caused the v1.1.40 `Tk_GetPixmap`/CreateDIBSection failure. PLAN: (a) capture only the needed BAND/region instead of the whole window (PrintWindow can't sub-rect, but BitBlt the band from the window DC into a smaller compatible bitmap, or capture then immediately crop+free), and/or (b) allocate the DIB at a reduced scale up front (DPI-aware StretchBlt into a ½–¼ size bitmap before the buffer copy), and (c) hard-cap to ONE in-flight full-frame capture AND throttle harvests to ≤1 / N seconds during `_live_mode` concerts (the existing single-flight + once-per-track guards don't bound a 43-min multi-song concert's cumulative cadence). Goal: cut the per-capture bitmap from ~8 MB to ~1–2 MB so the heap never approaches full even under the worst concert load. Verify via the GetGuiResources GDI count staying flat across a long concert.

---

## v1.1.39 — 2026-06-29 session (TICKET-148 → 150)
Live testing: a 歌ってみた COVER ("可能世界のロンド covered by ヰ世界情緒") had "ABSOLUTELY FUCKED" highlights; an Original MV ("共鳴 / V.W.P #5") "starts in sync then gets late"; force-sync "couldn't fix it". Designed from a four-part review plus a cross-check. ROOT for covers: a re-sung cover NEVER matches the ORIGINAL's studio LRC timing (which the app fetches title-first), and energy-align/Shazam then lock a confidently-wrong offset against that mismatched grid; the video's OWN captions (this performance's real timing) were fetched 4s LATE so the bad LRC drove sync first.
- **TICKET-148 — /syncdiag diagnostics endpoint + sync-event ring buffer** 🟢 v1.1.39. New bounded `self._sync_events` ring + `_sync_event(kind, **fields)` helper (appends only at real events, never per-frame → cheap), wired at: hi-snap, idx-skip, drift-read, offset-defer, mode-change, energy-align-skip, drift-monotonic. `get_sync_diag()` + GET `/syncdiag` return the ring + a live snapshot (hi_clock/hi_offset/hi_out/hi_corr/offset/pending/last_drift/drift_integral, current line bounds, is_cover/live/verified/force_sync flags, source, position, frame_worst, offset_hist). So a future "fucked highlights" is one `curl 127.0.0.1:8765/syncdiag` away from a full diagnosis. Knobs: sync_event_enabled=1, sync_event_buffer_size=200.
- **TICKET-149 — eager closed-captions for covers + skip energy-align on covers** 🟢 v1.1.39. (a) For `_is_cover` OR `_live_arrangement`, schedule the video's caption fetch at **250ms instead of 4000ms** so THIS performance's CC (real lyrics+timing) beats the mismatched original LRC. (b) `_maybe_auto_align` now SKIPS the energy correlator for covers (it correlates the cover's vocals against the ORIGINAL's grid → locks a wrong offset = the "in sync then way off" cover failure) and kicks a caption fetch instead. Studio originals unchanged (4s CC, energy-align as before). NOTE (cross-check): covers already had `_live_mode=False`, so the caption GATE was never the blocker — the 4s DELAY was; do not "unblock" 3171/3192.
- **TICKET-150 — studio-only monotonic drift-recovery** 🟢 v1.1.39. For an Original MV that "starts in sync then gets LATE" (共鳴), track `sign(drift)` across Shazam reads: ≥`drift_monotonic_reads_n`=3 consecutive same-sign reads outside the deadband (STUDIO only — not cover/live/force-sync) sets `_drift_monotonic_since`, which drops the energy-align cooldown from 14s → `drift_recovery_cooldown`=5s so a steady creep re-locks ~3× faster. Cleared on a sign flip back into the deadband and on track change. Kept STRICTLY boundary-deferred (energy-align still routes through `_smooth_offset`) — NO direct offset writes, NO continuous per-frame pull, NO global lift-floor drop — so the v1.1.36 steady fill is preserved (the cross-check's hard constraint). FORCE-SYNC aggressiveness (chorus-grace, multi-signal fusion, faster poll) deferred to a next pass to be validated against the new /syncdiag data first.
- **TICKET-147 — `/overlay` endpoint for an external render client (Tauri PoC)** 🟢 v1.1.38. New GET `/overlay` (auth-exempt on localhost, CORS `*`) returns `get_overlay_state()`: the CURRENT line (furigana `漢字(かな)` / romaji / translation), its song-time `start`/`end`, the display `position`, `idx`, `playing`, title/artist/source. Lets an external overlay render the line and run a STEADY **local** fill animation (frac = (pos−start)/(end−start), interpolated client-side) — only a LINE CHANGE re-anchors it, realizing "only the lyrics follow sync; the highlight just proceeds" in the renderer itself. Companion project: `<lyric-overlay-tauri>` (Tauri 2 transparent click-through overlay: per-pixel alpha, native `<ruby>` furigana, local CSS gold fill). The Python engine stays the brain; the overlay is just a GPU-composited render client over `127.0.0.1:8765`.

---

## v1.1.36 — 2026-06-29 session (TICKET-142 → 145)
Live testing of v1.1.35: (1) Tokyo Friday Night studio — "perfectly synced → falsely synced ~1s early → lost lyrics completely → got them back"; (2) the highlight STILL jumps instead of steadily filling, esp. on concerts — "only the lyrics themselves should be affected by sync, highlights should just proceed". Designed from a four-part audit plus a cross-check that REJECTED the naive "wall-clock fill anchor" (it would discard pos_hi's seek/pause handling) and correctly reframed the fix as: stop pos_hi being PERTURBED mid-line.
- **TICKET-142 — false decision SWITCH on feat./variant titles wiped the correct lyrics** 🟢 v1.1.36. `_score_source_agree` returned DEGRADED whenever one title was a substring of the other, so "Tokyo Friday Night" (loaded) vs heard "Tokyo Friday Night (feat. Kana Hanazawa & Mori Calliope)" scored DEGRADED **despite the artist matching** → TRUST→CAUTION→SWITCH → blacklisted the correct lyrics ("lost lyrics then got them back"). FIX: new shared `_strip_title_credits` peels trailing (feat.)/(Live)/[Remix]/（…）/【…】 tags; a bare-title match WITH artist corroboration (equal or substring either way) → OK; title overlap returns OK iff the artist agrees, else DEGRADED. Also: `source=="youtube-captions"` → OK (the video's own lyrics are ground truth, never a source mismatch).
- **TICKET-143 — concert/live gold fill jitters because the offset glides every frame** 🟢 v1.1.36. In live/concert mode `_hi_pos` GLIDED `_hi_offset` toward the target every frame (`hi_live_pull_per_sec=4.0`); even with the §3 slew cap, that continuously moved the fill mid-line — the user's "highlights jumping" on concerts. FIX: live mode now SNAPS `_hi_offset = target`. Since `target == self.offset` is boundary-deferred (`_smooth_offset` commits at line ends), the offset only changes at boundaries, so the fill stays STEADY mid-line and re-anchors only at a line change ("only the lyrics follow sync"). Following stays aggressive (a committed correction is taken in full at the boundary) and the §3 slew-limit smooths the hand-off. This is the architecturally-correct realization of the user's principle — NOT a parallel wall-clock fill clock (rejected: it would break seek/pause + the boundary hand-off; pos_hi already IS a steady offset-frozen slew-limited clock).
- **TICKET-144 — stranded sync correction could lurch the fill after 8s mid-line** 🟢 v1.1.36. The boundary-deferred offset commit had an 8s safety cap (commits regardless if a line never ends), which could fire MID-line on a later line and jolt the fill. Lowered to a tunable `offset_defer_cap_s=3.0`. A genuine seek / >5s jump / no-line still snaps immediately (unchanged).
- **TICKET-145 — `_cancel_pending_swap()` TypeError every junk track** 🟢 v1.1.36. main.py:2806 called `_cancel_pending_swap()` with no arg but the signature requires `reason` → "TypeError: missing 1 required positional argument" caught by the tick handler on every non-music page, silently aborting the pending-swap cancel. Passed `"non-music-page"`.
- **TICKET-146 — concert handling: single-song LIVE MV detection + caption fusion + fast lock** 🟢 v1.1.37. Shipped the concert batch.
  - **(a) Reclassification** — a SHORT single-song LIVE MV ("【LIVE MV】「言葉」from V.W.P 4th ONE-MAN LIVE「現象Ⅳ -反転運命-」") was wrongly classified by `is_live_or_compilation` as a multi-song event (`_live_mode`) → title IGNORED, driven sound-only, captions SKIPPED. New `_has_single_song_at_event()` (a 'from … LIVE/ONE-MAN/TOUR' reference WITH a quoted-「」/non-generic song head) makes it `_live_arrangement` instead. **Verified on the real functions:** VWP LIVE MV → live_or_comp=False / live_arr=True / `clean_title='言葉'`; the 18-min 理芽 concert STAYS multi-song (live_or_comp=True); the parenthetical-aside and bare-concert cases unchanged.
  - **(b) Song+artist** — `clean_title` already extracts the head 「」 song (→ 言葉); the channel supplies the artist. No extra parse needed once (a) stops the title being ignored.
  - **(c) Captions** — because the VWP case is now `_live_arrangement` (not `_live_mode`), `_maybe_fetch_captions` (gated on `_live_mode`) now RUNS for it → the video's own caption track (real lyrics + perfect timing) loads. Multi-song concerts stay sound-only (whole-concert CC would be wrong). Also: decision SWITCH is now SUPPRESSED on `source=="youtube-captions"` (a blacklist+re-fetch is futile — same video → same captions — and would drop the only ground-truth source); a genuinely-wrong CC can still be replaced by REGEN (generate-by-ear).
  - **(d) Fast lock** — on a VERIFIED + title-locked song a MODEST first-read offset (≤`fast_lock_max_s=6.0`) now commits immediately instead of waiting ~8s for two-point confirmation (the studio "found sync at ~1 min" complaint). A large first offset (chorus match / big MV intro) still uses two-point.
  - STILL OPEN: explicit 'from <ARTIST>' artist parse + a caption-quality promotion gate (timed vs ASR) — refinements, not blockers. VWP 「言葉」from "V.W.P 4th ONE-MAN LIVE「現象Ⅳ -反転運命-」": the title is the EVENT name (misleading) but contains song=言葉 + artist=VWP, and the video has CC with the real lyrics+timing. Plan: (a) a SHORT single-song LIVE MV (e.g. ONE-MAN LIVE, ~4min) is currently MISclassified by `is_live_or_compilation` as a multi-song event (`_live_mode`) → captions skipped (`_maybe_fetch_captions` early-returns on `_live_mode`) + slow sound-only — needs single-song-vs-multi-song reclassification (tricky: `dur None` on browser sources); (b) extract song from head 「」 + artist from "from <ARTIST>" / VWP; (c) ungate captions for single-song concerts, promote CC over LRC only when TIMED + quality-passing, and suppress decision SWITCH/REGEN on the captions source so the only ground truth can't be blacklisted; (d) faster first-read sync lock (verified song → commit first read immediately; official VWP studio "found sync at ~1 min"). Held back from v1.1.36 to avoid destabilizing the hard-won multi-song concert detection — shipping the highlight + false-switch core first.

---

## v1.1.35 — 2026-06-29 session (TICKET-138 → 141)
User: "BRING UP SUCCESS TO FAILURE RATE… LOOK UP HUMAN EAR EYE COORDINATION STUDIES… RECOMMEND CHANGES TO SUCCESS AND FAILURE TARGETS" → "do all 4". Telemetry from the live `karaoke.log` quantified the real failure surface (ID-match 9/13, confident title-hit ~4/15, fetches blowing the 8s cap at 24-54s, decision REGEN cascades); the perception literature set the sync targets.
- **TICKET-138 — perceptual sync retune (asymmetric in-sync window + smaller lead)** 🟢 v1.1.35. The old symmetric `deadband=0.8s` called anything within ~800ms "in sync" — 4-9× looser than a listener can perceive (ITU-R BT.1359: detectable at audio +45/−125ms, unacceptable past +90/−185ms; AV binding window ~30ms-lead/170ms-lag, asymmetric, and music is *less* sensitive than speech). New **asymmetric in-sync gate** at the sync-read site: tolerate the highlight running AHEAD of the vocal up to `sync_win_ahead_s=0.17` (the forgiving direction) but correct once it lags past `sync_win_behind_s=0.09` ("lyrics late" = the annoying direction). `display_lead_s` 0.3→0.12 (the 0.3 was masking the choppy-clock lag the v1.1.34 slew-limited clock removed; net on-screen = highlight ~0.05s behind … 0.21s ahead, inside the window). Structural `deadband` (0.8) kept for the live-follow / reset strategy decisions. All live-tunable.
- **TICKET-139 — `/status.success_rate` scorecard (measure the success:failure ratio)** 🟢 v1.1.35. None of the rates were counted — only reverse-engineered from the log. Added session counters (`self._stats` + module `_TITLE_STATS`) incremented at the existing emit sites: ID-match (heard vs loaded), title-hit/miss (cache title-matcher), by-ear fallback rate, sync-in-window adherence (vs the 138 window), REGEN/SWITCH, fetch P50/P95 + timeouts. `success_rate_snapshot()` folds them into `/status` against explicit targets (id-match 90%, title-hit 70%, by-ear ≤20%, sync-in-window 95%, fetch p95 ≤6s) plus an `llm` armed/model field. NOTE: `id_match` counts STRICT `_titles_match`, so feat./remix/parenthetical variants of the right song undercount (a "(feat. …)" title reads as a mismatch even when title-lock treats it as equivalent) — directional, not a wrong-song count.
- **TICKET-140 — hard-cancel hung lyric-fetch swaps (kill the 24-54s freeze + spam)** 🟢 v1.1.35. `_try_apply_swap` logged "swap: fetch still pending" EVERY tick (observed 1179×) while a correction/switch fetch hung 24-54s with the WRONG/stale body frozen on screen, and never gave up. Now: log once, and at `swap_fetch_hard_cap_s=30` (kept above the legit niche/VTuber 25-35s slow-fetch window) ABANDON the fetch; if it was a REGEN (force_ai_gen, current body deemed wrong) fall back to generate-by-ear so the screen isn't stuck on the wrong song. **Proven live** in the deploy log: `swap: fetch TIMED OUT after 30.0s (hard cap 30.0s) … → abandon`.
- **TICKET-141 — optional Anthropic LLM lyric disambiguation (gated on an API key)** 🟢 v1.1.35 (OFF by default — no key present). New `llm_disambiguate.py`: when an Anthropic key is resolvable (`ANTHROPIC_API_KEY` env, `<secrets-dir>\anthropic-api-key.txt`, or `<data>/anthropic-api-key.txt` — mirrors the DeepL gating), the configured model (`claude-sonnet-4-6` by default, override `LYRIC_LLM_MODEL`) decides which candidate the live vocals are, matching the Whisper transcription against candidate lyric bodies — far more robust than char-fuzzy on a short/noisy transcript, the lever for the wrong-song (31%) + title-miss (73%) failures. Called inside `_decide_by_ear`'s worker thread ONLY on the hard cases (loaded looks wrong / scores ambiguous / library-expanded); a high-confidence `matches_audio` verdict is AUTHORITATIVE in `_apply_decision` (same `_file_valid` safety as the score path), else rapidfuzz stands. Raw urllib (no SDK to bundle), cached per (transcription, candidate-set), pinned in the spec. No key → `available()` False → `pick_best_match` returns None → existing behaviour unchanged. NOTE: no key is bundled — set `ANTHROPIC_API_KEY` (or `ANTHROPIC_API_KEY_FILE`, or drop `anthropic-api-key.txt` next to the app) to arm it; a live round-trip still needs first-run verification.

---

## v1.1.x — Shipped (2026-06-28 session, TICKET-119 → 129)
Release train v1.0.97 → v1.1.7. All built non-lean (whisper bundled) + deployed.
- **TICKET-129 — "last core drives the product" CPU policy + multi-CPU/GPU compatibility** 🟢 Replaces TICKET-127's IDLE-while-gaming downgrade. Default = pin this process to the LAST PHYSICAL core (SMT-aware mask via `GetLogicalProcessorInformation`) and run it ABOVE_NORMAL: dedicating one core keeps the overlay smooth while a game (on the other cores) is barely touched. Hardware-agnostic helpers (`_last_physical_core_mask` / `_dedicate_last_core_mask` / `_upper_cores_mask` / `_apply_affinity_priority`) compute the mask from live topology, verified correct + off-core-0 on 1..64-thread machines (SMT or not). Single code path: `ov._apply_dynamic_priority()` is called once after Overlay build and idempotently re-asserted each monitor tick (a live `/tune cpu_dedicate_last_core 0` reverts to the legacy upper-cores/BELOW_NORMAL spread within ~3s). GPU side already degrades cleanly (`gpu_setup`: no-CUDA → CPU, NVML-missing → {}, single-GPU → CPU, AMD/Intel → CPU) so generation/OCR are GPU-agnostic.
- **TICKET-119 — ルフラン live-title** 🟢 `clean_title` was extracting the concert name from `(from … ONE-MAN LIVE「NEUROMANCE Ⅱ」)`; now keeps the head song + reclassifies as a live arrangement (not compilation). Also: THE FIRST TAKE → live arrangement; sync menu ±5s.
- **TICKET-120 — OCR burned-in lyrics** 🟢 `ocr_lyrics.py` (PrintWindow capture = self-read-safe, WinRT OCR EN+JP, metadata/CJK filter, timed `LyricOcrHarvester`). Wired BEFORE generation (no-lyrics path) AND on decision-engine SWITCH/REGEN (wrong-lyrics path, e.g. Play On! → Sixth Sense). source="ocr".
- **TICKET-121 — release telemetry** 🟢 `metrics.py` `ReleaseMetrics`; GET `/metrics`. success/wobbler/fail bucketed by version, persisted to `metrics.json`. Success=synced ≤60s real source (incl ocr) ≤2 resyncs; Fail=generated OR >10 resyncs; Concert=≥10 wrong-detections/5min window.
- **TICKET-122 — ground-truth barrier (2-tier)** 🟢 Bundled=unconditional immunity; captions/OCR=PROVISIONAL (immune only once `_body_corroborated` earned). Fixes feelingradation (bundled) regenerating on its instrumental outro (sync_stable=BAD). Also: アイドル English-translation-body rejection; jp_vagency (ReGLOSS/hololive/神椿 → reject ko/zh body); captions-escalation on SWITCH/REGEN; cover-speedup.
- **TICKET-123 — OCR-assisted sync** 🟢 When energy-correlation lift < floor (ambient MVs like みらいのかたち, 2-3s off), OCR the on-screen burned-in line, match to the LRC, set offset precisely. New API: GET `/sync`, POST `/resync` + `/ocrsync`.
- **TICKET-124 — NO bundled lyrics (sellable product)** 🟢 Removed `bundled_lyrics/` from the repo + build + `_seed_bundled_lyrics`. Every lyric is now FOUND BY CODE (providers / captions / OCR / by-ear). Copyrighted text never ships. (bundles backed up privately.)
- **TICKET-125 — OCR backs off for games** 🟢 OCR (screen capture + GPU-backed WinRT OCR) was hitching a borderless game sharing the high-perf GPU. `_ocr_gpu_safe()` skips OCR when a game is active or any GPU ≥45% util (override: `ocr_when_gaming`). Generation already targets the idle secondary GPU via `pick_inference_device`.
- **TICKET-127 — app yields to games + OCR-during-gaming revision** 🟢 `_apply_dynamic_priority()` (from `_check_monitors`, ~3s) drops the app to IDLE_PRIORITY_CLASS while a game is active (exclusive-fullscreen OR any GPU ≥45% util), restores BELOW_NORMAL otherwise. Revised TICKET-125: `_ocr_gpu_safe()` now ALLOWS OCR during gaming when ≥2 CUDA GPUs (rides the idle secondary GPU's headroom + yields via IDLE priority); only a single-GPU box backs off. (WinRT OCR can't be GPU-pinned in code — "idle GPU" = IDLE priority + spare-card headroom.)
- **TICKET-128 — OCR tofu/box-glyph strip** 🟢 OCR emitted a "tofu" box (□ / U+FFFD) where the burned-in frame had a decorative separator or wide space — overlay showed `S M T W T F S□Back to the beginning.` (isomers cover). `_strip_tofu()` in `ocr_lyrics.py` drops geometric-shapes / replacement / PUA / control code points + normalizes exotic spaces, run at the single `filter_lyric_lines` funnel before CJK-space collapse. Verified: `…S□Back…` → `…S Back…`.
- **OPEN / NEXT:** GPU display renderer (spike PASSED — transparency+click-through+109fps on high-perf GPU; M1 = standalone GL renderer process, not yet built); GPU for current-line highlighting (part of renderer); explicit GPU-override menu (force secondary GPU/high-perf GPU/CPU per task); feelingradation katakana↔English cross-script title matching on Spotify (TICKET-126, open).

---

## v1.0.96 — Shipped (2 tickets bundled: TICKET-117 + TICKET-118)
Closes the user's two-tab "Tab A muted / Tab B audible" scenario (Brave with TAB A = Cyberpunk 2077 POV motorbike video MUTED, TAB B = Rosa Walton "I Really Want to Stay at Your House" PLAYING). Before v1.0.96 both tabs published to SMTC and `MediaWatcher._pick` returned the most-recently-active session, so any browser focus swap ping-ponged the overlay between the two tracks. v1.0.96 fixes it with TWO complementary mechanisms: TICKET-117 = explicit tray "Source →" pin (persistent, absolute), TICKET-118 = Core Audio audible-session preference (automatic tiebreaker — the loudest process wins when no pin is set). See per-ticket sections below.

---

## v1.0.95 — Shipped (4 tickets bundled: TICKET-112 + TICKET-113 + TICKET-114 + TICKET-115)
Two parallel change sets landed in the same version bump: song-ID hardening and translation fixes. TICKET-112/113/114 close a long-standing wrong-song failure mode (ambiguous SMTC titles like "Shooting Star" matching the wrong "Shooting Star" forever, with `/wrong` unable to escape the same bad LRC); TICKET-115 closes the translation-whitelist drift. See per-ticket sections below.

---

## v1.0.95 — Shipped (six-language translation actually delivered + /retranslate, TICKET-115)
Closes TICKET-115. The README's "English translation for Japanese / Chinese / Korean / Spanish / German / Russian" claim was previously aspirational on the German + Russian side: live capture on Rammstein "Deutschland" (lang=de, 51 lines from lrclib/search) showed every `en` field empty. Root cause was a two-place language whitelist that had drifted between `fetch_lyrics._translate_lines` and `main._maybe_translate`, plus a per-line gate that only ran translation when CJK lines were present — so a fully German body never reached the translate worker.

**Fix in `fetch_lyrics.py`:** hoisted the language set into a module constant `_LANGS = ("ja", "ko", "zh", "es", "de", "ru", "fr", "pt", "it")` used at BOTH the per-line `detect_lang(raw) in _LANGS` gate AND the whole-song `song_lang in (*_LANGS, "ja-romaji")` gate inside `_translate_lines`, so the two can never drift apart again. `annotate()` comment updated: any Latin-script source (es / de / fr / it / pt / en) renders as-is with empty `rm` — no romaji needed.

**Fix in `main.py`:** mirrored the same set into `Overlay._maybe_translate`'s "whole" tuple — `("es", "de", "fr", "it", "pt", "ru", "ja-romaji")` — with an explicit "keep in sync with `_translate_lines._LANGS`" comment so a future edit doesn't re-introduce the drift. CJK songs still get only their CJK lines translated; non-English Latin/Cyrillic songs get every line.

**Bonus delivered alongside the six promised:** French / Italian / Portuguese now work too (the whitelist already classifies them via `detect_lang`; the bug was the missing wire-up).

**New endpoint `POST /retranslate`:** force a translation backfill of the currently loaded track without re-fetching lyrics. `api.py` marshals the request onto the Tk thread via a `threading.Event` + `_run` pattern so the HTTP response carries the worker's snapshot (`{ok, action, path, lang, n_lines, n_missing}`). Bounded 5 s wait so a frozen UI thread can never hang the API (returns 503 with `"UI thread did not respond within 5s"`). Backed by `Overlay.retranslate_loaded()` which clears any stuck `_translating` guard and routes through `_start_translate` → `backfill_file` (atomic LRC rewrite, main tick re-loads in place, playback position preserved).

**In-flight guard hardening:** the `_start_translate` worker's `_translating = None` release moved into a `try/finally` so an exception in `backfill_file` no longer poisons the guard and silently blocks every future `_maybe_translate` call for the same path. `/retranslate` also clears `_translating` before kicking off to guarantee a prior poisoned run can't block a manual retry.

**Files:** `fetch_lyrics.py` (`_translate_lines` _LANGS hoist + dual-gate; `annotate()` comment), `main.py` (`_maybe_translate` whole-set; `_start_translate` finally; new `retranslate_loaded` method), `api.py` (`/retranslate` route + `_ROUTES` help blurb).

**Verify:** load Rammstein "Deutschland" (or any German / French / Italian / Portuguese track); `/status.lines` should show `en` populated within seconds. Or on an already-loaded track with empty translations: `POST /retranslate` returns `{ok: true, n_lines: N, n_missing: N}` and the overlay shows English translations after `backfill_file` completes. Pre-existing CJK + Spanish + Russian behavior is unchanged (same gates, just live-tunable via the constant now).

---

## v1.0.93 — Shipped (boundary-deferred whole-lyrics swap, TICKET-111)
Closes TICKET-111. All five immediate-clear paths (decision-engine SWITCH at Site H, decision-engine REGEN at Site I, wrong-song-strike at Site D, user `/wrong` at Site G, and AI-gen `_begin_generation` at Site C) now queue the replacement on `self._pending_swap` instead of blanking `self.lines` on fire. The fetch (or AI-gen) is kicked off in parallel so latency overlaps with the remaining playback of the old lyrics. `_consume_async` routes the completed payload into `pending_swap["lines"]` when the fetch's captured swap_token still matches; `_apply_generated` does the same for REGEN via a `gen_token` check. The new `_tick_body` consumer (placed RIGHT AFTER the `_pending_offset` consumer to preserve TICKET-088 same-tick ordering) tracks an `_idx_minus_one_since` wall-clock timer and calls `_try_apply_swap`, which checks the per-mode boundary via `_swap_ready` (LINE-mode: current line ends or `≥swap_defer_instrumental_gap_s` on `idx==-1`; SCROLL-mode: belt drained or instrumental gap). `_apply_pending_swap` commits atomically: cancels in-flight LINE-mode slide-in, clears scroll belt, wipes canvas, swaps `self.meta`/`self.lines`/`self._lyrics_path` in one tick, drops the verified gate ONLY if this swap set it, invalidates PERF-102 block cache, and increments `_swap_commit_seq`. Safety cap (`swap_defer_max_s` default 8.0s) force-commits if the boundary never lands; `/wrong` uses a tighter cap (`swap_defer_user_max_s` default 3.0s). Track change calls `_cancel_pending_swap("track-change")`. Stale fetch tokens are dropped with a log line. Kill-switch via `swap_defer_enabled` (default 1), flipping to 0 via `/tune` restores v1.0.92 immediate-clear without a re-release.

**Four new tune knobs:** `swap_defer_enabled` (1), `swap_defer_max_s` (8.0), `swap_defer_instrumental_gap_s` (2.0), `swap_defer_user_max_s` (3.0). All live-tunable via `/tune`.

**Observability:** `/diag.pending_swap` returns `{queued, kind, source_site, artist, title, queued_age_s, fetch_ready, ready_for_swap, blocked_by, force_ai_gen, fetch_token, will_force_commit_in_s, set_gate, last_commit_seq}` so the operator can watch a swap traverse the state machine in real time. `api.py` `/diag` help blurb updated to mention pending-swap; the dict itself passes through automatically since `api.py:277-279` already forwards everything `app.get_diag()` returns.

**Smoke test:** baseline `/diag.pending_swap.queued == false`; play a known-wrong-LRC song until decision engine flips SWITCH (`/diag.decision_engine.state == "SWITCH"`); confirm `pending_swap.queued == true` and old lyrics keep rendering; when current line ends, `pending_swap.queued == false` and `last_commit_seq` increments. Kill-switch test: `POST /tune swap_defer_enabled=0`; queued swaps commit immediately with `reason == "disabled"`.

---

## v1.0.92 — Shipped (continuous decision engine, TICKET-109)
Closes TICKET-109. New background watcher `_decision_tick` (self-throttled to `decision_tick_interval_s`, default 2.0s) aggregates four signal dimensions (SMTC<->Shazam agreement, drift trend, lyric-quality flags, decide-by-ear corroboration) into a strike score over a rolling window of `decision_score_window` samples (default 12). State machine promotes TRUST -> CAUTION -> SWITCH -> REGEN at strike thresholds 3 / 5 / 8 (knobs `decision_caution_strikes`, `decision_switch_strikes`, `decision_regen_strikes`). `_fire_decision_action` executes SWITCH (re-fetch alternative source, cooldown `decision_action_cooldown_s` default 30.0s) or REGEN (force AI generation). Engine forgets prior song's strikes on track change via `_reset_decision_engine`. Tray hint surfaces state; `/diag.decision_engine` exposes state, strikes, last_action_age_s, dim scores for live observability.

**Knobs:** `decision_engine_on` (1), `decision_caution_strikes` (3), `decision_switch_strikes` (5), `decision_regen_strikes` (8), `decision_score_window` (12), `decision_tick_interval_s` (2.0), `decision_action_cooldown_s` (30.0).

**Known issue closed in v1.0.93:** the SWITCH/REGEN branches in `_fire_decision_action` cleared `self.lines = []` immediately on fire, producing a 1-5s on-screen blackout while the new lyrics arrived (TICKET-111 fixed by deferring the swap to a boundary).

---

## TICKET-118 — Audible-session preference (Core Audio peak meter → pick the audible SMTC session) 🟢 (v1.0.96)
**Symptom (user report, 2026-06-27):** *"i want to be able to watch a muted video while having the actual music video in a different tab providing lyrics and music without interference"*. Brave with TWO YouTube tabs both publishing to SMTC: TAB A = Cyberpunk 2077 POV hyper-realistic motorbike ride (MUTED, visual only), TAB B = cyberpunk edgerunners "I Really Want to Stay at Your House" by Rosa Walton (Hyper) (AUDIBLE, the actual music). `MediaWatcher._pick` returned the most-recently-active SMTC session, so as soon as the user clicked back into Tab A to watch the video, the overlay swapped onto Tab A's track and lost lyric sync with the music actually playing.

**Root cause:** SMTC has no concept of "which session is making sound right now". `playback_status` reports PLAYING for both tabs (they both technically play — Tab A is just muted at the browser level, not paused), and the OS-level "current session" cycles based on which tab was last interacted with. Without an external audibility signal, the picker is blind.

**Fix (v1.0.96):** new `audible_sessions.py` module + `MediaWatcher._pick` tiebreaker.
- `audible_sessions.get_process_audio_levels()` returns `{executable_basename_lower: max_peak_amplitude}` by:
  1. Lazy-importing `pycaw.pycaw.AudioUtilities` (sentinel `_AVAILABLE` flips False on first ImportError so non-Windows / missing-dep boxes never retry).
  2. `AudioUtilities.GetAllSessions()` → per-PID max of `IAudioMeterInformation.GetPeakValue()` (a Chromium PID hosts multiple audio streams; the loudest is what "this process is audible" means).
  3. Aggregating per-executable basename (lowercased, no `.exe`) so the caller can substring-match SMTC `source_app` AUMIDs like `app.brave.brave` against `brave`.
  4. **Hard 500 ms timeout** on a daemon worker thread — a hung audio endpoint (sleeping HDMI is a known cause) cannot stall the 0.15s SMTC poll loop.
  5. **1 s cache** — `GetAllSessions` has COM overhead; rate-limit to once per second.
- `MediaWatcher._pick` (when MULTIPLE sessions are eligible AND no explicit pin from TICKET-117 is set AND `prefer_audible_session=1`): call `_audible_peaks_for_sessions()` which maps `{session_id: peak}` by substring-matching the session's `source_app` against the audible-levels dict. Sessions with peak `>= prefer_audible_threshold` (default 0.02) sort first; ties fall through to the pre-118 sticky behavior. Result: Tab A (muted, peak ≈ 0.0) loses to Tab B (audible, peak > 0.02) deterministically.
- Sentinel-disabled path (`_AVAILABLE=False`) → empty dict → caller falls through to pre-118 sticky → zero regression on non-Windows / missing-pycaw boxes.

**New tune knobs:** `prefer_audible_session` (1 = on, 0 = pre-118 sticky-only), `prefer_audible_threshold` (0.02 — peaks below this are treated as "silent enough to ignore"). Both live-tunable via `/tune`; a `/tune` POST that flips either mirrors the new value into `MediaWatcher` immediately (`Overlay.set_audible_pref(enabled, threshold)`) so no restart is needed.

**New dep:** `pycaw>=20240210; sys_platform == "win32"` in `requirements.txt`. ~50 KB plus comtypes (already a transitive dep via pystray's COM stack). Frozen build pins in `DesktopKaraoke.spec`: hiddenimports `audible_sessions`, `pycaw`, `pycaw.pycaw`, `comtypes`, `comtypes.gen`; `collect_all` for `pycaw` + `comtypes` so the COM proxy stubs comtypes generates lazily are pulled into the bundle.

**Observability:** `/diag.audible_pref` returns `{enabled, threshold, levels, module: audible_sessions.diag()}` where `module.diag()` exposes `{available, last_error, cache_age_s, cache_ttl_s, timeout_s, levels}`. `/diag.sessions` (added by TICKET-117) lists every visible SMTC session so the operator can correlate `source_app` → audible-peak.

**Files:** `audible_sessions.py` (new, ~230 lines), `main.py` (`MediaWatcher.__init__` audible-pref state, `_audible_peaks_for_sessions`, `_pick` tiebreaker around line 593, `set_audible_pref` setter, `audible_diag`, `Overlay._tune` defaults `prefer_audible_session` / `prefer_audible_threshold`, `Overlay.__init__` mirror into watcher around line 2444, `_tune` POST mirror around line 6810, `/diag.audible_pref` entry around line 6565), `requirements.txt`, `DesktopKaraoke.spec`.

**Verify (the user's scenario):** open Brave with TAB A (any muted YouTube video — the Cyberpunk motorbike POV) and TAB B (Rosa Walton "I Really Want to Stay at Your House", PLAYING audibly); `/diag.sessions` lists BOTH; `/diag.audible_pref.levels` shows `brave` with a non-zero peak (Tab B is the source); `/source.session_id` should lock to Tab B and stay there even when Tab A is clicked into the foreground. Toggle `prefer_audible_session=0` via `/tune` → behavior reverts to pre-118 sticky (ping-pongs on browser focus) — proves the tiebreaker is doing the work. With `prefer_audible_threshold=0.5` (artificially high) → no session clears the bar → falls through to sticky → also proves the threshold is honored.

---

## TICKET-117 — Explicit SMTC session pin (tray "Source →" submenu, persistent) 🟢 (v1.0.96)
**Symptom (user report, 2026-06-27, same scenario as TICKET-118):** *"i want to be able to watch a muted video while having the actual music video in a different tab providing lyrics and music without interference"*. The TICKET-118 audible-session tiebreaker handles the common case automatically (loudest process wins), but the user wanted a GUARANTEED lock — a way to say "the lyric source IS this exact tab, ignore everything else, even if it briefly goes silent during an instrumental break or while paused mid-song." Audible-pref alone can't deliver that because peak=0 during a paused / instrumental moment would still tip the picker to whichever other session is currently loudest.

**Fix (v1.0.96):** explicit per-session pin in `MediaWatcher`, surfaced as a tray "Source →" submenu.
- New `MediaWatcher` state: `_pinned_id: str` (16-hex composite id from `_composite_session_id(session)` = hash of `(source_app, title, artist)` — stable across the session's lifetime, unique per SMTC session), `_pinned_source_app: str` (captured at pin time so the grace-window re-adopt knows which app to look for), `_pinned_last_seen_t: float` (last time the pinned id was visible in the enumerated session list).
- `set_pinned_session(id_hex: str, source_app: str)` installs the pin (or clears it with `id_hex=''`). `_pick` returns the pinned session unconditionally if it's in the current session list. If the pinned id is MISSING from the list, the watcher enters a grace window: for `pin_grace_s` (default 20.0s) any single new session whose `source_app` substring-matches `_pinned_source_app` is silently adopted as the new pin target (so a Brave tab refresh — which assigns a new SMTC session id — doesn't break the lock). After the grace window expires, `_pinned_id` is cleared and AUTO behavior resumes.
- Tray "Source →" submenu: lists every visible SMTC session as `{app_name} — {title} ({artist})` with a checkmark on the currently-pinned one (or AUTO if no pin). Click a session → `set_pinned_session(...)` + persist; click AUTO → `set_pinned_session('', '')` + persist. The menu is rebuilt on a debounced callback the watcher fires whenever the visible session set changes (max 1 rebuild per 2s to keep the tray cheap).
- Persistence via `_persist()`: new keys `pinned_session_id` (the 16-hex) + `pinned_source_app`. Loaded in `Overlay.__init__` and pushed into the watcher AFTER the watcher's first enumeration so the very first pick already honors the pin. If the pinned session isn't present at boot, the grace-window machinery picks it up as soon as the browser tab is reopened.

**New tune knob:** `pin_grace_s` (20.0) — how long the watcher will hold a missing pin open waiting for a re-appearance of a same-`source_app` session. Live-tunable; 0 = no grace (clear immediately on first missing-pin pick).

**Observability:** `/diag.sessions` returns a list of `{id, app_name, source_app, title, artist, status, age_s, pinned: bool}` — one entry per visible SMTC session, with `pinned: true` on the currently-locked one. `/diag.pin` returns `{id, source_app, alive, age_s, grace_remaining_s}` (or `null` when no pin is set).

**Interaction with TICKET-118:** the pin is ABSOLUTE — if a pin is set, `_pick` never consults the audible-pref tiebreaker. The two tickets together give the user: (a) automatic loudest-wins (TICKET-118) for the common case, (b) explicit override (TICKET-117) for full control.

**Files:** `audible_sessions.py` is TICKET-118 only; TICKET-117 changes live in `main.py`:
- `MediaWatcher` (~line 410-770): `_composite_session_id`, `_sessions_cache`, `_sessions_keys_digest`, `_pinned_id` / `_pinned_source_app` / `_pinned_last_seen_t` state, `_pick` pinned-session branch (line 563), `set_pinned_session`, `get_pin`, `list_sessions`, `_sessions_changed_cb` register/fire.
- `Overlay.__init__` (line 2249-2266 + 2439-2444): `_pinned_session_id` loaded from `_tune`, pushed into watcher via `set_pinned_session(...)` post-construction.
- `Overlay._on_track_change` (line 3528): pin-liveness check — if the pinned id has been missing for `> pin_grace_s`, clear the pin and the persisted keys.
- Tray submenu (line 10115 + 10353): "Source →" build, checkmark on pinned, click handler `set_pinned_session(...)` + `_persist()`.
- `_persist()` (existing pattern): two new keys `pinned_session_id` + `pinned_source_app`.

**Verify (the user's scenario):** open Brave with TAB A (Cyberpunk POV, muted) and TAB B (Rosa Walton, audible, playing); right-click tray → Source → pick the Rosa Walton entry; `/diag.pin.id` should equal that session's id and `/source.session_id` should match; click into Tab A to bring it to the foreground → `/source.session_id` should NOT change (pre-117 it would); pause Tab B mid-song → `/source.session_id` should STILL not change (the audible-pref tiebreaker would have lost interest, but the pin is absolute); close Tab B, then reopen the same URL in a new tab within 20s → the new tab's session is auto-adopted (`/diag.pin.id` changes, `source_app` stays `brave`). Close Tab B and wait >20s with no Brave session reappearing → pin clears, AUTO resumes. Persist test: pin Tab B, restart app, Tab B's URL still open → `/diag.pin.id` is restored from `settings.json` on first poll cycle.

---

## TICKET-114 — Instrumental-gap timer reset on every track change (and every `idx>=0` transition) 🟢 (v1.0.95)
**Symptom (live capture, 2026-06-27):** `/diag.pending_swap` reported `blocked_by='instrumental-gap(204.2s)'` on a **161.7-second song** ("Play On！" by NTE 公式, position 11.73s). 204.2s of "instrumental gap" on a 161s song is nonsense — that's the wall-clock age since the app booted, not the time the current track has been on `idx==-1`. TICKET-111's LINE-mode boundary check therefore could never satisfy `gap_s >= swap_defer_instrumental_gap_s` cleanly, and `will_force_commit_in_s` was being driven by the `swap_defer_max_s` hard cap (e.g. 0.96s in the captured frame) instead of the real boundary. Effectively, the gap-gated boundary commit was dead code on every song after the first.

**Root cause:** `self._idx_minus_one_since = time.monotonic()` was set once in `Overlay.__init__` (boot wall-clock) and never reassigned. Within a single track, idx transitions from `>=0` to `-1` never updated it; track changes never reset it. So `now - self._idx_minus_one_since` measured "seconds since the app started" forever.

**Fix (v1.0.95):**
- `_on_track_change`: reset `self._idx_minus_one_since = None` (the natural reset point — a new track has no prior gap).
- `_tick_body` idx-transition arm: on every transition where `prev_idx == -1 and idx >= 0` (singing resumed), clear `self._idx_minus_one_since = None`; on every transition where `prev_idx >= 0 and idx == -1` (just entered instrumental), set `self._idx_minus_one_since = time.monotonic()`. Within a single track, the timer now always measures the current `idx==-1` stretch — exactly what `swap_defer_instrumental_gap_s` was designed to threshold.
- Diag display clamped: `gap_s = min(gap_s, position)` so the operator-facing number can't exceed playback position (defensive against any clock skew).

**Net effect:** TICKET-111's `_swap_ready` LINE-mode `idx==-1 and gap_s >= swap_defer_instrumental_gap_s` branch actually fires now. `will_force_commit_in_s` is driven by the real boundary on most songs, not the 8s safety cap, so SWITCH/REGEN/wrong-song swaps land much sooner on songs with any instrumental section >= 2.0s. The `swap_defer_max_s` cap returns to being a fallback, not the primary commit trigger.

**Files:** `main.py` (`_on_track_change` reset; `_tick_body` idx-transition arms; `_diag_pending_swap` clamp).

**Verify:** play a song with a clear instrumental intro; `/diag.pending_swap.gap_s` should read `0` at first beat, climb up to ~2.0s during silence, reset on the first sung line. Trigger `/wrong` during an instrumental gap — swap should commit within 2s, not 3s (`swap_defer_user_max_s`) or 8s (`swap_defer_max_s`).

---

## TICKET-113 — Per-track lyric blacklist + provider rotation on `/wrong` / REGEN 🟢 (v1.0.95)
**Symptom (user report, 2026-06-27, ReGLOSS × BEMANI "Shooting Star"):** *"wrong song, even when I told it, it didnt try to find a new one"*. SMTC.title = "Shooting Star" (one of dozens of songs with that name); the app loaded the wrong cached "Shooting Star" LRC. User pressed `/wrong`, the cache was cleared and `report_wrong` triggered a re-fetch — but the query going into the provider chain was IDENTICAL (`title + smtc_artist`), so lrclib / syncedlyrics / NetEase returned the SAME wrong LRC, the app re-cached it, and the user was stuck in a loop where `/wrong` accomplished nothing.

**Root cause:** the fetch path had no memory of what it had just rejected. `fetch_and_save` was idempotent on `(title, artist)`, and the provider chain order was fixed.

**Fix (v1.0.95):**
- New `self._lyrics_blacklist: set[tuple[str, str]]` on Overlay — entries are `(sha1(first 500 chars of LRC normalized), source_provider)` tuples. Reset on every `_on_track_change` (per-track scope — a wrong "Shooting Star" for THIS track doesn't poison the same LRC if it happens to be correct for a different track later).
- `report_wrong` (and decision-engine REGEN): compute the signature of the CURRENTLY LOADED lyrics, add to `self._lyrics_blacklist` BEFORE kicking the re-fetch. So the re-fetch knows what NOT to return.
- `fetch_lyrics.fetch_and_save` accepts `reject_signatures: set[tuple[str, str]] | None`. Every provider hit is sha1'd at the same normalization the blacklist uses; matches are skipped, the chain tries the next candidate (next provider, or next NetEase result, etc.).
- **2-strike escalation:** two `/wrong` calls within `wrong_escalate_window_s` (default 60s) for the same track set `force_ai_gen=True` on the re-fetch, bypassing the provider chain entirely and going straight to AI-gen (the providers have proven they only have the wrong file for this query).
- **Provider chain rotation:** per-track chain order rotates one slot on each `/wrong` (e.g. `[lrclib, synced, netease, ai]` → `[synced, netease, lrclib, ai]`) so a stubborn primary doesn't dominate. Reset on track change.

**Files:** `main.py` (`_lyrics_blacklist` init, `_on_track_change` reset, `report_wrong` signature compute + add, `_fire_decision_action` REGEN add, escalation counter); `fetch_lyrics.py` (`fetch_and_save` `reject_signatures` kwarg + per-provider signature check; provider chain rotation helper). New tune knobs: `wrong_escalate_window_s` (60.0), `wrong_escalate_strikes` (2).

**Verify:** load a song with multiple candidate LRCs in cache; press `/wrong` repeatedly; each press should land a DIFFERENT LRC (or escalate to AI-gen after the 2nd strike within 60s). `/diag.lyrics_blacklist` exposes the current set so the operator can see what's been rejected this track.

---

## TICKET-112 — YouTube video-description metadata extractor (composer / vocals / original-artist tags) 🟢 (v1.0.95)
**Symptom:** SMTC for browser-played YouTube videos exposes only `title` and `artist` (the channel name, often a label / VTuber agency, NOT the actual vocalist). For ambiguous titles like "Shooting Star" — shared by dozens of songs from completely different artists — `title + smtc_artist` is not enough to disambiguate, and the fetch returns the wrong LRC consistently (this is the upstream root cause of the TICKET-113 user complaint).

**Available ground truth:** the YouTube description on the ReGLOSS × BEMANI "Shooting Star" video reads `作詞・作曲：kors k / 歌唱：ReGLOSS(音乃瀬奏/一条莉々華/儒烏風亭らでん/轟はじめ)` — exactly the disambiguators we need (composer = kors k, vocals = ReGLOSS members). `yt-dlp` is already bundled (`DesktopKaraoke.spec` hiddenimports for `deep_transcribe.py` audio download), so a metadata-only call (`--skip-download`) is free.

**Fix (v1.0.95):** new `yt_description.py` module.
- `_video_id(url)`: extracts the `v=` param from `youtube.com/watch?v=...` / `youtu.be/...` / `music.youtube.com/...` URLs, returns `None` for non-YouTube.
- `extract_video_metadata(url, timeout_s=8.0) -> dict | None`: lazy `import yt_dlp` (so the module loads fast even if yt-dlp is heavy); calls `YoutubeDL({'skip_download': True, 'socket_timeout': timeout_s, 'quiet': True})`.extract_info, parses the description against templated tags:
  - **JP:** `作詞・作曲` / `作詞` / `作曲` / `編曲` / `歌唱` / `ボーカル` / `Original` / `カバー元`
  - **EN:** `Music:` / `Vocals:` / `Lyrics:` / `Composer:` / `Original by:`
  - **KR:** `작사` / `작곡` / `노래`
  - Returns `{composer, lyricist, arranger, vocals, original_artist, source: 'yt_description', video_id, fetched_at}` or `None` on parse failure / timeout / non-YouTube.
- In-process LRU keyed by `video_id` (default cap 256; controlled by `yt_description_cache_days` for soft TTL). Hard `yt_description_timeout_s` (8.0s) so a slow network call can't block the calling thread.

**Wire-up in `main.py`:**
- `_on_track_change` (main.py:2479): if the source is `youtube*.com` / `steamwebhelper` and `yt_description_lookup` is on, schedule `_maybe_fetch_yt_description(track_seq)` on a worker thread; the result lands in `self._yt_metadata` keyed by track_seq (so a late return for a stale track is ignored).
- `report_wrong` (main.py:6830): when `/wrong` fires on a YouTube source whose `yt_metadata` is still missing, force a fresh fetch — the metadata might be what unblocks the next provider try.
- `_fetch_lyrics` query construction (main.py:7321): if `yt_metadata.vocals` exists and the SMTC artist is short / one of the channel-name patterns (`* Music`, `* Records`, etc.), substitute `vocals` as the artist; pass `composer` + `original_artist` as fetch disambiguators (joined into the lrclib `q=` query) so the provider chain narrows from "any 'Shooting Star'" to "the kors k / ReGLOSS one".

**Observability:** `/diag.yt_metadata` returns the parsed dict for the current track (or `null` if not YT / not yet fetched / disabled). New `GET /yt-meta` route in `api.py` returns the same.

**New tune knobs:** `yt_description_lookup` (1 = on, 0 = off for diagnosis), `yt_description_cache_days` (30), `yt_description_timeout_s` (8.0). All live-tunable.

**Frozen-build pin:** `yt_description` is lazy-imported (`from yt_description import _video_id, extract_video_metadata` inside functions), so PyInstaller's static analyser misses it. Added to `DesktopKaraoke.spec` hiddenimports next to the other lazy-loaded local modules. Without this pin a frozen build would `ImportError` on first browser-source track change.

**Files:** `yt_description.py` (new, ~440 lines), `main.py` (`_yt_metadata` state, `_maybe_fetch_yt_description`, `_on_track_change` schedule, `report_wrong` force-fetch, `_fetch_lyrics` query merge, `get_diag` entry, tune knob defaults), `api.py` (`/yt-meta` route + `_ROUTES` help blurb), `DesktopKaraoke.spec` (hiddenimports pin).

**Verify:** load the ReGLOSS × BEMANI "Shooting Star" video in a browser (SMTC title alone = ambiguous); `/diag.yt_metadata` should populate with `{composer: 'kors k', vocals: 'ReGLOSS(...)', ...}` within ~8s of track change; `/diag.last_query` should show `kors k` / `ReGLOSS` substituted into the fetch query; the loaded LRC should match the BEMANI version, not a different "Shooting Star".

---

## TICKET-115 — Six-language translation actually delivered + `/retranslate` endpoint 🟢 (v1.0.95)
**Symptom (live capture, 2026-06-27):** README has long claimed "English translation for Japanese / Chinese / Korean / Spanish / German / Russian songs" but on Rammstein "Deutschland" (lang=de, 51 lines loaded from lrclib/search) every `en` field came back empty. Romaji `rm` was also empty (correct — German doesn't need it), but no translation ever ran. The whole German + Russian "delivered" claim was effectively a lie for the songs the user actually queues up.

**Root cause:** two-place language whitelist drift plus a per-line gate that only fired when CJK lines were present.
- `fetch_lyrics._translate_lines` had `("ja", "ko", "zh", "es", "de", "ru")` at the per-line gate and `("ja", "ko", "zh", "es", "de", "ru", "ja-romaji")` at the whole-song gate — already two slightly different sets.
- `main.Overlay._maybe_translate` "whole" tuple was `("es", "de", "ru", "ja-romaji")` — tighter still, and missing the CJK + es overlap that `_translate_lines` got right.
- The per-line gate only marked a line for translation when `detect_lang(raw)` hit the whitelist; a fully German body that the song-language pipeline correctly classified as `de` never had any individual lines forced into the want-set because the CJK-presence shortcut didn't fire on a Latin-script song.

**Fix (v1.0.95):**
- `fetch_lyrics._translate_lines`: hoisted into `_LANGS = ("ja", "ko", "zh", "es", "de", "ru", "fr", "pt", "it")` used at BOTH gates (per-line `detect_lang(raw) in _LANGS`, whole-song `song_lang in (*_LANGS, "ja-romaji")`). One source of truth, can't drift.
- `main._maybe_translate`: "whole" tuple expanded to `("es", "de", "fr", "it", "pt", "ru", "ja-romaji")` with an explicit "keep in sync with `_translate_lines._LANGS`" comment.
- `annotate()` comment clarified: any Latin-script source (es / de / fr / it / pt / en) renders as-is with empty `rm` — the `backfill_file` re-process guard already skips lines with `rm` set, so leaving Latin lines empty is intentional and correct.

**Bonus:** French / Italian / Portuguese also work now (already classified by `detect_lang`, only the wire-up was missing).

**New endpoint:** `POST /retranslate` forces a translation backfill of the currently loaded track without re-fetching lyrics. `api.py` marshals onto the Tk thread via `threading.Event` + `_run` so the HTTP response carries the worker snapshot (`{ok, action, path, lang, n_lines, n_missing}`); bounded 5 s wait returns 503 if the UI thread is hung. Backed by `Overlay.retranslate_loaded()` which clears any stuck `_translating` guard and routes through the existing `_start_translate` → `backfill_file` pipeline (atomic rewrite, main tick re-loads in place, playback position preserved). Help blurb in `_ROUTES`.

**In-flight guard hardening:** `_start_translate`'s `self._translating = None` release moved into a `try/finally` so an exception in `backfill_file` no longer poisons the guard and silently blocks every future `_maybe_translate` call for the same path.

**Files:** `fetch_lyrics.py` (`_translate_lines` _LANGS hoist + dual-gate; `annotate()` comment), `main.py` (`_maybe_translate` whole-set; `_start_translate` finally; new `retranslate_loaded` method), `api.py` (`/retranslate` route + `_ROUTES` help blurb).

**Verify:** see v1.0.95 smoke test above. README's "Japanese / Chinese / Korean / Spanish / German / Russian" claim is now actually delivered end-to-end; French / Italian / Portuguese delivered as a bonus.

---

## TICKET-111 — Boundary-deferred whole-lyrics swap (no more 1-5s blackout on SWITCH/REGEN/wrong-song) 🟢 (v1.0.93)
**Symptom (post-v1.0.92):** the v1.0.92 decision engine's `_fire_decision_action` SWITCH and REGEN branches, the wrong-song-strike teardown (Site D), and the user-driven `/wrong` (Site G) all blanked `self.lines = []` immediately on fire and re-fetched. Result: 1-5 seconds of empty overlay while the new lyrics loaded, exactly the kind of visible "snap" the user has been hunting for (lineage: TICKET-088 smooth-transitions still snapping).

**Fix (v1.0.93):** queue the swap on `self._pending_swap` instead. Fetch / AI-gen runs in parallel so latency overlaps. Old lyrics keep rendering until the boundary fires. Atomic commit at the boundary via `_apply_pending_swap`. Same shape as TICKET-078 `_pending_offset` (precedent for offset corrections; this is the same state machine generalized to whole-lyrics replacement). TICKET-088 same-tick ordering preserved: the new consumer is placed RIGHT AFTER the `_pending_offset` consumer in `_tick_body`, so offset commits first and the swap commits against the fresh offset.

**Five sites converted:**
- Site C: `_begin_generation` (guards its immediate clear when a REGEN swap is pending, `_apply_generated` writes into `pending["lines"]` and `_apply_pending_swap` commits atomically)
- Site D: wrong-song-strike teardown in `_tick`
- Site G: `report_wrong` (user `/wrong`, tighter `swap_defer_user_max_s` cap)
- Site H: `_fire_decision_action` SWITCH branch
- Site I: `_fire_decision_action` REGEN branch

**Boundary detection per render mode:**
- LINE modes (`none`/`left`/`right`/`top`/`bottom`): current line ends (post-last-line is a fast-path) OR `idx==-1` instrumental gap exceeds `swap_defer_instrumental_gap_s` (default 2.0s).
- SCROLL modes (`lr`/`rl`/`tb`/`bt`): scroll belt drained (`len(self._stream) == 0`) OR same instrumental-gap rule.
- Animation in progress in LINE mode blocks the commit (`anim-in-progress`) so a slide-in can't snap the new line.

**Safety:** `swap_defer_max_s` (default 8.0s) is a hard cap, force-commit with `reason="timeout(...)"` if the boundary never lands. `swap_defer_user_max_s` (default 3.0s) is a tighter cap for `/wrong` since the user explicitly asked for the fix. Kill-switch `swap_defer_enabled` (default 1), `/tune swap_defer_enabled=0` restores v1.0.92 immediate-clear without a re-release; any in-flight swap flushes (`reason="disabled"`) or cancels.

**Race-safety:** stale fetch tokens (`_swap_fetch_token` monotonic) are dropped when `_consume_async` sees a token that doesn't match `pending_swap["fetch_token"]`, rapid double `/wrong` no longer races. Real track change in `_on_track_change` calls `_cancel_pending_swap("track-change")` so an old-song target can't land against new-song state. A sibling `_pending_offset` write is dropped in `_apply_pending_swap` since the line timings under it just changed.

**Files:** `main.py` (13 edits: tune knobs, init state, on_track_change cancel, _start_fetch swap_token threading, _consume_async route, Sites C/D/G/H/I, _tick_body consumer, helper methods `_queue_swap`/`_try_apply_swap`/`_apply_pending_swap`/`_cancel_pending_swap`/`_swap_ready`/`_diag_pending_swap`, get_diag entry); `api.py` (1 edit, /diag help blurb only).

**Verify:** see v1.0.93 smoke test above. /diag.pending_swap fields cover every state-machine transition.

---

## TICKET-110 — No-repo-lyrics path (handle songs the user has no LRC for, AI-gen disabled) 🔴
**Symptom:** when a track has no cached LRC, no provider lyrics, and the user has AI generation disabled (or it fails), the overlay just sits empty with no user feedback. The current paths assume one of fetch/cache/generation succeeds.

**Proposal:** explicit "no lyrics available" hint + tray badge state. Detection runs after the full fetch chain returns empty AND `force_ai_gen`/`generate_by_ear_on` is false (or AI-gen yielded nothing in N seconds). Surface a tray-icon overlay glyph + `/status.no_lyrics_reason ∈ {"chain-empty", "ai-disabled", "ai-failed"}` so the user knows WHY. Allow inline `/wrong` to bypass and try again; allow `/identify` to re-run sound-ID in case the track field was wrong.

**Status:** queued for v1.0.94. Pairs naturally with TICKET-111's deferred-swap state machine (a no-lyrics signal could route through `_cancel_pending_swap` cleanly).

---

## TICKET-109 — Continuous decision engine (SMTC/Shazam/drift/quality aggregation -> SWITCH/REGEN) 🟢 (v1.0.92)
**Shipped in v1.0.92.** Background watcher `_decision_tick` aggregates four dimensions into strike scores over a rolling window; state machine promotes TRUST -> CAUTION -> SWITCH -> REGEN; `_fire_decision_action` executes the chosen action with a per-state cooldown. See v1.0.92 changelog above for full detail. Closed by v1.0.93 follow-up (TICKET-111) which removed the immediate-clear blackout on SWITCH/REGEN.

---

## v1.0.91 — Shipped (LINE-mode render perf + perf instrumentation + title-alias album fallback + verified-render grace + scroll_ rename + concert-detect regex + fine-tune pause + GPU policy followups + Start Menu self-heal)
**Seven review-driven fixes in one build, all targeting failure classes captured by the v1.0.90 performance audit on V.W.P "歌姫" + cluster A/B stall traces.** Closes TICKET-103 followups, TICKET-104, TICKET-105, TICKET-106 and lands the batch4 A1/A2/A3 plus the scroll_ rename batch1.

- **A1 (TICKET-104 followup):** bounded LRU cache for `measure_text()` keyed by `(font_name, font_size, char)`, capped at the new `measure_text_cache_size` knob (default 4096). Eliminates >95% of per-char canvas create+destroy in `_render()` after warm-up. Hit rate surfaced in `/diag.measure_text_cache_hit_rate`.
- **A2:** sub-branch perf instrumentation. `_perf_branch(name)` context manager wraps `_render()`, `_karaoke()`, and the per-char `itemconfig` loop; perf-log line gains a ` | branch=render=… kara=… itemconfig=…` suffix. Raw-frame-ms column added too (controlled by `perf_record_raw_frame_ms`, default 1) — the EWMA was hiding 800-960 ms real stalls as 156 ms entries. New knobs: `perf_record_branches` (default `render|kara|itemconfig`), `perf_record_raw_frame_ms`, `perf_record_dt_cap_ms` (default 2000).
- **A3 (TICKET batch4):** `title_alias_album_fallback` (default 1) — when Shazam's title is the album-string-with-features pattern (歌姫 vs DIVA (feat. …)), populate `_sound_title_alias` so the v1.0.89 strict source-priority gate keeps `verified=true` instead of blanking the overlay. `verified_render_gate_s` (default 3.0) — keep the last good lyrics on screen for N seconds after a verified→False flip before tearing down `line_count`. Single chokepoint `_set_verified` consolidates all `self._verified` assignments and records `_verified_gate_t` on every edge. `/diag.derived` exposes `sound_title_alias`, `verified_render_gate_remaining_s`.
- **scroll_ rename (batch1, TICKET-104 follow-on):** five scroll-mode-only knobs renamed with `scroll_` prefix so a future operator can't confuse them with LINE-mode work: `heavy_budget_ms` → `scroll_heavy_budget_ms`, `spawn_budget` → `scroll_spawn_budget`, `repaint_budget` → `scroll_repaint_budget`, `fill_skip` → `scroll_fill_skip`, `fill_interval` → `scroll_fill_interval`. Back-compat via `_TUNE_LEGACY_ALIASES` in `set_tune` — old keys still POST cleanly with a warning log.
- **TICKET-106:** `_LIVE_VER_RE` expanded to catch the `Nth ONE-MAN LIVE` / `Nth LIVE TOUR` / `Nth ANNIVERSARY LIVE` family plus `ワンマン` / `ワンマンライブ` JP idioms, fixing the V.W.P "4th ONE-MAN LIVE" miss (clear LIVE / ONE-MAN LIVE markers in the title were not firing in-tick concert detection). Live-resync cadence shortened: 12.0 → 6.0 s (`live_resync_s`), listen window 6.0 → 4.0 s (`live_resync_listen_s`), fast gap 1.5 → 1.0 s (`live_resync_fast_gap_s`) so inside-wrapper song-ID happens ~12×/min on a hot tier.
- **TICKET-104:** `fine_tune_max_pause_s` 1.0 → 3.0 (user-requested); `fine_tune_exit_drift_s` 2.5 → 3.5 (must be > pause cap + 0.5 buffer).
- **TICKET-103 followups:** `gpu_solo_override` knob added (default 0) so a single-GPU user can opt back into GPU; live-tune flip propagates immediately via `align.set_gpu_solo_override(bool)`. GPU device + index + reason + count surfaced in `/diag`.
- **TICKET-105:** Start Menu shortcut self-heal at frozen-app startup — deletes a broken `Desktop Karaoke.lnk` whose target doesn't exist; creates fresh `Lyric Immersion and Karaoke.lnk` pointing to `sys.executable` (WindowStyle 7, minimized + no-activate). Skipped in dev (sys.frozen=False).

**No new modules** — every change is in main.py — so `DesktopKaraoke.spec` hiddenimports needed no edit.

**Follow-up (re-measure required):** A1 should reduce the per-IDX-transition spikes (cluster B 1:1 correlation) substantially; ask the user to re-run the perf capture post-restart and confirm p99 drops from 78.5 ms toward the 33-40 ms baseline. If A1 alone is insufficient, schedule `render_idx_change_budget_ms` for v1.0.92 (soft-budget `_render()` and re-queue residual segment work via `root.after(0, …)` so the next eased belt frame renders unblocked).

---

## TICKET-106 — Concert detection: "Nth ONE-MAN LIVE" / "ワンマン" family miss 🟢 (v1.0.91)
**Symptom (performance audit, 2026-06-27):** V.W.P "4th ONE-MAN LIVE" track title carried both `LIVE` and `ONE-MAN LIVE` markers, yet the in-tick `_LIVE_VER_RE` regex did not classify the wrapper as a concert — so the live-resync hot tier never engaged and song-ID inside the container missed. Pairs with the verification deadlock identified in slice 3 (tier_incon_streak=1 for the full 90s without escalation).
**Fix (v1.0.91):** `_LIVE_VER_RE` expanded:
- `one[\s-]?man\s*live` matches `ONE-MAN LIVE` / `ONE MAN LIVE` / `ONEMANLIVE`.
- `\d+(?:st|nd|rd|th)\s+one[\s-]?man(?:\s*live)?` catches the `4th ONE-MAN LIVE` / `5th ONE-MAN` ordinal form.
- `\d+(?:st|nd|rd|th)\s+(?:live|tour|anniversary)\s+(?:tour|live|stage|fes(?:tival)?)` covers `10th LIVE TOUR`, `5th ANNIVERSARY LIVE`, `3rd TOUR FES`.
- `ワンマン(?:ライブ)?` covers the JP solo-concert idiom.

Live-resync cadence retuned in the same edit: `live_resync_s` 12.0 → 6.0 (default cadence), `live_resync_listen_s` 6.0 → 4.0 (shorter capture, faster cycle), `live_resync_fast_gap_s` 1.5 → 1.0 (~12×/min while missing / fresh song). Inside-wrapper song-ID now retries fast enough that a 90 s deadlock can no longer recur on a recognized concert title.

**Verify:** play any V.W.P concert track titled with `Nth ONE-MAN LIVE` / `Nth LIVE TOUR`; `/diag.derived` shows `live_arrangement=true` within the first tick; the tier scheduler escalates to live-resync hot tier within 6 s.

---

## TICKET-105 — Self-heal Start Menu shortcut on app startup (rebrand migration) 🟢 (v1.0.91)
**Symptom (user, 2026-06-27):** "i dont see the exe on my start menu". After the rebrand from "Desktop Karaoke" → "Lyric Immersion and Karaoke" (and exe rename `DesktopKaraoke.exe` → `Lyric-Immersion-and-Karaoke.exe`), the old `Desktop Karaoke.lnk` in the user's Start Menu still pointed to the now-deleted `<install-dir>\DesktopKaraoke.exe`. Clicking did nothing, and searching "lyric" found nothing.
**Inline fix this session:** deleted the broken `Desktop Karaoke.lnk`, created fresh `Lyric Immersion and Karaoke.lnk` pointing to the new exe.
**Permanent fix:** add a startup migration in main.py (only when `getattr(sys, 'frozen', False)`):
- Probe `$APPDATA\Microsoft\Windows\Start Menu\Programs\` for any `Desktop Karaoke.lnk`.
- If found AND its target doesn't exist on disk, delete it.
- If no `Lyric Immersion and Karaoke.lnk` exists in the same dir, create one pointing to `sys.executable` (the running exe path).
- WindowStyle = 7 (minimized, no-activate) for polite helper launch behavior.
**Why this matters:** any future rename or deploy-path move will leave broken shortcuts that the existing autostart path (already at main.py:177) doesn't touch. Self-heal makes upgrades silent for the user.
**Verify:** simulate by renaming the lnk back and relaunching; should restore. Don't run in dev (sys.frozen=False) to avoid noise during development.

---

## TICKET-104 — Fine-tune pause range: max 1.0 → 3.0 seconds 🟢 (v1.0.91)
**User request:** the fine-tune mode's max forward-drift pause is too short — bump to 3.0 s.
**Current (main.py self._tune):**
- `fine_tune_max_pause_s = 1.0` — biggest forward-drift pause when lyrics are ahead of vocals.
**Change:**
- `fine_tune_max_pause_s = 3.0` (3× current).
- Re-check `fine_tune_exit_drift_s` (currently 2.5) — must be GREATER than the new pause ceiling so a drift just under 3.0 doesn't immediately hand back to the regular tier. Proposed: bump to 3.5 (3.0 cap + 0.5 buffer).
**Why bigger pauses help:** holding the current line still for up to 3 s is visually quieter than a 3-s backward nudge that re-scrolls already-shown text. The fine-tune was designed to bias toward pauses over jumps; this just raises the ceiling on that bias.
**Risk:** a 3-s pause feels like a freeze if the song is fast (≥4 lyrics/s). Mitigation: existing `fine_tune_min_step_s` (0.2) keeps small corrections proportional; the 3-s cap only fires for big drifts where the alternative (backward nudge) is worse.
**Status:** queued behind TICKET-102's review batch (avoid concurrent main.py edits); apply in the v1.0.90 commit.

---

## TICKET-103 — GPU policy: secondary GPU when gaming, disabled on single-GPU 🟢 (v1.0.91)
**User request:** "make sure any gpu acceleration is on the secondary gpu when gaming or otherwise disabled when single gpu".
**Current state (gpu_setup.pick_inference_device):**
- Multi-GPU + gaming → idlest GPU (any GPU >=30% util treated as the game's, skipped) ✓
- Multi-GPU + not gaming → cuda:0 preferred for model-cache stickiness ✓
- Single-GPU + gaming → CPU ✓
- Single-GPU + not gaming → was that GPU; **CHANGED inline to CPU** per user policy.
**Inline change (this session):** modified `pick_inference_device` so `n == 1 → ("cpu", 0, "single GPU → CPU")` regardless of gaming. The lone card stays free for the game/system; the marginal Whisper speed-up isn't worth competing for the only GPU available.
**Followups (need main.py edits, queued behind TICKET-102):**
- Add tune knob `gpu_solo_override` default 0 — user who explicitly wants GPU on single-GPU machine can flip it.
- Expose current device + reason in /diag: `gpu_device`, `gpu_index`, `gpu_reason`, `gpu_count`.
- Richer tray label dynamically reflecting state: "⚡ GPU: cuda:1 (high-perf GPU, active)" / "⚡ GPU: CPU (game running)" / "⚡ GPU: CPU (single-GPU policy)".
**Status:** core policy change applied 🟢; followups 🟢 (v1.0.91 — `gpu_solo_override` knob added, `/diag` exposes gpu_device/index/reason/count, live-tune flip propagates via `align.set_gpu_solo_override`).

---

## TICKET-102 — Window title scraping (Steam Overlay browser + generic Chromium fallback) 🟢 (v1.0.90)
**Shipped in v1.0.90.** New module `window_titles.py` (stdlib + ctypes, mirrors `discord_rpc.py`'s daemon-watcher + lock-guarded-slot style). EnumWindows on a 2s background daemon, allowlist-first PID→exe gate (privacy: non-allowlisted window text is never read), `SendMessageTimeoutW(WM_GETTEXT, SMTO_ABORTIFHUNG, 100ms)` for the title text, suffix-based music-marker filter, negative-suffix and bare-hostname rejection, foreground-preferred candidate selection within a tier. HIGH tier (default ON): steamwebhelper.exe, discord(.exe|canary|ptb), slack.exe, teams.exe, ms-teams.exe. LOW tier (default OFF, opt-in via `window_titles_generic_browsers`): chrome/edge/brave/firefox/opera/vivaldi/arc. Wired into `_tick` as a NEW source between SMTC (`playing=true` still wins) and Shazam-live for HIGH, below Shazam for LOW. 3 tune knobs (`window_titles_on` default 1, `window_titles_generic_browsers` default 0, `window_titles_poll_s` default 2.0); two tray items under detection group; `/diag.window_titles` + `/source.capabilities` mirror state. Pinned in `DesktopKaraoke.spec` hiddenimports. No new requirements (ctypes is stdlib). Teardown mirrors `discord_rpc.stop_watcher()`.


**Symptom (live observation, 2026-06-27, ReGLOSS Hour Time Yellow in Steam Overlay browser):**
- User has YouTube open inside Steam Overlay (steamwebhelper.exe). Window title bar reads `ReGLOSS - Hour Time Yellow OFFICIAL MV - YouTube` — perfect metadata.
- Steam's embedded CEF browser does NOT register with Windows SMTC (unlike Chrome/Edge/Firefox).
- SMTC remains locked on a STALE track from earlier (`BANG BANG / IVE`, playing=false).
- Shazam picks up the audio but matches a DIFFERENT ReGLOSS song (`FG ROADSTER` → FLOW GLOW), likely because Hour Time Yellow isn't in Shazam's DB yet.
- Result: app is locked to the wrong song while the WINDOW TITLE has the right answer right there.

**Fix:** new module `window_titles.py` using ctypes (no new deps) for Win32 EnumWindows + GetWindowText + GetWindowThreadProcessId + QueryFullProcessImageNameW. Filter:
- Process names (high priority): `steamwebhelper.exe` (Steam Overlay), `Discord.exe` / `DiscordCanary.exe` / `DiscordPTB.exe`.
- Process names (low priority, generic fallback): `chrome.exe`, `msedge.exe`, `brave.exe`, `firefox.exe`, `opera.exe` (these usually hit SMTC; only kick in if SMTC silent).
- Title suffix patterns: ` - YouTube`, ` - Spotify`, ` - SoundCloud`, ` - Bandcamp`, ` - Apple Music`, ` - Tidal`, ` - YouTube Music`.
- Parse: strip the suffix, split title on ` - ` / ` | `. Run through existing `clean_title` / `clean_artist` heuristics for VTuber / official-channel cruft.

**Priority slot:**
1. SMTC `playing=true` (authoritative player clock)
2. **Steam Overlay window-title** (explicit metadata, player Windows can't see otherwise — slots between SMTC and Shazam)
3. Shazam-live (sound-truth fallback)
4. Generic-browser window-title (lowest — most browsers ARE in SMTC; this only matters if SMTC silent)
5. Discord RP (TICKET-100/A)

**Polling:** 2s on a background daemon thread. Window enumeration is fast (~5ms for ~100 windows). Lock-guarded slot exactly like discord_rpc.py — Tk thread reads in <1ms.

**Tray toggle:** under section 2 (Detection / Lyric Sources) — "Read browser window titles (Steam Overlay, etc.)". Default ON for the high-priority processes (Steam Overlay is unambiguously music when title matches a music host); separate tune knob `window_titles_generic_browsers = 0` (default OFF) to enable the generic browser fallback.

**Verify:** open Steam Overlay → YouTube ReGLOSS Hour Time Yellow → app switches to that track within 2-5s. Open Spotify in browser → SMTC still wins. Open Spotify desktop app → SMTC still wins. Open a non-music YouTube tutorial in Steam Overlay → don't claim it as music (require either Shazam-confirmed audio OR a music-host suffix).

**Risk:** false positives on non-music YouTube videos. Mitigation: combine the title source with Shazam — if Shazam catches recognizable audio AND title matches a music-host pattern, accept. If Shazam returns nothing AND title is just generic YouTube, hold off until Shazam corroborates.

---

## TICKET-101 — Game Rich Presence + Steam SteamWorks music ingestion 🔵
**Spun off from TICKET-100 (v1.0.89 shipped the Spotify-Listening Discord RP reader; this ticket owns the deferred parts).**
**(A) Per-game Discord Rich Presence:** rhythm/music games (Muse Dash, beatmania, BMS clients, some VRChat worlds) publish "now playing" track info in their Discord RP `details`/`state`/`large_text`. Each game uses an ad-hoc string format, so this needs a per-`application_id` allowlist + per-game parser. Gate behind a new tune knob `discord_game_rpc = 0` (default OFF) and a separate menu item so a wrong parser can't poison the Spotify-Listening path that already works.
**(B) Steam Rich Presence / SteamWorks:** as previously scoped under TICKET-100. Read self via `steam_api64.dll` `ISteamFriends::GetFriendRichPresence` for keys `"steam_display"` / `"music_track"`. Effectiveness varies wildly per game; ship behind `steam_rpc = 0` (default OFF).
**(C) Registered Discord application id:** the current discord_rpc.py uses a placeholder client_id for the read-only GET_ACTIVITY handshake. Discord has historically allowed unregistered ids for read-only IPC, but the discord_rpc.py module-level comment flags this as undocumented. If Discord tightens enforcement we need a real registered application (one-time setup at discord.com/developers/applications). Track as a sub-task here so it's not lost.
**Priority:** lowest of the new batch — TICKET-100/A (Spotify Listening) already covers the high-value case (audio playing on a phone / BT speaker / non-SMTC device while the laptop's Discord shows the track).

---

## TICKET-100 — Discord Rich Presence + Steam overlay music ingestion 🟢 (v1.0.89; A landed, B + Steam deferred to TICKET-101)
**Goal:** add supplementary "what's playing" sources beyond SMTC so we catch overlay/ambient music.
**(A) Discord Rich Presence (local IPC):** Discord client exposes a local named pipe on Windows (`\\?\pipe\discord-ipc-0` through `discord-ipc-9`). The IPC supports `GET_ACTIVITY` to read the current user's Activity, which includes "Listening to Spotify" with track/artist/timestamps. Implementation: connect via win32pipe, send the Discord RPC handshake + GET_ACTIVITY, parse the response. Wire as a SOURCE that augments SMTC when SMTC is silent (or as a "what's my friend playing" presence-aware mode behind a tray toggle).
**(B) Steam Rich Presence / SteamWorks:** Steam doesn't expose game audio directly, but some games broadcast track info via Steam Rich Presence (visible on profile). Read via `steam_api64.dll` `ISteamFriends::GetFriendRichPresence` for self, key `"steam_display"` or `"music_track"`. Effectiveness varies wildly per game; most games don't populate it.
**(C) Existing fallback works:** Shazam-by-sound via WASAPI loopback (recognize.py) already identifies any audio playing through the system mixer including game BGM, Discord call music bots, etc. See TICKET-099 for prioritization.
**Priority order (proposed):** SMTC.playing=true → SMTC. SMTC.playing=false + Shazam confident → Shazam (handle in TICKET-099). Discord Rich Presence as supplementary cross-check.
**Status:** research / partial. Lowest priority of the new batch.

---

## TICKET-099 — SMTC vs Shazam disagreement: trust live audio over paused player 🟢 (v1.0.89)
**Symptom (live diag, 2026-06-27):**
```
player_title: "HAVE A NICE DAY"     ← SMTC (paused YouTube)
player_artist: "WORLD ORDER"        ← SMTC
playing: false                       ← SMTC says paused
heard_by_sound: ["Counter-Strike 2: (Original Menu Music)", "Crosshair Kings"]   ← Shazam: actual live audio
matched_title: "HAVE A NICE DAY"    ← we locked SMTC track
verified: true                       ← but Shazam DISAGREES with SMTC
```
User was actually listening to CS2 menu music in-game. SMTC had a stale paused YouTube tab. The app locked the WRONG song (the paused tab) and ignored what was actually playing through the speakers.
**Fix:**
- Add disagreement detector: when `heard_by_sound` returns a song that does NOT fuzzy-match SMTC `player_title`/`player_artist`, log the conflict.
- Resolution rule:
  - SMTC `playing=true` → trust SMTC (browser kept playing).
  - SMTC `playing=false` AND Shazam confident → **trust Shazam** (switch the loaded track to the Shazam match, re-fetch lyrics).
  - Both confident + agree → `verified=true` (current behavior).
  - Both confident + disagree → log + prefer per the rule above.
- New state field: `source_priority` ∈ `{"smtc", "shazam-live", "agree"}` exposed in /diag.
- Verification rule (clean up): `verified=true` should require AGREEMENT between SMTC and Shazam (or one of them strongly confident and the other silent). Mismatched + verified is the current bug.
**Verify:** repro by pausing a YT tab and launching CS2 menu music; app should switch to CS2 menu music within ~10s once Shazam fires. Repro by playing Spotify (SMTC active) while game BGM plays underneath; app should keep Spotify (SMTC.playing=true wins).
**Risk:** rapid SMTC/Shazam ping-pong if a game's BGM partially matches another song. Mitigate with a 15s switch debounce + minimum Shazam confidence threshold for the swap.

---

## TICKET-098 — Scroll-in: slide-in from top/bottom (centered) + right-orient on slide-in right 🟢 (v1.0.89)
**User request:** under Scroll-in menu, add "Slide in from top" and "Slide in from bottom" which auto-center text horizontally. "Slide in from right" should auto-set text orientation to right-aligned.
**Current state (main.py:6989-7002):**
- `none` (stationary), `left`/`right` (per-line slide from edge), `lr`/`rl` (continuous scroll-through), `tb`/`bt` (continuous vertical scroll-through).
- `left`/`right` slide-in are PER LINE (text starts at edge, animates to its anchored position, stays).
- `tb`/`bt` are MARQUEE-style continuous (not per-line).
**Implementation:**
- Add new scroll_dir values `top` and `bottom` for per-line vertical slide-in (each line drops in from above / rises from below, animates to anchored position, stays).
- In `set_scroll(d)`:
  - `d == "top"` or `d == "bottom"` → set `self.pos_x = "center"`
  - `d == "right"` → set `self.pos_x = "right"`
  - `d == "left"` → set `self.pos_x = "left"`
  - Persist both via existing settings save.
- In `scroll_menu` tray (main.py:6989), add the two new items between `"right"` and the SEPARATOR before `"lr"/"rl"`.
- Renderer: extend the per-line slide-in code that already handles `left`/`right` (search for `self.scroll_dir == "left"` and similar) to also handle `top`/`bottom` with Y-axis animation.
**Verify:** select each new mode in tray; text appears correctly anchored; per-line slide animation is smooth (no snap).
**Risk:** existing `tb`/`bt` continuous-vertical-scroll keeps working unchanged (different code path).

---

## TICKET-097 — Tray menu logical reorganization 🟢 (v1.0.88)
**Symptom:** current tray menu order grew organically; alike controls are scattered. "Re-fetch lyrics" sits in the system section, "Local API" sits between visual settings and Start-with-Windows, "Show / Hide" lives below "Re-fetch lyrics" instead of at the top of the visual block.
**Target structure** (separators between each group; alike controls grouped):
1. **Per-song actions** — Wrong lyrics, Identify by sound, Force Sync, Re-fetch lyrics, Get captions for this video now
2. **Detection / Lyric sources** — Fast song-change detect, Use YouTube captions, Generate lyrics by ear (AI)
3. **Sync behavior** — Sync timing, Auto re-sync by sound (+ Calibrate when added)
4. **Visual / Display** — Show / Hide, Position, Display, Opacity, Font size, Scroll-in, Scroll-through speed, Dancing character
5. **Performance** — Performance, GPU acceleration
6. **Library / Content** — Presets, Import playlist, Library backup (Git)
7. **App / System** — Local API, Start with Windows
8. **Updates** — Check for updates, About, Quit
**Verify:** menu still works (all callbacks intact); checkbox/radio states still bind; emoji icons preserved on relevant items.

---

## TICKET-096 — Cantonese detection + Jyutping mode 🔵
**Symptom:** `lang: "zh"` is ambiguous between Mandarin and Cantonese. Cantopop / HK artists need Jyutping (粵拼) not pinyin — different romanization system reflecting actual Cantonese pronunciation.
**Detection signals:** artist region (HK label / 香港 / Cantonese-only artist list), traditional-character ratio >50%, presence of Cantonese-only function words (嘅 / 嘢 / 喺 / 咗 / 啲), explicit "粵語" / "粤语" / "Cantonese" tag.
**Implementation:** new `lang_zh_variant(text, artist)` returning "yue" or "zh". When "yue", call `pycantonese` or `jyutping` library for romanization. Falls back to pinyin when lib missing. New tune knob `cantonese_detect = 1`.
**Verify:** test against Beyond, 陳奕迅 (Eason Chan, Cantopop tracks), MIRROR, etc. → Jyutping; Faye Wong / Jay Chou / 周深 / mainland rap → pinyin.

---

## TICKET-095 — NetEase Cloud Music lyrics fallback (Chinese coverage) 🟢 (v1.0.88)
**Symptom:** Chinese long-tail (pop, rap, indie) often missing from lrclib + syncedlyrics. NetEase 网易云音乐 is the de-facto Chinese lyrics database with synced LRC for almost every Chinese release.
**Implementation:** add `_fetch_netease_lyrics(title, artist, lang)` provider to `fetch_lyrics.py` chain. Search via public NetEase search API (`/cloudsearch?keywords=...`), grab top match by title+artist fuzzy, fetch lyrics via `/lyric?id=...`, parse `[mm:ss.xx]` LRC. Only attempted when `lang == "zh"` (skip cost for non-Chinese tracks). Falls in chain after lrclib/syncedlyrics, before AI generation.
**Verify:** 揽佬SKAI, 法老, 陳奕迅, 五月天, 周杰倫 all hit NetEase before generation.
**Risk:** NetEase API rate-limits / geofences — add 5s timeout, single retry, swallow failures silently.

---

## TICKET-094 — jieba word segmentation + per-word karaoke fill (zh) 🟢 (v1.0.88)
**Symptom:** Chinese lyrics chunked per-character. Karaoke fill highlights one hanzi at a time when it should highlight one WORD at a time (`梦想` should fill together, not 梦 then 想). Also: pinyin generated per-character misses polyphonic disambiguation (`行` could be `xíng` or `háng` depending on word context).
**Implementation:** `jieba` segments full line. Karaoke fill iterates segments instead of chars. Pinyin generated per-segment via `pypinyin.lazy_pinyin(segment, style=Style.TONE)` (after TICKET-093), which uses dictionary-based disambiguation. Falls back to per-character when jieba unavailable.
**Verify:** a real six-character lyric line (quote redacted) → jieba word segments → per-segment pinyin. Per-word fill animation visible.
**Risk:** jieba ~5MB dict load on first use; cache module-level. Worst case = static download cost on first Chinese song.

---

## TICKET-093 — Pinyin tone marks (pypinyin Style.TONE) 🟢 (v1.0.88)
**Symptom:** Chinese pinyin currently displays without tone marks (`yao zou shang hang ye ta jian`). For learners this is critically incomplete — tones distinguish words (`mā má mǎ mà` = mother/hemp/horse/scold). Without tones the romanization is half-useful.
**Fix:** `fetch_lyrics.py:595` — change `lazy_pinyin(text)` to `lazy_pinyin(text, style=Style.TONE)`. Add `from pypinyin import Style` import.
**Verify:** Chinese track displays "yāo zǒu shàng háng yè tǎ jiān" instead of "yao zou shang hang ye ta jian"; tone-mark glyphs (ā/á/ǎ/à/ē/é/ě/è/ī/í/ǐ/ì/ō/ó/ǒ/ò/ū/ú/ǔ/ù/ǖ/ǘ/ǚ/ǜ) render correctly in current font. If font lacks combining diacritics, fall back to numbered tones (`yao1 zou3 shang4`) via `Style.TONE3`.
**Effort:** literally 1 line + 1 import. Highest user-value per byte changed in this entire ticket batch.

---

## TICKET-092 — Pygame + OpenGL renderer substrate swap (GPU acceleration) 🔵
**Symptom:** at 18 fps target 62 on Chinese rap rendering 3 lines of pinyin + hanzi + English in scroll-mode. Stutter pattern is 1-in-3 frames degrading to 190ms worst-case. Tk Canvas + PIL software rasterization is the bottleneck. The target class of machine had multiple capable GPUs sitting idle.
**Path:** swap Tk Canvas → Pygame with SDL2's OpenGL backend.
- Glyph atlas pre-rasterized via PIL ONCE, uploaded to GPU texture.
- Karaoke fill = 1 shader uniform (no per-frame PIL composite).
- Scroll translation = vertex matrix (free).
- Bouncing animation (TICKET-FUTURE-001) becomes a 50-line shader, not "impossible".
- Window flags: SDL2 supports WS_EX_LAYERED / TRANSPARENT / NOACTIVATE via `pygame.display.set_mode(flags=SRCALPHA, vsync=1)` + `ctypes.windll.user32.SetWindowLongW` for the same extended styles we apply now.
**Effort:** ~1 week dedicated. Defer to a focused session.
**Wins:**
- 60+ fps sustained, no stutter (the entire 1-in-3 pattern collapses).
- Bouncing-animation feature unblocked.
- Frees the secondary GPU from idle (currently paperweight while not gaming).
**Risk:** transparent OpenGL overlays on Windows require specific window creation order (pre-create the HWND, set extended styles BEFORE first composite). Pattern is well-documented but needs careful porting of `_click_through` + topmost re-assert.
**Status:** queued behind TICKET-088 + TICKET-082b (cheaper interim wins first).

---

## TICKET-091 — SMTC artist normalizer (number-word handles, PascalCase compaction) 🟢 (v1.0.88)
**Symptom (live diag, Calibre 50 - Corrido De Juanito):** SMTC reports `player_artist: "CalibreCincuenta"` (YouTube channel handle, "50" spelled out in Spanish, no space). Library lookup by SMTC text never finds the song; only Shazam succeeded. Generalizes to any artist whose YT/Spotify handle compacts spaces or spells digits.
**Fix:**
- Add `_normalize_smtc_artist(s)` invoked in `_on_track_change` before library lookup. Original SMTC string preserved on the side for display, normalized string used for matching.
- De-PascalCase: split on lowercase->uppercase boundary (`CalibreCincuenta` -> `Calibre Cincuenta`, `BandaMS` -> `Banda MS`). Preserve all-caps runs of length >=2 as single tokens.
- Number-word -> digit map. Spanish first (uno/dos/tres/cuatro/cinco/seis/siete/ocho/nueve/diez/veinte/treinta/cuarenta/cincuenta/sesenta/setenta/ochenta/noventa/cien). Add English (one/two/.../hundred) and Japanese (ichi/ni/san/.../hyaku) as cheap extensions.
- Composite handling: `Cincuenta` -> `50`, `Setenta y Cinco` -> `75`. Limit composites to two tokens to avoid false positives.
- Strip "Ch." / "VEVO" / "TV" / "Official" channel suffixes after normalization.
**Verify:** unit list — `CalibreCincuenta` -> `Calibre 50`, `BandaMS` -> `Banda MS`, `LosTigresDelNorte` -> `Los Tigres Del Norte`, `MarcoAntonioSolis` -> `Marco Antonio Solis`, `Maluma` -> `Maluma` (no over-normalization), `Calibre 50` -> `Calibre 50` (idempotent). Library lookup against `_normalize_smtc_artist(artist)` for both stored and query strings.
**Risk:** over-aggressive number conversion (`Trio Los Panchos` must not turn `Trio` into anything). Mitigate by only applying number-word map to tokens AFTER the PascalCase split has been done and only to recognized words.

---

## TICKET-090 — Verified-Shazam wins (gate decide loop, clear stale offset on lock) 🟢 (v1.0.88)
**Symptom (live diag, Calibre 50 - Corrido De Juanito):** Shazam correctly identified the song (`verified: true`, `heard_by_sound: ["Corrido De Juanito", "Calibre 50"]`, `matched_title`/`matched_artist` set, `line_count: 54` lyrics loaded). But `title_locked: false` and `identifying: true` — decide loop kept running, ranked four wrong Japanese candidates (`too_late_18if_episode_7_ending`, `the_world_is_mine_feat_hatsune_miku`, `sky_high`, `reincarnation`), and a stale offset of `-22.98s` from an earlier wrong-song decide blocked all lyric display (`effective_song_time: -16.99` while position was `+5.8`, `current_line: null`).
**Fix:**
- When Shazam returns `verified=True` AND `heard_by_sound` matches the loaded library file (title+artist fuzzy), immediately set `_title_locked = True`.
- On title-lock transition, clear `self.offset = 0.0` (NOT through `_smooth_offset` — this is a coherent track-change reset, not a glide), clear `_pending_offset`, clear `_drift_integral`, reset `_offset_history` to baseline.
- Gate `_decide_tick` early-return: `if self._verified and self._title_locked and not self._decide_force: return`. New tune knob `decide_after_verified = 0` (default off — verified+locked stops decide; user can enable for paranoia).
- `/wrong` and `/forcesync` clear `_title_locked` so decide loop re-engages on demand.
**Verify:** Spanish/English/Korean tracks reach `title_locked=True` within 5s of Shazam verification; `decision.ranked` does NOT populate with hallucinated candidates after lock; offset starts at 0.0 not at a leftover negative from prior track.
**Risk:** Shazam false-positive matches a song that ISN'T in the library and we still lock — mitigated by requiring `heard_by_sound` to fuzzy-match the loaded `matched_title`/`matched_artist`, not just any name.

---

## TICKET-089 — Whisper language lock from SMTC / Shazam metadata (kill Japanese hallucination on non-CJK audio) 🟢 (v1.0.88)
**Symptom (live diag, Calibre 50 - Corrido De Juanito):** `lang: "es"` was known from artist/title heuristics, yet faster-whisper transcription returned `"てれこにでもなくすまさきまで読むとってよメタ取ればズラだった呼吸やるはやかててもる受け取れた"`. Whisper's language auto-detect defaulted to Japanese on Spanish vocals (likely because our library is ~95% Japanese, hyperparameters tuned for it, and Spanish input below the model's confidence floor). The Japanese hallucination then ranked four Japanese candidates in `decision.ranked` and triggered `wrong_song_strikes` against the actually-correct Shazam match.
**Fix:**
- In `deep_transcribe.transcribe_audio` (and wherever WhisperModel.transcribe is called for ID purposes), accept an optional `language` arg. Default `None` (current behavior).
- In `main._consume_async` (where deep transcribe is invoked), pass `language=self._lang` when `self._lang in {"es", "en", "de", "fr", "ko", "zh", "pt", "it", "ru"}`. Japanese stays auto-detect (most of our library + some bilingual tracks).
- Source of `self._lang`: existing language-from-title/artist heuristic, plus Shazam's metadata when present.
- Add tune knob `whisper_lang_lock = 1` (default on); set to 0 to revert to auto-detect for A/B comparison.
**Verify:** play any Spanish, English, or Korean track; `/diag` shows `decision.heard` in the correct script for that language (Latin chars for es/en/de/fr/pt/it, Hangul for ko, Hanzi for zh); no Japanese-character hallucinations on non-CJK audio.
**Risk:** SMTC/Shazam language wrong (rare — e.g., Japanese cover of Spanish song with Spanish-language artist field) — mitigated by `lang_lock` knob and by Shazam's verification reset overriding if track-change.
**Priority:** ABOVE TICKET-088, since it affects every non-Japanese song the user plays (entire roadmap toward Spanish/English/German/Korean was the stated goal of this app's recent direction).

---

## TICKET-088 — Smooth sync transitions still SNAPPING (user observation post-v1.0.85) 🟢 (v1.0.88)
**Symptom (user report):** "we have bad performance with merely sliding text right now and smooth transitions arent working yet they are snapping instead." Despite TICKET-078 (line-boundary deferral), TICKET-081 (in-tick Shazam routed through `_smooth_offset`), TICKET-082a (karaoke fill decoupling + scroll-mode deferral coverage + wall-clock ease), TICKET-082c (topmost re-assert) — transitions are still visually snapping for the user.
**Hypotheses to verify:**
- Some offset-write path I missed routing through `_smooth_offset` (e.g., `_on_song_onset` lines 4322/4324 still snap by design; maybe one of those is firing during normal playback now)
- The wall-clock ease (TICKET-082a) might be too aggressive under load; check the `(target - cur) * (1 - exp(-pull*dt))` math when dt is large (heavy frame catches up too fast = visible snap)
- Force-sync writes (main.py:5077/5092/5122) still snap intentionally; if user is hitting force-sync without realizing, they'd see snaps
- Scroll-mode rendering may still snap the karaoke fill on offset commit even when line position is deferred (need to re-audit the fill commit path)
- Tk thread freezes (TICKET-082b territory) DURING an eased glide would manifest as a "snap" once Tk resumes — the math says "smooth glide" but the user sees the catch-up jump after the freeze
**Next step:** read-only diagnostic pass with live perf.log capture during the user's observed snap, then targeted fix per finding. Bundles naturally with TICKET-082b (subprocess refactor) since the freeze→snap-on-resume hypothesis links them.

---

## TICKET-086 — YouTube Music URL normalization + ampersand-collab cover signal + YT Music metadata trust 🟢
**User spec:** "Keep the same logic for the most part" — surgical changes for YouTube Music
sources without regressing youtube.com / generic-browser flow.

**Three areas:**
- **(A) URL normalization** at every yt-dlp / video-id entry point.
  - New `deep_transcribe._normalize_youtube_url(q)`: lossless `urlsplit`/`urlunsplit` roundtrip
    that rewrites `music.youtube.com` and `m.music.youtube.com` netloc to `www.youtube.com`.
    NO-OP for non-http (titles / 11-char ids).
  - Wired into `fetch_captions_only` (replaces `q = query.strip()`) and `_download_audio`
    (passes the URL straight through instead of `ytsearch1:` prefixing it).
  - Inline guard in `main.set_now_url` so `_now_url` (and `/source`/`/status`) always carry
    the canonical host.
- **(B) Cover detector gains an ampersand-collab signal** (lower-confidence than the explicit tag).
  - New `_is_amp_collab_title(title, cover_channel)`: HTML-unescapes, refuses
    `_AMP_ARTIST_ALLOWLIST` (Hall & Oates, Simon & Garfunkel, Crosby/Stills/Nash, Earth Wind &
    Fire, Florence + the Machine, Kool & the Gang, …), requires a real title separator
    (`-–—/|:【「(\[`) before the right side, and the right side must be ≥2 word/CJK tokens of
    length ≥2 joined by `&` / `＆`. Cover-channel-only matches are rejected.
  - New `cover_signal(title, cover_channel)` returns `'explicit'` / `'amp_collab'` / `None`.
  - `is_cover_title` now ORs the amp_collab signal in (same routing — title-first fetch).
  - `extract_cover_original` short-circuits for amp_collab: returns `(None, song_before_sep)`,
    so the existing `_on_track_change` fall-through sets `fetch_artist = ''` (title-only).
  - **Demotion:** in the tick loop, if `cover_signal == 'amp_collab'` AND YT Music source AND
    SMTC `album` field is non-empty, demote (`_cover_signal = None; _is_cover = False`).
    YT Music only populates album for OFFICIAL tracks, so this is strong original-evidence.
    Tunable via `cover_amp_album_demote` (default 1.0 = ON).
- **(C) YT Music metadata trust.**
  - `clean_artist(artist, source="")` — source-aware bypass: if `'music.youtube' in source`,
    return the SMTC artist verbatim (already clean: `'轟はじめ'`, not `'Hajime Ch. 轟はじめ ‐ ReGLOSS'`).
    Default empty preserves all existing callers; updated the single call site to pass `src`.
  - `clean_title` strips a BOL-anchored `^\s*Mix\s*[-–—]\s*` autoplay-mix prefix. Anchored to
    BOL so `'DJ Mix - Track'` and `'Track - Mix'` are not touched.

**State + diagnostics:**
- `_cover_signal` init in `__init__`, set every tick alongside `_is_cover`.
- `/source` `derived` exposes `cover_signal`, `yt_music_source`, `album` (mirrored from raw).
- 1 new tune knob: `cover_amp_album_demote` (live-tunable via `/tune`).

**Sanity-tested (scratchpad/test_086.py):**
- URL normalize: 7/7 (music.youtube + m.music.youtube → www; www.youtube preserved with
  `?v=`/`&list=`/`&t=` params; 11-char id, plain title, empty, None all pass through).
- amp_collab detection: positives ("Despacito - Luis Fonsi & Daddy Yankee", "Song / Alka &
  Lumi") + allowlist negatives (Hall & Oates, Simon & Garfunkel) + plain-title negatives.
- Mix - prefix: BOL match strips, "DJ Mix - Track" and "Track - Mix" preserved, lowercase
  case-insensitive.

**Live verify post-deploy:** `/health` reports 1.0.86; `/source` derived shows
`cover_signal` + `yt_music_source` + `album` when a YT Music tab is playing.

---

## TICKET-085 — Fine-tune sync mode (±0.2s target via lyric pause) 🟢
**User spec:** "My aim is to get most songs synced to the 0.2s of the sung lyric but not break any other syncing activity. Let's go in that mode after 20 seconds of satisfactory sync. Allow for the lyric procession to pause for 0.2 to 1 second at a time to assist with sync instead of moving place and causing stuttering user experience. Also allow it to move ahead 0.2 to 2 seconds if needed but prefer pause if that's what it needs."

**Design:** post-major-sync precision pass that runs in addition to the existing sync-tier; does NOT replace anything.
- **Enter** when `_sync_good_streak`'s wall-clock duration ≥ 20s (`fine_tune_enter_after_s`).
- **Listen** every 8s (`fine_tune_listen_interval_s`) via the existing Whisper-anchor path.
- **Per-tick classification** of measured drift:
  - `|drift| ≤ 0.2s` (`fine_tune_target_s`) → in sync, no action, reset incon counter.
  - `+0.2 < drift ≤ +1.0s` (lyrics AHEAD of vocals): **PAUSE** lyric procession for `drift` seconds. Both `pos` (eased) and `pos_raw` (raw clock) are held at the pause-start value so the line index, karaoke fill, AND the scroll belt all freeze in lockstep. At pause expiry, `self.offset -= pause_amount` so the displayed frame becomes the resumed frame with zero visible jump.
  - `-2.0s ≤ drift < -0.2s` (lyrics BEHIND vocals): **MOVE-AHEAD** nudge of `min(|drift|, 2.0)` via `_smooth_offset("fine-tune-catchup")`. Higher cap than pause (`fine_tune_max_move_ahead_s = 2.0`) because a tiny forward skip is less perceptible than holding lyrics frozen. Asymmetric caps because pause >1s starts to feel like a bug, while a 2s forward skip is just a quick re-anchor.
  - `|drift| > 2.5s` (`fine_tune_exit_drift_s`): exit fine-tune, hand off to the regular tier-commit to handle the bigger correction.
- **Exit** triggers (call `_fine_exit(reason)` — drops all pause buffers, cancels listen, resets good-streak): big drift, 2 consecutive inconclusive listens, track change, force-sync activation, decide-by-ear switch, manual nudge/reset, manual /align by user.
- **Cannot decide both directions per tick** (a single drift measurement is either positive or negative).
- **"If still off-sync"**: fine-tune mode stays active across listen ticks; each ~8s tick decides anew based on a fresh drift measurement.

**Implementation (v1.0.85):**
- 7 fine_tune_* tune knobs (live-tunable via `/tune`)
- 9 new instance state fields (`_fine_active`, `_fine_good_t0`, `_fine_incon`, `_fine_listen_after`, `_fine_listen_pending`, `_fine_pause_until`, `_fine_pause_pos_eased`, `_fine_pause_pos_raw`, `_fine_pause_amount`)
- 5 new methods (`_maybe_enter_fine_tune`, `_fine_exit`, `_fine_listen_tick`, `_fine_pause`, `_apply_fine_listen`)
- 11 edit sites in main.py (init, _tune, _tick pause-override + pause-end, _note_sync_verdict entry trigger, _apply_tier_listen handoff, all the major-sync exit paths)
- `/diag` `sync` block exposes: `fine_active`, `fine_good_streak_s`, `fine_incon`, `fine_pause_remaining_s`, `fine_pause_amount`

**Adversarial verify caught + fixed before ship:**
- **HIGH same-tick race** between deferred-commit (TICKET-078) and pause-end: deferred-commit consumed `_pending_offset` first, then pause-end checked `if _pending_offset is None` and would have applied the subtraction against the freshly-committed offset. Fix: snapshot `had_pending_pre = self._pending_offset is not None` BEFORE the deferred-commit block; pause-end now guards on `not had_pending_pre and self._pending_offset is None`.
- **MEDIUM `_maybe_auto_align` and silent `_apply_align`** were still firing in parallel with fine-tune's own listen cadence. Both now `return` early when `_fine_active and reason != "mv-intro-onset"` (energy-align) / `silent and _fine_active` (apply_align). Fine-tune owns the cadence while it's active.
- **LOW**: added `/diag` fine_* surface, fixed a misleading "BEFORE the offset reset" comment in `_on_track_change` (the clears happen AFTER the offset = 0 line on purpose), added a missing log when `_fine_pause` is called with no media state.

**Live verified post-deploy:** all 8 knobs present in /tune, all 5 fine_* fields surface in /diag, app at v1.0.85.

---

## TICKET-084 — Rebrand "Desktop Karaoke" → "Lyric Immersion and Karaoke" (display strings) 🟢
**Symptom:** taskbar tray icon tooltip still read "Desktop Karaoke" (and other user-facing surfaces did too), even though the exe + product were renamed in v1.0.77. Discord-shared previews, the /health endpoint, the About / window-title, and the MSIX DisplayName all still showed the old name.

**Audit + replacement (v1.0.84 — rebrand display strings):**
- **15 DISPLAY-REPLACE edits** across 6 files; every internal data-path / cache slug / build artifact preserved:
  - `api.py:190 / 238` — `/health` `app` field (both endpoints)
  - `character.py:180` — artist-fallback theme key
  - `main.py:177` — Startup .lnk filename
  - `main.py:1161` — Tk window title
  - `main.py:6418, 6428, 6463, 6470, 6479, 6487, 6533, 6556` — pystray tray tooltip + all `icon.notify(..., "Lyric Immersion and Karaoke")` toast titles + update-checker hints
  - `playlist_import_gui.py:70` — Import Playlist window title
  - `packaging/AppxManifest.template.xml:20, 38` — MSIX `<DisplayName>` + VisualElements DisplayName
  - `packaging/AppxManifest.template.xml:35` — MSIX `Executable="Lyric-Immersion-and-Karaoke.exe"` (caught by adversarial verify, the audit had missed it — would have made the MSIX point at a file that no longer existed)
  - `packaging/build_msix.ps1:46` — `-SkipBuild` Test-Path now checks the renamed exe so the fast-path actually skips the rebuild (caught by verify too)
  - `version.py` → `1.0.84`
- **INTERNAL-PRESERVE** kept intact:
  - `<install-dir>\` deploy folder, `%LOCALAPPDATA%\DesktopKaraoke` data-dir (would orphan lyric cache + Whisper models if changed)
  - `DesktopKaraoke.spec` PyInstaller spec name + `dist/DesktopKaraoke/` build output dir
  - `<Application Id="DesktopKaraoke">` MSIX AppId (immutable identity — changing loses upgrade path)
  - mutex name, UA strings, pystray icon-name slug (`"desktop-karaoke"`), build scripts, all code comments / docstrings / historical doc references
- **Live verified:** `/health` returns `"app":"Lyric Immersion and Karaoke","version":"1.0.84"` immediately after deploy.

---

## TICKET-082c — Overlay below game/app layer (z-order topmost re-assert) 🟢
**Symptom:** the click-through lyric overlay was getting buried under a borderless-windowed game (or any other app that asserts topmost) after a focus change. Tk's `-topmost True` attribute is one-shot at window creation — nothing kept the overlay at top z-order over time. v1.0.82's `_click_guard` re-asserted click-through every 500 ms but not topmost.
**Fix (v1.0.83):** `_click_through` now also calls `SetWindowPos(hwnd, HWND_TOPMOST, …, SWP_NOMOVE|SWP_NOSIZE|SWP_NOACTIVATE)` on every guard tick — the exact pattern Discord/Steam/Nvidia overlays use, and a no-op when the window is already topmost so it's free to call at 500 ms cadence. Also added `WS_EX_TOPMOST` (0x00000008) to the EXSTYLE mask so the bit stays set. Mirror windows get the same treatment per-HWND inside the same loop.
**Caveat (documented):** exclusive-fullscreen DirectX games cannot be overlaid by any Win32 windowed app without DXGI hooks. Use borderless-fullscreen-windowed mode in the game settings. (Most modern games default to this anyway.)

---

## TICKET-082 — Karaoke fill decoupling + scroll-mode deferral + MV-intro fast-sync + in-app perf recorder 🟡 (082a landed; 082b open)
**Symptoms:**
- The currently-sung lyrics highlighting (karaoke fill) doesn't ramp smoothly across syllables — visible "stutter" or "race-then-snap" feel even on songs where sync is technically correct.
- Scroll-through modes (`tb`/`bt`/`lr`/`rl`) bypass the v1.0.78/81 `_smooth_offset` deferral entirely, so every offset write snaps in scroll mode.
- Studio MVs with long instrumental preambles (綺麗事 / Suisei: 33s of quiet before vocals) take too long to lock sync — the 25s track-start auto-align fires BEFORE vocals against silence and produces nothing.
- Heisenberg observer effect: polling `/diag` at 4 Hz (the diagnostic tool itself) dragged the render thread from baseline 33ms/frame to 60-200ms/frame, making the diagnostic worse than the bug.

**Fix (v1.0.82 — 082a):**
- **Karaoke fill on the RAW song clock** (not the eased display offset). `_tick` now computes BOTH `pos = state["position"] + self._eased_offset()` AND `pos_raw = state["position"] + self.offset`. `_ticker_update` / `_ticker_update_v` / `_karaoke` all take a new `pos_raw` argument and use it for the karaoke `frac` computation, while line POSITION (where the line is drawn on the belt) continues to use the eased `pos`. Result: lines glide smoothly into place visually, but the sung-vs-unsung highlight tracks the actual song clock — no more "fill races ahead during ease then snaps back."
- **Frac clamp to [0,1]** in both ticker renderers — the old `else 0.0` fallback briefly reset the karaoke fill to 0 whenever pos exited the line's window during ease. Now clamped, no more zero-flash.
- **Scroll-mode deferral coverage.** Removed the `if is_scroll: snap` bypass in `_smooth_offset`. Scroll modes now queue at the next line boundary too. Belt position still glides via `_eased_offset` regardless.
- **Wall-clock based ease** in `_eased_offset` instead of per-frame. Old: `(target - cur) * 0.2` capped at 0.10 s/frame. New: exponential pull `1 - exp(-pull * dt)` with absolute cap `rate_per_sec * dt`. Heavy frames no longer stretch the glide; a 1s drift finishes in ~0.5s regardless of FPS. Tune knobs: `ease_slew_cap_s` (default 3.0), `ease_pull_per_sec` (default 3.5).
- **MV-intro fast-sync** in `_on_vocal_onset`: when `_mv_mode` is true (studio MV, LRC shorter than video) AND vocals just calibrated the offset, schedule a fresh `_maybe_auto_align(reason="mv-intro-onset")` ~5s later. Locks precise sync before chorus 2 instead of waiting for the slow tier loop.
- **In-app perf recorder** — new tune knob `perf_record` (1 = on). When on, every `_tick` appends a line to `<install-dir>\perf.log` (rotated at `perf_record_cap_mb`=20 MB) with: timestamp, frame_ms, render-branch (line/scroll-h/scroll-v), pos_eased, pos_raw, offset, pending_offset, idx, ease_delta, and meta tags for OFFSET_JUMP / IDX transitions. Buffered append on the Tk thread itself = zero observer effect. This is what the user asked for: a diagnostic that doesn't interfere with what it observes.
- **Live trace already proved itself** — captured Tk-thread freezes of 3.3s, 5.8s, 6.2s during normal playback. Song-time keeps advancing while the render hangs (audio is on a separate thread). That's the user-visible stutter root cause beyond the fill ramp — Tk's event loop is sometimes blocked for seconds.

**Still open (082b):**
- The 100-500 ms spikes correlate with track changes (LRC load + first-line render) and `_consume_async` handling Shazam results. These need to be offloaded from the Tk thread (currently the load/render is synchronous). Next fix: move LRC parse + first-block PIL render to a worker thread, marshal the finished render back via `root.after(0, ...)`.
- /diag GIL isolation: the `/diag` handler reads live state under Python's GIL, which contends with the render thread. Should snapshot at end of each `_tick` into a deque, /diag reads the deque. Makes any future polling free.

---

## TICKET-081 — Title/artist weight rebalancing + cover-as-live + Shazam smooth-sync + lib MIN 60 🟢
**Symptoms (one big session of failures, all rooted in the same weight issues):**
- **GHOST/Suisei "halloween thing"**: title-match for `GHOST` picked `ghosting.json` (score 78 via substring-cover) over the correct `ghost.json`. Title-lock then suppressed Shazam for ~37 s until 5-strike override. The displayed "Ghost in your house, ghost in your arms" was actually `ghosting.json`'s English lyrics — not a literal halloween song.
- **「名前のない怪物」 / 「快晴」 by 音乃瀬奏 (covers)**: right LRC loaded (`名前のない怪物_egoist.json` / `快晴.json`), but the EGOIST original's timing didn't match the cover. `_on_vocal_onset` bailed via the `first_start > 8.0 → "LRC already has intro baked in"` early-return, even though measured vocals arrived 78 s LATER than the LRC's first_start. The cover wasn't classified as live_arrangement, so the FOLLOW path that would have absorbed the drift never engaged. Offset stayed at +0.00 the whole song while singing was 78 s behind.
- **Hand Sign / KizunaAI**: cached `hand_sign.json` was the wrong song's lyrics (poisoned cache); decide-by-ear scored loaded 20 — clearly wrong — but library best was `datte.json` at 33 (below library MIN=70) so the re-fetch path fired uselessly.
- **kamone**: TICKET-080 fixed the romaji↔CJK match, but library MIN=70 still made by-ear wins one-point misses.

**Fix (v1.0.81 — bundled because the failure modes share the same scoring weakness):**
- **`decide_library_min`: 70 → 60** (user-requested + matches the lopsided-win heuristic from TICKET-080).
- **`_score_form` substring-superset penalty (-12)**: when the CANDIDATE title is a strict superset of the QUERY (`ghost` ⊂ `ghosting`), penalize. The reverse direction (query is the superset, e.g. dropping `feat. X`) still scores full. Live-verified: `ghost`→`ghost.json` (was `ghosting.json`); `ghosting`→`ghosting.json`.
- **Artist corroboration bumped**: was `+5` exact match. Now `+12` exact, `+6` partial (one contains the other, length ratio ≥0.5). `'Suisei' ⊂ 'Hoshimachi Suisei'` and `'Suisei' ⊂ 'Suisei Channel'` (the latter via `clean_artist`'s existing channel-stripping at main.py:835) all corroborate.
- **Title-lock parenthetical equivalence in `_consume_async`**: `GHOST` and `Ghost (Still Still Stellar ver.)` are the same song. Strips `(…)` / `[…]` / `（…）` / `［…］` suffixes before comparing. Doesn't count as a strike when equivalent.
- **Title-lock artist-disagree doubles strikes (5 → 10)**: when SMTC artist clearly disagrees with the heard artist (and neither corroborates loaded artist), require twice the strikes before flipping the lock. Genuine same-artist mis-IDs (the feelingradation/SKAVLA case) still use the base 5.
- **Cover → live_arrangement** in `_on_track_change`: `_live_arrangement = is_live_arrangement(title) or self._is_cover`. Covers now use the FOLLOW path that absorbs the inevitable cover-vs-original timing drift.
- **`_on_vocal_onset` extended-intro fix**: when the LRC's first_start is >8 s AND the song is a cover/live AND measured vocals arrive >15 s LATER than that first_start, compute `offset = first_start - vpos` (negative, cap −300 s) and route through `_smooth_offset`. The 名前のない怪物 cover would have got `≈ −78 s` instead of the silent bail.
- **In-tick Shazam writes routed through `_smooth_offset`** (4 callsites previously snapping): live-FOLLOW (`sync(live)-follow`), ambiguous-reset (`sync-ambiguous-reset`), audio≈0 reset (`sync-audio0-reset`), confirmed (`sync-confirmed`). These are the high-frequency steady-state writes — the user-reported "mid-line jump" is almost always one of these four. Big-jump (>5 s) gate inside `_smooth_offset` preserves clean snap behavior for genuine song changes.
- **`_apply_decision` minimum heard-chars (20)**: a tie at 62/62 with only 11 transcribed chars (the cover case) now logs "inconclusive, no action" instead of silently confirming "in sync."
- **`_apply_decision` artist-disagree penalty (−8)** for library expansion: when SMTC artist is known and best library candidate's artist clearly disagrees AND we're not in cover mode, penalize best_score before MIN/MARGIN compare. Don't let a high-fuzz cross-artist transcript pull us off a correctly-loaded artist-confirmed song.
- **Cache: deleted poisoned `hand_sign.json`** so the next play re-fetches clean.

**Still open (deferred to TICKET-082 / TICKET-083):**
- **TICKET-082**: smooth-sync coverage gaps remaining (scroll-mode bypass; karaoke fill stuttering during glide because `frac=(pos-ln.start)/dur` uses eased pos against fixed ln.start; force-sync writes still snap). The big in-tick fix in v1.0.81 covered the worst offender; finer polish queued.
- **TICKET-083**: `ghost.json` and `ghost_still_still_stellar_ver.json` are byte-identical content — dedup via sha + alias.
- Feature stack (v1.0.82+): YouTube chapters + DOM via yt-dlp/`/nowplaying`, Demucs source separation (opt-in), librosa beat-anchor, AcoustID fingerprint.

---

## TICKET-080 — Romaji↔CJK title equivalence + lopsided decide-by-ear win + GPU picker by utilization 🟢
**Symptom (kamone took ~41 s):**
```
01:13:23 track change: 'kamone' / 'Kizuna AI' (dur None)
01:13:23 no confident title-match for 'kamone' (best 0); will use sound
01:14:04 decide-by-ear[library]: heard … → best かもね.json (69) vs loaded (20)
01:14:04 decide-by-ear: loaded doesn't match the singing (20) and no library song fits → re-fetching 'kamone'
```
Two compounding misses:
1. The cache has `かもね.json` (and a separate `kamone.json` provider variant) but a
   ROMAJI player title 'kamone' couldn't title-match a JP-script cached file → fell
   through to a 22 s wait for decide-by-ear, then 12 s for Whisper transcribe.
2. Whisper found the right song (`かもね.json` at 69 vs loaded 20 — 49-point margin)
   but the library-scope `MIN=70` rejected it by 1 point → triggered a slow re-fetch.

**Fix (v1.0.80):**
- **Hepburn romaji form on every JP-titled cache entry** (`_to_hepburn` via pykakasi).
  `かもね` is indexed under `kamone` too so a romanized YouTube title hits it.
  Stored in a SEPARATE `forms_alt` set so the matcher can prefer a same-script entry
  when both exist — `_title_forms_split` returns `(native, alt)` and `LyricsIndex.match`
  applies −3 when either side of the match used the cross-script bridge. Verified:
  `kamone` → `kamone.json`, `かもね` → `かもね.json`, `Kireigoto` → `kireigoto.json`,
  `綺麗事` → `綺麗事.json` (each picks its native-script entry when both cached).
- **Lopsided-win override in `_apply_decision`:** when `loaded_score < wrong_floor`
  AND `best_score >= MIN−10` AND `best − loaded >= 3·MARGIN`, accept even when below
  the library MIN. The kamone case (69 vs 20, MIN 70, MARGIN 12 → 3× = 36) qualifies
  by every condition — the strict gate was throwing away a clear net win for a slow
  re-fetch.
- **`pick_inference_device` is now utilization-based always**, not just under games.
  Drops the "game is on cuda:0" assumption (broken on some dual-GPU systems where
  the busy gaming GPU is not cuda:0; the old rule could put Whisper RIGHT on the
  game's card). Picks the idlest GPU with a small cache-locality bias toward cuda:0.
  Under a fullscreen game, any GPU at >=30% util is skipped; if all are busy → CPU.
  Verified live on dual-GPU hardware: cache-locality keeps cuda:0 when the spread is
  small, and the swap to the other GPU only happens once the utilization gap widens.

---

## TICKET-079 — Concert SMTC wrapper defeats song-ID 🟡 (a+c landed; b+d open)
**Symptom (VESPERBELL 3rd ONE-MAN LIVE BEYOND):** the app was alive for the entire
6-minute window inside the concert (00:50:37 → 00:56:34) without showing a single lyric.
The logs say it all:
```
00:50:37 track change: 'VESPERBELL 3rd ONE' / 'VESPERBELL' (dur None)
00:50:37 no confident title-match for 'VESPERBELL 3rd ONE' (best 0); will use sound
00:50:57 decide-by-ear (track-start): listening among 5 title candidates   ← stale candidates from prior song; result NEVER logged
00:51:03 audio boundary detected → re-identifying by sound
00:51:32 audio boundary detected → re-identifying by sound
00:54:14 audio boundary detected → re-identifying by sound
00:54:54 audio boundary detected → re-identifying by sound
00:54:58 same song re-reported ('VESPERBELL 3rd ONE') — keeping sync, no reset
```
ZERO `heard '...'` lines for the whole window. Four piled-up failures:
- **(1) SMTC truncated** `【冒頭無料】VESPERBELL 3rd ONE-MAN LIVE BEYOND #VESP3rdONEMAN`
  to `VESPERBELL 3rd ONE` — `is_live_arrangement` (`_LIVE_VER_RE` requires `one[\s-]?man`)
  missed it → `live_arrangement=false` → no live-mode aggressive resync, no follow-the-offset.
- **(2) Shazam never returned a hit** — expected for MMD/live performances (TICKET-072
  already documents this).
- **(3) `_decide_by_ear` bailed on `not self.lines`** — no LRC was loaded because the
  wrapper title matched nothing (best 0). The "5 title candidates" line is misleading
  — those were stale candidates and the function didn't reach the whole-library scan
  for a freshly-empty `self.lines`.
- **(4) Boundary detections were no-ops** — `_on_boundary` fires Shazam (which fails on
  live cuts) and that's all. Inside a concert wrapper that means every real song change
  inside the container goes un-identified, while the "same song re-reported" SMTC gate
  suppresses any wrapper-level reset.

**Fix (v1.0.79) — (a) + (c) landed:**
- **(a) Truncation-tolerant `_LIVE_VER_RE`:** adds `\d+(?:st|nd|rd|th)\s+(?:one|live|tour|anniv(?:ersary)?)`
  so "3rd ONE" / "5th LIVE" / "10th Anniversary" / "3rd Tour" all classify as live
  arrangements regardless of where SMTC chops the title. Also adds `【冒頭無料】` / `【無料配信】`
  live-broadcast banners and hashtag tells `#…ONEMAN` / `#3rdLIVE`. Smoke-tested: VESPERBELL
  truncated form → LIVE, normal song titles (feelingradation / white balance / `KAF #128`) → std.
- **(c) Boundary in a concert wrapper fires whole-library decide-by-ear:** `_on_boundary`
  now schedules `_decide_by_ear(reason="boundary")` ~12 s after Shazam when
  `_live_arrangement or _live_mode`. The gate in `_decide_by_ear` is opened for concert
  contexts: it no longer bails on `not self.lines` when we're inside a concert wrapper or
  boundary-triggered — the whole-library scan path (loaded_score < wrong_floor) takes over
  and adopts the best library match for the song actually playing inside the container.

**Still open (deferred):**
- **(b) Decide-by-ear MIN tuning for concerts** — the current 70 library threshold may be
  too high for a 12 s vocal sample at concert audio quality. Watch real runs.
- **(d) Concert setlist mode** — pull setlist.fm (or accept paste-in `MM:SS — Title` CSV)
  and pre-seed candidates by time window inside the concert wrapper. Highest value, biggest
  surface area; do as its own pass.

---

## TICKET-078 — Defer auto-sync corrections to the next line boundary (no mid-line snap) 🟢
**Symptom:** every auto-sync correction (energy-align, tier-listen, align-by-ear) wrote
`self.offset = X` immediately + `self.idx = -1`, so any line on screen jump-cut to a
different line mid-display whenever the sync moved by even ~0.5 s. The eased display
offset hides this for the karaoke FILL but not for the LINE selection — pos = position
+ eased crossed line boundaries during the glide, swapping lines under the user's eyes.
**User's ask:** *"i want it to fade into the sync, allow the current line onscreen to
finish even if its wrong and start the next line it thinks it is after the last wrong
line for better user experience."*
**Fix (v1.0.78):**
- New `_smooth_offset(new_off, reason)` — queues `_pending_offset` instead of writing
  `self.offset` directly when a line is currently visible and the jump is ≤ 5 s.
- `_tick` commits the queued offset only when `cur_pos >= current_line.end` (the wrong
  line has finished), then clears `idx = -1` so the very next tick picks the right next
  line under the new offset. 8 s safety cap commits a stuck pending regardless.
- Big jumps (>5 s), continuous-scroll modes (`lr/rl/tb/bt`, no discrete lines), and
  corrections taken when no line is showing (`idx<0`) all bypass and snap as before —
  deferring those would be worse than snapping.
- Routed through `_smooth_offset`: `_apply_align`, `_tier_commit`, `_apply_energy_align`.
- Untouched (delicate or already-staggered): the in-tick Shazam follow/confirm path
  at `main.py:~2320` (live-follow has its own two-point hesitation), `_apply_decision`
  resets, force-sync probes, vocal-onset calibration (fires while `idx==-1` anyway),
  track-change reset to 0.
- `_pending_offset` is cleared on every track change so a queued correction from the
  previous song can't bleed across.

---

## TICKET-077 — Reject the song when sync-by-ear keeps failing (poisoned cache) 🟢
**Symptom:** "Deep Dive" / 轟はじめ (ReGLOSS) showed **Dunk's** lyrics the whole time.
```
22:17:06 title-match 'Deep Dive' -> deep_dive_轟はじめ.json (score 85)
22:17:25 heard 'Deep Dive' / 'Todoroki Hajime' | loaded 'Deep Dive/轟はじめ' | match=True   (every read)
```
Both checks PASS — because they check the **name**. But `deep_dive_轟はじめ.json` is a
mislabeled `syncedlyrics` LRC whose LINES are Dunk's ("Game on, hearts racing again",
"踏み鳴らすMotion"). Same poisoned-cache class as [[TICKET-075]] (kamone) — title-match +
Shazam confirm the title; **nothing verified the lyric CONTENT against the singing**, so a
provider that mislabels song B's LRC as song A is trusted forever.
**Fix (the user's ask — "reject once sync fails a few times"):** the periodic sync-by-ear
(Whisper) transcribes the vocals and tries to ANCHOR them to the loaded lines. For the right
lyrics it anchors; for the wrong lyrics it returns NO anchor *every* time. So count
consecutive no-anchor reads (`_sync_fail_streak`, reset on any real anchor in
`_apply_tier_listen`/`_tier_commit`) and after `sync_reject_strikes` (3) → reject:
`report_wrong()` (bin the cache + unlock + re-identify) and, for a browser video, pull the
video's OWN captions (authoritative real lyrics, now fetchable via the v1.0.76 anti-bot).
Capped at 2 rejects/track so it can't loop. This is the CONTENT verification the name-checks
lack — the centerpiece of the queued intelligence work.
**Note:** relies on the periodic Whisper sync check running (it escalates when the energy
correlator goes blind, which a wrong-lyrics song does). The poisoned `deep_dive_轟はじめ.json`
was also purged.

## TICKET-076 — Cover of a famous song not detected: "Black Sheep" (Suko, cover of Metric) 🔴
**Symptom:** A Suko cover of Metric's "Black Sheep" (same lyrics, close to the original)
generated by ear instead of matching the well-known original. "Adjust the weights so the
confidence is high enough to run it."
**Log evidence (Spotify source):**
```
21:05:30 track change: 'Black Sheep' / 'Suko' (dur 259.9)          # Spotify authoritative title/artist
21:05:30 no confident title-match for 'Black Sheep' (best 0)       # not cached
21:06:14 no lyrics after the grace window (lookup came up empty) -> generating by ear
21:06:18 deep: download failed 'Black Sheep Suko': HTTP 403        # Spotify has no video to fetch — path is moot
# (no 'heard …' Shazam line at all → the fingerprint never bridged the cover)
```
**Why it happened:**
1. **Not cover-tagged + searched under the COVER artist.** "Black Sheep / Suko" has no
   `cover` marker, so `extract_cover_original` never fired; the lyric fetch ran as
   **"Black Sheep" / "Suko"** and came up empty. The original — **"Black Sheep" / Metric**
   — is trivially available from providers, but nothing ever searched Metric.
2. **No title-only fallback.** When the artist-qualified fetch is empty, the app doesn't
   retry **title-only**, which would surface Metric's original (whose lyrics the cover
   matches almost exactly).
3. **A fingerprint can't bridge a cover.** Shazam fingerprints the *recording*; Suko's
   cover is a different recording, so it IDs (at best) Suko's own track, not Metric's —
   hence no `heard …` line. The signal that COULD bridge it, **decide-by-ear lyric
   matching**, had nothing to match against because the original's lyrics aren't cached.
4. **Weights never let a same-title / different-artist candidate "run."** Even if a
   title-only fetch found Metric's "Black Sheep", the current confidence model doesn't
   trust a different-artist match enough to commit — so it defaults to generating. This
   is the weight the user wants raised.
5. *(side)* The **deep-download path is moot for Spotify** (no video to pull) yet still
   runs and 403s.
**Proposed fix:**
- **Title-only fallback fetch:** when `title / artist` returns nothing, retry **title-only**
  and take the most popular provider hit (the original), **gated by decide-by-ear
  verification** — does the heard vocal match the fetched lyrics? A cover "close to the
  original" passes easily, and the ear-verify stops a wrong same-title song from locking.
- **Adjust the confidence weights** so a title-only / different-artist candidate that the
  EAR confirms clears the run threshold (let a strong by-ear match override the missing
  artist match). This is the user's "make the confidence high enough to run it."
- **Skip the deep video-download path for non-YouTube (Spotify) sources** — it can't
  download and just 403s. Optionally read richer metadata via the spotipy Web API.
- Shares the root with the cover-ID reasons list (translingual/same-title covers) and
  [[TICKET-075]] (don't generate when a confident source is reachable).

## TICKET-075 — XOVERLINE (cached ReGLOSS song) generated by ear instead of playing it 🔴
**Symptom:** XOVERLINE / ReGLOSS — a common, already-CACHED song — showed AI-generated
`***` lines for ~30 s+ before the real lyrics appeared. "It shouldn't be hard to find"
— and it wasn't: `xoverline.json` was already on disk. The app just didn't use it.
**Log evidence (21:00, before the v1.0.72 redeploy):**
```
20:59:48 title-match 'XOVERLINE' -> xoverline.json (score 65)     # cache HIT, but low score
20:59:49 audio boundary detected -> re-identifying by sound        # diverted off the cache @ +1s
21:00:20 no lyrics after the grace window (lookup came up empty) -> generating by ear
21:00:25 deep: download failed 'XOVERLINE ReGLOSS': HTTP 403 Forbidden
21:00:39 deep: 11 lines transcribed (lang=jw)                      # mis-detected JAVANESE → garbage
21:03:01 decide-by-ear[title]: best xoverline.json (56) vs loaded (56)   # finally re-confirmed the cache
```
**Why it happened (the chain):**
1. **The cache existed but wasn't trusted.** `xoverline.json` title-matched at only
   **65** — under the confident-lock bar — so the song counted as "not confidently
   known" and the app went looking (sound/fetch/generate) instead of just playing it.
   (Score 65 because "XOVERLINE"/"ReGLOSS" doesn't score clean against the cached
   entry's stored title/artist — a generic-ish title + verbose-channel penalty.)
2. **Immediate audio-boundary false trigger.** 1 s after load it fired "re-identifying
   by sound," diverting off the provisional cache onto the sound path.
3. **ReGLOSS provider gap.** The sound-path lookup "came up empty" — the same blind
   spot as feelingradation: XOVERLINE's real LRC isn't found under "ReGLOSS" / the
   "hololive DEV_IS ReGLOSS" channel string.
4. **Grace window shorter than the lookup (~31 s).** With no confident lyrics in time,
   it fell back to **generating by ear**, so the `***` placeholder showed *before* the
   cache was eventually re-confirmed by decide-by-ear (56 vs 56).
5. **Deep fallback also failed:** yt-dlp source download = **403 Forbidden** (YouTube
   block), and the by-ear deep transcribe **mis-detected the language as Javanese
   (`jw`)**, yielding 11 garbage lines.
**Proposed fix:**
- **Play a cached exact-title hit immediately**, even at a modest score — if
  `xoverline.json` is cached and the title matches, show it and verify-and-switch in
  the background. Never *generate* a song that's already cached.
- **Suppress audio-boundary re-ID for the first ~2 s** after a fresh title-match load.
- **Fix the ReGLOSS provider query** (search "ReGLOSS" + bare title, drop the verbose
  "hololive DEV_IS" prefix — the feelingradation lesson) and/or seed common ReGLOSS songs.
- **Pin transcription language from `language_confidence`** (ReGLOSS = JA) so deep
  transcribe can't land on Javanese.
- **Hold the grace window open** while a cache or provider hit is still in flight, so
  generation doesn't start prematurely. Relates to the cover-ID reasons list (#1, #3).

## TICKET-074 — Force Sync: try ranked match candidates, skip chorus traps 🟢
**Ask:** Force Sync wasn't working well — make it try several methods; if the
highest-probability match fails to KEEP matching, try the second-highest, to avoid
"chorus traps" where the lyrics lock onto a repeated phrase then run past the spot.
**Cause:** Force Sync committed to a SINGLE best anchor (`capture_and_align` →
`_best_anchor`). A chorus hook recurs at several timestamps, so the one chosen was
often the wrong occurrence; the old "two reads agree within 1s" check could even
*confirm* a wrong occurrence while the chorus repeated, or ping-pong forever.
**Fix (multi-hypothesis + forward verify):**
- `align._rank_anchors` (new) ranks EVERY (segment, line) pair, not just the best;
  `align.rank_offsets` (new) turns the top matches into a deduped, ranked list of
  candidate OFFSETs — a recurring chorus yields one per occurrence (verified by a
  unit test: hook at 45/120/200 s → offsets +0/−80/−155, incl. the true one).
- `_force_sync_apply` is now a small state machine: try the best candidate, then
  FORWARD-VERIFY it against each fresh read (does the offset still predict what's
  sung *now*?). A candidate that keeps matching across reads spanning ≥
  `force_sync_span_s` (16 s, so it can't lock inside one chorus pass) and ≥
  `force_sync_streak` (3) confirms → **locks**. One that stops lining up is
  **blacklisted** and the next-best candidate is tried (freshest read = the song's
  current spot). A single noisy read gets a 1-read grace before blacklisting.
- New tune knobs: `force_sync_span_s`, `force_sync_top_n`; `force_sync_streak`/
  `force_sync_agree_s` repurposed for the confirm machine. Tray label + `/forcesync`
  help updated. Background auto-sync (`capture_and_align`) is unchanged.
**Verified:** built v1.0.70, deployed, `/forcesync` engages with the new logic
(log: "try ranked matches until one holds 3× over 16s"); track-change cancels it;
unit test confirms multi-candidate generation incl. the true offset.

## TICKET-073 — Add waveform analysis to the sync + matching algorithms 🟢
**Ask:** use waveform analysis (not just the transcript) in syncing and song matching.
**Already there:** vocal-band energy (FFT) + **spectral flatness** + vocal-onset detection
+ baseline (`songchange.py`) already power the energy-correlation sync and the
song-change/applause detection.
**Added (fusion):** (1) **waveform-gated listening** — the periodic Whisper listens
(live-resync, the tier) only transcribe when the vocal-band energy says singing is
happening NOW (`_vocals_active_now`), so a clip is never an instrumental break (cleaner
transcript → better sync AND by-ear match). (2) **waveform-pinned offset** — after
`_decide_by_ear` identifies the song by its lyrics (the *what*), the energy correlation
pins the precise OFFSET (the *when*). **Scope note:** local audio FINGERPRINTING for
matching is NOT added — it needs reference audio we don't store, and a cover/MMD differs
from the original anyway; Shazam covers online fingerprinting, the lyric-match stays primary.

## TICKET-072 — Live/concert versions: resync by ear ~5×/min 🟢
**Ask:** a 【LIVE MV】 / ONE-MAN LIVE cut should expect resyncs + odd pauses; have live
versions lyric-match ~5×/min.
**Cause:** live cuts DO register (`is_live_arrangement`) but only polled Shazam, which
can't fingerprint the (usually MMD) performance — so tempo shifts + applause pauses
drifted the timing with no recovery.
**Fix:** `_live_resync_loop` — for a registered live arrangement/concert with lyrics
loaded, transcribe + match to the loaded lyrics ~5×/min (`live_resync_s=12s`), FOLLOWING
the measured live offset. Waveform-gated (only when vocals are active) so it doesn't
waste a transcription on an instrumental/applause gap.

## TICKET-071 — Smart song decision by ear (Whisper 'small' + rapidfuzz) 🟢
**Ask:** a small (~250 MB) model that makes smart decisions about WHICH song's lyrics to
show — the title/Shazam signals keep failing on MMD/cover/performance videos and
mislabeled provider LRCs.
**Researched:** Whisper does SOTA zero-shot lyric transcription with no fine-tuning
(LyricWhiz, ISMIR'23); rapidfuzz partial/token-ratio is the recommended transcript matcher.
Neural audio fingerprinting needs a reference DB of the exact tracks (useless for MMD
covers); CLAP embeddings are ~600 MB and match audio→text descriptions, not exact songs.
So: **faster-whisper 'small' (~250 MB int8, already bundled) + rapidfuzz**.
**Fix:** `align.transcribe_vocals` (small model, ~12 s) + `align.score_candidates`
(rapidfuzz `partial_ratio` char-level → works for Japanese, + `token_set_ratio`).
`_decide_by_ear` runs ~20 s into a track (and via `POST /decide`) in TWO stages: score the
loaded cache + title-similar caches first; **if the loaded song matches the singing below
`decide_wrong_floor`, identify against the WHOLE cached library** ("trained on everything we
have" — score the one transcript against every cached song, the right one self-matches ~100
vs ~30, must clear the higher `decide_library_min`). Switches to a clear winner, else
re-fetches by title (cover-qualified). Verified: 快晴 chunk → #1 of **833 songs** at 100 vs
~30; feelingradation → 100 vs 13-28. Skips baked/caption/live songs. `/diag.decision`.

## TICKET-070 — ReGLOSS songs always wrong (feelingradation, サクラミラージュ) → baked in 🟢
**Symptom:** "feelingradation" and "サクラミラージュ Performance Video" (hololive DEV_IS
ReGLOSS) were always wrong — feelingradation fell back to a poor Whisper transcription;
サクラミラージュ loaded a totally unrelated song ("Daybreak Frontline", then "Mumei").
**Cause (two layers):** (1) the app searches under the verbose channel "hololive DEV_IS
ReGLOSS", which every provider misses (the real LRCs are under "ReGLOSS"). (2) These
MMD/"Performance Video" cuts can't be Shazam-fingerprinted, so Shazam keeps mis-ID'ing
them as random other tracks and SOUND OVERRODE the title, loading the wrong lyrics.
**Fix:** (1) a `bundled_lyrics/` dir SHIPS with the app (PyInstaller datas); at startup
`_seed_bundled_lyrics()` copies it into the runtime cache over a weaker cache. Both songs
are baked in (`source: bundled`, full furigana/romaji/translation). (2) a baked cache is
now **authoritative**: the heard-handling ignores a contradicting Shazam read for a
`source: bundled` song (no switch, no strikes) so a mis-ID can't override ground truth.
Any always-failing song can be added the same way (generate via the providers' working
search term, drop the JSON in `bundled_lyrics/`).

## TICKET-069 — "Cinematic intro" shown while the song is already singing 🟢
**Symptom:** the "🎬 Cinematic intro — waiting for vocals…" card stuck on screen for a
cover (RIDE ON TIME) that clearly had vocals from early on.
**Cause:** `_vocals_active_now` was too strict (vocal-band ≥ baseline×1.5 across 60% of
the window) and missed real singing on backing-heavy mixes, and the backstop was 75 s —
long enough to hold most of a song. **Fix:** loosened the detector (×1.3 / 50% / min 2)
so present vocals release the hold, and dropped `mv_intro_timeout` 75 → 20 s so a false
hold can never sit through more than 20 s of vocals.

## TICKET-068 — Wrong song never recovers when title-locked (Deep Dive→Dunk) 🟢
**Symptom:** the overlay shows the WRONG song's lyrics and no amount of re-syncing fixes
it — e.g. playing "Dunk" lyrics over the "Deep Dive" video.
**Cause:** when `_title_locked`, the app IGNORED Shazam hearing a different song forever
(to resist same-artist mis-IDs), so a genuinely wrong title-lock never self-corrected.
**Fix (user's rule):** count strikes — hearing the SAME other song N× (`wrong_song_strikes`,
default **5**) means the loaded song is wrong, so BREAK the title-lock and switch to what
we hear (load cache or fetch). Strikes reset when the heard song matches the loaded one or
on track change; a different heard song restarts the count (spurious single mis-IDs can't trip it).

## TICKET-067 — Out-of-context single-line translation + Suisei mis-language 🟢
**Ask:** translate each line WITH its ±2 neighbours for context, not in isolation; and stop
getting Suisei songs in the wrong language.
**Fix:** translation already used ±2-line windows, but when the translator merged/split
lines the bare newline-join misaligned and dropped the WHOLE window to context-free
per-line translation. `_translate_window` now uses a **numbered protocol** ("1. …\n2. …")
that survives merges/splits/reorders, so context is preserved; a missed line retries in a
small numbered ±2 window before any isolated fallback. Confidence: `_ALWAYS_JA` (Suisei,
Hoshimachi, Hoshimatic) + known romanized JP acts now score **full Japanese (certainty
1.0)**, not a weak partial — a romanized JP channel still beats an English same-title collision.

## TICKET-066 — Stutter on Shazam-unconfirmable songs (MMD/cover/performance) 🟢
**Symptom:** on songs Shazam can't fingerprint (an MMD "Performance Video", a cover, a
live arrangement) the overlay stutters — `/diag.fps.worst_ms` spikes to 150-475 ms,
render drops to ~8 fps in bursts — and it never settles. Lyrics ARE loaded and in sync
(`drift ≈ 0`); the jank is purely the render hitching.
**Cause:** the recal loop treats the track as `unconfirmed` forever (Shazam never
matches the arrangement) and so polls `recognize_playing` every ~4 s indefinitely. Each
recognize stalls the Tk render thread via **GIL contention** (the fingerprint compute) —
caught live: `worst_ms` spikes track `identifying=True`, not the new Whisper tier.
**Fix:** an **anti-stutter back-off** in `_recalibrate_loop`. (1) A settled-but-
unconfirmable track (lyrics loaded, `|offset| ≤ 1 s`, ~45 s elapsed, no sound lock,
not `live_mode`) backs the Shazam poll off to `unconfirmed_backoff_s` (30 s) instead of
4 s. (2) A CONFIRMED + boundary-watched track relaxes its Shazam re-lock to
`confirmed_recal_s` (45 s, was 25 s) — drift is now re-locked by the adaptive sync tier
and song changes by the boundary detector, so the frequent recognize was redundant.
Measured: `identifying=True` went from near-constant (stall every ~4 s) to ~20% of
samples. Concerts (`live_mode`) keep polling. Remaining: the occasional ~25-45 s spike is
shazamio's LOCAL fingerprint holding the GIL during `recognize` — the deeper fix is to
run `recognize_playing` in a separate PROCESS (no shared GIL), deferred (risky in a
frozen PyInstaller build). This is the same escalation/de-escalation the user asked for,
applied to the Shazam loop.

## TICKET-065 — Adaptive escalation/de-escalation sync-verification tier 🟢
**Ask:** sample sound-matching more often while syncing — verify with TPVR ≥3×/min;
once a check succeeds drop to 1×/min; any failure resyncs and snaps back to 3×/min,
staying fast while failures continue.
**Fix:** `_periodic_auto_align` is now an **adaptive heartbeat**. `_sync_tier_interval`
starts at **20 s (3×/min)**; each check yields a verdict via `_note_sync_verdict`:
`insync` steps the cadence down (40 s → 60 s after two good checks), `corrected` snaps
back to 20 s and stays fast while misses continue, `inconclusive` holds. The cheap
energy correlation gives the verdict when it reads a clear peak; when it's **blind on a
song** (flat/ambiguous — the off-vocal ReGLOSS 'サクラミラージュ' case, `_energy_blind`)
the tier escalates to a short **Whisper listen** (`_tier_listen_now`, 6 s capture),
**two-point verified** for any large jump before it can move sync. A song we genuinely
**can't read** (Whisper keeps returning `inconclusive`) is NOT hammered at 3×/min — two
blind checks **back the cadence off** one notch toward 1×/min, so a futile check can't
cost a stutter (only a detected MISS keeps it fast). Whisper CPU is capped
(`cpu_threads=4` in `align.py`) so the transcribe can't stutter the overlay. Knobs:
`sync_tier_fast_s` / `_mid_s` / `_slow_s` / `_ok_drift` / `_listen_s`. Telemetry in
`/diag.sync`: `tier_interval_s`, `tier_good_streak`, `tier_miss_streak`,
`tier_energy_blind`, `tier_listening`.

## TICKET-064 — Cover videos (【Cover MV】) searched by the cover channel 🟢
**Symptom:** "【Cover MV】MAFIA / マフィア - Ouro Kronii" loaded the wrong/no lyrics and
took ages — the cover went undetected, so the search used the COVER CHANNEL ("Ouro
Kronii Ch. hololive-EN"), which has no lyrics listed for that song.
**Cause:** `_COVER_RE` matched `(cover)` / `[cover]` / `/cover` but **not** the
lenticular/fullwidth bracket tags VTuber covers use — `【Cover MV】`, `（Cover MV）`,
`［Cover］`. Also `extract_cover_original` was handed the *cleaned* artist
("hololive-EN"), so " - Ouro Kronii" looked like the ORIGINAL artist.
**Fix:** `_COVER_RE` now catches a `cover` tag after any common opening bracket;
`extract_cover_original` strips the bracketed tag, is passed the **raw** channel, and
keeps the song when the tail is the cover channel. On a cover with no parseable original
artist, `_on_track_change` **drops the channel** (`fetch_artist=""`) and searches by
title alone (the original's lyrics fit the cover) — never re-introducing the channel.
This also fixes the "taking too long to detect" complaint for covers (title-first
resolves instantly instead of failing through to sound).

## TICKET-063 — Long concert videos: weak song detection between songs 🟡
**Ask:** in a 1h+ concert, combine OCR (banner) + audio (Shazam) + the applause
detector + a 2-6 min duration heuristic to detect & switch songs and sync smoothly.
**Done:** in `live_mode`, an **applause gap is treated as a song BOUNDARY** —
`_check_applause_gap` re-identifies (Shazam + a forced OCR banner read) the NEXT song
instead of resyncing the old one. Plus a **2-6 min heuristic**: a concert song still
showing after ~6.5 min forces a re-identify (caught a missed transition).
**Open:** transcription-based song-ID (transcribe the singing → match against the whole
lyric LIBRARY to pick the song) as the last fallback when OCR + Shazam both miss.

## TICKET-062 — Language-confidence score (artist's usual language) 🟢
**Ask:** weight the artist's usual language so a Japanese act's English-titled song
isn't matched to an English same-title collision (Suisei's "GHOST" → English "Ghost";
ReGLOSS "feelingradation" must read Japanese). As a percentage with other factors.
**Fix:** `confidence.language_confidence(title, artist)` → {ja,en,zh,ko,certainty}.
Strong cue = the artist NAME script (kana→JA, hangul→KO) + a `_KNOWN_JA` reference for
romanized acts (hololive/ReGLOSS/V.W.P/Reol…). When certainty is high and CJK clearly
beats EN, `fetch_lrc.take()` rejects an English body as a collision; `_file_valid`
self-heals a cached English body for a kana-named artist. Neutral (certainty 0) for
plain romanized names so DEADPOOL/Suisei is unaffected. Measured: GHOST/星街すいせい
75% JA, feelingradation/ReGLOSS 71% JA, DEADPOOL/Suisei-Hoshimachi 0 certainty.

## TICKET-061 — Concert applause/cheering pause drifts the lyrics 🟢
**Symptom:** in a LIVE/concert cut the song pauses for applause & cheering; the player
clock keeps running, so the lyrics scroll ahead and stay desynced after the music
resumes.
**Fix:** `_check_applause_gap` (polled ~3×/s) watches the live audio in a live cut for
a sustained **loud-but-non-vocal** stretch (broadband cheering — high spectral
flatness, no tonal singing). When singing returns it fires a **Whisper
transcribe-and-match resync gated by TWO-POINT verification**: align by ear, HOLD the
offset, confirm with a 2nd listen ~2.5 s later, apply only if the two agree. Tunable
via `/tune applause_min_s`.

## TICKET-060 — Kanji song matched Korean lyrics (花譜 邂逅 → "Chance meeting") 🟢
**Fix:** kanji (Han) is JA/ZH, never modern Korean (hangul) — so a kanji title/artist
rejects a Korean lyric body at fetch AND on cache load (self-healing). Suppressed when
the title/artist itself carries hangul.

## TICKET-059 — Auto-captions = wrong/excess lyrics + [音楽] tags 🟢
**Symptom:** lyrics "close but wrong" (鉄後/ラッキラ mis-hearings), full of `[音楽]` /
`[ongaku]` tags, and an "excess" wall of duplicated text — on ReGLOSS MVs.
**Root cause:** `fetch_captions_only` had `writeautomaticsub: True`, so it used
YouTube's AUTO (ASR) caption track. ASR is inaccurate, inserts sound-event tags, and
ROLLS (each word repeats across overlapping cues) → duplicated lines. v1.0.25
(commit 616a70c) used MANUAL captions only.
**Fix:** back to **manual captions only** (`writeautomaticsub: False`); a song with no
manual track falls through to the provider LRC (cleaner) instead of bad ASR. Also
strip `[...]`/`【...】`/♪ sound tags in `_parse_vtt`. Purged the 48 cached
auto-caption songs so they re-fetch correctly.

## TICKET-058 — Karaoke fill (yellow highlight) jumped on sync correction 🟢
**Symptom:** the sung-word highlight was "a bit off" and SKIPPED to a new place when
sound-sync corrected the offset — poor UX.
**Fix:** render against an **eased display offset** (`_eased_offset`) that glides
toward the sound-sync target (Shazam offset + energy correlation) at ~20%/frame
(capped 0.10 s/frame) instead of snapping, so the highlight + scroll slide smoothly
into a correction. A major re-sync (>5 s, e.g. a song change) still snaps. The match
target is unchanged (still heard-audio driven) — only its APPLICATION is now smooth.

## TICKET-057 — MV intro hold never released → lyrics never started 🟢
**Symptom:** a music video (V.W.P 電脳) sat on "Instrumental intro — waiting for
vocals…" for the WHOLE song; lyrics never appeared even after singing clearly began.
**Root cause:** the MV intro hold only released on (a) the one-shot
`_fire_vocal_event` (a detector-thread band-energy-rise event that can simply never
fire), (b) Shazam setting `_sound_song` (fails when the song isn't fingerprinted),
or (c) a **100 s** hard timeout. With (a) and (b) both silent, the user faced a
~100 s stall — effectively "never started".
**Fix (this build):**
  - **Vocal-energy POLL** (`_vocals_active_now`) inside the hold: release the
    instant the live vocal-band ratio stays clearly above the learned instrumental
    baseline for ~1.2 s. Reuses the always-on vocal buffer the sync correlator keeps,
    so it doesn't depend on the flaky one-shot event. Calls `_on_vocal_onset` to
    calibrate the offset, then anchors.
  - Timeout backstop made tunable (`mv_intro_timeout`, default 75 s).
**Also (per request):** the MV hold card now reads **"🎬 Cinematic intro — waiting
for vocals…"** (a music video's lead-in is cinematic dead-space, often dialogue) —
distinct from a plain audio "Instrumental intro".

## TICKET-055 — Wrong song: Ludacris "The Potion" shown for Michiru Shisui's "Potion" 🟢
**Symptom:** Spotify playing **Potion — Michiru Shisui** (a Japanese Phase-Connect
VTuber's debut original). Overlay showed **English gangsta-rap** lines — "What up aye
shawty what it is", "Lil' buddy what you want? Some violent shit", "Tell yo' momma
I'm a ghet-to su-per-star". Those are **Ludacris — "The Potion"**, a different song.
**Diagnosis (via API):** `/lyricstate` meta = `source: "syncedlyrics/cover"`,
`lang: "es"` (Spanish tag on English text = garbage). `/diag` energy_align
`ambiguous, lift 0.02, rival 0.673 > best 0.592`; `offset_history` thrashing
0→-15→-27→0 — the words never line up with the audio.
**Root cause:** two compounding bugs.
  1. The two "Potion"s are **both 3:43** (223 s). That rare duration coincidence
     beat **every** duration gate (`verify_lrc`, `_strict_ok`, `validate_file`).
  2. The cache file `potion.json` was written by the **weak title-only "cover"
     fallback** in an older build. Today's code fetches this clean Spotify track
     with `cover=False, strict=True`, which already SKIPS both weak paths — but the
     app **trusted the stale cache forever** without re-checking its provenance.
The kana/hangul language guards don't fire (Latin title "Potion", romaji artist).
**Fix (this build):**
  - **Provenance guard** (`main.py:_file_valid`): a cache whose `meta.source` is
    `syncedlyrics/cover` or `syncedlyrics/title` is REJECTED for a clean,
    non-cover source → re-fetch under strict rules (returns the right song or
    nothing, never the wrong one). Duration-independent.
  - **Artist cross-check** (`fetch_lyrics._lrc_artist_conflict`): parse the LRC's
    own `[ar:]` tag; reject a cover-fallback hit whose tagged artist is a different
    script or shares no token with the requested artist (`[ar:Ludacris]` ≠ Michiru
    Shisui). Conservative — a missing/near-match tag never rejects.
  - Purged the stale `potion.json`.
**Defense principle:** when we have an AUTHORITATIVE artist (clean source, not a
cover), never trust a bare-title provider match — no matter how well the duration
matches. Sound/energy can't save a rap song (continuous vocals → flat energy mask).

## TICKET-056 — TPVR commits the wrong chorus on "All The Things She Said" 🟡
**Symptom:** two-point sound-verification "constantly gets the wrong chorus then
runs with it." A repeated chorus is acoustically identical each time, so read 1 and
the confirming read 2 can both match a chorus instance and report the SAME offset,
falsely "agreeing" → a wrong offset commits and sticks.
**Change (this build):** the hesitation-before-confirm and the confirming-listen
length are now **/tune knobs** (`sync_confirm_hold_ms` default 2600,
`sync_confirm_listen_s` default 5.0) so the timing can be dialled in live without a
rebuild. A LONGER hold separates the two reads by more song time, so two different
instances of one chorus are less likely to both read at the same offset.
**Open:** live-test a few songs (esp. "All The Things She Said") to find the
hold/listen pair that hits **≥80 %** correct-commit. Candidate further work: require
the two reads' implied song-position to be self-consistent with the player-clock
advance (a true lock advances by exactly Δt; a re-matched chorus does not).

## TICKET-001 — Dance/play covers generate instead of fetching real lyrics 🟢
**Symptom:** "Breaking Dimensions を踊ってみた", hololive covers, sit on "Generating…"
and never load real lyrics (slow, spotty, wrong language).
**Root cause:** `is_cover_title`/`clean_title` only knew 歌ってみた/(cover); they missed
踊ってみた (dance), 演奏してみた, 弾いてみた, 叩いてみた → no title-first fetch → fell to
generation.
**Fix (pushed 87a6de9):** added those markers to `_COVER_RE` + the `clean_title` strip
(handles the を particle). `fetch_lrc('Breaking Dimensions', cover=True)` → 70 lines.

## TICKET-002 — Same-title collision: wrong song's lyrics loaded for a common title 🟡
**Symptom (cover, FIXED):** 【歌ってみた】地球儀 covered by 花譜 — the video plays Kenshi
Yonezu's 地球儀 (僕が生まれた日の空は…, the *Boy and the Heron* theme) but the overlay
showed an **unrelated** 地球儀 (愛に飢えている / こんな夜に流されあっている / 揺られちまえよ).
Proved: those overlay lines are lines 6-8 of the *wrong* 地球儀, so it was a wrong-title
**fetch**, not generation.
**Root cause (cover):** the COVER fast-path queried by TITLE only, skipped the
`_strict_ok` guard, and ran **before** the artist-keyed queries — so for a super-common
title it short-circuited onto the first same-title hit. Yet `fetch_lrc('地球儀','花譜')`
artist-keyed *already* resolves Yonezu's 地球儀 (the song actually covered); the cover
path was overriding a correct result with a wrong one.
**Fix (pushed 0eeb696):** reordered `fetch_lrc` so the **artist-keyed queries run first**
and the title-only cover path is the **FALLBACK** — only for true 歌ってみた uploads where
the channel-as-artist genuinely derails search (TICKET-001). Verified: 地球儀/花譜 cover
now returns 僕が生まれた日の空は; Breaking Dimensions dance cover still fetches (no regression).
**Research:** lyric finders stress **verifying the artist + not trusting the first
match** — "compare the artist… before you save it, the first match is not always the
right version." ([Musely](https://musely.ai/tools/lyrics-finder), [Chosic](https://www.chosic.com/find-song-by-lyrics/))
**Still open (non-cover + no-artist edge):** the original "BANCHO / 轟はじめ" report was a
*non-cover* same-title collision (relies on `_strict_ok` in the title-only last resort);
and a bare-title cover with **no artist at all** can't disambiguate (returns the first
同名 song). Both want the **duration cross-check** against a trusted duration (player
duration arrives a few s late, or the master-tracks library DB, TICKET-009). → kept 🟡.

## TICKET-003 — Desync: correct lyrics, wrong timing 🔴
**Symptom:** "Deep Dive / 轟はじめ" matched the right lyrics but the displayed line was
far off the video's burned-in line. Likely behind several "wrong song" reports.
**Research:** the field uses **forced alignment** — separate vocals, recognize the
singing, align phonetic units (Viterbi/HMM). ([AutoLyrixAlign/MIREX](https://music-ir.org/mirex/wiki/2024:Lyrics-to-Audio_Alignment),
[lyrics-sync](https://github.com/mikezzb/lyrics-sync), [real-time chroma+phonetic](https://laurenceyoon.github.io/real-time-lyrics-alignment/))
**Plan:** the app's "Sync by listening" is forced-alignment-lite (Whisper transcribe →
fuzzy-match heard words to lyric lines → offset). Run it **automatically** right after a
fetch on MV/cover titles (where catalog offset ≠ video), and re-anchor on a confident
hit. Vocal separation (Demucs) is the heavier upgrade.

## TICKET-004 — Identification too slow ("identifying…" for a long time) 🔴
**Symptom:** long delay before lyrics appear, especially covers.
**Research:** faster-whisper chunk tuning; Shazam capture length. ([faster-whisper](https://github.com/SYSTRAN/faster-whisper))
**Plan:** TICKET-001 makes covers fetch by title immediately. Also: try the
master-tracks DB **locally first** (instant, no network), and shorten the first Shazam
capture. Overlaps TICKET-010.

## TICKET-005 — Spotty / intermittent generation ("pieces then blank") 🔴
**Symptom:** generated lyrics appear, go blank, reappear — big gaps.
**Research:** for streaming, **`condition_on_previous_text=False`** is recommended (True
"causes the model to condition on potentially incorrect previous hypotheses"). Smaller
chunks + overlap reduce latency/gaps; **RMS-VAD** segmentation cuts hallucinations
without dropping vocals. ([saytowords](https://www.saytowords.com/blogs/Real-Time-Streaming-with-Whisper/),
[arXiv ALT](https://arxiv.org/html/2506.15514v1))
**Plan:** test `condition_on_previous_text=False` for generation; add RMS-VAD; overlap
chunks so boundaries don't drop words. (Most of these songs should MATCH after
TICKET-001, avoiding generation entirely.)

## TICKET-006 — Box / "tofu" characters in the overlay 🔵
**Symptom:** some lines show □ boxes.
**Finding:** a scan of cached lyrics found **no** corrupt chars in the DATA → it's a
**font glyph-coverage** issue in the Tk renderer (missing glyphs → .notdef tofu).
**Research:** "tofu = font lacks a glyph and no fallback." Fix = a font with full CJK +
symbol coverage and a fallback chain. ([SimpleLocalize](https://simplelocalize.io/blog/posts/tofu-symbol/),
[SymbolFYI](https://symbolfyi.com/guides/tofu-missing-glyphs/))
**Plan:** set the overlay font to a verified full-coverage CJK face (Meiryo / Yu Gothic
UI / MS Gothic) and add a per-glyph fallback. Need the exact line that shows boxes to
confirm which glyphs are missing (symbols? half-width katakana? rare kanji?).

## TICKET-007 — Sync precision (general) 🔴
**Symptom:** request for "greater precision in lyric syncing."
**Research:** word-level "enhanced LRC" exists but free providers return line-level only;
real-time alignment uses chroma + phonetic features. ([EasyLRC enhanced LRC](https://easylrc.com/blog/enhanced-lrc-word-level-timing-guide-2026),
[real-time alignment](https://laurenceyoon.github.io/real-time-lyrics-alignment/))
**Plan:** tighten the Shazam offset recal cadence on unstable songs; interpolate
word-fill across each line (already partial); overlaps TICKET-003.

## TICKET-008 — Multi-monitor: move / scroll-across / mirror 🟡 (feature)
**Request:** move the overlay to a chosen display; **scroll lyrics continuously ACROSS
all** displays; **mirror** the same lyrics on every display.
**Done (built):** `_monitors()` via Win32 `EnumDisplayMonitors` (no new dep — `screeninfo`
would need PyInstaller bundling); tray **"Display"** submenu (each screen + "Scroll across
ALL screens"); `set_display('primary' | 'mon:N' | 'span')` repositions the
W-parameterized band (span = one band over the whole virtual desktop, so lyrics scroll
through every screen). The `primary` default is **unchanged** (verified safe on 1 display).
**Remaining:** **MIRROR** (same lyrics on every screen at once) needs one Toplevel+canvas
per monitor sharing the render — a render-target refactor. The menu is built once at
startup, so hot-plugging a display needs a restart to refresh.
**Validation:** all multi-screen modes need a **2nd display connected** to test (only 1
attached now: 1920×1080). Research: [wikiPython](https://www.wikipython.com/tkinter-ttk-tix/gui-demos/a-tkinter-multi-screen-strategy-demo/),
[PySimpleGUI](https://docs.pysimplegui.com/en/latest/cookbook/original/multi_monitor/).
**Status:** 🟡 move + scroll-across shipped; mirror + live validation pending a 2nd screen.

## TICKET-009 — Use the master-tracks library DB for matching/verification 🔴 (feature)
**Idea:** `Music-Migrator/data/master_tracks.json` (ISRC → track/artist/album/
duration_ms) is the user's real library. Fuzzy-match messy YouTube titles → clean
(artist, title, duration) for accurate fetch + same-title disambiguation (TICKET-002).
**Research:** duration + artist verification is the standard guard. ([Musely](https://musely.ai/tools/lyrics-finder))
**Plan:** load the DB once; normalized-title index; on track-change, fuzzy-match → if a
confident hit, use its (artist, title, duration) to fetch + verify. (CSV obtained.)

## TICKET-010 — Generate-vs-fetch race (covers still flash "Generating…") 🔴
**Plan:** the generate-defer (commit 71fdc2c) waits while a lookup is in flight; extend
so a cover/title fetch (TICKET-001) always pre-empts the 11s generate deadline, so
findable covers never flash AI text.

## TICKET-011 — Performance (render FPS / GPU / CPU) 🟡
**Status:** GPU now used for Whisper (cuBLAS/cuDNN fix, commit 142512a); render is
throttled (PIL repaint budget). FPS shows N/A in the browser HUD (that's the page, not
us). **Plan:** profile the Tk frame time (`/status` render_fps) on heavy songs.

## TICKET-012 — Generated-lyric language detection 🟢
**Was:** generation hard-forced Japanese → English covers became gibberish ("あかんぽう").
**Fix (pushed):** auto-detect the language **per chunk** (no first-chunk pin), flowing
into transcription / annotate / translate / saved meta.lang.

## TICKET-013 — Single-instance + "only the latest running" 🟢
**Request:** make sure only the latest version is installed/running; prevent >1 instance.
**Found:** repeated dev restarts had left **two** instance-pairs running (the venv
`pythonw` stub re-execs the real interpreter, so each instance = 2 processes; only the
newest owned port 8765). Desktop + Startup shortcuts both target the SOURCE
`pythonw main.py` (= latest). A stale `dist\DesktopKaraoke.exe` build exists but is
unlinked + not running.
**Fix (pushed):** `_is_only_instance()` — a process-lifetime named mutex
(`Local\\DesktopKaraoke.SingleInstance`); `main()` exits if it's already held. It runs
only in the real app (the venv stub doesn't), so it never blocks its own stub→child
pair. Killed the duplicates → one clean latest instance. **Tested:** a 2nd launch
self-exits, port owner unchanged.
**Note:** the stale dist exe is old code; rebuild it if you want the *packaged* version
current — the active path (shortcuts + running instance) is already the latest source.

## TICKET-014 — Common songs generate instead of fetching (title not cleaned) 🟢
**Symptom:** "ReGLOSS 'サクラミラージュ' Performance Video" generated wrong JP; "Clione feat.
轟はじめ (Live at PQ)" generated "me me me ***" — both HAVE real lyrics (サクラミラージュ 65
lines, Clione 27). Generation is meant to be a LAST RESORT.
**Root cause:** `clean_title` left the messy title so the fetch missed → fell to
generation: (a) the song wasn't extracted from `'…'`/`"…"` quotes (only 「」/『』);
(b) "Performance Video" not stripped; (c) "feat. X" not stripped; (d) a SINGLE live song
("X (Live at Y)") matched the bare word "live" in `_LIVE_RE` → forced sound-only mode (no
title fetch) → generated.
**Fix (pushed):** `clean_title` now also extracts a song from straight/smart quotes,
strips "Performance Video"/"Visualizer" and "feat./ft. X"; `_LIVE_RE` no longer trips on
bare "live" (keeps concert/tour/festival/medley/3D-live + the >12 min duration guard).
Verified: both titles clean to the song and fetch real lyrics; concerts ("Live Tour")
stay sound-driven; apostrophes ("Don't Stop Me Now") + covers unaffected. Bad generated
caches deleted.

## TICKET-015 — Auto re-sync by sound over-corrects a baseline that was already good 🟢
**Symptom:** "sometimes auto sync makes it more out of sync, so I reset to -0.0 and it's
good again." The periodic Shazam recal drifted a correctly-synced song OUT of sync.
**Root cause:** the recal eased the offset by `0.8*diff` on every reading where
`diff > 0.15s`. But Shazam's per-read timing is noisy (±~1s, worse on niche tracks) and
digital playback has **no clock drift** (the player's position is exact), so it kept
chasing that noise away from a good baseline.
**Research:** sync is a **constant offset, not drift**, for digital playback; the fix is
a **filtered/dead-banded** estimate, not per-read chasing. ([AudioEdit constant-vs-drift](https://audioedit.io/blog/how-to-fix-audio-out-of-sync),
[Acrovid drift correction](https://www.acrovid.com/audio_video_sync_drift_correction.htm))
**Fix (pushed fc6cabe):** move the offset ONLY when a correction is (a) outside a **0.8s
dead-band** (inside it the player clock is the better authority — leave it), AND (b)
**confirmed by a 2nd reading** agreeing within 2.5s. A real seek / long intro re-confirms
in seconds; random noise never does. Removed the 0.15s/0.8-gain easing. Verified in sim:
noise around 0 stays 0, single spikes ignored, real +30/+5 offsets still apply,
disagreeing spikes rejected. (On-demand "Sync by listening" already gates on a match
ratio, so it was left as-is.)

## TICKET-016 — Read music-source context: trust Spotify / YT-Topic, strict same-title fetch 🟢
**Request:** read the source context (esp. Spotify) so the app knows the ONE song actually
playing and doesn't grab a wrong same-title song, a cover's wrong version, or a whole
concert. Example: "Lucky Star" by Kaneko Lumi (VOID, 3:41 — Spotify/YT-Topic).
**Root cause:** Kaneko Lumi's "Lucky Star" isn't on the lyric providers, so the
artist-unconfirmed **title-only last resort** grabbed a DIFFERENT same-title song
("Twinkle Twinkle Lucky Star") — even with the artist + duration passed (the provider hit
carried no duration, so the guard couldn't reject it).
**Research:** verify against an **authoritative source** (the player's own clean metadata)
and don't trust a title-only hit; duration/album disambiguates same-title songs
(TICKET-002/009). The app already reads SMTC (`GlobalSystemMediaTransportControls`), which
carries Spotify/Topic title+artist+album+duration.
**Fix (pushed 19ebafa):** a `_clean_source()` signal — a real audio app (Spotify) or a
YT-Music "- Topic" channel is AUTHORITATIVE. When clean, `fetch_lrc(strict=True)` skips
the artist-unconfirmed title-only last resort: a generic title that misses the
artist-keyed queries returns nothing → **generate by ear from the real audio** instead of
showing the wrong song. Also: Topic-channel **duration is now trusted** (audio-only upload
= track length) and SMTC **album_title** is read, both for same-title disambiguation.
Covers (歌ってみた) + messy YouTube uploads are unaffected (not a clean source → loose path).
Verified: Lucky Star strict→None (no Twinkle Twinkle); 世惑い子/Lemon/Driver's License still
fetch real lyrics; 地球儀 cover still correct; source-classification unit-tested.

## TICKET-017 — Generated lyrics incomplete: add a deep OFFLINE transcription pass 🟢 (feature)
**Symptom:** for songs we MUST generate (no synced lyrics anywhere — e.g. Lucky Star /
Kaneko Lumi, Clione live), the realtime by-ear generation is **incomplete + rough**: it
transcribes short loopback chunks with a *small* model while racing the playhead.
**Request:** keep the realtime pass as instant best-effort, but ALSO download the source
audio and do a **proper full-file transcription**, cache that, and delete the audio.
**Fix (pushed):** new [`deep_transcribe.py`](deep_transcribe.py) — Tier 2:
(1) `yt-dlp` searches `ytsearch1:<title> <artist>` and downloads **audio-only** (`bestaudio`;
no ffmpeg — PyAV decodes the .webm/.m4a); (2) faster-whisper **`medium`** transcribes the
WHOLE file (`vad_filter=False` so sung vocals survive, `condition_on_previous_text=False`);
(3) lines are annotated + saved as `source: "generated-deep"`; (4) the audio is **deleted**
(`finally:`). Wired in `main.py`: `_begin_generation` also spawns `_begin_deep_generation`
→ `_apply_deep` (saves + upgrades the overlay live if still playing); `_deep_token` cancels
on track change; `_deep_tried` runs it **once per song**; an existing `generated-deep` cache
is never re-downloaded.
**Key findings:** YouTube now 403s audio downloads without a JS runtime — yt-dlp enables only
`deno` by default, so we opt in to **`node`** when on PATH (fixed the 403, 3.5 MB in ~3 s).
`large-v3` was exact-match accurate but spilled to CPU (~4 min) next to the running app, so
the default is **`medium`** (fits GPU, faster, near-identical on clear vocals). Verified on
Lucky Star: deep pass returned **48 complete lines** matching the video's burned-in lyrics
("And I'll be there for you, finding hope from a spark") vs the fragmentary best-effort.
Degrades gracefully (no yt-dlp / 403 / over-long match / <4 lines → best-effort stands).
Documented in [docs/GENERATION.md](docs/GENERATION.md).

## TICKET-018 — Overlay ate mouse clicks ("can't click anything in a game") 🟢
**Symptom:** with the overlay up, clicks didn't reach the game/app underneath — the
full-screen (fixed full-work-area) overlay was intercepting mouse input instead of
being click-through.
**Root cause:** click-through (`WS_EX_TRANSPARENT`) is applied **once at startup**.
It can be lost later (the overlay is a layered, `overrideredirect`, topmost window, and
various window operations re-touch the extended style) — and because the window covers
the **whole screen**, the moment that bit drops, the ENTIRE screen stops accepting clicks.
**Research/verify:** confirmed live — forcibly clearing `WS_EX_TRANSPARENT` on the running
overlay made it eat clicks; the new guard restored it automatically within ~0.5 s.
**Fix (pushed):** extracted `_click_through()` (NOACTIVATE|TOOLWINDOW|LAYERED|TRANSPARENT,
only writes when a bit is missing) and **re-assert it after every window-attribute change**
— init, `set_opacity`, `apply_preset` (the 45 %-opacity Gaming preset was a prime trigger),
`_place_window`, and `toggle()` (Show/Hide re-`deiconify`). Plus a **`_click_guard`** that
re-asserts every **500 ms** as a self-heal, so the overlay can *never* get stuck eating
clicks regardless of the trigger. Verified self-healing on the live window.

## TICKET-019 — "Song/Artist" MV titles generate instead of fetching (Dunk) 🟢
**Symptom:** "[Original] Dunk/Todoroki Hajime [Official MV]" (a *common* ReGLOSS song)
showed **generated** lyrics. Two stale caches existed: `dunk_轟はじめ.json` (good, 86 lines)
and `dunk_todoroki_hajime.json` (generated, 13 lines).
**Root cause:** `clean_title` stripped the brackets but left **`Dunk/Todoroki Hajime`**
(the slash-split only ran for *covers*). So the title-match exact-hit the *generated*
13-line file, and a live fetch of the messy title took **36.5 s** — far past the 11 s
generate deadline. `fetch_lrc("Dunk", "轟はじめ")` returns 87 real lines, and the good
`Dunk` cache was already there — it just wasn't being matched.
**Fix (pushed):** `clean_title` now treats an **Original/MV** upload like a cover for the
`Song/Artist` slash: "Dunk/Todoroki Hajime" → **"Dunk"** (so it instant-matches the good
cache). Guarded to a single slash with no `" - "` on either side, so bilingual
"Artist - JP / Artist - EN" uploads are left for `_title_variants`. Deleted the stale
generated cache. Verified: Dunk→"Dunk"; シンメトリー/アイドル/covers/bilingual all still correct.

## TICKET-020 — Sync-by-listening: reset to 0 when a big offset is low-confidence 🟢
**Request:** "return timing to 0 if significantly desynced after attempting sync — that
fixes it often." A big alignment offset on a song whose player clock is accurate is
usually a *mis-match* (the transcript matched the wrong repeated line).
**Fix (pushed):** `_apply_align` now snaps the offset back to **0** when the aligned
offset is large (>6 s) **and** the match ratio is low (<0.72) — the player position is
right far more often than a low-confidence big jump. High-confidence large offsets
(genuine long intros) still apply. Complements TICKET-015's dead-band on the Shazam recal.

## TICKET-027 — feelingradation showed SKAVLA again — my own clean_title fix broke the title-lock 🟢
**Symptom:** ReGLOSS "feelingradation" (bread-and-butter) showed SKAVLA's lyrics — the old
"feelingradation → SKAVLA" Shazam mis-ID, back again.
**Root cause (a regression I introduced):** the title-lock used an EXACT-string compare of
the player title vs the loaded cache's stored title. The TICKET-023 cleaning made the
player title `'ReGLOSS - feelingradation' → 'feelingradation'`, which no longer
string-equals a cache stored under the longer name (e.g. a stale generated
`regloss_feelingradation.json`) — so `_title_locked` went False and a Shazam mis-ID
(SKAVLA, a different ReGLOSS-adjacent song) overrode it.
**Fix (pushed, v1.0.17):** the lock now MATCHES by **containment** (player title == cache
title OR one contains the other) AND requires the title to be **distinctive**
(`confidence.title_distinctiveness >= 0.40`) — robust to cleaning, while common titles
(Awake/BANG) still defer to audio. So feelingradation (0.85) locks and SKAVLA can't take
over. Deleted the stale generated `regloss_feelingradation.json` (kept the real
`feelingradation.json`) and the old `dist/` build so the latest code runs.

## TICKET-035 — Use the video's OWN caption track (exact lyrics + perfect sync) 🟢
**Insight (from そして花になる):** even after TICKET-034 found the real provider lyrics, they were
~4.7s OUT OF SYNC — the lrclib LRC put line 1 at 0.7s but the video sings it at 5.4s. The official
MV ships a **manual Japanese caption track** (verified via yt-dlp: `subtitles: ['zh-TW','ja','ko']`)
which is the EXACT official lyrics WITH the video's own timing — strictly better than any provider
LRC on both counts, and it also confirmed "微かな人生の幸せを追う" was a Whisper mis-hearing (absent
from the official captions).
**Fix (v1.0.25):** the deep path already downloads the video with yt-dlp; it now also pulls the
**manual caption track** and uses it as the TOP-priority lyrics source (above provider lookup and
Whisper). New `_parse_vtt` / `_captions_from_dir` in `deep_transcribe.py` pick the ORIGINAL-language
track only (a 'ja' track is lyrics; a 'zh-TW'/'en' track is a translation, never shown as lyrics),
collapse rolling-caption dupes, and drop credit lines; saved as `source: youtube-captions`. So any
official MV with manual captions gets exact words AND zero-offset sync. (Fixed そして花になる /
kaf_27_and_become_a_flower in place with the caption track.)

## TICKET-034 — English player title → real lyrics generated by ear (花譜「そして花になる」) 🟢
**Symptom:** 花譜「そして花になる」showed by-ear AI lyrics (a mis-transcribed line "微かな人生の
幸せを追う" that isn't in the song) even though the real synced lyrics are cached AND on lrclib.
**Root cause:** YouTube's SMTC reported the **English** title "KAF #27 - And Become a Flower"
(KAF titles its videos in English). That missed the Japanese cache `そして花になる.json` and every
provider lookup, so the app fell back to deep-transcription — which saved a generated file under
the English slug `kaf_27_and_become_a_flower.json`.
**Fix (v1.0.24):** the deep-transcription path already downloads the video with yt-dlp, whose
metadata carries the REAL title (花譜「そして花になる」). It now **extracts the canonical title and
looks up real provider lyrics BEFORE transcribing by ear** — `そして花になる` → 66 real lines, so it
skips Whisper entirely and saves them as REAL (no AI "***" marker). `deep_transcribe()` returns
`(lines, lang, meta)`; `_apply_deep` saves with the real source. Bridges the whole class of
English/translated player titles for Japanese songs. (Also fixed the stale
`kaf_27_and_become_a_flower.json` in place.)

## TICKET-033 — Cover matched a WRONG-LANGUAGE same-title song (Beyond the Way → German) 🟡
**Symptom:** the cover 「Beyond the way」(音乃瀬奏＆Mori Calliope) was being generated, and the
title-only cover search was matching an unrelated **German** song also called "Beyond the Way".
**Root cause:** the cover fast-path does a TITLE-only lookup, and `verify_lrc`'s language gate
only fires for CJK *titles* — "Beyond the way" is Latin, so a German body passed. The real
Japanese cover lives on NetEase under the romanized artist "Kanade Otonose", which the app
can't derive (音乃瀬奏 romanizes to "oto no se sou", a literal kanji reading, not the name).
**Fix (v1.0.23):** `fetch_lrc` now gates on the ARTIST's script — a CJK-script artist's song is
CJK (or, for a cover, English), never German/Spanish/Russian/etc., so a European-language hit on
a Latin title is rejected as a same-title collision. This also stops the v1.0.22 romaji/generated
re-fetch from *replacing* a deep-transcription with the German words. English covers by JP artists
still pass (detect_lang→"other" is allowed).
**Status:** 🟡 the German collision is fixed, but this specific cover still falls back to
deep-transcription (its real synced lyrics are only findable via a romanized name we can't derive
from 音乃瀬奏). Tracked as a known limit; the transcription is at least the real audio.

## TICKET-032 — "Was fine then desynced": a spurious same-song track-change wiped the offset 🟢
**Symptom:** a song synced correctly, then suddenly jumped ~30s off (Shinigami Eyes, white
balance). Telemetry showed it: `CONFIRMED offset -29.89s → applied` (drift→0, synced), then a
few reads later `shown_off=+0.00` with `track change: 'Shinigami Eyes' / 'Grimes'` — the SAME
song re-fired as a track change.
**Root cause:** track changes fire on exact `(clean_artist, clean_title)` inequality, but YouTube's
SMTC re-reports the same song mid-play with slightly fluctuating metadata (a channel suffix
appears/disappears, the title reflows). Each flicker re-entered `_on_track_change`, which resets
`self.offset = 0.0` and re-identifies — wiping a confirmed sync.
**Fix (v1.0.22):** `_on_track_change` now bails early when the "new" track's title still matches
the currently-loaded song (`_titles_match`, and not live) — it keeps the sync and only refreshes
duration. The recal loop keeps listening, so a genuine same-title-different-song is still caught
by sound. Fixes the "was fine then desynced" class for every song.

## TICKET-031 — Romaji-only cover showed no Japanese / no English (Blue Bird) 🟢
**Symptom:** Raon Lee's "Blue Bird" cover displayed ONLY romaji ("aoi aoi ano sora") — no
kanji/kana, no English translation.
**Root cause:** the cached `naruto_shippuden_blue_bird.json` was `lang: ja-romaji` (a romanized
upload) with `en` = the romaji copied verbatim (romaji can't be furigana'd or translated). The
romaji→kanji upgrade (`_synced_cjk`, which DOES find the NetEase Japanese original) had failed
when first fetched — it searched the COVER channel ("Raon") as the artist — and the stale
romaji then stuck forever, because a cache hit never re-fetched (same trap as TICKET-028, but
for romaji). A stray `┃` (a truncated "| Cover by …") in the stored title also made the search
match a *Spanish* track.
**Fix (v1.0.21):**
- The runtime cache-hit upgrade (TICKET-028) now also fires for **romaji-only** hits
  (`lang endswith '-romaji'`), re-fetching **cover-style** (by TITLE) so it reaches the kanji
  original; `load()` supersedes the romaji the moment Japanese arrives. Romaji hits never lock.
- `clean_title` + `_title_variants` now strip box-drawing / fullwidth bars (`┃│｜／・‖`) so a
  truncated "Song┃" no longer poisons the search.
- `audit_cache.py --upgrade-generated` now also upgrades romaji-only files in place (only when a
  real CJK result is found). Verified: `fetch_lrc('Blue Bird', cover=True)` → 42 JP lines.

## TICKET-030 — Mode-aware sync: FOLLOW live/short arrangements, distrust repeated-chorus reads 🟢
**Symptoms (from live telemetry, TICKET-029's logs):**
- Studio サクラミラージュ "desynced multiple times" — log showed offsets oscillating
  `applied -10.36s` … `holding -70.36s`. The ~60 s jumps are the spacing between the song's
  **repeated choruses** (花桜/徒桜 ×3): Shazam matched the *wrong repetition*, and on a studio
  track the player clock is exact so chasing them is what desynced it.
- V.W.P `【LIVE MV】魔女(真) Short Ver.` — a **live/short arrangement** whose timing is wildly
  different from the studio LRC (massive real offset). It wasn't even detected as live
  (`is_live_or_compilation`=False) so it got studio handling and stranded.
**Insight:** studio and live pull in OPPOSITE directions — studio wants the offset *reset*
(exact clock, big reads = artifacts); live wants it *followed* (the offset is real and drifts
with tempo). So sync must be **mode-aware**.
**Fix (v1.0.20):**
- New `is_live_arrangement()` (`_LIVE_VER_RE`: LIVE/LIVE MV/Short Ver/Acoustic/`from "…"`/
  ライブ/弾き語り…) + a **duration-mismatch** test (playing length vs the LRC's span >25 s)
  classify each track as **studio** or **live** per read.
- **Live = FOLLOW:** apply a corroborated offset even when large (cap raised to the studio
  length), EWMA-smoothed (`0.6·new + 0.4·old`) to ride tempo drift, polling every ≤8 s.
- **Studio = distrust ambiguity:** track the spread of recent reads; if they diverge >15 s
  (repeated-chorus matches), RESET to 0 instead of chasing — plus all of TICKET-029's
  reset-first logic. Legit small studio offsets still apply.
- Telemetry now logs `mode=studio|live` and `spread=` per read. Verified by simulation across
  studio-repetitive, live-short, and studio-normal scenarios.

## TICKET-029 — Sync redesign: RESET is the first-line defense; add/drop time only on sonic confirmation 🟢
**Request:** "I just reset to get me back to proper place but that should happen
automatically. Make the reset the first line of defense against desync; only when sonic
markers indicate the lyrics are wrong should the system drop/add time. Improve logs so the
desync is visible."
**Insight:** digital playback has **no clock drift** — the player position is exact — so the
correct offset is almost always **0**, and a *chased* non-zero offset is the usual cause of
desync. Reset, not nudging, is the right default.
**Fix (v1.0.19, `_consume_async`):** the Shazam-read handler now:
1. **AUTO-RESETs to 0** the moment the audio implies ~no offset (`|corr|≤0.8`) while we're
   showing one — the manual "reset to 0 and it's fixed", made automatic and the first-line
   defense.
2. **Drops/adds time ONLY when corroborated** — two independent reads agree (`|corr−pending|
   <2.0`) before any non-zero offset is applied (sonic markers confirm a real mis-timing).
3. **Never disturbs a correct offset on noise** — absurd reads (≥ duration cap) and single
   uncorroborated reads are ignored/held, so a confirmed MV-intro offset survives Shazam's
   ±1–2 s jitter. Verified by a 7-scenario decision simulation.
4. **Re-verifies a live offset fast** — recal cadence drops to ≤12 s whenever `|offset|>0.8`,
   so a bad offset is reset within seconds, not a full slow cycle.
**Logging:** every read now emits `sync-read: drift=±Xs audio_off=… shown_off=… pos=… line#…`
so a developing desync is visible in `/logs`; `/status` gains `sync_drift`, `sync_drift_age`,
`sync_pending`. Supersedes the eager-correction behavior behind TICKET-015/026.

## TICKET-028 — Generated lyrics cached then served FOREVER (popular songs "keep generating") 🟢
**Symptom:** popular songs that providers DO have kept showing AI-generated lyrics on every
replay.
**Root cause:** a cache hit short-circuits the matcher — once a song got a `generated` file
(from a one-time transient fetch failure or a since-fixed cleaning bug), `LyricsIndex.match`
served it on every future play and **never re-fetched** the real lyrics. The audit found 66
such files.
**Fix (v1.0.19):** (a) **runtime upgrade** — a generated cache hit now shows instantly *and*
kicks off a background real-fetch; `load()` supersedes it the moment real lyrics arrive, and a
generated hit no longer title-locks. (b) **`audit_cache.py`** — a reusable cache accuracy
auditor (meta, romaji↔furigana, language, timing gaps, duplicates) with `--upgrade-generated`
to re-fetch the backlog **in place** (same filename, no slug duplicates). Audit of 492 files:
66 generated · 216 missing duration · 1 benign duplicate. (The romaji↔furigana check is
informational only — deriving romaji from furigana regresses 287 files via a compound-verb
doubling bug, so the stored cutlet romaji is kept.)

## TICKET-026 — Absurd Shazam offset desynced a song (シンメトリー +160s) 🟢
**Symptom:** "messing up on this ReGLOSS song again" (シンメトリー). The LYRICS were correct
(`heard 'シンメトリー' | loaded 'シンメトリー' | match=True`, 51-line cache); the SYNC was the
problem — `sync: holding +160.61s` in the log.
**Root cause:** Shazam matched a DIFFERENT recording/segment and returned a +160s offset.
The TICKET-015 cap was 180s, so +160s slipped through; the dead-band held it for ONE read,
but two consistent bad reads would "confirm" each other and apply +160s → the whole song
desyncs (clione live had the same shape).
**Fix (pushed, v1.0.15):** the re-sync cap is now duration-aware — reject any |corr| ≥
`min(120, max(45, 0.4×duration))` (a correction that's a big fraction of the song is a
Shazam mismatch, not a real seek) AND clear the pending value so two bad reads can't
confirm. Real seeks/intros (small) still apply via the dead-band + 2-read confirm.

## TICKET-024 — "Multiple sets of lyrics" + "some ended up generated too" 🟢
**Symptom:** lyrics looked like two overlapping sets; songs that eventually FETCHED real
lyrics sometimes still showed AI-generated lines.
**Root cause (a feature CONFLICT):** when a slow song generated and THEN the fetch
finally resolved, `load()` of the real lyrics cancelled the realtime generation
(`_gen_token`) but **NOT the background deep transcription** (`_deep_token`). The deep
pass would complete a bit later and `_apply_deep` would **overwrite the real fetched
lyrics** with its `generated-deep` version (and re-save the cache).
**Fix (pushed, v1.0.14):** real lyrics now supersede ALL generation — `load()` of a
non-`generated*` source bumps `_deep_token` too, clears `_gen_lines`, and stops the gen
loop; `_apply_deep` also bails if real lyrics are already loaded (no save, no display).
Plus the generate-vs-fetch defer was widened (~43s) so a slow-but-successful fetch wins
before generation even starts ("generated before finding it"); cleaner titles (TICKET-023)
already make most fetches resolve in <15s.

## TICKET-025 — Confidence score: generic titles must defer to the AUDIO ("Awake" rule) 🟢
**Request:** "Awake"/"BANG" are common names — the AUDIO should weigh more than the title;
document in source what contributes to the confidence score.
**Root cause:** `_is_generic_title` only caught tie-in *tags* ("OP Theme"), so a common but
real name like "Awake" / "BANG" / "Lucky Star" still got **title-locked** — and a wrong
same-title match couldn't be corrected by sound.
**Fix (pushed, v1.0.14):** new [confidence.py](confidence.py) documents EVERY signal that
contributes to song-match confidence (banner OCR > clean-source title > heard-by-sound >
title-exactness > duration > artist > language) and adds `title_distinctiveness()` /
`is_common_title()`. The title-lock now also requires the title to be DISTINCTIVE, so
Awake/BANG/Love/Lucky Star (distinctiveness 0.10–0.27) stay unlocked and let Shazam decide,
while feelingradation/シンメトリー/white balance (0.57–0.85) still lock. Logged for transparency.

## TICKET-023 — Popular JP/VTuber songs generate when the providers HAVE them 🟢
**Symptom:** very popular songs (KizunaAI "white balance" 2M views, "LOVESHII", 大神ミオ
"Howling") **generated** lyrics. The user assumed a database gap and asked for better
lyric libraries.
**Decisive finding (NOT a database gap):** the providers already carry them —
`fetch_lrc("white balance", "Kizuna AI")` → 32 lines, `fetch_lrc("LOVESHII", "Kizuna AI")`
→ 47. The bottleneck was the **title/artist cleaning algorithm**:
  - `clean_title` left the **"Artist - Song" hyphen prefix** ("KizunaAI - white balance",
    "Kizuna AI x KAF - LOVESHII", "Reol - Edge") — the same class as Dunk's "Song/Artist"
    slash, but with `-`.
  - `clean_artist` didn't strip a **dash-prefixed channel suffix** ("Kizuna AI -
    A.I.Channel" → must be "Kizuna AI"; the suffix made the search miss).
**Fix (pushed, v1.0.13):** the artist-aware reducer now also strips a leading artist
credit before the first ` - ` (only when that head matches the artist, so a real "A - B"
song title is left alone), and `clean_artist` strips `- …Channel`. Verified: white balance
→ 32 lines, LOVESHII → 47, Edge → 73, Dunk/LOAD/幻界 unaffected, bilingual untouched.
Deleted the stale generated caches so they re-fetch the real lyrics.
**Takeaway documented in RESEARCH.md:** for this catalog, *matching* (clean titles +
right artist) beats *adding databases* — Musixmatch + NetEase + LRCLIB already cover most
J-pop / anime / VTuber; the gaps left (genuinely niche covers, live takes) are handled by
OCR (TICKET-022) + generation, not a different fingerprinter.

## TICKET-022 — Concert song detection via on-screen banner OCR 🟡 (feature)
**Request:** in a long concert video (ReGLOSS 3D live) the app should play the CURRENT
song's lyrics (SUPER DUPER, 泡沫メイビー, …) and sync — Shazam alone fails on live takes.
Use the **song name shown on screen** as a high-confidence hint feeding the confidence score.
**Approach (new [concert_ocr.py](concert_ocr.py) + [docs/CONCERT_DETECTION.md](docs/CONCERT_DETECTION.md)):**
capture the screen → crop the top banner strip → OCR with the **built-in Windows OCR**
(`Windows.Media.Ocr` via winsdk — no new dep) → fuzzy-match to the song library → if
`score >= 0.85`, load that song (cache/fetch), title-lock it (OCR is authoritative in a
concert), and lock timing by sound. Runs throttled in **live mode** on a background thread.
**Research:** burned-in-text OCR is a proven approach (VideOCR/PaddleOCR ~99% JP). Combining
**on-screen text OCR + audio fingerprint** is the recommended design.
([VideOCR](https://www.fcportables.com/videocr-portable/), [meikipop JP OCR](https://github.com/rtr46/meikipop))
**Findings:** Windows OCR works (read "SUPER DUPER" cleanly); ships **en-US** only —
Japanese banners need the pack once: `Add-WindowsCapability -Online -Name
"Language.OCR~~~ja-JP~0.0.1.0"`. The in-memory bitmap path segfaults → use a temp PNG +
`StorageFile`. Matcher verified: real banners → 1.0, hashtag/chat noise → 0.36 (ignored).
**Status:** 🟡 module + matcher built & tested, wired into live mode (en-US live now);
needs the ja-JP pack for Japanese banners + live concert validation + intermission handling.
**v1.0.16 update:** the concert ("Departures") sat on "Listening to identify…" because OCR
only matched ALREADY-CACHED songs. Added `concert_ocr.plausible_title()` (extracts a clean
Latin banner name, filters hashtag/chat/UI noise; OCR cropped to the top-LEFT to skip the
right-side chat panel) + `_fetch_ocr_song()` so a confident banner we DON'T have is
**fetched cover-style** ('Departures' → 37 lines). Verified the matcher rejects "Top fans"/
"Top chat replay" noise. So concert detection is no longer limited to pre-cached songs.

## TICKET-021 — MV-intro onset-anchor double-shifted a fetched LRC (サクラミラージュ drift) 🟢
**Symptom:** サクラミラージュ's lyrics drifted ~11s late; resetting Sync→0 fixed it every
time. The watcher caught a persistent **-11s** offset on it.
**Root cause:** `_on_song_onset` anchored the MV intro with `offset = -vpos`, which
ASSUMES the lyrics start at time 0 (true for *generated* lyrics). But サクラミラージュ's
**fetched LRC already has the intro built in** (first line @18.9s, audio onset @~11s), so
its timestamps are **absolute video-time** — the right offset is **0**, and `-vpos`
double-shifted it by 11s.
**Fix (pushed, v1.0.11):** anchor only when the lyrics genuinely run AHEAD — if the first
line is already at/after the onset (`first_start >= vpos-2`), the LRC is absolute →
**offset 0** (no anchor); otherwise (generated ~0, or relative LRC) keep the `-vpos`
anchor. This makes the user's manual "reset to 0" automatic and correct. Verified across
fetched-with-intro / generated / relative / at-onset cases. Clione lyrics confirmed correct.

## TICKET-032 — Display persistence + mirror/cycle modes + 4s sample default 🟢
**Request:** chosen display falls back to primary too easily; need to mirror lyrics to ALL
screens or cycle through each; make 4-second sample for lyric sync the default (was 10s).
**Root cause (display fallback):** stored `display="mon:N"` was an INDEX into the current
enumeration. After a monitor sleep/wake/replug, indices renumber → the saved index pointed
nowhere → `_apply_display` silently fell back to primary.
**Fix (pushed, v1.0.26):** added monitor fingerprinting (`_mon_fingerprint` = "x,y,wxh")
saved alongside the index, with index fallback then primary fallback (each logged).
Watchdog (`_check_monitors`) re-enumerates every 3 s and re-applies the display if the
topology changed. Added "Mirror on ALL screens" (transparent Toplevel clones rendering
the current line via simplified `_create_mirrors` / `_update_mirrors`) and "Cycle through
screens" (rotates `_cycle_idx` on each `_render`). `recal_secs` default lowered from 10
to 4.

## TICKET-033 — Maneki-neko dancing character (cuteness rebuild) 🟢
**Request:** turn the dancing character into a maneki-neko — beckoning paw, red collar
with bell, gold koban; first attempt looked "kinda fucked up."
**Fix (pushed, v1.0.26):** complete redesign of `_draw_chibi` in `character.py` matching
classic kawaii proportions researched online (CLIP STUDIO chibi tutorial, DeviantArt
Maneki-Neko tutorial): big head (~60% of figure), small round body, raised right paw
that waves with `math.sin(phase * 3.2)`, calico patches themed to artist colors, koban
coin with 福 kanji in left paw, red collar arc + gold bell, happy closed-crescent eyes
(the signature kawaii expression), pink nose + ‿ smile, cheek blush with stipple, music
notes while playing. Clean tkinter primitives (oval/polygon/arc/line), no ugly triple-
line arm hack.

## TICKET-034 — Cover lyrics: search by ORIGINAL artist, not covering channel 🟢
**Symptom:** "[COVER] Coffee - Alka | Kaneko Lumi" loaded the wrong Lumi song; reporting
wrong-song never recovered. The covering channel (Lumi) was searched as the artist
instead of the original artist (Alka), so Shazam couldn't disambiguate either.
**Root cause:** `_COVER_RE` matched `(cover)` and 歌ってみた but NOT `[COVER]` (square
brackets). Generic bracket-strip in `clean_title` then ate the cover marker entirely, so
`_is_cover` was False — no title-first cover path, no original-artist extraction.
**Fix (pushed, v1.0.26):**
1. Added `\[\s*cover\s*\]` to `_COVER_RE` so square-bracket covers are recognized.
2. Added `extract_cover_original(raw_title, cover_channel)` that parses common cover
   patterns ("Song - OrigArtist | CoverChannel", "Song - OrigArtist / CoverChannel") and
   returns the original artist. Identifies which side is the cover channel by lowercase
   normalised-substring match against the cleaned artist.
3. `_on_track_change` uses `_cover_original_artist` as the `fetch_artist` for both index
   lookup and `_start_fetch` when set, falling back to the channel name otherwise.
4. `report_wrong` (user-driven correction) ALSO re-fetches by the original artist for
   covers before falling back to sound ID (which often fails on covers).
**Research:** lyric finders verify artist + don't trust the first match (TICKET-002).
This applies the same principle to the cover artist token.

## TICKET-035 — Long instrumental intros desync (Grimes "Genesis") 🟢
**Symptom:** "Grimes - Genesis" video is 5:32 but the song's first vocal is at ~1:10
(~70 s instrumental intro). Lyrics started showing at video time 0 and ran ahead of
singing until Shazam mid-song fingerprinted a vocal phrase to calibrate (often half the
song later).
**Root cause:** `_mv_mode` only triggered on titles containing MV/PV/Music Video/Official
markers — "Grimes - Genesis" has none. And the existing `_on_song_onset` only fires on a
quiet→music transition (a leading silent gap), which YouTube videos don't have — music
plays from frame 0.
**Fix (pushed, v1.0.26):**
1. Added **vocal-band onset detection** to `SongChangeDetector`: tracks the ratio of
   spectral energy in 200-3000 Hz (vocal range) to total energy via cheap real FFT on
   each 0.2 s block (~0.5 ms per check). Learns the instrumental baseline for the first
   ~5 s of music, then fires `on_vocal()` when ratio runs 1.4× baseline (or > 0.55
   absolute) sustained for 1 s.
2. New `_on_vocal_onset` handler in main.py calibrates `offset = first_line.start -
   player_position` when fired, jumping the displayed lyrics to line 1 the instant
   singing starts. Guarded: only when vpos > 8 s, first line < 8 s, new offset in
   (-120, 0).
3. `load()` auto-enables MV mode when LRC duration > 15 s shorter than the YouTube
   duration AND first line starts before 5 s — catches Grimes-class uploads with no
   MV markers in the title.
4. Bumped the MV intro-hold timeout from 50 s → 100 s (most MV intros are under 90 s)
   now that vocal-onset can release it precisely.
**Research:** Silero VAD and webrtcvad were rejected (both miss singing in polyphonic
music). HPSS + mid-band energy ratio is the lightweight robust approach
([MDPI: Singing Onset](https://www.mdpi.com/2076-3417/12/15/7391),
[Silero VAD #546](https://github.com/snakers4/silero-vad/discussions/546)).

## TICKET-054 — Paused tab hijacked playback (Coffee↔Mix flip-flop) 🟢🟢🟢
**Symptom:** on a YouTube Mix, the log showed the app flip-flopping between the
playing song and a PAUSED background tab every track: `track change: Coffee → Rumor
→ Coffee → Hug → Coffee …`. So the overlay kept loading the paused Coffee tab's
lyrics over the actually-playing song → "No lyrics found", wrong song, stale lyrics,
and a fresh fetch/caption churn on every flip. A huge part of the live "desync."
**Root cause:** `MediaWatcher._pick` returned `get_current_session()` when no session
was "playing". Between Mix tracks there's a brief gap where NOTHING is playing — so
`_pick` fell to the OS "current session", which was often the paused Coffee tab. Next
poll the Mix was playing again → back to it. Flip-flop.
**Fix (pushed, v1.0.48):** made `_pick` STICKY. It tracks the source_app it's
following and (1) keeps it while still playing, (2) else the first playing session,
(3) and when NOTHING is playing — a transition gap — KEEPS the followed session
instead of jumping to a different paused tab. A paused background tab can no longer
hijack the overlay.
**Also:** `_on_track_change` now clears `self.meta` so a new song with no lyrics yet
can't display the previous song's stale source (the "youtube-captions / 0 lines" bug).
**Verified live:** "【歌ってみた】One Last Kiss" held stable for 60 s with no Coffee
interleaving; got youtube-captions/62 lines, drift 0.0.

## TICKET-053 — Overlay FROZE on the old song (the real "hella bad") 🟢🟢🟢
**Symptom:** caught live — the SMTC title had changed to a new song
("【歌ってみた】林檎売りの泡沫少女") but the app was STUCK showing the previous song's
lyrics ("Break Into My Heart", 53 lines), its clock running 357 s past their end,
showing nothing. `/diag` confirmed `render_fps: None` — the render loop was DEAD.
**Root cause:** a `NameError` I introduced in v1.0.42. The auto-caption scheduling
block added to `_on_track_change` referenced a variable `src` that isn't in that
method's scope. So **every track change raised NameError**, which propagated out of
`_tick` — and since `_tick` is a self-rescheduling `root.after` loop, the exception
stopped it from rescheduling. The loop DIED while the OS media kept advancing, so the
overlay froze on whatever was last loaded. (Other timers — auto-align, monitors —
survived independently, which is why the app looked half-alive.) This single bug
explains a huge share of the "stuck / wrong song / no lyrics / hella bad" reports.
**Fix (pushed, v1.0.44-46):**
1. **Crash-proof the render loop (v1.0.44):** wrapped `_tick` so ANY frame exception
   is logged and the loop ALWAYS reschedules. One bad frame can never freeze the
   overlay again — it self-heals and logs the cause.
2. **Fixed the NameError (v1.0.45):** use the cached `self._last_src` (the source IS
   tracked in the tick loop) instead of the undefined `src`.
3. **Captions now actually apply (v1.0.46):** the caption fetch logged "76 lines
   fetched" but `src` stayed `lrclib` — `_apply_deep`'s `_deep_token` check discarded
   them because generation / a title re-report bumped that shared token during the
   ~20 s yt-dlp fetch. Added `_apply_captions` guarded by `_track_seq` (bumped ONLY on
   a real song change), so captions apply as long as the same song is still playing.
**Verified live end-to-end:** track changes no longer freeze; `西憂花『ふわふわhazy』` →
"captions: 64 ja lines" → "captions: applied 64 lines" → src `youtube-captions`,
drift 0.0, in_sync. The found-real-lyrics hint also no longer sticks (force re-render).

## TICKET-052 — YouTube caption track = accurate lyrics + perfect sync 🟢🟢
**The big one.** Watching live, the app showed WRONG TEXT for KizunaAI "white balance":
app LRC said "未来 未開 見たことない…" (mirai mikai mita koto nai) while the song actually
sings "未来 見たい 君の傍で…" (mirai mitai kimi no katawara de). syncedlyrics returned a
different/worse transcription than the video — and even with perfect timing, wrong WORDS
read as "hella bad." Provider LRCs also drift because their timing is for a different cut.
**Root insight:** a YouTube video's OWN caption track is the ground truth — correct words
AND timing locked to THIS exact video. (The user's other agent proved it: "pulled the
caption track, 35 lines, perfectly timestamped.")
**Fix (pushed, v1.0.42):**
1. **Bundled yt-dlp** into the build (it was never included → deep_transcribe AND captions
   silently no-op'd). Added `yt_dlp` to the spec's collect_all.
2. **`fetch_captions_only(query, lang)`** in deep_transcribe — a FAST subs-only yt-dlp
   pull (no audio download, no Whisper): manual subs first, then YouTube auto-captions
   (ASR), parsed to timed lines. Requests ONLY the song's language (asking all 5 CJK
   langs at once → YouTube 429) with `ignoreerrors` so one rate-limited lang can't abort.
3. **`Overlay.load_youtube_captions()`** annotates (furigana/romaji/translation) + saves
   as source `youtube-captions` (real → replaces a wrong LRC), upgrades the overlay live.
4. **Auto for browser videos:** `_on_track_change` schedules a background caption fetch
   ~4 s in for any browser (YouTube) source, throttled (≥8 s between yt-dlp calls,
   once per song) so a fast playlist can't 429. Tray toggle "Use YouTube captions" +
   "Get captions for this video now"; `POST /captions`; setting `captions` (default on).
**Verified end-to-end live:** `Reol - 'ミュータント' Music Video` → log "captions: 43 ja
lines from YouTube caption track" → saved → source `youtube-captions`, drift 0.0,
in_sync True, "✨ Found the real lyrics". This replaces the approximate
LRC+Shazam+correlation stack with the video's own ground-truth lyrics for YouTube.

## TICKET-047b — Scroll fill: layer-composite to kill per-fill glyph render 🟢
**After the timer fix (047), ~10% of frames still spiked 27-44 ms** — the karaoke fill
re-rendered every glyph WITH stroke outlines (8-9 draws/char) ~5×/s per singing line.
**Fix (pushed, v1.0.41):** render the block's base layer once at spawn and the fully-sung
layer LAZILY on first-sing; the per-fill step now just composites the two via a cheap
rectangle mask (no glyph render). Steady state went to a solid ~16 ms (60 fps). Splitting
the sung layer to first-sing (not spawn) avoids doubling the spawn cost into one big hitch.

## TICKET-051 — Game noise must not corrupt sync / recognition 🟢
**Concern:** the overlay is built to run WHILE gaming (there's a "Gaming" preset), but
the sync + recognition listen to the **system loopback**, which mixes the music with
GAME AUDIO — gunfire, explosions, UI clicks. Those dump energy into the 200-3000 Hz
vocal band, which would create false "vocal" blocks and corrupt the energy-correlation
sync (and could false-trigger vocal-onset detection).
**Fix (pushed, v1.0.40) — tonality gate on vocal detection:**
The discriminator between SINGING and game NOISE is **tonality**. A voice (and pitched
music) is harmonic — energy concentrated at a few frequencies → LOW spectral flatness.
Broadband SFX is noise-like → HIGH spectral flatness. `_vocal_ratio` now computes the
vocal band's spectral flatness (geometric/arithmetic mean of the power spectrum) and
scales the band-energy score by a tonality weight: full weight at flatness ≤0.35,
ramping to ZERO at ≥0.65. So a gunshot/explosion block contributes ~nothing to the
vocal mask, keeping the sync correlation clean while a game plays. Cheap (one extra
flatness ratio on the FFT already computed).
**Other layers already robust:** Shazam mis-IDs from noise can't switch songs without
a 2nd confirming read (TICKET-anti-churn); the energy correlator's small-shift prior +
uniqueness + lift floor (TICKET-049) make a noisy correlation DECLINE to act (player
clock carries sync) rather than jump.
**Diagnostics:** `/audio` now reports `band_flatness` and a `noise_like` flag, so a
game-noise period is visible in the listener view.
**Verified:** on the Marine song the vocal band's flatness stays low (vocals still
detected normally); a broadband-noise block reads high flatness and is gated out.

## TICKET-050 — Diagnostic views: source / audio listener / lyric-state analyzer 🟢
**Request:** add a video/music source view, an audio listener, and a lyric current-state
analyzer to the diagnostics API "and anything else that may help."
**Fix (pushed, v1.0.38) — three new GET endpoints:**
- **`/source`** — the RAW Windows SMTC data the app receives (title, artist, album,
  status, position, duration, rate, source_app) AND what it derived (clean_title,
  clean_artist, track_tuple, is_cover, cover_original_artist, trusted_duration,
  live_mode, mv_mode, intro_anchored) + media_error. Traces a desync to the SOURCE
  (wrong title leaking, stale position, paused) before blaming sync logic.
- **`/audio`** — live audio LISTENER from the loopback recorder: capturing flag +
  age, rms, loud_ema, is_silent, live vocal_ratio, vocal_detected_now, window on/off
  block counts + adaptive threshold, vocal_baseline, buffer_len, blocks_seen,
  music_for/silent_for. Plus a `recent_pattern` ASCII strip (█/· for the last ~6 s
  of vocal on/off). Confirms audio is flowing and vocals are being detected.
- **`/lyricstate`** — current/prev/next lines with timings, karaoke fill_fraction,
  between_lines flag, lrc_span vs video_duration, and structural anomaly checks
  (LRC past video end, low coverage, big gaps). Surfaces "lyrics don't fit the song"
  problems that masquerade as desync.
Implemented `SongChangeDetector.live_audio()` (latest rms/vocal_ratio/silence) and
`Overlay.get_source()/get_audio()/get_lyric_state()`.
**Already paid off:** `/source` revealed the Coffee cover's clean_title still carries
the "- A!ka | Kaneko Lumi" suffix (cover_original_artist extracts "A!ka" fine, but the
title isn't fully reduced) — a real title-cleaning gap to tighten. `/lyricstate`
confirmed that cover is healthily matched (span 180.6 vs video 186.2, no anomalies).

## TICKET-049 — Energy correlator chorus-repetition phantom (small-shift prior) 🟢
**Symptom:** intermittent MASSIVE desyncs (offset jumping ~15s). /diag caught the
mechanism live: on "Coffee - A!ka | Kaneko Lumi" the energy correlator persistently
reported `best_shift=-14.8, lift=0.262` while the song was actually in sync at
offset 0. That -14.8 s is a CHORUS-REPETITION match — the vocal on/off pattern one
chorus away looks identical, so the cross-correlation has a near-equal peak there.
Whenever Shazam couldn't provide a fresh anchor (its agreement-guard, TICKET-043,
needs a recent reading), the phantom could win and yank the offset 15 s → the
"massive desync."
**Research (score-following literature, ICASSP 2024 real-time lyrics alignment;
Dixon OLTW):** production systems use a TRANSITION PRIOR — the true position rarely
jumps far between updates — and reject ambiguous matches rather than treating the
global-best peak as truth.
**Fix (pushed, v1.0.37):**
1. **Small-shift prior:** `scored = agree − penalty·|shift|` (penalty 0.012/s,
   tunable). A distant shift must beat the no-change score by `penalty·|shift|` to
   win, so the correlator prefers KEEPING the current sync unless evidence is
   overwhelming. Directly kills the -14.8 phantom (it scored ~0.14 over no-change,
   under the 14.8×0.012≈0.18 it needed).
2. **Peak-uniqueness rejection:** mask ±2 s around the winner, find the next-best
   distant peak; if it's within `energy_peak_margin` (0.06) of the best, the match
   is ambiguous (chorus) → no change. Both knobs live-tunable via /tune.
3. **Adaptive vocal threshold:** the old absolute floor `max(0.50, baseline*1.25)`
   left the correlator BLIND on many songs ("insufficient vocal activity, 0 blocks")
   because the vocal-band ratio's absolute level varies hugely by genre. Replaced
   with a per-window split at `median + 0.5·(p75−median)` + a contrast gate
   (need ≥6 on AND ≥6 off blocks, spread ≥0.02). Result: 0 → 57 vocal blocks
   detected on the Marine cover, so the correlator can actually evaluate it (and
   then correctly reject the ambiguous chorus rival rather than jumping).
4. **Diagnostics:** /diag energy_align now reports `rival_shift`, `rival_score`,
   `ambiguous`; sync block adds `offset_history` (last 20 offset changes with
   timestamps) so a jump is visible after the fact.
**Live-verified:** Niconico Marine "Ahoy!!" cover now frame-matches the video's
burned-in karaoke ("ヨーソロー！ついておいで 共に Yo-Ho…"), drift 0.02, with the
correlator logging `best=0.0(0.38) rival=-15.0(0.38) ambiguous=True → no change`
— the exact phantom that used to cause the -15 s jump, now correctly rejected.
**Note:** the deeper fix the research points to (OLTW over chroma + a
constant-velocity Kalman filter with innovation gating, replacing the whole
ad-hoc reconciliation) is logged as future work — the prior+uniqueness is the
high-leverage 80% with far less risk.

## TICKET-047 — Scroll-through stutter: Windows timer granularity (+ GIL) 🟢
**Symptom:** scroll-through ("lr"/"rl") modes "very stuttery", "has been suffering."
**Diagnosis (via the new /diag, TICKET-048):** `recent_ms` frame history showed the
belt alternating between **16 ms and 30 ms even with ZERO render work** (spawns and
repaints disabled live via /tune). That pattern is the giveaway: Windows' default
system timer granularity is ~15.6 ms, so Tk's `after(16)` for a 60 fps loop fires at
EITHER ~15.6 ms OR ~31.2 ms unpredictably — an uneven cadence the eye reads as stutter.
**Root cause (primary):** system timer resolution. **(secondary):**
`_run_energy_correlation` ran a 151-iteration Python loop holding the GIL on a
background thread every 15 s, adding periodic hitches.
**Fix (pushed, v1.0.36):**
1. `ctypes.windll.winmm.timeBeginPeriod(1)` at startup → raises timer resolution to
   1 ms so `after(16)` is accurate. **Result: steady frames went 16/30 ms (jitter
   ~10 ms) → solid ~16 ms (jitter ~1 ms), render 48–60 fps.**
2. Vectorized the energy-correlation shift search — LRC mask built once on a 0.2 s
   grid, all 151 shifts evaluated with one numpy gather (`(151, nblocks)`, C-level,
   no GIL hold). Removes the periodic background hitch.
3. Time-budgeted the heavy ticker section (`heavy_budget_ms`, default 10) + made
   `fill_skip`/`spawn_budget`/`repaint_budget` live-tunable via /tune, so a slow PIL
   paste can't stall the belt and the knobs can be tuned without a rebuild.

## TICKET-048 — Deep diagnostics API (/diag) + FPS/frame-timing metrics 🟢
**Request:** "include those functions in the app so you can diagnose better" +
"include fps in diagnostics api." Iterating on sync/perf needed observability
without rebuilding.
**Fix (pushed, v1.0.36):**
- `GET /diag` returns the full sync state machine (offset, drift, drift_integral,
  pending_corr, last_audio_off + age, sound_song, sound_title_alias, title_locked,
  effective_song_time, showing_idx vs should_show_idx, in_sync flag), the last
  energy-correlation result (best_shift/score/median/lift, vocal-block counts), and
  FPS/frame-timing (target, render, frame_ms, worst_ms, jitter_ms, recent 60-frame
  history, perf_mode, scroll_dir).
- `_tick` now tracks frame jitter (EWMA of |frame − target|) and worst-frame ms
  over a 120-frame ring buffer — the stutter metrics.
- `/status` gained `frame_jitter_ms` + `frame_worst_ms` for at-a-glance checks.

## TICKET-046 — Cross-language load broke Shazam calibration → -23.7s drift 🟢
**Symptom:** after TICKET-045 made the Marine song load the correct Japanese cache,
a QA pass found the offset had silently drifted to **-23.7s**. The lyrics were right
but the timing was badly off.
**Root cause:** the loaded Japanese cache title ("Ahoy!! 我ら宝鐘海賊団☆") never
string-matches Shazam's romanized heard title ("Ahoy!! We are Houshou Pirates"), so
`loaded_ok` was always False → the Shazam timing-calibration block (`if loaded_ok:`)
never ran → `_last_audio_off` stayed stale → the energy correlator's chorus-guard
(TICKET-043, which needs a recent Shazam reading to compare against) couldn't fire,
and the correlator locked onto a chorus-repetition match (-23.7s). The CJK-preference
fix (045) fixed the lyrics but orphaned the calibration path.
**Fix (pushed, v1.0.35):** added `_sound_title_alias`. When the sound-correction
path loads a cache whose title doesn't match the romanized heard title, it records
that heard title as an alias. `loaded_ok` now also passes when the heard title
matches the alias — so Shazam calibrates the song's timing normally, keeping
`_last_audio_off` fresh and the chorus-guard armed. Reset on track change.
**QA-verified:** part of a full button/setting + all-tabs audit (see below).

## TICKET-045 — Romanized Shazam title loaded English lyrics for a JP song 🟢
**Symptom:** after TICKET-044 fixed identification, the Niconico Marine "Ahoy!!"
karaoke synced correctly (drift -0.01) but showed **English** lyrics ("A black sky
above / Die in the waves...") with `lang: "de"` — while the video is the Japanese
"On vocal" karaoke. The correct Japanese cache (`ahoy_我ら宝鐘海賊団.json`, 65 lines)
already existed from an earlier session.
**Root cause:** with the player title leaked (TICKET-044), the app trusts Shazam's
title — but Shazam returns the ROMANIZED/English title "Ahoy!! We are Houshou
Pirates". `index.match` then exact-matched a wrong English-lyrics LRC (and saved
it as a new cache), instead of the Japanese original whose title only shares the
leading token "Ahoy". Same class as the romaji-vs-kanji problem fetch_lrc already
solves for fetching — but it wasn't applied to the local cache match.
**Fix (pushed, v1.0.34):** added `Overlay._prefer_cjk_cache(artist, heard_title,
duration)`. When the heard title is pure-Latin (no CJK) but a cached entry by the
SAME artist HAS CJK script and shares the leading Latin token (e.g. "ahoy"
survives in "Ahoy!! 我ら宝鐘海賊団☆"), prefer that original-script cache. Wired into
the sound-correction path BEFORE `index.match`. Deleted the wrong English cache.
**Verified (live, title still leaking):** log shows the full chain —
`share no content → trusting Shazam` → `preferring original-script cache
ahoy_我ら宝鐘海賊団.json over romanized title` → `correcting -> cached`. App now
shows 「あん、神様ぁ、いつかこのマリンを本物の海賊に…」 with furigana + romaji + English,
matching the video's burned-in karaoke line frame-by-frame. drift -0.02.

## TICKET-044 — Niconico sidebar title leaks → wrong song fetched 🟢
**Symptom:** while playing the Marine "Ahoy!!" karaoke on Niconico, the app
reported `player_title: "Space Marine 2 プレイ動画 #35"` (a recommended video in
Niconico's sidebar) and `heard_by_sound: ["Space Marine 2 プレイ動画 #35",
"Houshou Marine"]`. Shazam correctly heard Houshou Marine (the artist) but the
fetch attempt used the wrong title and failed; no lyrics loaded.
**Root cause:** Niconico can populate its SMTC media session with the sidebar
"now-playing" preview metadata instead of the actual main video. The app's
CJK-preserve logic (`_has_cjk(g_title) and not _has_cjk(title)`) preserves
the player's CJK title even when it's completely unrelated to what Shazam
heard. Designed for legit cases like "Kira" (Shazam) vs "綺羅" (player) — the
same song, different scripts. Backfires when the player title is from a
completely different video.
**Fix (pushed, v1.0.33):** added `_titles_share_content(a, b)` static method.
Returns True when the two titles share ANY plausible content (4-char
normalised-substring overlap, or CJK 2-gram overlap). The CJK-preserve now
requires both `_has_cjk(g_title) and not _has_cjk(title)` AND
`_titles_share_content(g_title, title)` — so a totally-unrelated player
title falls through to "trust Shazam." Logs the override decision so the
behavior is auditable.
**Verified:** Marine "Ahoy!!" with Shazam reading the correct artist
(Houshou Marine) but stale sidebar title now uses Shazam's title for the
fetch, finding the real LRC. Existing partial-script cases (Kira ↔ 綺羅,
romanized JP) still preserve the player's CJK title via the n-gram overlap
check.

## TICKET-043 — Energy correlator picked chorus-repetition match → wrong offset 🟢
**Symptom (live observation):** On Grimes "Oblivion", offset jumped from -0.76s to
**-14.37s** in a single energy-align cycle, then took 4+ Shazam re-confirmations to
recover. Log evidence: `energy-align: offset -0.76s → -14.37s (α=0.91, score 0.255,
lift 0.221)` — the correlator's "sharp peak" was actually a chorus-repetition match,
not the true offset. Shazam was simultaneously reading `audio_off=-0.47` (the correct
value).
**Root cause:** songs with repeated patterns ("la la la" choruses, repetitive hooks)
produce sharp correlation peaks at MULTIPLE candidate shifts because the vocal-mask
pattern repeats periodically. `peak_lift > 0.10` alone can't distinguish "I found the
right alignment" from "I found a chorus that looks like the previous chorus." The
correlator picked the latter and the auto-sync chased a false offset.
**Fix (pushed, v1.0.32):**
1. Track Shazam's absolute implied offset (`_last_audio_off` + `_last_audio_off_t`)
   on every Shazam read — separate from `_last_drift` which is relative.
2. In `_run_energy_correlation`, before applying a candidate offset, sanity-check
   against the last Shazam reading (within 60s):
   - If `|new_off - _last_audio_off| > 4.0s` → reject the candidate as a probable
     chorus-repetition match.
3. Reset `_last_audio_off` on track change so it doesn't leak across songs.
**Why the band is 4s:** Shazam itself can read +/-2s due to chorus ambiguity, so
allowing 4s deviation absorbs that noise while catching the gross mismatches
(13.6s in this case). Tunable via `/tune` if needed.

## TICKET-042 — Karaoke version drift cap was too tight (Niconico Marine) 🟢
**Live test:** Watching the Niconico Ahoy!! karaoke video, drift hit +7.85s
(legitimate — karaoke version is offset ~7s vs studio). Auto-sync's fast-path
refused to apply because `drift_fastpath_cap` was 5.0s. The convergence still
worked via the 2-read confirmation path eventually, but slowly.
**Iteration via `/tune` (no rebuild):**
1. Raised `drift_fastpath_cap` 5.0 → 10.0 to allow karaoke-version corrections.
2. Lowered `drift_fastpath` 4.0 → 2.0 — was too aggressive (1-read momentary
   spikes would have applied).
3. After convergence to drift 0.02s, settled on `{drift_fastpath: 3.0,
   drift_fastpath_cap: 8.0}` — handles real karaoke-version offsets (up to
   8s) without applying single momentary chorus-ambiguity reads.
**Fix (pushed, v1.0.31):** promoted those tuned values to code defaults.
This is exactly what `/tune` is for — try values live, ship the winners.

## TICKET-041 — Live-tunable sync params via /tune API (no rebuild needed) 🟢
**Request:** allow adjusting sync tuning constants on the fly without rebuilding —
iterating on `DEADBAND`/`AGREE`/spread thresholds/drift integrals took 5-minute
rebuild cycles each.
**Fix (pushed, v1.0.30):**
1. Lifted all sync constants to a `self._tune` dict on Overlay (15 parameters
   covering Shazam confirmation gates, drift integral, energy correlation, and
   auto-align cadence). Defaults match what shipped.
2. Replaced every hardcoded literal in `_consume_async` + `_maybe_auto_align`
   + `_run_energy_correlation` + `_periodic_auto_align` with `self._tune[key]`
   lookups.
3. Added Overlay methods `get_tune()` (snapshot dict) and `set_tune(key, value)`
   (type-coercing setter with logging).
4. Added two API endpoints:
   - `GET /tune` → current state of every parameter
   - `POST /tune?key=X&value=Y` OR `POST /tune` with JSON body `{k: v, …}` →
     update one or many; returns per-key results + full new state
**Tunable keys** (defaults in parens):
- `deadband` (0.8), `agree` (2.0), `agree_live` (4.0) — Shazam confirmation gates
- `spread_reset` (20), `reset_offset_max` (5) — chorus-ambiguity reset thresholds
- `drift_fastpath` (4.0), `drift_align_trigger` (6.0), `drift_min_for_accum` (0.8),
  `drift_fastpath_cap` (5.0) — drift integral mechanics
- `auto_align_cooldown` (25), `auto_align_min_pos` (12), `shazam_lock_grace` (30) —
  auto-align gating
- `continuous_recal_ms` (15000) — background correlation cadence
- `energy_apply_min` (0.4), `energy_lift_floor` (0.10), `energy_max_offset` (60) —
  energy correlation acceptance thresholds
**Verified live:** `curl GET /tune` returns all values; `curl POST /tune?key=K&value=V`
updates one; `curl POST /tune -d '{"agree":2.5}'` updates many; new values apply
immediately on the next sync tick (no restart).

## TICKET-040 — Live-tested on Grimes "Oblivion": chorus reset + slow confirmation hurt sync 🟢
**Symptom (observed live):** Grimes "Oblivion" started ~15 s desynced (lyrics ahead).
Energy correlation pulled drift to ~2.4 s, but it stuck there — the offset never fully
locked. Watching `/status` showed Shazam reading the offset correctly but the 2-read
confirmation never fired (repeated choruses → Shazam reads varied widely).
**Root causes:**
1. **Chorus-ambiguity reset was too aggressive.** When recent Shazam reads spread
   > 15 s (chorus repetition), the code reset `self.offset = 0`. For a song needing a
   real -22 s offset (studio LRC vs album cut), this kept undoing the correction every
   time it came around to a chorus. The サクラミラージュ fix that motivated this logic
   was for a SMALL offset (-11 s); the same logic killed convergence for larger ones.
2. **2-read confirmation never converged on Grimes.** Shazam reads jumped between
   different choruses, so two reads within 2.0 s of each other rarely happened. The
   pending correction kept getting replaced rather than applied.
**Fix (pushed, v1.0.29):**
1. Spread threshold 15 → 20 s, AND only reset when `|offset| < 5 s`. A larger offset is
   doing real work and shouldn't be wiped on chorus ambiguity. Verified Grimes-class
   songs no longer revert mid-song.
2. **Drift-integral fast-path** in the sync ladder: when `_drift_integral > 4.0` AND
   `|diff| < 5 s`, apply the single-read correction immediately. The accumulated
   integral IS the agreement (consistent drift direction over multiple reads). Capped
   `|diff| < 5 s` so one wild Shazam read can't yank the offset.

## TICKET-038 — Algorithmic sync: continuous drift integral, confidence-weighted updates 🟢
**Request:** make the song-position detection algorithmic rather than rely on song-specific
counters (`_align_drift_strikes >= 3` was an arbitrary threshold).
**Fix (pushed, v1.0.28):**
1. **Drift integral** replaces the strike counter. Each Shazam read where the drift
   exceeds 0.8s contributes `|drift| × time_since_last_read` to `_drift_integral`; the
   integral decays by ×0.5 when drift drops into the deadband. When it crosses 6.0
   (e.g. 1.5s drift held for 4s, or 3s drift held for 2s), auto-align triggers. Cleanly
   proportional to "how wrong the sync actually is over time," not a hardcoded count of
   reads.
2. **Continuous correlation cadence** lowered from 45s → 15s. Energy correlation is now
   the primary continuous sync source; runs every 15s in the background. The 60s vocal
   buffer keeps building enough new signal between runs.
3. **Confidence-weighted application** in `_apply_energy_align`: alpha is computed from
   correlation peak lift (sharper peak → snap to measurement; marginal peak → blend
   conservatively via EMA). Avoids yanking the offset on noisy detections while still
   converging quickly when the signal is strong. `α = max(0.3, min(1.0, (lift-0.10)/0.20 + 0.3))`.
4. A successful energy-align zeros the drift integral (clean state for next round).

## TICKET-039 — Wide-range future fixes (researched, not yet implemented) 🔴
**Findings from web research (June 2026) — high-impact, low-risk additions for a future pass:**

- **YouTube CC track fast-path** via `youtube-transcript-api` (pure Python, no
  yt-dlp/ffmpeg). Many official lyric videos / K-pop / J-pop MVs ship MANUAL caption
  tracks containing the actual lyrics with millisecond timing — bypasses LRC providers
  entirely. Auto-captions on music are unreliable (often `[Music]`), but manual tracks
  are ground truth. Needs a URL-from-browser-tab extraction step.

- **QQ Music / KuGou** via `syncedlyrics_aio` (async fork of existing dep) — adds
  Tencent provider as a drop-in. KuGou via `ll-kugou-lyric-api` or direct endpoint
  closes the Mandopop gap. Together cover ~70% of Chinese market.

- **Phonetic matching with `jellyfish`** (pure-Rust wheel, no compiler) for Whisper
  alignment. Currently uses raw `difflib.SequenceMatcher`; switching to Double Metaphone
  + Jaro-Winkler would handle ASR noise (silent letters, homophones) far better. Big
  win when faster-whisper is bundled.

- **ytmdesktop Companion Server** at `localhost:9863/api/v1` with Socket.IO real-time
  state. Push-based sub-second `videoProgress` beats Windows Media Transport polling
  for YouTube Music Desktop users specifically. Optional listener, no harm if absent.

- **`silero-vad`** (ONNX, ~1MB) for vocal-section gating. Outperforms `webrtcvad` on
  music-mixed audio. Could improve Shazam capture hit rate by fingerprinting only
  during vocal-active windows.

- **Highlighted-line + word-wipe UI with 3-dot lookahead** (UltraStar Deluxe pattern).
  Tolerates more drift than continuous scrolling because the eye locks to the active
  word. MIREX-standard tolerance is ±300ms; current scrolling exposes drift at ~150ms.

Status: 🔴 documented as future work after the user asked for "wide-range" benefits.
None blocking, all additive.

## TICKET-037 — Niconico (and other video-site) tab suffix taken as song title 🟢
**Symptom:** Niconico karaoke video showed lyrics ~10 s out of sync no matter what.
`/status` showed `matched_title: "ニコニコ動画"` and `matched_artist: "Ahoy!! 我ら宝鐘海賊団☆"` —
the LRC fetched was the **wrong song entirely**, under "ニコニコ動画" as the title.
**Root cause:** `clean_title` only stripped `" - YouTube"` from browser tab titles. For
Niconico the tab is "Ahoy!! 我ら宝鐘海賊団☆ - ニコニコ動画". Unstripped, the empty-artist
split in `_on_track_change` (`if not artist and " - " in title:`) made
artist="Ahoy!!…" and title="ニコニコ動画" — then fetched whatever same-title hit existed.
Auto-sync had nothing right to lock onto.
**Fix (pushed, v1.0.27):** broaden the browser-suffix stripper to include
ニコニコ動画 / niconico / nicovideo / Vimeo / Bilibili / Dailymotion / Twitch / SoundCloud /
Bandcamp / TikTok alongside YouTube. Deleted the two wrong cached LRCs
(`ニコニコ動画.json`, `ahoy_我ら宝鐘海賊団_ニコニコ動画.json`).
**Verified:** post-rebuild `/status` shows `matched_title: "Ahoy!! 我ら宝鐘海賊団☆"`,
`matched_artist: "Houshou Marine"`, `heard_by_sound: [same]` (Shazam confirmed),
`sync_offset: -7.14`, `sync_drift: 0.05` — tight sync. The Whisper-free auto-sync
(TICKET-036) was working all along; it just needed real lyrics to sync against.

## TICKET-036 — "Always listening" continuous auto-sync 🟢
**Request:** Niconico karaoke video (`【ニコカラHD】 Ahoy!! 我ら宝鐘海賊団`) showed lyrics ~10 s
ahead with no auto-correction. User wants the app to "always be listening and trying to
sync lyric to place in song" — Shazam can't fingerprint an off-vocal karaoke cut, so the
existing pipeline never locks the offset.
**Root cause:** sync-by-listening (`align_by_listening`) was opt-in only — needed a
manual /align trigger. And the lean .exe ships without faster-whisper (1+ GB extra), so
even if it ran it would no-op.
**Fix (pushed, v1.0.26) — two-tier continuous auto-sync:**
1. **Background Whisper align** (when faster-whisper is available):
   - Auto-aligns ~25 s into each new track (after vocal-onset gap)
   - Background heartbeat every 45 s re-checks silently
   - Drift trigger: when Shazam reports persistent drift (>1.5 s, 3 consecutive reads),
     triggers immediately — catches karaoke / live / off-vocal cuts Shazam can't lock
   - Silent UI: "🎤 Auto-synced (+X.Xs)" only when a meaningful correction lands
2. **Whisper-free fallback via vocal-energy correlation** (the default lean build):
   - `SongChangeDetector` keeps a rolling 60 s buffer of `(t_wall, vocal_ratio)` per
     0.2 s block (already computed for vocal-onset detection in TICKET-035).
   - Main thread builds a binary "vocals on/off" mask from the buffer (ratio above
     1.25× learned baseline), and a matching LRC mask for each candidate offset in
     [-15, +15] s at 0.2 s precision.
   - Picks the offset with the highest agreement score. Requires the peak to lift
     ≥0.10 above the median of all candidates — sparse / flat masks fail this check
     and the offset isn't touched.
   - Same triggers as the Whisper path. Whisper preferred when available.
**Conservative gates** (no churn, no UI spam): skips if Shazam locked within 30 s, skips
within 25 s of last align, skips while paused / in live mode, requires ≥12 s of buffer,
≥4 vocal blocks captured, new offset within 60 s, change ≥0.4 s. State (`_last_align_t`,
`_last_sound_lock_t`, `_align_drift_strikes`) resets per track.

---

### Research summary (cross-cutting)
- **Matching:** verify artist + duration, don't trust the first hit (TICKET-002/009/034).
- **Sync:** forced alignment / vocal separation; auto sync-by-listening (TICKET-003/007).
  Whisper-free fallback via vocal-band energy cross-correlation (TICKET-036).
- **Intros:** vocal-onset via band-energy ratio (not VAD) for songs with long instrumental
  intros (TICKET-035).
- **Generation:** `condition_on_previous_text=False`, RMS-VAD, overlap chunks (TICKET-005).
- **Rendering:** full-coverage CJK font + fallback to kill tofu (TICKET-006).
- **Multi-monitor:** fingerprint-based persistence + topology watchdog (TICKET-032).
- **Covers:** original-artist extraction from title beats channel-as-artist (TICKET-034).
