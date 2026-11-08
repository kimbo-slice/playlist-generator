"""Tests for searching the saved lyric corpus (no Genius or LRCLIB requests).

Offline: an in-memory store and a fake Spotify; Genius and LRCLIB are absent,
so any lyric request would fail the test.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import generate_playlist as gp
from lyric_store import InMemoryLyricStore, search_fragment
from test_pipeline import FakeCatalogSpotify, spotify_track


def saved(store, title, artist, lyrics, spotify_id=None):
    key = "\x1f".join(gp.song_key(title, artist))
    store.put({"song_key": key, "title": title, "artist": artist, "spotify_id": spotify_id,
               "lyrics": lyrics, "has_lyrics": bool(lyrics)})
    return key


class NoLyricRequests:
    """Stands in for Genius / LRCLIB: any use is a test failure."""

    def __getattr__(self, name):
        raise AssertionError("saved-lyrics search made a lyric request: %s" % name)


class FragmentTests(unittest.TestCase):
    def test_longest_word_piece(self):
        self.assertEqual(search_fragment("ain't"), "ain")
        self.assertEqual(search_fragment("Hot Dog"), "hot")
        self.assertEqual(search_fragment("!!"), "")

    def test_store_prefilter_skips_songs_without_the_word_or_without_lyrics(self):
        store = InMemoryLyricStore()
        saved(store, "A", "X", "pocket full of posies")
        saved(store, "B", "X", "nothing here")
        saved(store, "C", "X", "")
        self.assertEqual([r["title"] for r in store.songs_mentioning(["pocket"])], ["A"])
        self.assertEqual(store.songs_mentioning(["!!"]), [])


class SavedLyricsSearchTests(unittest.TestCase):
    def builder(self, store, catalog, threshold=3.0, terms=("pocket",)):
        return gp.PlaylistBuilder(
            sp=FakeCatalogSpotify(catalog), genius=NoLyricRequests(), searches=list(terms),
            threshold=threshold, playlist_id="pl", lyric_store=store,
            lyrics_fallback=lambda title, artist: (_ for _ in ()).throw(AssertionError("LRCLIB called")),
            on_event=lambda e: None)

    def test_matches_are_added_strongest_first_with_no_lyric_requests(self):
        store = InMemoryLyricStore()
        saved(store, "Weak", "Band", "pocket " + "filler words go here " * 8)
        saved(store, "Strong", "Band", "pocket pocket pocket in my pocket")
        saved(store, "Below", "Band", "pocket " + "filler words go here " * 40)
        catalog = [spotify_track("sp-strong", "Strong", "Band"), spotify_track("sp-weak", "Weak", "Band"),
                   spotify_track("sp-below", "Below", "Band")]
        b = self.builder(store, catalog)
        b.from_saved_lyrics()
        self.assertEqual(b.sp.added, ["sp-strong", "sp-weak"])

    def test_placed_track_is_remembered_and_reused(self):
        store = InMemoryLyricStore()
        key = saved(store, "Strong", "Band", "pocket pocket pocket")
        b = self.builder(store, [spotify_track("sp-strong", "Strong", "Band")])
        b.from_saved_lyrics()
        self.assertEqual(store.get(key)["spotify_id"], "sp-strong")
        self.assertEqual(store.get(key)["lyrics"], "pocket pocket pocket")   # lyrics untouched

        again = self.builder(store, [])          # Spotify search would find nothing now
        again.from_saved_lyrics()
        self.assertEqual(again.sp.added, ["sp-strong"])

    def test_songs_already_in_the_playlist_are_skipped(self):
        store = InMemoryLyricStore()
        saved(store, "Strong", "Band", "pocket pocket pocket", spotify_id="sp-strong")
        b = self.builder(store, [])
        b.register("sp-strong", "Strong", "Band")
        b.from_saved_lyrics()
        self.assertEqual(b.sp.added, [])

    def test_match_not_on_spotify_is_reported(self):
        store = InMemoryLyricStore()
        saved(store, "Rare", "Band", "pocket pocket pocket")
        b = self.builder(store, [])
        b.from_saved_lyrics()
        self.assertEqual([m["title"] for m in b.misses], ["Rare"])

    def test_multi_term_and_apostrophes(self):
        store = InMemoryLyricStore()
        saved(store, "Curly", "Band", "ain’t ain’t ain’t no way")
        saved(store, "Other", "Band", "kristin kristin kristin")
        catalog = [spotify_track("sp-curly", "Curly", "Band"), spotify_track("sp-other", "Other", "Band")]
        b = self.builder(store, catalog, terms=("ain't", "kristin"))
        b.from_saved_lyrics()
        self.assertEqual(sorted(b.sp.added), ["sp-curly", "sp-other"])

    def test_no_store_is_a_no_op(self):
        b = self.builder(None, [])
        b.from_saved_lyrics()
        self.assertEqual(b.sp.added, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
