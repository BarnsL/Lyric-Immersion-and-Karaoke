# Desktop Karaoke — Subsystem Docs

This folder breaks the app into its logical **parts**, one folder per subsystem.
The confidence-bearing subsystems have a file per **method**, each explaining how
that method produces a **confidence score** and what threshold gates it.

Start with [`ARCHITECTURE.md`](ARCHITECTURE.md) for the one-page map,
[`REPO_ORGANIZATION.md`](REPO_ORGANIZATION.md) for the current runtime diagram
and source/data inventory, then drill in here.

| Folder | Question it answers | Methods (→ confidence) |
|---|---|---|
| [song-identification](song-identification/) | *What song is this?* | player metadata · sound fingerprint · concert OCR · title cleaning |
| [lyric-sourcing](lyric-sourcing/) | *Get the right words + timing* | YouTube captions · provider LRC · generation by ear |
| [lyric-translation](lyric-translation/) | *Furigana / romaji / translation* | per-language annotation |
| [sync-by-sound](sync-by-sound/) | *Line lyrics up to the audio* | Shazam offset (two-point) · energy correlation · Whisper align |
| [wrong-song-rejection](wrong-song-rejection/) | *Don't show the wrong song* | cross-cutting guards |
| [mv-concert-detection](mv-concert-detection/) | *MV intros, live sets* | intro hold · live/compilation · banner OCR |

## Every doc in this folder

**Orientation**

| Doc | What it is for |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | The one-page map: the process model, each subsystem's methods, and the confidence score that gates each one. |
| [REPO_ORGANIZATION.md](REPO_ORGANIZATION.md) | Contributor-facing inventory: runtime diagram, source ownership boundaries, and where runtime data lives. |
| [DEPLOYMENT.md](DEPLOYMENT.md) | Where everything lives and how source becomes a running app: repo layout, the module map, the build/deploy/run pipeline. |
| [USAGE.md](USAGE.md) | The user-facing side: every tray menu item and setting. |

**Building and shipping**

| Doc | What it is for |
|---|---|
| [BUILD.md](BUILD.md) | Producing the packages, and the guards that stop a silently whisper-dead build shipping. Read before any hand-run build. |
| [STORE_SUBMISSION.md](STORE_SUBMISSION.md) | Publishing the MSIX to the Microsoft Store, the zero-warning install path for non-technical users. |
| [PORTING.md](PORTING.md) | The plan of record for Linux and macOS, and which phases have landed. |

**Subsystem deep dives**

| Doc | What it is for |
|---|---|
| [GENERATION.md](GENERATION.md) | Why and how lyrics get generated when no real synced lyric exists anywhere, in two tiers. |
| [SUBTITLES_MODEL_API.md](SUBTITLES_MODEL_API.md) | Subtitles mode as an explicit toggle/preset, and the model-facing behavior contract. |
| [CONCERT_DETECTION.md](CONCERT_DETECTION.md) | Reading the on-screen song banner, because a concert is one long video whose title never changes. |
| [CONCERT_AUDIO_SYNC.md](CONCERT_AUDIO_SYNC.md) | The offline pass over a concert's own audio (`concert_audio.py`), for when live arrangements defeat real-time ID. |
| [CONCERT_RESEARCH.md](CONCERT_RESEARCH.md) | A living worksheet, not a spec: per-concert test logs from the ongoing push to stay synced across full live sets. |
| [DEV_CONSOLE.md](DEV_CONSOLE.md) | The companion desktop app that shows what the engine is thinking and edits tuning knobs live, over the localhost API. |

**Tickets and logs**

| Doc | What it is for |
|---|---|
| [ISSUES.md](ISSUES.md) | Numbered matching / sync / rendering / feature tickets, with the verification rule. |
| [PERFORMANCE.md](PERFORMANCE.md) | CPU / audio-stutter / build performance tickets (PERF-###). |
| [LYRIC_PERFORMANCE.md](LYRIC_PERFORMANCE.md) | Frame rate and smoothness of the scrolling lyric overlay specifically (LP-###). |
| [RESEARCH.md](RESEARCH.md) | Every subsystem reviewed against current best practice, tagged implemented / documented / deferred. |
| [AUTORESEARCH.md](AUTORESEARCH.md) | The offline agent-driven knob-tuning loop over a known playlist (`scripts/autoresearch.py`). |

**The one rule that ties it together:** the player's title is a *hint*; **sound is
the authority**. Every subsystem is built to distrust a single signal and only act
when corroborated — that's what "confidence score" means throughout these docs.
