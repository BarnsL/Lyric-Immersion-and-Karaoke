"""
Playlist import — fetch Desktop Karaoke lyrics for every track in a playlist
and cache them locally so they're ready before the song plays.

Three import paths, each blocks until done or cancelled (call from a thread):

  1. Exportify CSV  — CSV exported from exportify.net (no auth needed):
         job = ImportJob(); import_from_csv("MyPlaylist.csv", job)

  2. Spotify OAuth  — live fetch via Spotify Web API (needs a Developer Client ID).
         job = ImportJob(); import_from_spotify("abc123clientid", job)

  3. YouTube Music  — playlist URL(s) via yt-dlp (close browser first, or pass
     a cookies.txt). "LM" is a shortcut for your Liked Music playlist.
         job = ImportJob(); import_from_youtube(["LM"], job)

`ImportJob` is thread-safe. Pass `on_progress=callback` to receive live updates
(called with the job as the only argument after each track). Call `job.cancel()`
or set `job.cancelled = True` to abort mid-run.

Environment variables respected:
  SPOTIFY_CLIENT_ID   — pre-set Spotify Client ID (overridden by the client_id arg)
  DEEPL_API_KEY       — if set, translate uses DeepL instead of Google (better JP)
"""

from __future__ import annotations

import csv
import os
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from fetch_lyrics import fetch_and_save, slugify, LYRICS_DIR


# ── ImportJob ─────────────────────────────────────────────────────────────────

class ImportJob:
    """Thread-safe progress tracker for a playlist import run.

    Attributes (read from any thread after acquiring no lock — atomicity is
    guaranteed because Python's GIL makes int reads atomic, and lists grow
    monotonically):
        total       — total tracks queued (set before the run starts)
        done        — tracks processed so far (ok + skipped + failed)
        ok          — lyrics successfully fetched
        skipped     — already cached, no network call made
        failed      — list of (title, artist, reason) for errors / misses
        cancelled   — set True (or call cancel()) to abort after the current track
        last_track  — (title, artist) of the most recently processed track
        last_result — result code for last_track: "ok", "skip", "miss", "err"
        on_progress — optional callback(job) called after each track (from bg thread)
    """

    def __init__(self, on_progress: Optional[Callable] = None):
        self.total: int = 0
        self.done: int = 0
        self.ok: int = 0
        self.skipped: int = 0
        self.failed: list[tuple[str, str, str]] = []
        self.cancelled: bool = False
        self.last_track: tuple[str, str] = ("", "")
        self.last_result: str = ""
        self.on_progress = on_progress
        self._lock = threading.Lock()

    def cancel(self) -> None:
        self.cancelled = True

    def _tick(self, title: str, artist: str, result: str, error: str = "") -> None:
        """Record one processed track. Thread-safe; fires on_progress."""
        with self._lock:
            self.done += 1
            self.last_track = (title, artist)
            self.last_result = result
            if result == "ok":
                self.ok += 1
            elif result == "skip":
                self.skipped += 1
            else:
                self.failed.append((title, artist, error or result))
        if self.on_progress:
            try:
                self.on_progress(self)
            except Exception:
                pass

    @property
    def pct(self) -> float:
        """Completion percentage 0–100."""
        return (self.done / self.total * 100) if self.total else 0.0

    @property
    def summary(self) -> str:
        return (
            f"{self.ok} fetched, {self.skipped} already cached, "
            f"{len(self.failed)} missed  ({self.done}/{self.total})"
        )


# ── Exportify CSV parser ───────────────────────────────────────────────────────

def parse_exportify_csv(path: str | Path) -> list[tuple[str, str]]:
    """Parse an Exportify CSV (from exportify.net) into [(title, artist), ...].

    Handles both old-style and new-style Exportify column names. BOM-safe.
    Rows where both title and artist are empty are skipped (e.g. local files
    without metadata). Duplicate (title, artist) pairs are deduplicated
    in the order they first appear.
    """
    tracks: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    with open(path, newline="", encoding="utf-8-sig") as f:   # utf-8-sig strips BOM
        reader = csv.DictReader(f)
        for row in reader:
            title = (row.get("Track Name") or row.get("name") or "").strip()
            artist = (row.get("Artist Name(s)") or row.get("artist") or "").strip()
            if not title and not artist:
                continue
            key = (title.lower(), artist.lower())
            if key not in seen:
                seen.add(key)
                tracks.append((title, artist))

    return tracks


