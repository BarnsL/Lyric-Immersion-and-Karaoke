"""
Tests for the TICKET-226 media source-eligibility gate in main.py.

Run:  python -m pytest tests/test_media_source_policy.py -v
      (from the Desktop-Karaoke repo root, with the system Python or venv)

Regression cover for the 2026-07-19 incident: a non-media desktop application
published a Windows media session for a ~15-second notification blip (title
only, no artist, no album, no URL), the overlay accepted it as a track, and in
Subtitles mode fed its bare name to a web search that returned an unrelated
video's caption track.

No live media session is needed — the policy is pure, and MediaWatcher._pick is
exercised against a hand-built session list on an instance created without its
polling thread. All fixture text is invented placeholder text.
"""

import sys
import threading
import unittest
from pathlib import Path

# Make the repo root importable regardless of where pytest is invoked from.
_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import main
from main import (
    MediaWatcher,
    PLAYING,
    captions_body_overrun,
    is_known_media_app,
    media_source_eligible,
)


# The incident session, exactly as Windows reported it.
_INCIDENT = dict(source_app="", title="Hermes", artist="", album="",
                 duration=15.424, url="")


# ── media_source_eligible ─────────────────────────────────────────────────────

class TestMediaSourceEligible(unittest.TestCase):

    def test_incident_session_is_rejected(self):
        ok, why = media_source_eligible(**_INCIDENT)
        self.assertFalse(ok, why)
        self.assertIn("no now-playing evidence", why)

    def test_observed_hermes_session_is_rejected(self):
        # GROUND TRUTH, captured from a live GET /source on 2026-07-20 while the
        # app was misbehaving. Every other fixture in this file is reconstructed
        # or invented; this one is exactly what Windows reported, and it differs
        # from `_INCIDENT` in the field that matters most: the publisher id was
        # NOT empty. The original ticket was written without ever seeing it, so
        # the "hermes" deny entry was an educated guess that happened to be
        # right. Pinning the real string means a future rename of the deny hint
        # (or a tightening of the substring match) fails here instead of in the
        # field, on the exact session class that prompted the ticket.
        observed = dict(source_app="com.nousresearch.hermes", title="Hermes",
                        artist="", album="", duration=9.056, url="")
        ok, why = media_source_eligible(**observed)
        self.assertFalse(ok, why)
        # It must be caught by the DENY tier, not merely by the evidence floor.
        # Both would reject it today (0 of 4 signals, and 9s is under the 30s
        # floor), so asserting only "rejected" would keep passing if the deny
        # entry were removed — and then a longer TTS clip carrying an album tag
        # would sail through.
        self.assertIn("not a media application", why)
        self.assertFalse(is_known_media_app("com.nousresearch.hermes"))

    def test_tts_blip_denied_even_if_it_looks_like_a_track(self):
        # The failure the deny tier actually protects against: a text-to-speech
        # or notification session that happens to carry enough metadata to clear
        # the two-signal evidence bar. Without the deny entry this passes.
        ok, why = media_source_eligible(
            "com.nousresearch.hermes", "Placeholder Title",
            artist="Placeholder Artist", album="Placeholder Album", duration=600.0)
        self.assertFalse(ok, why)
        self.assertIn("not a media application", why)

    def test_agent_app_rejected_even_with_evidence(self):
        # The deny tier is absolute: an app that is never a media source stays
        # out even when it publishes a full-looking, long session.
        for app in ("Hermes.exe", "claude.exe", "com.anthropic.claude",
                    "ChatGPT.exe", "Code.exe", "WindowsTerminal_8wekyb3d8bbwe",
                    "pwsh.exe", "msedgewebview2.exe"):
            with self.subTest(app=app):
                ok, why = media_source_eligible(
                    app, "Placeholder Title", artist="Placeholder Artist",
                    album="Placeholder Album", duration=240.0)
                self.assertFalse(ok, f"{app} should never be a media source ({why})")
                self.assertIn("not a media application", why)

    def test_browser_session_is_accepted(self):
        ok, why = media_source_eligible("app.brave.brave", "Placeholder Video Title")
        self.assertTrue(ok, why)

    def test_music_and_video_players_accepted(self):
        for app in ("Spotify.exe", "vlc.exe", "Microsoft.ZuneMusic_8wekyb3d8bbwe"):
            with self.subTest(app=app):
                ok, why = media_source_eligible(app, "Placeholder Track")
                self.assertTrue(ok, f"{app} should be a media source ({why})")

    def test_synthetic_sources_accepted(self):
        # window_titles / discord_rpc already apply positive gates of their own.
        for app in ("window-title:steamwebhelper.exe", "discord-rpc:spotify"):
            with self.subTest(app=app):
                ok, why = media_source_eligible(app, "Placeholder Track")
                self.assertTrue(ok, why)

    def test_denied_app_wins_over_synthetic_prefix(self):
        ok, why = media_source_eligible("window-title:claude.exe", "Placeholder Track",
                                        artist="Placeholder Artist", duration=300.0)
        self.assertFalse(ok, why)

    def test_unknown_app_needs_two_signals(self):
        ok, _ = media_source_eligible("com.example.player", "Placeholder Track",
                                      artist="Placeholder Artist", duration=210.0)
        self.assertTrue(ok)
        ok, why = media_source_eligible("com.example.player", "Placeholder Track",
                                        duration=210.0)
        self.assertFalse(ok, why)
        ok, _ = media_source_eligible("com.example.player", "Placeholder Track",
                                      artist="Placeholder Artist",
                                      album="Placeholder Album", duration=0.0)
        self.assertTrue(ok)

    def test_unknown_app_short_session_is_a_blip(self):
        # Same shape as a real short track but under the duration floor: without
        # a second signal it does not qualify.
        ok, why = media_source_eligible("com.example.player", "Placeholder Track",
                                        duration=15.4)
        self.assertFalse(ok, why)

    def test_duration_floor_is_tunable(self):
        ok, _ = media_source_eligible("com.example.player", "Placeholder Track",
                                      artist="Placeholder Artist", duration=15.4,
                                      min_duration_s=10.0)
        self.assertTrue(ok)

    def test_empty_title_is_never_a_track(self):
        ok, why = media_source_eligible("app.brave.brave", "   ")
        self.assertFalse(ok, why)

    def test_policy_never_keys_on_the_title(self):
        # A real song may be NAMED after a denied app; matching is on the
        # publishing application only, so it still plays.
        ok, why = media_source_eligible("app.brave.brave", "Hermes")
        self.assertTrue(ok, why)
        ok, why = media_source_eligible("Spotify.exe", "Claude",
                                        artist="Placeholder Artist")
        self.assertTrue(ok, why)

    def test_is_known_media_app(self):
        self.assertTrue(is_known_media_app("app.brave.brave"))
        self.assertFalse(is_known_media_app(""))
        self.assertFalse(is_known_media_app("Hermes.exe"))


