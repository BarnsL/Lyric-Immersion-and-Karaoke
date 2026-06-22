# Desktop Karaoke — Usage & Settings

Everything lives in the **system-tray icon (あ)** — right-click it. Settings save
instantly to `settings.json` (next to the app) and persist across restarts.

## Quick start

1. Launch (the portable `DesktopKaraoke.exe`, or `pythonw main.py` from source).
2. Play a song anywhere — Spotify, YouTube in a browser, any media app.
3. Lyrics appear over your screen. That's it.

## Presets (start here)

| Preset | What it's for | What it sets |
|--------|---------------|--------------|
| 🎮 **Gaming** | Passive language learning while playing | 45% opacity · top · slide-in left · 100% font · Performance |
| 🎤 **Karaoke** | Big flowing lyrics for a room | 100% opacity · bottom · scroll-through ← · 150% font · Smooth · auto re-sync on |

Both are just starting points — tweak anything afterward.

## Tray menu reference

| Item | Does |
|------|------|
| **Presets** | One-click Gaming / Karaoke setups (above). |
| **⚑ Wrong lyrics — fix this song** | Bin the current match and re-identify by sound. |
| **🎧 Identify by sound** | Force an immediate Shazam listen. |
| **Auto re-sync by sound** | Re-listen on an interval (Off / 20s / 30s / 60s) to keep timing locked and to catch a new song inside a concert/live video. |
| **Library backup (Git)** | *Auto-push new songs* (opt-in) and *Back up now* — see below. |
| **Sync timing** | Nudge the offset ±0.5s / ±2.0s; shows the current offset. |
| **Opacity** | 25–100%. Low = unobtrusive over games (background stays transparent). |
| **Font size** | 25–200% in 25% steps. Scales text, layout and window. |
| **Position** | Top or bottom of the screen. |
| **Scroll-in** | Stationary · Slide from left/right · **Scroll-through →/←** (continuous ticker). |
| **Scroll-through speed** | Slow / Medium / Fast / Very fast (continuous mode only). |
| **Performance** | *Smooth* (60fps, full outline) or *Performance* (30fps, light outline). |
| **Start with Windows** | Launch automatically at login. |
| **Show / Hide**, **Re-fetch lyrics**, **Quit** | Self-explanatory. |

## How detection & sync work

- **Position** comes from Windows media controls (works for any player).
- **Song identity** is title/artist first (instant), then **Shazam confirms by
  ear** and overrides if the title was wrong (covers, mislabeled uploads).
- **Timing** is auto-calibrated to Shazam's reported song offset, and
  re-checked on the *Auto re-sync* interval — this fixes MV intros, drift and
  seeks, and follows song changes inside **concert / live videos**.

## Languages

Detected per song: **Japanese** (furigana + romaji), **Chinese** (pinyin),
**Korean** (romaja), **Spanish** (line + English). English/other songs show the
synced line. English translation fills in on first play and is cached.

## Building your library

- It builds itself as you listen — every identified song is cached to
  `lyrics/*.json` **next to the app** and never fetched again.
- Seed a big set: `python preload.py` (ReGLOSS/hololive/V.W.P/J-pop/K/C-pop/
  corridos/classic anime…).
- Pull your playlists: `python sync_playlists.py` (Spotify) /
  `python youtube_music.py` (YouTube Music).

## Optional: back your library up to Git

Off by default. The app is **portable** — its whole folder (exe + `lyrics/` +
`settings.json`) can be a git repo. One-time:

```bat
cd <the Desktop Karaoke folder>
git init && git add -A && git commit -m "my library"
git remote add origin <your-own-repo-url>   # your repo, not the public code one
```

Then in the tray, **Library backup (Git) → Auto-push new songs** commits & pushes
each new song automatically (or use **Back up now**). Lyrics are third-party
copyrighted content — push only to **your own** repo and keep it private if in
doubt.

## Files (all in one folder — portable)

```
DesktopKaraoke.exe   the app (or main.py from source)
lyrics/              cached, annotated, timed lyrics  (your growing library)
settings.json        your tray preferences
```
