"""
Tests for Overlay._player_title_verdict — whether a sound-ID agrees with the
player's own title.

Regression cover for the VIOLET incident: with nothing loaded, a single Shazam
mis-ID ("Gata Only", picked up from voice-call audio) switched instantly and
its lyrics ran over the real song, while the read that agreed with the player
title had to wait for confirmation.

Run:  python -m pytest tests/test_player_title_verdict.py -v
"""

import sys
import types
import unittest
from pathlib import Path

_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import main
from main import Overlay


def _verdict(title, artist="", heard="", heard_artist="", live=False):
    o = Overlay.__new__(Overlay)        # no window / threads — pure helpers only
    o._track = (artist, title)
    o._live_mode = live
    return o._player_title_verdict(heard, heard_artist)


class TestPlayerTitleVerdict(unittest.TestCase):

    def test_contradicting_hearing(self):
        self.assertEqual(_verdict("VIOLET", "hololive",
                                  "Gata Only (Remix)", "FloyyMenor"), "contradict")

    def test_agreeing_hearing(self):
        self.assertEqual(_verdict("VIOLET", "hololive",
                                  "VIOLET", "Ninomae Ina'nis"), "agree")

    def test_artist_prefixed_player_title_agrees(self):
        self.assertEqual(_verdict("Hoshimachi Suisei - BIBBIDIBA", "Suisei",
                                  "BIBBIDIBA", "Hoshimachi Suisei"), "agree")

    def test_site_page_cannot_vouch(self):
        self.assertIsNone(_verdict("Instagram", "", "NEXT LIFE", "paco3eyes"))

    def test_concert_cannot_vouch(self):
        self.assertIsNone(_verdict("Some Live 2026", "", "Song", "Band", live=True))

    def test_script_mismatch_cannot_vouch(self):
        self.assertIsNone(_verdict("落噺", "ReGLOSS", "Otoshibanashi", "ReGLOSS"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
