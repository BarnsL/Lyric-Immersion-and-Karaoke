# 🎤 Desktop Karaoke

**A transparent, always-on-top karaoke overlay that floats synced, annotated
lyrics over whatever you're playing — built for learning Japanese (plus Chinese,
Korean, Spanish, German, and Russian) by singing along.**

Play a song in **Spotify, YouTube, or any app** that talks to Windows' media
controls, and Desktop Karaoke pulls the **real playback position** and streams
the lyrics across your screen — with furigana over every kanji, a reading you can
pronounce, an English translation, and a karaoke fill that sweeps in time with
the singing. There's no window and no panel: just clean, outlined text over your
screen that never steals focus, so you can keep working, watching, or gaming
while it runs.

![overlay](docs/preview.png)

---

## ✨ What it does

- **Furigana + romaji for Japanese.** Readings come from a real morphological
  analyzer (**fugashi + UniDic**, with **cutlet** for romaji), so compounds are
  segmented correctly — 今生きてる → 今(いま)生き *"ima ikite"*, not *"konjou"*.
  Katakana English is recovered as English (ベイビーアイラブユー → *"baby I love
  you"*), not spelled out phonetically.
- **Chinese → pinyin**, **Korean → romaja**, and **Russian → Latin
  transliteration**, detected per song. Every script renders with a font that
  has its glyphs, so nothing turns into □ boxes — even mixed-language lines.
- **English translation** for Japanese / Chinese / Korean / **Spanish /
  German / Russian** songs (corridos, Rammstein, t.A.T.u. …), translated in
  context for natural results. Songs are often multilingual — each part is
  romanized and translated on its own.
- **Karaoke fill** that sweeps each line at singing speed, kept in time by the
  **real song position** — not a guess from when the app launched.
- **Identify by sound.** When a title is wrong (covers, mislabeled uploads, DJ
  mixes), it listens with Shazam and fetches the lyrics for what's *actually*
  playing.
- **Seamless switching in compilations.** In one long video with many songs
  back-to-back ("openings 1-26", an album upload, a DJ set, a concert) the
  player's title never changes — so a lightweight audio **song-change detector**
  hears the moment one track ends and the next begins, and re-identifies *right
  then* instead of on a slow timer. Switches are quick, and because changes are
  now event-driven the heavy recognizer can idle between songs (**lower CPU**).
- **Scroll-through mode** with staggered lanes for a flowing, room-filling
  karaoke look — or a clean fixed-line mode.
- **Responsive sizing.** Text scales to your display automatically, so it looks
  right on a laptop or a big TV.
- **Optional dancing character.** A little companion, themed to the current
  song's artist, that bobs along to the music. Toggle it from the tray.
- **Portable & private.** No account, no telemetry. The whole app is one folder
  you can copy anywhere.

---

## ⬇️ Install (one click, no Python needed)

1. Download **`DesktopKaraoke-Setup.exe`** from the
   [**Releases**](https://github.com/BarnsL/Desktop-Karaoke/releases) page.
2. Double-click it and click **Next → Install** (no admin rights required — it
   installs just for you).
3. Optionally tick **desktop shortcut** and **Start with Windows**.

That's it. The app launches into your **system tray** (look for the purple
microphone icon) and
adds a **Desktop Karaoke** entry to your **Start menu**. Play any song and the
lyrics appear automatically.

> **Prefer portable?** Download `DesktopKaraoke.exe` instead and just
> double-click it — nothing to install. It keeps its lyric library and settings
> in the same folder, so you can run it from a USB stick.

Everything is controlled from the tray icon: presets, opacity, font size,
position, scroll style, the dancing character, and more. See **[USAGE.md](USAGE.md)**
for the full menu reference.

---

## 🎛️ Two presets to start from

Right-click the tray icon → **Presets**:

### 🎮 Learn a language while you game
A faint overlay at the top that stays out of the way. Glance up between fights
and you'll passively absorb furigana, readings, and meaning.
> Opacity **45%** · top · slide-in from left · font **100%** · Performance mode.

### 🎤 Karaoke night for a room
Big, bold, flowing lyrics everyone can read and sing from across the room.
> Opacity **100%** · bottom · scroll-through ← · font **150%** · Smooth 60 fps ·
> auto re-sync by sound.

Both are just starting points — mix your own from the tray menu.

---

## 🔎 How it works

### Stays in sync
Most lyric overlays guess timing from when they launched. Desktop Karaoke reads
the **actual song position** from Windows'
`GlobalSystemMediaTransportControls` session (the data behind your media keys),
so it follows scrubbing, pausing, and track changes for *any* player — and
freezes when the music does. It also **listens**: it identifies the song with
Shazam and aligns the clock to the true offset, with a quick burst of re-checks
right after a song starts so the timing locks within ~25 seconds. That
auto-corrects YouTube MV intros, catches drift, and follows **concert / live
videos** that contain many songs back-to-back.

For those multi-song videos there's a dedicated **song-change detector**
(`songchange.py`): a cheap RMS loudness meter on the system audio that spots the
brief near-silent gap between tracks and triggers an immediate re-identify — so
the swap to the next song's lyrics happens in a second or two, not after the next
blind poll. It's event-driven, so once a song is confirmed the Shazam poll relaxes
to a slow safety heartbeat (much less CPU/network across a long compilation).
Toggle it from the tray (**Fast song-change detect**); a *crossfaded* compilation
with no gap falls back to that heartbeat.

### Gets the *right* lyrics (sound is the authority)
Titles are unreliable — two songs by the same artist share a vibe, MV titles are
messy, and covers lie. So matching is **paranoid and sound-led**: a cached title
is accepted only if it's an exact or near-exact match (never a loose substring,
so a *different* track by the same artist is never grabbed), and **every few
seconds the song is re-checked by ear** — if what's heard doesn't match the
loaded lyrics, they're corrected on the spot. Force a correction any time with
the tray's **⚑ Wrong lyrics**, or sweep the whole library with
`python validate.py --purge`.

