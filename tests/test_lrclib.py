"""Tests for the LRCLIB backup and Genius-refusal handling.

Offline: LRCLIB and Genius are fakes, the cache is in memory, and the polite
pause between LRCLIB lookups is patched out.
"""

import os
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import generate_playlist as gp
import lrclib
from lyric_store import InMemoryLyricStore


def record(title, artist, lyrics="la la", instrumental=False):
    return {"trackName": title, "artistName": artist, "plainLyrics": lyrics,
            "instrumental": instrumental}


class LookupTests(unittest.TestCase):
    def test_exact_lookup(self):
        calls = []

        def http_get(path, params):
            calls.append(path)
            return record("Tennis Court", "Lorde", "lyric text")

        self.assertEqual(lrclib.lookup_lyrics("Tennis Court", "Lorde", http_get=http_get), "lyric text")
        self.assertEqual(calls, ["get"])

    def test_wrong_song_from_get_falls_back_to_verified_search(self):
        def http_get(path, params):
            if path == "get":
                return record("Tennis Court", "Some Cover Band", "wrong")
            return [record("Tennis Court", "Some Cover Band", "wrong"),
                    record("Tennis Court", "Lorde", "right")]

        self.assertEqual(lrclib.lookup_lyrics("Tennis Court", "Lorde", http_get=http_get), "right")

    def test_not_found_is_none_and_instrumental_is_empty(self):
        missing = lambda path, params: None if path == "get" else []
        instrumental = lambda path, params: record("Kids", "Kyle Dixon", None, instrumental=True)
        self.assertIsNone(lrclib.lookup_lyrics("Nothing", "Nobody", http_get=missing))
        self.assertEqual(lrclib.lookup_lyrics("Kids", "Kyle Dixon", http_get=instrumental), "")


class VariantTests(unittest.TestCase):
    """Second search without release details; sung versions beat instrumental entries."""

    def test_plain_name_is_searched_once(self):
        self.assertEqual(lrclib.query_variants("Tennis Court", "Lorde"), [("Tennis Court", "Lorde")])
        self.assertEqual(lrclib.query_variants("When We\u2019re Together", "Kristen Bell"),
                         [("When We\u2019re Together", "Kristen Bell")])

    def test_release_details_add_a_bare_variant(self):
        self.assertEqual(lrclib.query_variants("Lovesick Blues (Walmart Performance)", "Mason Ramsey")[-1],
                         ("Lovesick Blues", "Mason Ramsey"))
        self.assertEqual(lrclib.query_variants("BLUE MOON", "HYOLYN (\ud6a8\ub9b0)")[-1],
                         ("BLUE MOON", "HYOLYN"))

    def literal_search(self, *stored):
        """A fake LRCLIB: ``get`` finds nothing; ``search`` needs the exact stored name."""
        calls = []

        def http_get(path, params):
            calls.append((path, params.get("track_name"), params.get("artist_name")))
            if path == "get":
                return None
            return [r for r in stored if (params["track_name"], params["artist_name"]) == (
                r["trackName"], r["artistName"])]
        return http_get, calls

    def test_curly_apostrophe_accepts_the_straight_quoted_record(self):
        def http_get(path, params):
            return None if path == "get" else [record("When We're Together", "Kristen Bell", "found")]
        self.assertEqual(lrclib.lookup_lyrics("When We\u2019re Together", "Kristen Bell", http_get=http_get),
                         "found")

    def test_sung_version_beats_an_instrumental_entry(self):
        def http_get(path, params):
            if path == "get":
                return record("Song", "Band", None, instrumental=True)
            return [record("Song", "Band", None, instrumental=True), record("Song", "Band", "sung")]
        self.assertEqual(lrclib.lookup_lyrics("Song", "Band", http_get=http_get), "sung")

    def test_only_instrumental_entries_mean_no_lyrics(self):
        def http_get(path, params):
            return record("Kids", "Kyle Dixon", None, instrumental=True) if path == "get" else []
        self.assertEqual(lrclib.lookup_lyrics("Kids", "Kyle Dixon", http_get=http_get), "")

    def test_release_details_are_dropped_to_find_the_song(self):
        http_get, _ = self.literal_search(record("Lovesick Blues", "Mason Ramsey", "found"))
        self.assertEqual(lrclib.lookup_lyrics("Lovesick Blues (Walmart Performance)", "Mason Ramsey",
                                              http_get=http_get), "found")

    def test_a_plain_miss_costs_two_requests(self):
        http_get, calls = self.literal_search(record("Other", "Band"))
        self.assertIsNone(lrclib.lookup_lyrics("Nothing", "Nobody", http_get=http_get))
        self.assertEqual([c[0] for c in calls], ["get", "search"])

    def test_variants_are_still_verified(self):
        http_get, _ = self.literal_search(record("Subaru", "Someone Else", "wrong"))
        self.assertIsNone(lrclib.lookup_lyrics("Subaru (Remix)", "Tuza", http_get=http_get))

    def test_get_record_without_lyrics_falls_through_to_search(self):
        def http_get(path, params):
            if path == "get":
                return record("Song", "Band", None)
            return [record("Song", "Band", "from search")]
        self.assertEqual(lrclib.lookup_lyrics("Song", "Band", http_get=http_get), "from search")