# ── Core import runner ─────────────────────────────────────────────────────────

def run_import(
    tracks: list[tuple[str, str]],
    job: ImportJob,
    *,
    translate: bool = False,
    force: bool = False,
    delay: float = 0.3,
) -> None:
    """Fetch lyrics for every (title, artist) in `tracks`, updating `job`.

    Skips already-cached songs unless `force=True`. Stops early when
    `job.cancelled` becomes True. `delay` seconds are inserted between network
    calls to avoid hammering lyrics providers.

    Safe to call from a background thread; `job` is thread-safe.
    """
    LYRICS_DIR.mkdir(exist_ok=True)
    for title, artist in tracks:
        if job.cancelled:
            break
        cache_path = LYRICS_DIR / f"{slugify(title)}.json"
        if cache_path.exists() and not force:
            job._tick(title, artist, "skip")
            continue
        try:
            p = fetch_and_save(title, artist, translate=translate)
            job._tick(title, artist, "ok" if p else "miss")
        except Exception as exc:
            job._tick(title, artist, "err", str(exc))
        time.sleep(delay)


# ── Import sources ─────────────────────────────────────────────────────────────

def import_from_csv(
    path: str | Path,
    job: ImportJob,
    *,
    translate: bool = False,
    force: bool = False,
    delay: float = 0.3,
) -> None:
    """Parse an Exportify CSV and fetch lyrics for every track.

    Blocks until complete or `job.cancelled`. Call from a background thread.
    Raises `FileNotFoundError` if `path` does not exist.
    """
    tracks = parse_exportify_csv(path)
    job.total = len(tracks)
    run_import(tracks, job, translate=translate, force=force, delay=delay)


def import_from_spotify(
    client_id: Optional[str],
    job: ImportJob,
    *,
    include_liked: bool = False,
    translate: bool = False,
    force: bool = False,
    delay: float = 0.3,
) -> None:
    """Spotify OAuth-PKCE — fetch all your playlists then cache lyrics.

    Opens the browser once on first run for user consent; subsequent runs use
    the cached token in `spotify_config.json` / `.spotify_cache`. Blocks until
    complete or `job.cancelled`. Call from a background thread.

    `client_id` may be None if one is already saved in `spotify_config.json` or
    set via the `SPOTIFY_CLIENT_ID` environment variable.

    Requires `spotipy` (already in requirements.txt) and a Spotify Developer App
    with redirect URI `http://localhost:8888/callback`.
    """
    # Honour env var fallback so the GUI doesn't require the user to re-type it.
    cid = client_id or os.environ.get("SPOTIFY_CLIENT_ID")

    try:
        from sync_playlists import get_client, collect_tracks
    except ImportError as exc:
        raise RuntimeError("spotipy not installed — run: pip install spotipy") from exc

    try:
        sp = get_client(cid)
    except SystemExit as exc:
        raise RuntimeError(
            "Spotify auth failed. Check your Client ID and try again."
        ) from exc

    tracks = collect_tracks(sp, include_liked=include_liked)
    job.total = len(tracks)
    run_import(tracks, job, translate=translate, force=force, delay=delay)


def import_from_youtube(
    urls: list[str],
    job: ImportJob,
    *,
    browser: str = "brave",
    cookies: Optional[str] = None,
    translate: bool = False,
    force: bool = False,
    delay: float = 0.3,
) -> None:
    """YouTube Music — fetch playlist tracks via yt-dlp then cache lyrics.

    Close your browser before importing (yt-dlp reads the cookie DB, which is
    locked while the browser is running). Alternatively pass a `cookies` path to
    a cookies.txt exported with the "Get cookies.txt" browser extension.

    `urls` can contain playlist URLs or the shortcut "LM" (Liked Music).
    Blocks until complete or `job.cancelled`. Call from a background thread.
    """
    try:
        from youtube_music import fetch_playlist
    except ImportError as exc:
        raise RuntimeError("youtube_music module not found") from exc

    all_tracks: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    for url in urls:
        rows = fetch_playlist(url, browser=(None if cookies else browser), cookies=cookies)
        for title, artist in rows:
            key = (title.lower(), artist.lower())
            if key not in seen:
                seen.add(key)
                all_tracks.append((title, artist))

    job.total = len(all_tracks)
    run_import(all_tracks, job, translate=translate, force=force, delay=delay)
