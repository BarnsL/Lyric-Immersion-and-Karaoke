"""
Tests for playlist_import.py.

Run:  python -m pytest tests/test_playlist_import.py -v
      (from the Desktop-Karaoke repo root, with the system Python or venv)

Live API calls (Spotify OAuth, YouTube yt-dlp) are not exercised here — those
require auth. Set KARAOKE_INTEGRATION_TESTS=1 to also run gated integration
tests that need real credentials.
"""

import io
import sys
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, call

# Make the repo root importable regardless of where pytest is invoked from.
_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from playlist_import import (
    ImportJob,
    parse_exportify_csv,
    run_import,
)

_FIXTURES = Path(__file__).parent / "fixtures"


# ── parse_exportify_csv ────────────────────────────────────────────────────────

class TestParseExportifyCsv(unittest.TestCase):

    def test_happy_path(self):
        tracks = parse_exportify_csv(_FIXTURES / "sample_playlist.csv")
        # Deduplication: "Stellar Stellar" appears twice → once in output.
        titles = [t for t, _ in tracks]
        self.assertIn("Stellar Stellar", titles)
        self.assertEqual(titles.count("Stellar Stellar"), 1)

    def test_artist_extracted(self):
        tracks = parse_exportify_csv(_FIXTURES / "sample_playlist.csv")
        by_title = {t: a for t, a in tracks}
        self.assertEqual(by_title["Stellar Stellar"], "Hoshimachi Suisei")
        self.assertEqual(by_title["Lucky Loud"], "ReGLOSS")

    def test_all_blank_row_skipped(self):
        # Row with empty title AND empty artist must be skipped.
        tracks = parse_exportify_csv(_FIXTURES / "sample_playlist.csv")
        for title, artist in tracks:
            self.assertTrue(title or artist, "blank row leaked through")

    def test_only_title_row_kept(self):
        # "Song With No Artist" has a title but no artist → should be kept.
        tracks = parse_exportify_csv(_FIXTURES / "sample_playlist.csv")
        titles = [t for t, _ in tracks]
        self.assertIn("Song With No Artist", titles)

    def test_deduplication_preserves_order(self):
        tracks = parse_exportify_csv(_FIXTURES / "sample_playlist.csv")
        # First unique occurrence order: Stellar Stellar, ビビデバ, Lucky Loud, …
        titles = [t for t, _ in tracks]
        self.assertLess(titles.index("Stellar Stellar"), titles.index("ビビデバ"))
        self.assertLess(titles.index("ビビデバ"), titles.index("Lucky Loud"))

    def test_bom_file_reads_correctly(self):
        # utf-8-sig should strip any BOM that Excel adds when saving CSV.
        tracks = parse_exportify_csv(_FIXTURES / "sample_bom.csv")
        titles = [t for t, _ in tracks]
        self.assertIn("Hello", titles)
        self.assertIn("Rolling in the Deep", titles)

    def test_new_style_column_names(self):
        # music-migrator uses lowercase "name" / "artist" when re-serialising.
        content = "name,artist\nSome Song,Some Artist\n"
        tmp = _FIXTURES / "_tmp_new_style.csv"
        tmp.write_text(content, encoding="utf-8")
        try:
            tracks = parse_exportify_csv(tmp)
            self.assertEqual(tracks, [("Some Song", "Some Artist")])
        finally:
            tmp.unlink(missing_ok=True)

    def test_empty_file(self):
        content = "Track Name,Artist Name(s)\n"   # header only, no data rows
        tmp = _FIXTURES / "_tmp_empty.csv"
        tmp.write_text(content, encoding="utf-8")
        try:
            tracks = parse_exportify_csv(tmp)
            self.assertEqual(tracks, [])
        finally:
            tmp.unlink(missing_ok=True)

    def test_file_not_found_raises(self):
        with self.assertRaises(FileNotFoundError):
            parse_exportify_csv(_FIXTURES / "nonexistent.csv")


# ── ImportJob ──────────────────────────────────────────────────────────────────