class Response:
    def __init__(self, status, body=None, headers=None):
        self.status_code, self.body, self.headers = status, body, headers or {}

    def json(self):
        return self.body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class PoliteClientTests(unittest.TestCase):
    """_http_get paces requests and honors Retry-After (LRCLIB may ban clients that don't)."""

    def setUp(self):
        self.now = 1000.0
        self.slept = []
        lrclib._last_request = 0.0

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds

    def call(self, responses):
        replies = iter(responses)
        return lrclib._http_get("get", {}, get=lambda *a, **k: next(replies),
                                sleep=self.sleep, clock=lambda: self.now)

    def test_back_to_back_requests_are_spaced_out(self):
        self.call([Response(200, {})])
        self.call([Response(200, {})])
        self.assertAlmostEqual(self.slept[-1], lrclib.MIN_INTERVAL)

    def test_429_waits_retry_after_then_succeeds(self):
        self.assertEqual(self.call([Response(429, headers={"Retry-After": "10"}), Response(200, "ok")]), "ok")
        waits = [s for s in self.slept if s >= 10]
        self.assertEqual(len(waits), 1)
        self.assertLessEqual(waits[0], 13.0)          # Retry-After plus at most 30% jitter

    def test_gives_up_after_repeated_429s(self):
        with self.assertRaises(lrclib.RateLimited):
            self.call([Response(429, headers={"Retry-After": "1"})] * (lrclib.MAX_RETRIES + 1))

    def test_404_means_not_in_the_database(self):
        self.assertIsNone(self.call([Response(404)]))


class Forbidden(Exception):
    response = types.SimpleNamespace(status_code=403)


class FakeGenius:
    """search_song answers from a dict; raises 403 when ``blocked``."""

    def __init__(self, lyrics_by_title, blocked=False):
        self.lyrics_by_title, self.blocked, self.calls = lyrics_by_title, blocked, 0

    def search_song(self, title, artist=None, get_full_info=False):
        self.calls += 1
        if self.blocked:
            raise Forbidden("403 Client Error: Forbidden")
        text = self.lyrics_by_title.get(title)
        return types.SimpleNamespace(lyrics=text) if text is not None else None


class BackupTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(gp.time, "sleep")
        patcher.start()
        self.addCleanup(patcher.stop)

    def builder(self, genius, backup, store=None, events=None):
        return gp.PlaylistBuilder(sp=None, genius=genius, searches=["x"], threshold=3.0,
                                  lyric_store=store, lyrics_fallback=backup,
                                  on_event=(events.append if events is not None else lambda e: None))

    def test_genius_lyrics_are_used_and_backup_not_asked(self):
        backup = mock.Mock()
        b = self.builder(FakeGenius({"Song": "from genius"}), backup)
        self.assertEqual(b.cached_lyrics("Song", "Band"), "from genius")
        backup.assert_not_called()

    def test_backup_fills_in_when_genius_has_no_lyrics(self):
        backup = mock.Mock(return_value="from lrclib")
        b = self.builder(FakeGenius({}), backup)
        self.assertEqual(b.cached_lyrics("Song", "Band"), "from lrclib")

    def test_a_403_stops_genius_for_the_run_and_switches_to_backup(self):
        genius = FakeGenius({}, blocked=True)
        events = []
        b = self.builder(genius, mock.Mock(return_value="from lrclib"), events=events)
        self.assertEqual(b.cached_lyrics("One", "Band"), "from lrclib")
        self.assertEqual(b.cached_lyrics("Two", "Band"), "from lrclib")
        self.assertTrue(b.genius_blocked)
        self.assertEqual(genius.calls, 1)                      # asked once, then left alone
        refusals = [e for e in events if e.get("error") == "Forbidden"]
        self.assertEqual(len(refusals), 1)                     # one message, not one per song
        self.assertIn("LRCLIB", refusals[0]["message"])

    def test_lyrics_found_anywhere_are_cached(self):
        store = InMemoryLyricStore()
        b = self.builder(FakeGenius({}, blocked=True), mock.Mock(return_value="from lrclib"), store)
        b.cached_lyrics("Song", "Band")
        self.assertEqual(store.get("song\x1fband")["lyrics"], "from lrclib")

    def test_no_lyrics_is_cached_only_when_every_source_gave_a_definite_answer(self):
        store = InMemoryLyricStore()
        b = self.builder(FakeGenius({}), mock.Mock(return_value=None), store)
        b.cached_lyrics("Nowhere", "Band")                     # Genius: not found; LRCLIB: not found
        self.assertEqual(store.get("nowhere\x1fband")["has_lyrics"], False)

    def test_genius_refusal_plus_backup_miss_is_not_cached(self):
        # Genius might know this song once it lets us back in, so don't record "no lyrics".
        store = InMemoryLyricStore()
        b = self.builder(FakeGenius({}, blocked=True), mock.Mock(return_value=None), store)
        self.assertEqual(b.cached_lyrics("Nowhere", "Band"), "")
        self.assertIsNone(store.get("nowhere\x1fband")["has_lyrics"])      # no answer recorded
        self.assertIsNotNone(store.get("nowhere\x1fband")["lrclib_miss_at"])

    def test_backup_error_is_reported_and_not_cached(self):
        store = InMemoryLyricStore()
        events = []
        b = self.builder(FakeGenius({}), mock.Mock(side_effect=RuntimeError("down")), store, events)
        self.assertEqual(b.cached_lyrics("Song", "Band"), "")
        self.assertIsNone(store.get("song\x1fband"))
        self.assertTrue(any("LRCLIB" in (e.get("message") or "") for e in events))

    def test_without_a_backup_behaviour_is_unchanged(self):
        store = InMemoryLyricStore()
        b = self.builder(FakeGenius({}), None, store)
        b.cached_lyrics("Nowhere", "Band")
        self.assertEqual(store.get("nowhere\x1fband")["has_lyrics"], False)


