# 🎤 Desktop Karaoke

A transparent, always-on-top **karaoke overlay for Japanese learners**. It
watches whatever you're playing — Spotify, YouTube in any browser, anything that
talks to Windows' media controls — pulls the **real playback position**, and
floats synced lyrics over your screen with:

- **Furigana** above every kanji (漢字 → かんじ)
- **Romaji** reading
- **English** translation
- a **karaoke fill** that sweeps across each line *at singing speed*

No window, no panel — just clean outlined text over whatever's on screen. It
never steals focus, so you can keep working / watching while it runs.

![overlay](docs/preview.png)

---

## Why it stays in sync

Most lyric overlays guess timing from when they launched. Desktop Karaoke reads
the **actual song position** from the Windows `GlobalSystemMediaTransportControls`
session (the same data behind the media keys), so it tracks scrubbing, pausing,
and song changes for *any* player — and freezes when the music does.

## Lyrics coverage

Lyrics are fetched on demand via [`syncedlyrics`](https://github.com/moehmeni/syncedlyrics),
which aggregates **Musixmatch, NetEase, LRCLIB, Megalobiz and Genius** — strong
coverage for VTuber / hololive / anime / J-pop that single sources miss.
Japanese lines are annotated locally with [`pykakasi`](https://github.com/miurahr/pykakasi)
(furigana + romaji) and translated with
[`deep-translator`](https://github.com/nidhaloff/deep-translator).

Non-Japanese songs (English, etc.) work too — they just show the synced line
with the karaoke sweep, no furigana.

---

## Install

```bash
git clone https://github.com/BarnsL/Desktop-Karaoke.git
cd Desktop-Karaoke
pip install -r requirements.txt
```

## Run

```bash
pythonw main.py            # start the overlay (no console window)
python  main.py --offset -1.5   # nudge sync earlier for videos with an intro
```

A tray icon (あ) gives you **sync nudges**, **show/hide**, **re-fetch lyrics**
and **quit**. Play a song and the matching lyrics appear automatically; unknown
songs are fetched and added to your library on the fly.

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
