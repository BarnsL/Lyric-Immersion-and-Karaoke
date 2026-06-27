# Sales / Commercialization Considerations

Running checklist of the **legal / IP / ToS** items to clear before the app is
sold or otherwise distributed commercially. This is engineering's tracking list,
**not legal advice** — get an IP attorney's review before any paid release,
especially given how aggressively J-pop / K-pop / VTuber (hololive, Cover Corp,
Korean labels) rights-holders enforce.

Status: 🔴 open · 🟡 in-progress · 🟢 done

---

## The core principle
**The software is yours to sell; the lyrics are the liability.** The overlay
engine, sync, Whisper integration, furigana/romaji/annotation and UI are original
work. Song *lyrics* are copyrighted by music **publishers**; displaying or
redistributing them — especially commercially — needs a licence. Every item below
is about keeping copyrighted **content** and **ToS-restricted services** out of a
product you charge for.

## Checklist

### 1. Baked-in lyrics — 🟡 in-progress
- 🟢 **Untracked from the repo** (`bundled_lyrics/` git-ignored; kept locally only
  so personal builds still bundle `feelingradation` / `サクラミラージュ`). A clean
  clone / sold build ships with none.
- 🔴 **Purge from git history** — the two files still exist in older commits, so
  they're publicly reachable via `git log`/checkout. Needs a `git filter-repo`
  history rewrite of `master`. **Deferred:** rewriting `master` forces a re-clone
  on every other machine (multi-machine workflow) — do this as a coordinated step.

### 2. Provider lyric fetching — 🔴 open
- Genius / LRCLIB / syncedlyrics each have their own ToS; several **restrict
  scraping and/or commercial use**. Fetching for a *paid* product likely breaches
  them. Options: (a) license a commercial lyrics API (Musixmatch / LyricFind);
  (b) reframe fetching as the **user's own personal-use** action; (c) drop it.

### 3. yt-dlp (YouTube caption/audio pull) — 🔴 open
- Downloading from YouTube violates YouTube ToS; commercial use compounds it.
  Used for accurate caption tracks and for audio fed to Whisper. For a sold
  product, make it clearly user-driven/optional or remove it.

### 4. shazamio (song identification) — 🔴 open
- Unofficial Shazam (Apple) API client; commercial use is against their ToS.
  Replace with a licensed fingerprint service, or make ID rely on the (already
  strong) by-ear lyric matching + title signals instead.

### 5. Translations — 🔴 open
- Shown translations are **derivative works**; the right to translate belongs to
  the rights-holder. Same licensing/personal-use framing as the lyrics applies.

### 6. Dependency license audit — 🔴 open
- Confirm no **GPL/AGPL** dependency forces open-sourcing the whole app. Mostly
  permissive today (faster-whisper MIT, numpy BSD, PyInstaller has a commercial-OK
  bootloader exception), but audit the full tree before shipping.

### 7. Distribution model — 🔴 open
- Safest path to monetize: **sell the software, ship ZERO copyrighted content**,
  frame all lyric/audio fetching as the user's personal-use activity, and drop or
  gate the ToS-violating services. Alternatives: license lyrics properly (costs
  money, fully legit) or keep it free/donation (strongest fair-use footing as a
  language-learning tool).

### 8. Branding / assets — 🔴 open
- Ship only original or properly-licensed icons, fonts, and the dancing-character
  art. Don't use artist names/logos in a way that implies endorsement.
