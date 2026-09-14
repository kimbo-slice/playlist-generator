"""End-to-end pipeline tests: run the REAL from_genius / from_spotify loops.

Nothing in the production logic is mocked here -- only the network clients are
fake, returning canned API-shaped data. This exercises the full orchestration
(search paging -> lyric match -> Spotify resolution -> de-dup -> add -> miss
report) that the unit tests don't cover, and still makes ZERO real API calls.
"""

import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import generate_playlist as gp


def spotify_track(track_id, name, artist):
    return {"id": track_id, "name": name, "artists": [{"name": artist}]}


def genius_hit(url, title, artist):
    return {"result": {"url": url, "title": title, "primary_artist": {"name": artist}}}


class FakeGenius:
    """Fake lyricsgenius client: canned search pages + per-URL lyrics."""

    def __init__(self, pages, lyrics_map, songs_map=None):
        self.pages = pages
        self.lyrics_map = lyrics_map
        self.songs_map = songs_map or {}

    def search_songs(self, query, per_page=5, page=1):
        hits = self.pages[page - 1] if page - 1 < len(self.pages) else []
        return {"hits": hits}

    def lyrics(self, song_url):
        return self.lyrics_map.get(song_url, "")

    def search_song(self, title, artist=None, get_full_info=False):
        text = self.songs_map.get(title)
        return types.SimpleNamespace(lyrics=text) if text is not None else None


class FakeCatalogSpotify:
    """Fake Spotify whose search returns catalog tracks whose name appears in the query."""

    def __init__(self, catalog):
        self.catalog = catalog
        self.added = []

    def me(self):
        return {"id": "test-user"}

    def search(self, q, type="track", limit=10, offset=0):
        ql = q.lower()
        items = [t for t in self.catalog if t["name"].lower() in ql]
        return {"tracks": {"items": items, "total": len(items)}}

    def user_playlist_add_tracks(self, user, playlist_id, tracks):
        self.added.extend(tracks)


class FakeDiscoverySpotify:
    """Fake Spotify that returns a page of search results per offset (for from_spotify)."""

    def __init__(self, pages):
        self.pages = pages
        self.added = []

    def me(self):
        return {"id": "test-user"}

    def search(self, q, type="track", limit=50, offset=0):
        idx = offset // 50
        items = self.pages[idx] if idx < len(self.pages) else []
        return {"tracks": {"items": items, "total": 999}}

    def user_playlist_add_tracks(self, user, playlist_id, tracks):
        self.added.extend(tracks)


class FromGeniusPipelineTests(unittest.TestCase):
    def test_full_genius_run(self):
        # One hit that matches AND is on Spotify -> added.
        # One hit that matches lyrics but is NOT on Spotify -> recorded as a miss.
        # One hit whose lyrics do NOT match -> silently skipped (not a miss).
        pages = [
            [
                genius_hit("u1", "Hotdog", "Simian Mobile Disco"),
                genius_hit("u2", "Rare Jam", "Unknown"),
                genius_hit("u3", "Salad Song", "Veggie"),
            ],
            [],  # page 2 empty -> loop stops
        ]
        lyrics_map = {
            "u1": "hotdog hotdog hotdog hotdog",
            "u2": "hotdog hotdog hotdog",
            "u3": "lettuce and tomato only",
        }
        catalog = [spotify_track("sp-hotdog", "Hotdog", "Simian Mobile Disco")]

        builder = gp.PlaylistBuilder(
            sp=FakeCatalogSpotify(catalog),
            genius=FakeGenius(pages, lyrics_map),
            searches=["hotdog"],
            threshold=1.5,
            playlist_id="playlist-123",
        )
        builder.from_genius("hotdog")

        # Exactly the matched, resolvable song was added.
        self.assertEqual(builder.sp.added, ["sp-hotdog"])
        # The matched-but-unplaceable song was reported, not lost.
        self.assertEqual(len(builder.misses), 1)
        self.assertEqual(builder.misses[0]["title"], "Rare Jam")
        self.assertEqual(builder.misses[0]["reason"], "not_found")

    def test_genius_run_dedups_repeat_hit(self):
        # The same song appears on two pages -> added once.
        pages = [
            [genius_hit("u1", "Hotdog", "Simian Mobile Disco")],
            [genius_hit("u1b", "Hotdog", "Simian Mobile Disco")],
            [],
        ]
        lyrics_map = {"u1": "hotdog hotdog hotdog", "u1b": "hotdog hotdog hotdog"}
        catalog = [spotify_track("sp-hotdog", "Hotdog", "Simian Mobile Disco")]

        builder = gp.PlaylistBuilder(
            sp=FakeCatalogSpotify(catalog),
            genius=FakeGenius(pages, lyrics_map),
            searches=["hotdog"],
            threshold=1.5,
            playlist_id="p",
        )
        builder.from_genius("hotdog")
        self.assertEqual(builder.sp.added, ["sp-hotdog"])  # not duplicated


class SpotifyResilienceTests(unittest.TestCase):
    def test_search_timeout_does_not_crash_run(self):
        # A transient Spotify error must be caught, not abort the whole run.
        class BoomSpotify:
            def me(self):
                return {"id": "u"}

            def search(self, **kwargs):
                raise Exception("Read timed out. (read timeout=5)")

            def user_playlist_add_tracks(self, **kwargs):
                pass

        events = []
        builder = gp.PlaylistBuilder(
            sp=BoomSpotify(), genius=None, searches=["x"], threshold=3.0,
            playlist_id="p", on_event=events.append,
        )
        builder.from_spotify("x")  # must NOT raise
        self.assertTrue(any(e["kind"] == "error" for e in events))


class FromSpotifyPipelineTests(unittest.TestCase):
    def test_full_spotify_run(self):
        # Two discovered tracks: one has matching lyrics, one has none.
        pages = [
            [
                spotify_track("a", "Hotdog", "Band A"),
                spotify_track("b", "Hotdog Two", "Band B"),
            ],
            [],  # offset 50 empty -> loop stops
        ]
        songs_map = {"Hotdog": "hotdog hotdog hotdog hotdog"}  # "Hotdog Two" absent -> no lyrics

        builder = gp.PlaylistBuilder(
            sp=FakeDiscoverySpotify(pages),
            genius=FakeGenius([], {}, songs_map=songs_map),
            searches=["hotdog"],
            threshold=1.5,
            playlist_id="playlist-123",
        )
        builder.from_spotify("hotdog")

        # Only the song with matching lyrics was added; the lyric-less one was filtered.
        self.assertEqual(builder.sp.added, ["a"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
