"""
Pull songs from your YouTube Music playlists into the Desktop Karaoke library
(and a CSV), via yt-dlp using your browser's login.

    python youtube_music.py LM                       # your Liked Music
    python youtube_music.py <playlist-url> [more...]  # specific playlists
    python youtube_music.py --browser chrome LM       # use a different browser
    python youtube_music.py --cookies cookies.txt LM  # or a cookies.txt file

IMPORTANT: yt-dlp reads the browser's cookie database, which the browser LOCKS
while it's running. **Close the browser first** (or pass a cookies.txt exported
with a "Get cookies.txt" extension). For Spotify playlists use sync_playlists.py.

Writes playlists.csv (title,artist,playlist) and fetches lyrics for each track.
"""

import csv
import subprocess
import sys
from pathlib import Path

from fetch_lyrics import fetch_and_save, slugify, LYRICS_DIR

BASE = Path(__file__).parent


def _ytdlp():
    import shutil
    if shutil.which("yt-dlp"):
        return ["yt-dlp"]
    return [sys.executable, "-m", "yt_dlp"]


def fetch_playlist(url, browser=None, cookies=None):
    """Return list of (title, artist) for a YT Music playlist URL or 'LM'."""
    if url.upper() == "LM":
        url = "https://music.youtube.com/playlist?list=LM"
    cmd = _ytdlp() + ["--flat-playlist", "--no-warnings",
                      "--print", "%(title)s\t%(uploader)s\t%(channel)s", url]
    if cookies:
        cmd += ["--cookies", cookies]
    elif browser:
        cmd += ["--cookies-from-browser", browser]
    out = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    rows = []
    for line in (out.stdout or "").splitlines():
        parts = line.split("\t")
        if not parts or not parts[0]:
            continue
        title = parts[0].strip()
        artist = (parts[1] if len(parts) > 1 else "").replace(" - Topic", "").strip()
        rows.append((title, artist))
    if not rows and out.stderr:
        print(out.stderr.strip().splitlines()[-1] if out.stderr.strip() else "")
    return rows


def main():
    args = sys.argv[1:]
    browser, cookies, urls = "brave", None, []
    i = 0
    while i < len(args):
        if args[i] == "--browser" and i + 1 < len(args):
            browser, i = args[i + 1], i + 2
        elif args[i] == "--cookies" and i + 1 < len(args):
            cookies, browser, i = args[i + 1], None, i + 2
        else:
            urls.append(args[i]); i += 1
    if not urls:
        print(__doc__.strip())
        sys.exit(1)

    all_rows, seen = [], set()
    for url in urls:
        rows = fetch_playlist(url, browser=browser, cookies=cookies)
        print(f"  {url}: {len(rows)} tracks")
        for t, a in rows:
            key = (t.lower(), a.lower())
            if key not in seen:
                seen.add(key)
                all_rows.append((t, a, url))

    if not all_rows:
        print("\nNo tracks read. Close the browser (it locks its cookie DB) and "
              "retry, or pass --cookies cookies.txt.")
        sys.exit(1)

    csv_path = BASE / "playlists.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["title", "artist", "playlist"])
        w.writerows(all_rows)
    print(f"\nWrote {csv_path} ({len(all_rows)} unique songs). Fetching lyrics...\n")

    LYRICS_DIR.mkdir(exist_ok=True)
    ok = miss = skip = 0
    for i, (title, artist, _) in enumerate(all_rows, 1):
        if (LYRICS_DIR / f"{slugify(title)}.json").exists():
            skip += 1
            continue
        try:
            p = fetch_and_save(title, artist, translate=False)
            ok += p is not None
            miss += p is None
            print(f"[{i}/{len(all_rows)}] {'OK  ' if p else 'MISS'} {title} — {artist}")
        except Exception as e:
            miss += 1
            print(f"[{i}/{len(all_rows)}] ERR  {title}: {e}")

    print(f"\nDone — {ok} fetched, {skip} cached, {miss} missed.")


if __name__ == "__main__":
    main()
