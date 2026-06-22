# Security & privacy

Desktop Karaoke is a local, offline-first overlay. It has no account, no
telemetry, and no server of its own. This documents what it does and does not do
with data and the wider system.

## What leaves the machine

| Purpose | Sent | To | Notes |
|---------|------|----|-------|
| Fetch lyrics | song **title + artist** (public strings) | LRCLIB, and via `syncedlyrics` to Musixmatch / NetEase / Megalobiz | no account, no user/device info |
| Identify by sound | a few seconds of **system audio** | Shazam (via `shazamio`) | raw audio only — never a title, account, or device id |
| Translate | lyric **lines** | Google (free) or DeepL (only if `DEEPL_API_KEY` is set) | source text only |

Nothing else is transmitted. No playback history, library, settings, IP-linked
identifiers, or machine details are sent anywhere. There is no analytics or
crash-reporting service.

## Secrets

* The only secret the app reads is the optional **`DEEPL_API_KEY`** environment
  variable. It is read at translate time and used solely to construct the DeepL
  client — never logged, written to disk, or committed. No key is required to
  run the app (it falls back to the free Google endpoint).

## Process / command execution

* All `subprocess` calls use **list-form arguments** (never `shell=True`), so
  there is no shell-injection surface:
  * `git` for the optional library backup — fixed argument lists, scoped to the
    app's own data folder.
  * `powershell` only to create/remove the "Start with Windows" shortcut — every
    interpolated path is escaped with `_psq()` (PowerShell single-quote doubling).
  * `yt-dlp` (playlist tools) — list-form arguments.
* No `eval`, `exec`, `os.system`, or dynamic code loading.

## Files

* Reads/writes stay within the **portable data folder** (`_DATA`): the lyric
  cache (`lyrics/*.json`) and `settings.json`. The app never deletes or
  overwrites files outside its own folder.

## Repository hygiene

* No personal information is committed. The lyric cache, `settings.json`,
  `spotify_config.json`, and `.spotify_cache` are **git-ignored** (copyrighted
  content / local tokens).
* Cached lyrics are third-party copyrighted content and are intentionally **not
  redistributed** — the repo ships only the code that fetches/annotates them.

## Reporting

This is a personal/educational project. If you find an issue, open an issue on
the repository.