# ── MediaWatcher._pick ────────────────────────────────────────────────────────

def _watcher():
    """A MediaWatcher with the fields _pick needs, and no polling thread."""
    w = MediaWatcher.__new__(MediaWatcher)
    w._lock = threading.Lock()
    w._pick_src = None
    w._pinned_id = ""
    w._pinned_app = ""
    w._last_pick_reason = "init"
    w._audible_pref_on = 0
    w._audible_threshold = 0.005
    w._last_audible_scores = {}
    w._min_media_dur_s = main.MEDIA_MIN_DURATION_S
    w._rejected_sessions = []
    w._rejected_logged = set()
    return w


def _session(sid, source, title, artist="", album="", duration=0.0,
             status=PLAYING, is_current=True):
    return {"id": sid, "source": source, "title": title, "artist": artist,
            "album": album, "duration": duration, "status": status,
            "position": 0.0, "rate": 1.0, "ts": 0.0, "is_current": is_current}


class TestPickFiltersIneligibleSessions(unittest.TestCase):

    def test_incident_session_is_never_picked(self):
        w = _watcher()
        blip = _session("aaa", "", "Hermes", duration=15.424)
        self.assertIsNone(w._pick([blip], ""))
        self.assertEqual([r["reason"] for r in w.list_rejected_sessions()],
                         ["unknown app with no now-playing evidence"])

    def test_real_player_wins_over_a_blip(self):
        w = _watcher()
        blip = _session("aaa", "Hermes.exe", "Hermes", duration=15.424)
        song = _session("bbb", "app.brave.brave", "Placeholder Video Title")
        picked = w._pick([blip, song], "")
        self.assertIsNotNone(picked)
        self.assertEqual(picked["id"], "bbb")

    def test_pinned_ineligible_session_still_wins(self):
        # The user's escape hatch for a player the policy does not recognise.
        w = _watcher()
        odd = _session("ccc", "com.example.player", "Placeholder Track")
        picked = w._pick([odd], "ccc")
        self.assertIsNotNone(picked)
        self.assertEqual(picked["id"], "ccc")

    def test_rejections_are_logged_once_per_session(self):
        w = _watcher()
        blip = _session("aaa", "", "Hermes", duration=15.424)
        with self.assertLogs(main.log, level="INFO") as cm:
            for _ in range(5):
                w._pick([blip], "")
            # keep the context manager non-empty even if nothing else logs
            main.log.info("probe")
        ignored = [r for r in cm.output if "media source ignored" in r]
        self.assertEqual(len(ignored), 1, cm.output)


# ── caption body / track consistency ──────────────────────────────────────────

class TestCaptionsBodyOverrun(unittest.TestCase):

    def _body(self, end_s):
        return [{"t": [0.0, 2.0], "jp": "placeholder line one"},
                {"t": [end_s - 2.0, end_s], "jp": "placeholder line two"}]

    def test_incident_body_is_rejected(self):
        body_end, overruns = captions_body_overrun(self._body(1891.88), 15.424, 3.0)
        self.assertAlmostEqual(body_end, 1891.88, places=2)
        self.assertTrue(overruns)

    def test_matching_body_is_accepted(self):
        _, overruns = captions_body_overrun(self._body(208.0), 214.0, 3.0)
        self.assertFalse(overruns)

    def test_unreported_duration_is_not_checked(self):
        for duration in (0.0, None):
            with self.subTest(duration=duration):
                _, overruns = captions_body_overrun(self._body(1891.88), duration, 3.0)
                self.assertFalse(overruns)

    def test_factor_zero_disables_the_check(self):
        _, overruns = captions_body_overrun(self._body(1891.88), 15.424, 0)
        self.assertFalse(overruns)

    def test_empty_body(self):
        self.assertEqual(captions_body_overrun([], 214.0, 3.0), (0.0, False))


if __name__ == "__main__":
    unittest.main()
