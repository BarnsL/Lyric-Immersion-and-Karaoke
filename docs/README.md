# Desktop Karaoke — Subsystem Docs

This folder breaks the app into its logical **parts**, one folder per subsystem.
The confidence-bearing subsystems have a file per **method**, each explaining how
that method produces a **confidence score** and what threshold gates it.

Start with [`../ARCHITECTURE.md`](../ARCHITECTURE.md) for the one-page map, then
drill in here.

| Folder | Question it answers | Methods (→ confidence) |
|---|---|---|
| [song-identification](song-identification/) | *What song is this?* | player metadata · sound fingerprint · concert OCR · title cleaning |
| [lyric-sourcing](lyric-sourcing/) | *Get the right words + timing* | YouTube captions · provider LRC · generation by ear |
| [lyric-translation](lyric-translation/) | *Furigana / romaji / translation* | per-language annotation |
| [sync-by-sound](sync-by-sound/) | *Line lyrics up to the audio* | Shazam offset (two-point) · energy correlation · Whisper align |
| [wrong-song-rejection](wrong-song-rejection/) | *Don't show the wrong song* | cross-cutting guards |
| [mv-concert-detection](mv-concert-detection/) | *MV intros, live sets* | intro hold · live/compilation · banner OCR |

Rendering/performance lives in [`../PERFORMANCE.md`](../PERFORMANCE.md);
behaviour tickets in [`../ISSUES.md`](../ISSUES.md).

**The one rule that ties it together:** the player's title is a *hint*; **sound is
the authority**. Every subsystem is built to distrust a single signal and only act
when corroborated — that's what "confidence score" means throughout these docs.
