"""
Sync your Spotify playlists into the Desktop Karaoke library.

Reads every track in all your playlists (and Liked Songs) and pre-fetches
synced lyrics for them, so the overlay never has to fetch mid-song.

──────────────────────────────────────────────────────────────────────
ONE-TIME SETUP  (~2 minutes, no password shared with this tool)
  1. Open  https://developer.spotify.com/dashboard  and click "Create app".
       • App name / description: anything (e.g. "Desktop Karaoke").
       • Redirect URI:  http://localhost:8888/callback   (exactly this)
       • Check the "Web API" box, save.
  2. Open the app → Settings → copy the "Client ID".
  3. Run:
         python sync_playlists.py --client-id YOUR_CLIENT_ID
     A browser tab opens once — log in / click Agree. Done. The token is
     cached locally so future runs are just:  python sync_playlists.py
──────────────────────────────────────────────────────────────────────

    python sync_playlists.py --client-id abc123     # first run
    python sync_playlists.py                        # later (cached)
    python sync_playlists.py --translate-all        # bake English too (slow)
    python sync_playlists.py --liked                # include Liked Songs
"""

import argparse
import json
import sys
import time
from pathlib import Path

from fetch_lyrics import fetch_and_save, slugify, LYRICS_DIR

BASE = Path(__file__).parent
CONFIG = BASE / "spotify_config.json"
CACHE = BASE / ".spotify_cache"
REDIRECT = "http://localhost:8888/callback"
SCOPE = "playlist-read-private playlist-read-collaborative user-library-read"


def get_client(client_id: str | None):
    import spotipy
    from spotipy.oauth2 import SpotifyPKCE

    cfg = json.loads(CONFIG.read_text()) if CONFIG.exists() else {}
    cid = client_id or cfg.get("client_id")
    if not cid:
        print(__doc__)
        print("\n[!] No Client ID. See the one-time setup above.")
        sys.exit(1)
    cfg["client_id"] = cid
    CONFIG.write_text(json.dumps(cfg, indent=2))

    auth = SpotifyPKCE(client_id=cid, redirect_uri=REDIRECT, scope=SCOPE,
                       cache_path=str(CACHE), open_browser=True)
    return spotipy.Spotify(auth_manager=auth)


def collect_tracks(sp, include_liked=False):
    seen, songs = set(), []

    def add(track):
        if not track:
            return
        name = track.get("name")
        arts = track.get("artists") or []
        if not name:
            return
        artist = arts[0]["name"] if arts else ""
        key = (name.lower(), artist.lower())
        if key not in seen:
            seen.add(key)
            songs.append((name, artist))

    playlists = []
    res = sp.current_user_playlists(limit=50)
    while res:
        playlists.extend(res["items"])
        res = sp.next(res) if res.get("next") else None
    print(f"Found {len(playlists)} playlists\n")

    for pl in playlists:
        if not pl:
            continue
        print(f"  • {pl['name']}  ({pl['tracks']['total']} tracks)")
        tr = sp.playlist_items(
            pl["id"], limit=100,
            fields="items(track(name,artists(name))),next",
            additional_types=("track",),
        )
        while tr:
            for it in tr["items"]:
                add(it.get("track"))
            tr = sp.next(tr) if tr.get("next") else None

    if include_liked:
        print("  • Liked Songs")
        lk = sp.current_user_saved_tracks(limit=50)
        while lk:
            for it in lk["items"]:
                add(it.get("track"))
            lk = sp.next(lk) if lk.get("next") else None

    return songs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--client-id")
    ap.add_argument("--liked", action="store_true")
    ap.add_argument("--translate-all", action="store_true")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    sp = get_client(args.client_id)
    me = sp.current_user()
    print(f"Logged in as {me.get('display_name') or me.get('id')}\n")

    songs = collect_tracks(sp, include_liked=args.liked)
    print(f"\n{len(songs)} unique songs across your playlists. Fetching lyrics...\n")

    LYRICS_DIR.mkdir(exist_ok=True)
    ok = miss = skip = 0
    for i, (title, artist) in enumerate(songs, 1):
        out = LYRICS_DIR / f"{slugify(title)}.json"
        if out.exists() and not args.force:
            skip += 1
            continue
        try:
            p = fetch_and_save(title, artist, translate=args.translate_all)
            if p:
                ok += 1
                print(f"[{i}/{len(songs)}] OK   {title} — {artist}")
            else:
                miss += 1
                print(f"[{i}/{len(songs)}] MISS {title} — {artist}")
        except Exception as e:
            miss += 1
            print(f"[{i}/{len(songs)}] ERR  {title} — {artist}: {e}")
        time.sleep(0.3)

    have = len(list(LYRICS_DIR.glob("*.json")))
    print(f"\nDone — {ok} fetched, {skip} already cached, {miss} missed.")
    print(f"Library now holds {have} songs.")


if __name__ == "__main__":
    main()
