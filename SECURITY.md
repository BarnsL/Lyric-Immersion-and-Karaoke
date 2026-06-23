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
| Check for updates | nothing — a public **version query** | GitHub Releases API (`api.github.com`) | a `GET` only; a download happens **only if you click Install** (see *Software updates*) |
| Sync by listening (optional) | nothing — the **ASR model** is downloaded once | Hugging Face (`Systran/faster-whisper-base`) | transcription then runs **entirely locally**; no audio ever leaves the machine |

Nothing else is transmitted. No playback history, library, settings, IP-linked
identifiers, or machine details are sent anywhere. There is no analytics or
crash-reporting service. The **song-change detector** (`songchange.py`) reads the
system-audio loopback to measure **loudness only** — it computes an RMS level in
memory to spot the silent gap between tracks and transmits nothing; no audio is
stored or fingerprinted by it.

## Secrets

* The only secrets the app reads are optional **environment variables** —
  `DEEPL_API_KEY` (better translations) and `KARAOKE_API_TOKEN` (locks the local
  API, below). Both are read from the environment only, used in memory, and
  **never logged, written to disk, or committed**. Neither is required to run.

## Local API (`api.py`)

The optional agent-control API is built to be safe to leave on:

* **Localhost only.** It binds to `127.0.0.1`, so it is **never reachable from
  the network** — only processes on this machine can talk to it.
* **Optional token.** If `KARAOKE_API_TOKEN` is set, every request must present
  it (`X-API-Token` header or `?token=`); otherwise localhost is trusted.
* **Bounded + total.** POST bodies are size-capped (64 KB) and discarded; every
  handler is wrapped so a malformed request returns a clean JSON error (never a
  stack trace) and can never crash the overlay. Mutating calls are marshalled
  onto the UI thread.
* **No data exfiltration.** It only exposes the public now-playing/lyrics state
  and the local log; it cannot read files outside the app or run shell commands.
* Toggle it off entirely from the tray ("Local API") if you don't want it.

## Software updates (`updater.py`)

An updater downloads and runs code, so it is hardened to fail **closed**:

* **Verified HTTPS, GitHub hosts only.** Every request (the version check *and*
  the download) uses a certificate-verifying TLS context and is refused unless the
  URL is `https://` on `github.com` / `api.github.com` / `*.githubusercontent.com`.
  A tampered API response can't redirect the download to another host or downgrade
  to plain HTTP.
* **Integrity-checked.** If a release publishes a **SHA-256** (a `<asset>.sha256`
  file or a 64-hex digest in the notes), the downloaded `.zip` is verified against
  it and **a mismatch aborts the update**. The download is size-capped.
* **Safe extraction.** The archive is unpacked with **path-traversal ("zip-slip")
  protection** (no member may escape the staging folder) and must contain
  `DesktopKaraoke.exe`, or it's rejected.
* **Never silent / never destructive.** Updates are **opt-in** — the app only
  *notifies*; the swap runs after you click **Install update**. The helper copies
  files in without purging, so your lyric cache and settings survive, and on **any**
  failure it falls back to just opening the Releases page (nothing is applied).
* The **Microsoft Store** build defers entirely to the Store; **source** checkouts
  never self-update.

## Process / command execution

* All `subprocess` calls use **list-form arguments** (never `shell=True`), so
  there is no shell-injection surface:
  * `git` for the optional library backup — fixed argument lists, scoped to the
    app's own data folder.
  * `powershell` only to create/remove the "Start with Windows" shortcut — every
    interpolated path is escaped with `_psq()` (PowerShell single-quote doubling).
  * `yt-dlp` (playlist tools) — list-form arguments.
  * the **update helper** (`cmd /c apply_update.cmd`) runs windowless/detached and
    only **after** a download has passed the HTTPS, host, checksum, and zip-slip
    checks above; its source folder is validated to contain `DesktopKaraoke.exe`.
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