class TestImportJob(unittest.TestCase):

    def test_initial_state(self):
        job = ImportJob()
        self.assertEqual(job.total, 0)
        self.assertEqual(job.done, 0)
        self.assertEqual(job.ok, 0)
        self.assertEqual(job.skipped, 0)
        self.assertEqual(job.failed, [])
        self.assertFalse(job.cancelled)

    def test_pct_zero_when_total_zero(self):
        job = ImportJob()
        self.assertEqual(job.pct, 0.0)

    def test_pct_calculation(self):
        job = ImportJob()
        job.total = 10
        job._tick("T1", "A1", "ok")
        job._tick("T2", "A2", "ok")
        self.assertAlmostEqual(job.pct, 20.0)

    def test_cancel(self):
        job = ImportJob()
        self.assertFalse(job.cancelled)
        job.cancel()
        self.assertTrue(job.cancelled)

    def test_tick_ok(self):
        job = ImportJob()
        job._tick("Song", "Artist", "ok")
        self.assertEqual(job.ok, 1)
        self.assertEqual(job.skipped, 0)
        self.assertEqual(job.failed, [])
        self.assertEqual(job.done, 1)
        self.assertEqual(job.last_track, ("Song", "Artist"))
        self.assertEqual(job.last_result, "ok")

    def test_tick_skip(self):
        job = ImportJob()
        job._tick("Song", "Artist", "skip")
        self.assertEqual(job.skipped, 1)
        self.assertEqual(job.ok, 0)

    def test_tick_miss(self):
        job = ImportJob()
        job._tick("Song", "Artist", "miss")
        self.assertEqual(len(job.failed), 1)
        self.assertEqual(job.failed[0][0], "Song")
        self.assertEqual(job.failed[0][2], "miss")

    def test_tick_err(self):
        job = ImportJob()
        job._tick("Song", "Artist", "err", "connection refused")
        self.assertEqual(job.failed[0][2], "connection refused")

    def test_on_progress_called(self):
        received = []
        job = ImportJob(on_progress=received.append)
        job._tick("T", "A", "ok")
        self.assertEqual(len(received), 1)
        self.assertIs(received[0], job)

    def test_on_progress_exception_ignored(self):
        def bad_cb(job):
            raise RuntimeError("boom")
        job = ImportJob(on_progress=bad_cb)
        job._tick("T", "A", "ok")   # must not propagate

    def test_summary(self):
        job = ImportJob()
        job.total = 3
        job._tick("A", "", "ok")
        job._tick("B", "", "skip")
        job._tick("C", "", "miss")
        self.assertIn("1 fetched", job.summary)
        self.assertIn("1 already cached", job.summary)
        self.assertIn("1 missed", job.summary)

    def test_thread_safety(self):
        # Fire many ticks from multiple threads; counters must add up.
        job = ImportJob()
        job.total = 100
        threads = [
            threading.Thread(target=lambda: job._tick(f"T{i}", "", "ok"))
            for i in range(100)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(job.ok, 100)
        self.assertEqual(job.done, 100)


# ── run_import ─────────────────────────────────────────────────────────────────

class TestRunImport(unittest.TestCase):

    def _make_mock_module(self, fetch_return=True):
        """Return a fake fetch_lyrics module for patching."""
        mod = types.ModuleType("fetch_lyrics")
        mod.fetch_and_save = MagicMock(return_value=Path("/fake/lyrics/song.json") if fetch_return else None)
        mod.slugify = lambda s: s.lower().replace(" ", "_")
        mod.LYRICS_DIR = Path("/fake/lyrics")
        return mod

    @patch("playlist_import.LYRICS_DIR")
    @patch("playlist_import.fetch_and_save")
    @patch("playlist_import.slugify")
    def test_fetches_uncached_tracks(self, mock_slug, mock_fetch, mock_dir):
        mock_slug.side_effect = lambda s: s.lower().replace(" ", "_")
        mock_dir.__truediv__ = lambda self_, name: Path("/fake") / name
        # Simulate that no file exists → always fetches.
        with patch("pathlib.Path.exists", return_value=False), \
             patch("pathlib.Path.mkdir"):
            mock_fetch.return_value = Path("/fake/lyrics/song.json")
            job = ImportJob()
            job.total = 2
            tracks = [("Hello", "Adele"), ("Rolling in the Deep", "Adele")]
            run_import(tracks, job, delay=0)
        self.assertEqual(job.ok, 2)
        self.assertEqual(job.skipped, 0)
        self.assertEqual(mock_fetch.call_count, 2)

    @patch("playlist_import.LYRICS_DIR")
    @patch("playlist_import.fetch_and_save")
    @patch("playlist_import.slugify")
    def test_skips_cached_tracks(self, mock_slug, mock_fetch, mock_dir):
        mock_slug.side_effect = lambda s: s.lower().replace(" ", "_")
        mock_dir.__truediv__ = lambda self_, name: Path("/fake") / name
        with patch("pathlib.Path.exists", return_value=True), \
             patch("pathlib.Path.mkdir"):
            job = ImportJob()
            job.total = 2
            tracks = [("Hello", "Adele"), ("Rolling", "Adele")]
            run_import(tracks, job, delay=0)
        self.assertEqual(job.skipped, 2)
        self.assertEqual(job.ok, 0)
        mock_fetch.assert_not_called()

    @patch("playlist_import.LYRICS_DIR")
    @patch("playlist_import.fetch_and_save")
    @patch("playlist_import.slugify")
    def test_force_refetches_cached(self, mock_slug, mock_fetch, mock_dir):
        mock_slug.side_effect = lambda s: s.lower().replace(" ", "_")
        mock_dir.__truediv__ = lambda self_, name: Path("/fake") / name
        with patch("pathlib.Path.exists", return_value=True), \
             patch("pathlib.Path.mkdir"):
            mock_fetch.return_value = Path("/fake/song.json")
            job = ImportJob()
            job.total = 1
            run_import([("Hello", "Adele")], job, force=True, delay=0)
        self.assertEqual(job.ok, 1)
        self.assertEqual(job.skipped, 0)

    @patch("playlist_import.LYRICS_DIR")
    @patch("playlist_import.fetch_and_save")
    @patch("playlist_import.slugify")
    def test_cancel_stops_early(self, mock_slug, mock_fetch, mock_dir):
        mock_slug.side_effect = lambda s: s.lower()
        mock_dir.__truediv__ = lambda self_, name: Path("/fake") / name
        with patch("pathlib.Path.exists", return_value=False), \
             patch("pathlib.Path.mkdir"):
            fetch_calls = []
            def slow_fetch(title, artist, translate=False):
                fetch_calls.append(title)
                return Path("/fake/x.json")
            mock_fetch.side_effect = slow_fetch

            job = ImportJob()
            job.total = 5
            tracks = [(f"Song{i}", "Artist") for i in range(5)]
            # Cancel after the first fetch completes.
            original_tick = job._tick
            tick_count = [0]
            def cancelling_tick(title, artist, result, error=""):
                original_tick(title, artist, result, error)
                tick_count[0] += 1
                if tick_count[0] >= 1:
                    job.cancel()
            job._tick = cancelling_tick

            run_import(tracks, job, delay=0)

        # Should have stopped before all 5 tracks.
        self.assertLess(len(fetch_calls), 5)

    @patch("playlist_import.LYRICS_DIR")
    @patch("playlist_import.fetch_and_save")
    @patch("playlist_import.slugify")
    def test_miss_recorded_in_failed(self, mock_slug, mock_fetch, mock_dir):
        mock_slug.side_effect = lambda s: s.lower()
        mock_dir.__truediv__ = lambda self_, name: Path("/fake") / name
        with patch("pathlib.Path.exists", return_value=False), \
             patch("pathlib.Path.mkdir"):
            mock_fetch.return_value = None   # provider found nothing
            job = ImportJob()
            job.total = 1
            run_import([("UnknownSong", "UnknownArtist")], job, delay=0)
        self.assertEqual(job.ok, 0)
        self.assertEqual(len(job.failed), 1)
        self.assertEqual(job.failed[0][0], "UnknownSong")

    @patch("playlist_import.LYRICS_DIR")
    @patch("playlist_import.fetch_and_save")
    @patch("playlist_import.slugify")
    def test_exception_recorded_as_err(self, mock_slug, mock_fetch, mock_dir):
        mock_slug.side_effect = lambda s: s.lower()
        mock_dir.__truediv__ = lambda self_, name: Path("/fake") / name
        with patch("pathlib.Path.exists", return_value=False), \
             patch("pathlib.Path.mkdir"):
            mock_fetch.side_effect = ConnectionError("timeout")
            job = ImportJob()
            job.total = 1
            run_import([("Song", "Artist")], job, delay=0)
        self.assertEqual(len(job.failed), 1)
        self.assertIn("timeout", job.failed[0][2])

    @patch("playlist_import.LYRICS_DIR")
    @patch("playlist_import.fetch_and_save")
    @patch("playlist_import.slugify")
    def test_empty_track_list(self, mock_slug, mock_fetch, mock_dir):
        with patch("pathlib.Path.mkdir"):
            job = ImportJob()
            job.total = 0
            run_import([], job, delay=0)
        self.assertEqual(job.done, 0)
        mock_fetch.assert_not_called()

    @patch("playlist_import.LYRICS_DIR")
    @patch("playlist_import.fetch_and_save")
    @patch("playlist_import.slugify")
    def test_translate_flag_passed_through(self, mock_slug, mock_fetch, mock_dir):
        mock_slug.side_effect = lambda s: s.lower()
        mock_dir.__truediv__ = lambda self_, name: Path("/fake") / name
        with patch("pathlib.Path.exists", return_value=False), \
             patch("pathlib.Path.mkdir"):
            mock_fetch.return_value = Path("/fake/x.json")
            job = ImportJob()
            job.total = 1
            run_import([("Song", "Artist")], job, translate=True, delay=0)
        mock_fetch.assert_called_once_with("Song", "Artist", translate=True)


# ── import_from_csv (integration-lite) ────────────────────────────────────────

class TestImportFromCsv(unittest.TestCase):

    @patch("playlist_import.run_import")
    def test_sets_total_from_csv(self, mock_run):
        from playlist_import import import_from_csv
        job = ImportJob()
        import_from_csv(_FIXTURES / "sample_playlist.csv", job, delay=0)
        # The fixture has 5 unique, non-blank tracks.
        self.assertGreater(job.total, 0)
        mock_run.assert_called_once()
        # run_import(tracks, job, **kwargs) — first positional arg is the track list.
        called_tracks = mock_run.call_args.args[0]
        self.assertEqual(len(called_tracks), job.total)

    @patch("playlist_import.run_import")
    def test_passes_options(self, mock_run):
        from playlist_import import import_from_csv
        job = ImportJob()
        import_from_csv(_FIXTURES / "sample_playlist.csv", job,
                        translate=True, force=True, delay=0.7)
        kw = mock_run.call_args.kwargs
        self.assertTrue(kw["translate"])
        self.assertTrue(kw["force"])
        self.assertAlmostEqual(kw["delay"], 0.7)


if __name__ == "__main__":
    unittest.main(verbosity=2)
