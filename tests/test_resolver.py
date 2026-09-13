"""Unit tests for the Genius -> Spotify resolver and miss tracking.

Like test_matching.py, these never construct a real Spotify/Genius client.
A ``FakeSearchSpotify`` returns canned search results in call order, so we can
prove the query cascade, verification, and miss-recording behave correctly with
ZERO network calls (no rate-limit risk).
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import generate_playlist as gp


def track(track_id, name, artist):
    """Build a minimal Spotify track item."""
    return {"id": track_id, "name": name, "artists": [{"name": artist}]}


class FakeSearchSpotify:
    """Returns pre-canned search result lists, one per ``search`` call, in order.

    ``responses`` is a list of item-lists: the first search returns responses[0],
    the second responses[1], etc. Every query issued is recorded in ``queries``
    so tests can assert how far the cascade escalated.
    """

    def __init__(self, responses):
        self.responses = list(responses)
        self.queries = []

    def search(self, q, type="track", limit=10, offset=0):
        self.queries.append(q)
        items = self.responses.pop(0) if self.responses else []
        return {"tracks": {"items": items}}

    def me(self):
        return {"id": "test-user"}

    def user_playlist_add_tracks(self, user, playlist_id, tracks):
        pass


def make_builder(responses):
    return gp.PlaylistBuilder(
        sp=FakeSearchSpotify(responses),
        genius=None,
        searches=["hotdog"],
        threshold=1.5,
        playlist_id="playlist-123",
    )


class IsConfidentMatchTests(unittest.TestCase):
    def test_exact_match(self):
        self.assertTrue(gp.is_confident_match("Hotdog", "Simian Mobile Disco",
                                              "Hotdog", "Simian Mobile Disco"))

    def test_release_variant_matches(self):
        # feat. / remaster / parenthetical differences should still match.
        self.assertTrue(gp.is_confident_match(
            "Hotdog (Remastered 2011)", "Band feat. Guest", "Hotdog", "Band"))

    def test_spacing_difference_matches(self):
        self.assertTrue(gp.is_confident_match("Hot Dog", "Band", "Hotdog", "Band"))

    def test_wrong_artist_rejected(self):
        self.assertFalse(gp.is_confident_match("Hotdog", "Cover Band",
                                               "Hotdog", "Simian Mobile Disco"))

    def test_wrong_title_rejected(self):
        self.assertFalse(gp.is_confident_match("Cheeseburger", "Band", "Hotdog", "Band"))

    def test_empty_inputs_rejected(self):
        self.assertFalse(gp.is_confident_match("", "Band", "Hotdog", "Band"))
        self.assertFalse(gp.is_confident_match("Hotdog", "Band", "", "Band"))


class ResolveSpotifyTrackTests(unittest.TestCase):
    def test_strict_query_hits_first(self):
        b = make_builder([[track("id-1", "Hotdog", "Band")]])
        track_id, reason = b.resolve_spotify_track("Hotdog", "Band")
        self.assertEqual(track_id, "id-1")
        self.assertIsNone(reason)
        # Only the first (strict) query was needed -- no wasted API calls.
        self.assertEqual(len(b.sp.queries), 1)

    def test_escalates_when_strict_query_empty(self):
        # Strict query returns nothing; free-text query finds it.
        b = make_builder([[], [track("id-2", "Hotdog", "Band")]])
        track_id, reason = b.resolve_spotify_track("Hotdog", "Band")
        self.assertEqual(track_id, "id-2")
        self.assertIsNone(reason)
        self.assertEqual(len(b.sp.queries), 2)

    def test_rejects_wrong_track_and_reports_no_confident_match(self):
        # Every query returns a plausible-but-wrong track (a cover by another artist).
        wrong = [track("id-x", "Hotdog", "Some Cover Band")]
        b = make_builder([wrong, wrong, wrong])
        track_id, reason = b.resolve_spotify_track("Hotdog", "Simian Mobile Disco")
        self.assertIsNone(track_id)
        self.assertEqual(reason, "no_confident_match")

    def test_not_found_when_all_queries_empty(self):
        b = make_builder([[], [], []])
        track_id, reason = b.resolve_spotify_track("Obscure Song", "Nobody")
        self.assertIsNone(track_id)
        self.assertEqual(reason, "not_found")

    def test_picks_correct_track_among_several(self):
        # First candidate is wrong artist, second is the real one.
        b = make_builder([[
            track("id-wrong", "Hotdog", "Wrong Artist"),
            track("id-right", "Hotdog", "Simian Mobile Disco"),
        ]])
        track_id, reason = b.resolve_spotify_track("Hotdog", "Simian Mobile Disco")
        self.assertEqual(track_id, "id-right")
        self.assertIsNone(reason)

    def test_search_error_reason(self):
        class Boom(FakeSearchSpotify):
            def search(self, *a, **k):
                raise RuntimeError("network down")

        b = gp.PlaylistBuilder(sp=Boom([]), genius=None, searches=["x"],
                               threshold=1.5, playlist_id="p")
        track_id, reason = b.resolve_spotify_track("Hotdog", "Band")
        self.assertIsNone(track_id)
        self.assertEqual(reason, "search_error")


class MissTrackingTests(unittest.TestCase):
    def test_record_miss_appends(self):
        b = make_builder([])
        b.record_miss("Hotdog", "Band", "not_found")
        self.assertEqual(len(b.misses), 1)
        self.assertEqual(b.misses[0],
                         {"title": "Hotdog", "artist": "Band", "reason": "not_found"})

    def test_report_misses_empty_does_not_crash(self):
        b = make_builder([])
        b.report_misses()  # should simply report none
        self.assertEqual(b.misses, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
