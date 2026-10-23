# Method: Title cleaning & cover extraction

**Source:** `main.py` — `clean_title`, `clean_artist`, `is_cover_title`,
`is_mv_version`, `extract_cover_original`.

Browser titles are noise. This method extracts the real (artist, song) so the other
methods get clean inputs — it doesn't score confidence itself, it *raises everyone
else's* by removing collisions.

## What it strips / extracts
- **Decoration:** "(Official MV)", "[4K]", "【MV】", "「」", "feat.", channel
  suffixes, "｜from 神椿", site suffixes ("- YouTube", "- ニコニコ動画").
- **Cover markers** (`is_cover_title`): 歌ってみた, 踊ってみた, 演奏してみた, 弾いて
  みた, 叩いてみた, (cover). A cover's lyrics ARE the **original** song's, so
  `extract_cover_original` pulls the original artist out of the title and the lyric
  search runs by **original artist**, not the covering channel
  ("Coffee - Alka | Lumi" → search *Coffee* by *Alka*). [TICKET-001]
- **MV version** (`is_mv_version`): flags an MV so the intro-hold logic engages.

## Effect on confidence
- A cleaned, distinctive title raises `title_distinctiveness` → lockable.
- A cover routes to the original artist, which is what makes the artist-keyed LRC
  search resolve the right same-title song ([TICKET-002]).

**Downstream gate:** covers set `cover=True` for `fetch_lrc`; clean audio-app
sources set `strict=True`. See [../lyric-sourcing/provider-lrc.md](../lyric-sourcing/provider-lrc.md)
and [../wrong-song-rejection/](../wrong-song-rejection/).