class RememberedMissTests(unittest.TestCase):
    """LRCLIB "not found" is remembered for LRCLIB_RECHECK_DAYS, without marking the song empty."""

    def setUp(self):
        patcher = mock.patch.object(gp.time, "sleep")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.store = InMemoryLyricStore()

    def builder(self, genius, backup):
        return gp.PlaylistBuilder(sp=None, genius=genius, searches=["x"], threshold=3.0,
                                  lyric_store=self.store, lyrics_fallback=backup, on_event=lambda e: None)

    def miss_days_ago(self, days):
        from datetime import datetime, timedelta, timezone
        self.store.note_lrclib_miss("nowhere\x1fband", "Nowhere", "Band",
                                    when=datetime.now(timezone.utc) - timedelta(days=days))

    def test_repeat_run_skips_lrclib_for_a_recent_miss(self):
        self.builder(FakeGenius({}, blocked=True), mock.Mock(return_value=None)).cached_lyrics("Nowhere", "Band")
        backup = mock.Mock(return_value=None)
        again = self.builder(FakeGenius({}, blocked=True), backup)
        self.assertEqual(again.cached_lyrics("Nowhere", "Band"), "")
        backup.assert_not_called()
        self.assertIsNone(self.store.get("nowhere\x1fband")["has_lyrics"])   # still not marked empty

    def test_lrclib_is_asked_again_after_the_recheck_period(self):
        self.miss_days_ago(gp.LRCLIB_RECHECK_DAYS + 1)
        backup = mock.Mock(return_value="arrived since")
        b = self.builder(FakeGenius({}, blocked=True), backup)
        self.assertEqual(b.cached_lyrics("Nowhere", "Band"), "arrived since")
        self.assertEqual(self.store.get("nowhere\x1fband")["lyrics"], "arrived since")

    def test_genius_is_still_asked_about_a_remembered_miss(self):
        self.miss_days_ago(1)
        b = self.builder(FakeGenius({"Nowhere": "from genius"}), mock.Mock())
        self.assertEqual(b.cached_lyrics("Nowhere", "Band"), "from genius")
        self.assertTrue(self.store.get("nowhere\x1fband")["has_lyrics"])

    def test_genius_no_plus_remembered_lrclib_miss_is_a_definite_no(self):
        self.miss_days_ago(1)
        backup = mock.Mock()
        b = self.builder(FakeGenius({}), backup)
        self.assertEqual(b.cached_lyrics("Nowhere", "Band"), "")
        backup.assert_not_called()
        self.assertIs(self.store.get("nowhere\x1fband")["has_lyrics"], False)

    def test_saved_lyrics_search_ignores_pending_rows(self):
        self.miss_days_ago(1)
        self.assertEqual(self.store.songs_mentioning(["nowhere"]), [])


class EmptyVerdictTests(unittest.TestCase):
    """The console says why a song has no lyric text, not just "no lyrics"."""

    def setUp(self):
        patcher = mock.patch.object(gp.time, "sleep")
        patcher.start()
        self.addCleanup(patcher.stop)

    def verdict(self, genius, backup, store=None):
        events = []
        b = gp.PlaylistBuilder(sp=None, genius=genius, searches=["x"], threshold=3.0,
                               lyric_store=store, lyrics_fallback=backup, on_event=events.append)
        b.evaluate_match("Song", "Band", b.cached_lyrics("Song", "Band"))
        return [e for e in events if e["kind"] == "evaluate"][-1]["verdict"]

    def test_instrumental(self):
        self.assertEqual(self.verdict(FakeGenius({}, blocked=True), mock.Mock(return_value="")), "instrumental")

    def test_not_found_anywhere(self):
        self.assertEqual(self.verdict(FakeGenius({}, blocked=True), mock.Mock(return_value=None)), "not found")
        self.assertEqual(self.verdict(FakeGenius({}), mock.Mock(return_value=None)), "not found")

    def test_genius_page_with_no_lyrics(self):
        self.assertEqual(self.verdict(FakeGenius({"Song": ""}), mock.Mock(return_value=None)), "no lyrics")

    def test_errors_are_couldnt_check(self):
        self.assertEqual(self.verdict(FakeGenius({}, blocked=True), mock.Mock(side_effect=RuntimeError)),
                         "couldn't check")
        self.assertEqual(self.verdict(FakeGenius({}, blocked=True), None), "couldn't check")

    def test_cached_empty_is_no_lyrics(self):
        store = InMemoryLyricStore()
        store.put({"song_key": "song\x1fband", "title": "Song", "artist": "Band",
                   "lyrics": "", "has_lyrics": False})
        self.assertEqual(self.verdict(FakeGenius({}), mock.Mock(), store), "no lyrics")

    def test_found_lyrics_are_scored_as_usual(self):
        self.assertEqual(self.verdict(FakeGenius({}), mock.Mock(return_value="x x x")), "MATCH")


if __name__ == "__main__":
    unittest.main(verbosity=2)
