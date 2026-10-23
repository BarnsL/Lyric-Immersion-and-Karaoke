# Method: Concert banner OCR

**Source:** `concert_ocr.py` using Windows.Media.Ocr on a grab of the video region.
Live streams and concerts caption the current song on screen ("♪ 次の曲 / Now
Playing: …"); reading that banner identifies each song inside one long video where
the player title is just the event name.

**When it runs:** only in **live/compilation mode** (`is_live_or_compilation`), where
the title is deliberately ignored (see [../mv-concert-detection/](../mv-concert-detection/)).

## Confidence
- OCR text is fuzzy-matched against the local song library.
- **Accept only `score ≥ 0.85`** — a high bar, because OCR on stylized concert
  overlays is noisy and a loose match would load the wrong song.
- Below threshold → ignore the banner, fall back to [sound fingerprint](sound-fingerprint.md).

**Gate before acting:** fuzzy match ≥ 0.85 against a known library entry, otherwise
sound drives the identification.
