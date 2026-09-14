"""Tests for the lyric cache (LyricStore + cache-aware fetching).

Uses InMemoryLyricStore and a call-counting fake Genius -- zero network, zero
DB. Proves cache hits skip fetches, "no lyrics" is cached (negative caching),
and transient failures are NOT cached (so they can be retried).
"""

import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import generate_playlist as gp
from lyric_store import InMemoryLyricStore


class CountingGenius:
    """Counts search_song calls; optionally raises for specific titles."""

    def __init__(self, lyrics_by_title, raise_on=None):
        self.lyrics_by_title = lyrics_by_title
        self.raise_on = set(raise_on or [])
        self.calls = 0

    def search_song(self, title, artist=None, get_full_info=False):
        self.calls += 1
        if title in self.raise_on:
            raise TimeoutError("transient")
        text = self.lyrics_by_title.get(title)
        return types.SimpleNamespace(lyrics=text) if text is not None else None


def builder(store, genius):
    return gp.PlaylistBuilder(sp=None, genius=genius, searches=["hotdog"],
                              threshold=3.0, lyric_store=store)


class CacheTests(unittest.TestCase):
    def test_hit_skips_second_fetch(self):
        g = CountingGenius({"Song": "hotdog hotdog hotdog"})
        b = builder(InMemoryLyricStore(), g)
        first = b.cached_lyrics("Song", "Artist")
        second = b.cached_lyrics("Song", "Artist")
        self.assertEqual(first, second)
        self.assertEqual(g.calls, 1)  # second served from cache

    def test_variants_share_one_entry(self):
        # A remix / feat. variant normalizes to the same song_key -> one fetch.
        g = CountingGenius({"Song": "hotdog hotdog hotdog"})
        b = builder(InMemoryLyricStore(), g)
        b.cached_lyrics("Song", "Artist")
        b.cached_lyrics("Song (Remastered 2011)", "Artist feat. Guest")
        self.assertEqual(g.calls, 1)

    def test_negative_cache_skips_refetch(self):
        # search_song returns None (no lyrics) -> cached as has_lyrics=False.
        g = CountingGenius({})
        b = builder(InMemoryLyricStore(), g)
        b.cached_lyrics("Ghost Song", "Nobody")
        b.cached_lyrics("Ghost Song", "Nobody")
        self.assertEqual(g.calls, 1)  # the junk isn't re-fetched

    def test_transient_error_not_cached(self):
        # A raised error must NOT be cached as "no lyrics" -> it can be retried.
        g = CountingGenius({}, raise_on={"Flaky"})
        b = builder(InMemoryLyricStore(), g)
        b.cached_lyrics("Flaky", "X")
        b.cached_lyrics("Flaky", "X")
        self.assertEqual(g.calls, 2)  # re-fetched because the failure wasn't cached

    def test_no_store_still_fetches(self):
        # Without a store, behavior is unchanged (always fetch).
        g = CountingGenius({"Song": "hotdog hotdog"})
        b = builder(None, g)
        b.cached_lyrics("Song", "Artist")
        b.cached_lyrics("Song", "Artist")
        self.assertEqual(g.calls, 2)


class InMemoryStoreTests(unittest.TestCase):
    def test_roundtrip_and_isolation(self):
        s = InMemoryLyricStore()
        self.assertIsNone(s.get("k"))
        s.put({"song_key": "k", "title": "t", "artist": "a", "lyrics": "L", "has_lyrics": True})
        got = s.get("k")
        self.assertEqual(got["lyrics"], "L")
        got["lyrics"] = "mutated"          # returned dict is a copy...
        self.assertEqual(s.get("k")["lyrics"], "L")  # ...store is unaffected


if __name__ == "__main__":
    unittest.main(verbosity=2)
