"""Tests for the web UI's reorder endpoint (/api/reorder).

Real endpoint -> executor -> reorder path. Spotify is an in-memory fake and both
tempo services are replaced with canned answers, so there are zero network calls.
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient
import app as webapp
from tests.test_app_tracklist import FakeSpotify, drain, track

FAST, SLOW, MID, LOST = "F" * 22, "S" * 22, "M" * 22, "L" * 22
CATALOG = [track(FAST, "Fast", "Band"), track(SLOW, "Slow", "Band"),
           track(MID, "Mid", "Band"), track(LOST, "Lost", "Band")]
TEXT = "Fast - Band\nLost - Band\nNowhere - Nobody\nSlow - Band\nMid - Band\n"


def feat(bpm):
    return {"bpm": bpm, "key": None, "mode": None, "source": "x"}


class ReorderEndpointTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(webapp.app)
        webapp.manager.run = None
        webapp._tempo_cache.clear()
        self.sp = FakeSpotify(CATALOG)
        self.recco = mock.Mock(side_effect=lambda ids: {
            i: feat(b) for i, b in {FAST: 110, SLOW: 160, MID: 95}.items() if i in ids})
        self.gsb = mock.Mock(side_effect=lambda title, artist, api_key=None:
                             {"Slow": feat(80), "Mid": feat(112)}.get(title))
        self.clip = mock.Mock(return_value=None)   # no preview clips unless a test says so
        for patcher in (
            mock.patch.object(webapp, "_spotify_client", return_value=self.sp),
            mock.patch.object(webapp.reorder.tempo_sources, "reccobeats_features", self.recco),
            mock.patch.object(webapp.reorder.tempo_sources, "getsongbpm_lookup", self.gsb),
            mock.patch.object(webapp.reorder.audio_analysis, "measure", self.clip),
            mock.patch.object(webapp.reorder.time, "sleep"),
            mock.patch.dict(os.environ, {"GETSONGBPM_API_KEY": "k"}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def reorder(self, **body):
        r = self.client.post("/api/reorder", json={"text": TEXT, **body})
        self.assertEqual(r.status_code, 200)
        events, data = drain(self.client)
        self.assertEqual(data["status"], "complete")
        return next(e for e in events if e["kind"] == "reorder"), events

    def test_returns_the_new_order_and_what_needs_a_tempo(self):
        result, events = self.reorder()
        self.assertEqual(result["text"].split("\n"), [
            "Slow - Band", "Mid - Band", "Fast - Band",
            "# No tempo found -- place these by hand", "Lost - Band",
            "# Not found on Spotify", "Nowhere - Nobody"])
        self.assertEqual(result["needs"], [
            {"line": "Mid - Band", "flag": "sources disagree: 95 vs 112", "readings": [95, 112]},
            {"line": "Lost - Band", "flag": webapp.reorder.NOT_FOUND, "readings": []}])
        self.assertEqual(result["known"]["Slow - Band"], SLOW)
        self.assertTrue(any(e["kind"] == "tempo" and "2 need a decision" in e["message"]
                            for e in events))
        self.assertEqual((self.sp.created, self.sp.added), ([], []))   # Spotify untouched

    def test_a_typed_in_tempo_places_the_track_and_clears_the_question(self):
        result, events = self.reorder(tempos={"lost - band": 100, "MID - Band": 112})
        self.assertEqual(result["text"].split("\n")[:4],
                         ["Slow - Band", "Lost - Band", "Fast - Band", "Mid - Band"])
        self.assertEqual(result["needs"], [])
        self.assertTrue(any("your tempo" in e["message"] for e in events if e["kind"] == "tempo"))

    def test_reordering_again_asks_the_services_nothing(self):
        self.reorder()
        calls = (self.recco.call_count, self.gsb.call_count)
        self.reorder(tempos={"Lost - Band": 100})
        self.assertEqual((self.recco.call_count, self.gsb.call_count), calls)

    def test_empty_list_is_rejected(self):
        r = self.client.post("/api/reorder", json={"text": "# nothing\n"})
        self.assertEqual(r.status_code, 400)
        self.assertIsNone(webapp.manager.run)

    def test_a_track_neither_service_knows_is_placed_from_its_preview_clip(self):
        self.clip.side_effect = lambda title, artist: (
            {"bpm": 100.0, "key": 5, "mode": 1, "source": "audio"} if title == "Lost" else None)
        result, events = self.reorder()
        self.assertEqual(result["text"].split("\n"), [
            "Slow - Band", "Mid - Band", "Lost - Band", "Fast - Band",
            "# Not found on Spotify", "Nowhere - Nobody"])
        self.assertEqual(self.clip.call_count, 1)          # only the track nobody knew
        self.assertTrue(any("from preview clip" in (e.get("message") or "") for e in events))
        self.assertNotIn("Lost - Band", [n["line"] for n in result["needs"]])

    def test_clip_results_are_remembered(self):
        self.reorder()
        self.reorder()
        self.assertEqual(self.clip.call_count, 1)          # "no clip" was remembered too


if __name__ == "__main__":
    unittest.main(verbosity=2)
