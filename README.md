# 🎤 Desktop Karaoke

A transparent, always-on-top **karaoke overlay for Japanese learners**. It
watches whatever you're playing — Spotify, YouTube in any browser, anything that
talks to Windows' media controls — pulls the **real playback position**, and
floats synced lyrics over your screen with:

- **Furigana** above every kanji (漢字 → かんじ), **romaji** reading
- **Chinese → pinyin** and **Korean → romaja** readings too
- **English** translation (incl. **Spanish** songs & corridos)
- a **karaoke fill** that sweeps across each line *at singing speed*

Japanese, Chinese, and Korean are detected per song and romanized
appropriately. Spanish songs (and corridos) show the line plus an English
translation; English songs just show the synced line.

No window, no panel — just clean outlined text over whatever's on screen. It
never steals focus, so you can keep working / watching while it runs.

![overlay](docs/preview.png)

---

## Use cases (try a preset)

Right-click the tray icon → **Presets** for one-click setups:

### 🎮 Learn a language while you game
Keep a language ticking over in your peripheral vision while you play. The
**Gaming** preset sets a faint top overlay that never gets in the way:
> Opacity **45%**, Position **top**, Slide-in **from left**, Font **100%**,
> **Performance** mode (30fps / light outline so it costs your game nothing).

Put on a Japanese (or Chinese/Korean/Spanish) playlist, glance up between
fights, and you'll absorb furigana + readings + meaning passively.

### 🎤 Karaoke night for a room of people
Big, bold, flowing lyrics everyone can read and sing from across the room. The
**Karaoke** preset:
> Opacity **100%**, Position **bottom**, **Scroll-through ←**, Font **150%**,
> **Smooth** 60fps, **Auto re-sync by sound** on.

Lines stream across in staggered lanes (up to 3, depending on font), the current
line fills as it's sung, and the audio re-sync keeps it glued to the music — even
across a **concert / live video** with many songs back-to-back.

You can of course mix your own from the tray menu; both presets are starting
points.

---

## Why it stays in sync

Most lyric overlays guess timing from when they launched. Desktop Karaoke reads
the **actual song position** from the Windows `GlobalSystemMediaTransportControls`
session (the same data behind the media keys), so it tracks scrubbing, pausing,
and song changes for *any* player — and freezes when the music does.

On top of that it **listens**: it identifies the song with Shazam and aligns the
clock to the true offset, re-checking on an interval. That means **YouTube MV
intros** are auto-corrected, drift is caught, and **concert/live videos** that
contain many songs get the *right* lyrics for whatever is playing *right now*.

## Identify by **sound**, not just the title

Titles lie — covers, mislabeled uploads, and DJ mixes all defeat name-based
matching. Desktop Karaoke can listen to the actual audio (WASAPI loopback) and
ask Shazam what's really playing, then fetch *those* lyrics. It does this
automatically when a match looks wrong, and on demand from the tray
(**🎧 Identify by sound** / **⚑ Wrong lyrics**). Only raw audio is sent to
Shazam — no title, account, or device info.

## Getting the *right* lyrics (error detection & correction)

Common titles match the wrong song easily, and aggregators sometimes return
the wrong language entirely. Every fetch is **verified** before it's accepted:

- **Duration** — preferred matches come from LRCLIB's duration-exact endpoint;
  the real song length (from the OS) rejects same-titled wrong versions.
- **Artist** — search candidates are scored on artist + title match.
- **Language** — a CJK-titled song must come back in that script, so
  hallucinated / mistranslated lyrics are thrown out.

At runtime the overlay runs a **periodic health-check**: if the lyrics stop
fitting the song (wrong duration, lyrics ending too early, unverified match) it
identifies the track by sound and self-corrects — so it lands on the right song
eventually even if the first guess was wrong. The tray
**"⚑ Wrong lyrics — fix this song"** forces that correction immediately, and
`python validate.py --purge` sweeps the whole library.

## Lyrics coverage & sources

Lyrics are fetched on demand and the widened search tries the full credit, then
each individual / featured artist, then a guarded title-only pass — and if the
title/artist still miss (e.g. a name written as "Ikuta Rira" but filed under
"Lilas Ikuta"), it **identifies the song by sound** and fetches under the
canonical name.