Every one of these decisions is written to a **log** (`karaoke.log`) you (or an
agent) can read — see [Automation](#-automation--local-api) below.

### Never bare, never boxed
Readings are added **per line by each line's own script**, so a Japanese line
inside a mostly-English song still gets furigana, and Korean/Chinese render with
fonts that have the right glyphs (no □ boxes). Songs are cached to
`lyrics/*.json` on first play and never fetched again — the local library only
grows.

---

## 🌐 Lyric sources

Lyrics are fetched on demand. The search tries the full credit, then each
featured artist, then a guarded title-only pass; if the title still misses, it
identifies the song by **sound** and fetches under the canonical name.

- [`syncedlyrics`](https://github.com/moehmeni/syncedlyrics) — aggregates
  **Musixmatch, NetEase, LRCLIB, Megalobiz, Genius** (strong VTuber / hololive /
  anime / J-pop coverage)
- [LRCLIB](https://lrclib.net) directly — duration-exact, verifiable matches
- [`shazamio`](https://github.com/shazamio/ShazamIO) +
  [`soundcard`](https://github.com/bastibe/SoundCard) — identify by **audio**
- **fugashi + UniDic + [`cutlet`](https://github.com/polm/cutlet)** (Japanese),
  [`pypinyin`](https://github.com/mozillazg/python-pinyin) (Chinese),
  [`hangul-romanize`](https://github.com/youknowone/hangul-romanize) (Korean) —
  readings; `pykakasi` is the automatic fallback
- [`deep-translator`](https://github.com/nidhaloff/deep-translator) — English
  (Google by default; DeepL if a `DEEPL_API_KEY` is set)

See the header of `fetch_lyrics.py` and [RESEARCH.md](RESEARCH.md) for candidate
future sources (PetitLyrics, QQ Music / Kugou, Apple Music) and the research
behind each design choice.

### When a song isn't found
Niche VTuber / indie tracks (a B-side that isn't on LRCLIB, Musixmatch, or
NetEase) sometimes have **no lyrics on any provider** — that's a content gap, not
a bug. For those, find or make a timed `.lrc` (a fan wiki, the video description,
or a tool like QuickLRC) and add it yourself — it gets the same furigana / romaji
/ translation as a fetched song:

```bash
python add_lrc.py "TIME TO LUV.lrc" --title "TIME TO LUV" --artist "ピーナッツくん"
# or drop "Artist - Title.lrc" files into a folder:
python add_lrc.py --folder manual
```

---

## 🛠️ From source (developers)

```bash
git clone https://github.com/BarnsL/Desktop-Karaoke.git
cd Desktop-Karaoke
pip install -r requirements.txt
pythonw main.py                  # start the overlay (no console window)
python  main.py --offset -1.5    # nudge sync earlier for an intro-heavy video
```

**Build the one-click installer yourself** — see [BUILD.md](BUILD.md):

```bash
build.bat        # → dist\DesktopKaraoke.exe  (+ DesktopKaraoke-Setup.exe if Inno Setup is installed)
```

### Build a starter library
```bash
python preload.py                  # fetch a curated ReGLOSS / hololive / J-pop set
python preload.py --translate-all  # also bake English into every song (slow)
```

### Pre-cache your Spotify playlists
```bash
# One-time: create an app at https://developer.spotify.com/dashboard
# (redirect URI http://localhost:8888/callback), copy the Client ID, then:
python sync_playlists.py --client-id YOUR_CLIENT_ID   # authorize once in the browser
python sync_playlists.py                              # later runs reuse the cached token
python sync_playlists.py --liked                      # also include Liked Songs
```
Uses Spotify's official OAuth (PKCE) — the tool never sees your password, and
your token / Client ID stay local (git-ignored).

---

## 🤖 Automation & local API

Desktop Karaoke runs a tiny HTTP server on **`127.0.0.1:8765`** (localhost only —
never the network; toggle it in the tray) so an agent or script can see what it's
doing and drive it:

Every response is JSON with a consistent `{"ok": …}` shape, bad input returns a
clean error (never a stack trace), and `GET /` returns the machine-readable
route schema — so it's safe and predictable to drive from an agent.

| Method & path | What it does |
|---------------|--------------|
| `GET /health` | liveness + version + uptime |
| `GET /status` | now-playing, matched song, sync offset, current line, song-change detector state |
| `GET /logs?n=200` | the last N log lines (every match/sound/swap decision) |
| `GET /lyrics` | the full loaded, annotated lyric lines |
| `POST /identify` | re-identify the song by **sound** now |
| `POST /wrong` | mark the current lyrics wrong → re-identify + re-fetch |
| `POST /nudge?s=2.5` | shift sync by *s* seconds (for songs Shazam can't hear) |
| `POST /reset` | reset the sync offset to 0 |
| `POST /reindex` | rescan the local library |

```bash
curl http://127.0.0.1:8765/status
curl -X POST http://127.0.0.1:8765/identify        # "that's the wrong song — listen again"
curl -X POST "http://127.0.0.1:8765/nudge?s=2.5"   # nudge the timing
```

**Security:** it binds to `127.0.0.1` only (never the network). To require auth,
set `KARAOKE_API_TOKEN` and pass it as `X-API-Token` (or `?token=`). Toggle the
whole API off from the tray.

It also writes a rolling log to **`karaoke.log`** (next to the app) recording
every track change, title‑vs‑sound match, correction, and sync adjustment — so
when something looks off you can see exactly *why* it chose what it chose.

## 📁 Project structure

| File | Role |
|------|------|
| `main.py` | The overlay: transparent click-through window, media watcher, renderer, tray menu |
| `fetch_lyrics.py` | Multi-provider fetch + verification + furigana / romaji / translation |
| `recognize.py` | Identify the playing song by **sound** (loopback capture → Shazam) |
| `songchange.py` | Audio **song-change detector** — RMS gap-spotter for seamless switching in compilations |
| `api.py` | The local HTTP API (status / logs / identify) for agents & scripts |
| `gairaigo.py` | Katakana → English loanword table (so ベイビー → "baby") |
| `character.py` | The optional dancing on-screen companion |
| `preload.py` | Bulk-build the local lyric library from a curated list |
| `add_lrc.py` | Add **any** song from a local `.lrc` file (for tracks no provider has) |
| `reannotate.py` | Re-generate furigana / romaji for the cache after a romanizer change |
| `sync_playlists.py` | Pre-cache every track in your Spotify playlists |
| `validate.py` | Scan the cache for bad / mismatched files (`--purge`) |
| `lyrics/*.json` | Cached, annotated, timed lyrics (git-ignored — not redistributed) |

### Documentation
- **[USAGE.md](USAGE.md)** — every tray menu option, explained.
- **[ARCHITECTURE.md](ARCHITECTURE.md)** — module-by-module design, every public function.
- **[BUILD.md](BUILD.md)** — how the one-click installer is produced.
- **[AGENTS.md](AGENTS.md)** — how to add songs, languages, and katakana data.
- **[RESEARCH.md](RESEARCH.md)** — the investigation and root-cause notes behind each design choice.
- **[SECURITY.md](SECURITY.md)** — exactly what data leaves the machine (almost nothing).

---

## 🔒 Privacy

No account, no telemetry, no analytics. The only things that ever leave your
machine are public song **title/artist** strings (to lyric providers) and a few
seconds of **audio** (to Shazam, for identify-by-sound). Nothing about you, your
library, or your device is sent anywhere. Full details in [SECURITY.md](SECURITY.md).

## 🎵 A note on lyrics & copyright

Song lyrics belong to their rights-holders. This tool **fetches them at runtime
for personal study** and caches them locally — the cache is *not* committed or
redistributed (see `.gitignore`). The optional dancing character is a simple
drawn avatar, not any artist's actual model. Please support the artists. 💜

## 📄 License

MIT for the code. Lyrics and any artwork belong to their respective owners.