**Sources used**
- [`syncedlyrics`](https://github.com/moehmeni/syncedlyrics) — aggregates
  **Musixmatch, NetEase, LRCLIB, Megalobiz, Genius** (strong VTuber / hololive /
  anime / J-pop coverage)
- [LRCLIB](https://lrclib.net) directly — duration-exact, verifiable matches
- [`shazamio`](https://github.com/shazamio/ShazamIO) + [`soundcard`](https://github.com/bastibe/SoundCard) — identify by **audio**
- [`pykakasi`](https://github.com/miurahr/pykakasi) (JP furigana/romaji),
  [`pypinyin`](https://github.com/mozillazg/python-pinyin) (ZH),
  [`hangul-romanize`](https://github.com/youknowone/hangul-romanize) (KO)
- [`deep-translator`](https://github.com/nidhaloff/deep-translator) — English

**Candidate future sources** (for songs the above still miss — see the header of
`fetch_lyrics.py` for how to wire one in): **PetitLyrics (プチリリ)** for JP/anime/
VTuber, **QQ Music / Kugou** for Chinese, **Apple Music** time-synced lyrics, and
Genius/Uta-Net/J-Lyric as unsynced last-resort fallbacks.

Songs are cached to `lyrics/*.json` on first play (lyrics + readings +
translation) and **never fetched again** — the local library only grows.
Japanese / Chinese / Korean / Spanish are detected per song; English and other
languages just show the synced line.

---

## Install

**Easiest (no Python, no terminal):** download **`DesktopKaraoke-Setup.exe`**
from [Releases](https://github.com/BarnsL/Desktop-Karaoke/releases), double-click,
click through Next → Install. Tick **Start with Windows** if you want it always
on. It launches to the system tray (あ) — play any song and lyrics appear. You
can also toggle Start-with-Windows from the tray menu any time.

> Prefer portable? Grab `DesktopKaraoke.exe` and just double-click it.

**From source (developers):**

```bash
git clone https://github.com/BarnsL/Desktop-Karaoke.git
cd Desktop-Karaoke
pip install -r requirements.txt
```

To build the installer yourself, see [BUILD.md](BUILD.md) (`build.bat`).

## Run

```bash
pythonw main.py            # start the overlay (no console window)
python  main.py --offset -1.5   # nudge sync earlier for videos with an intro
```

Everything is controlled from the tray icon (あ): presets, opacity, font,
position, scroll style, sound re-sync, performance, library backup, and more —
**see [USAGE.md](USAGE.md) for the full reference.** Play a song and the matching
lyrics appear automatically; unknown songs are fetched and cached on the fly.

**Portable & self-contained:** the packaged app keeps its library (`lyrics/`)
and `settings.json` **right next to the .exe**, so the whole thing is one folder
you can copy to a USB stick or back up wherever you like.

## Build a starter library

```bash
python preload.py          # fetch a curated ReGLOSS / hololive / J-pop set
python preload.py --translate-all   # also bake English into every song (slow)
```

Re-running only fetches what's missing, so it doubles as a "top up my library"
command.

## Sync your Spotify playlists

Pre-cache every track in all your playlists so nothing fetches mid-song:

```bash
# One-time: create an app at https://developer.spotify.com/dashboard
# (redirect URI http://localhost:8888/callback), copy the Client ID, then:
python sync_playlists.py --client-id YOUR_CLIENT_ID   # authorize once in browser
python sync_playlists.py                              # later runs (token cached)
python sync_playlists.py --liked                      # also include Liked Songs
```

Uses Spotify's official OAuth (PKCE) — the tool never sees your password, and
your token/Client ID stay local (git-ignored).

---

## How it looks under the hood

| File | Role |
|------|------|
| `main.py` | Overlay window, GSMTC media watcher, karaoke renderer |
| `fetch_lyrics.py` | Multi-provider fetch + furigana/romaji/English annotation |
| `preload.py` | Bulk-build the local lyrics library |
| `lyrics/*.json` | Cached, annotated, timed lyrics (git-ignored) |

## A note on lyrics & copyright

Song lyrics are owned by their rights-holders. This tool **fetches them at
runtime for personal study** and caches them locally — the cache is *not*
committed or redistributed (see `.gitignore`). Please support the artists. 💜

## License

MIT (the code). Lyrics belong to their respective owners.
