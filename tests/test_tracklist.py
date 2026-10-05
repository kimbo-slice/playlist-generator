"""Tests for tracklist.py (ordered track list -> playlist).

Offline like the rest of the suite: a fake Spotify with a small catalog, zero
network calls.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tracklist as tl


def track(track_id, name, artist):
    return {"id": track_id, "name": name, "artists": [{"name": artist}]}


class FakeSpotify:
    """Search returns catalog tracks whose name appears in the query; records writes."""

    def __init__(self, catalog):
        self.catalog = catalog
        self.searches = 0
        self.playlists = {}  # playlist_id -> {"name": ..., "items": [...]}
        self.created = []    # (user, name)
        self.added = []      # (playlist_id, [ids]) per call
        self.replaced = []   # (playlist_id, [ids]) per call

    def me(self):
        return {"id": "test-user"}

    def search(self, q, type="track", limit=10, offset=0):
        self.searches += 1
        query = q.lower()
        items = [t for t in self.catalog if t["name"].lower() in query]
        return {"tracks": {"items": items}}

    def playlist(self, playlist_id, fields=None):
        return {"name": self.playlists[playlist_id]["name"]}

    def playlist_items(self, playlist_id, fields=None, limit=100, offset=0, additional_types=("track",)):
        items = self.playlists[playlist_id]["items"]
        page = items[offset:offset + limit]
        return {"items": page, "next": "more" if offset + limit < len(items) else None}

    def user_playlist_create(self, user, name, public=True, description=""):
        self.created.append((user, name))
        return {"id": "new-playlist"}

    def playlist_add_items(self, playlist_id, items):
        self.added.append((playlist_id, list(items)))

    def playlist_replace_items(self, playlist_id, items):
        self.replaced.append((playlist_id, list(items)))


CATALOG = [
    track("id-hotdog", "Hotdog", "Simian Mobile Disco"),
    track("id-tennis", "Tennis Court", "Lorde"),
    track("id-pocket", "Pocket", "Ariel Pink"),
]


def quiet(kind, message, **data):
    pass


class ParseTests(unittest.TestCase):
    def test_title_and_artist(self):
        self.assertEqual(tl.parse_tracklist("Hotdog - Simian Mobile Disco"),
                         [("Hotdog", "Simian Mobile Disco")])

    def test_splits_on_last_dash(self):
        self.assertEqual(tl.parse_tracklist("Thirst - Remix - Sean Feucht"),
                         [("Thirst - Remix", "Sean Feucht")])

    def test_ignores_numbering_bullets_blanks_and_comments(self):
        text = "# my list\n\n1. Hotdog - Simian Mobile Disco\n- Pocket - Ariel Pink\n2) Tennis Court - Lorde\n"
        self.assertEqual(tl.parse_tracklist(text), [
            ("Hotdog", "Simian Mobile Disco"),
            ("Pocket", "Ariel Pink"),
            ("Tennis Court", "Lorde"),
        ])

    def test_numeric_title_is_not_treated_as_numbering(self):
        self.assertEqual(tl.parse_tracklist("1979 - The Smashing Pumpkins"),
                         [("1979", "The Smashing Pumpkins")])

    def test_en_dash_and_missing_artist(self):
        self.assertEqual(tl.parse_tracklist("Hotdog – Simian Mobile Disco\nPocket"),
                         [("Hotdog", "Simian Mobile Disco"), ("Pocket", "")])


class ResolveTests(unittest.TestCase):
    def test_preserves_input_order(self):
        sp = FakeSpotify(CATALOG)
        entries = [("Pocket", "Ariel Pink"), ("Hotdog", "Simian Mobile Disco"), ("Tennis Court", "Lorde")]
        resolved, skipped = tl.resolve_tracklist(sp, entries, emit=quiet)
        self.assertEqual([tid for tid, _ in resolved], ["id-pocket", "id-hotdog", "id-tennis"])
        self.assertEqual(skipped, [])

    def test_artist_first_line_is_recovered(self):
        sp = FakeSpotify(CATALOG)
        resolved, skipped = tl.resolve_tracklist(sp, [("Lorde", "Tennis Court")], emit=quiet)
        self.assertEqual(resolved, [("id-tennis", "Tennis Court - Lorde")])
        self.assertEqual(skipped, [])

    def test_title_only_line(self):
        sp = FakeSpotify(CATALOG)
        resolved, _ = tl.resolve_tracklist(sp, [("Pocket", "")], emit=quiet)
        self.assertEqual(resolved, [("id-pocket", "Pocket - Ariel Pink")])

    def test_unknown_and_wrong_artist_are_skipped_with_reason(self):
        sp = FakeSpotify(CATALOG)
        entries = [("Nonexistent Song", "Nobody"), ("Hotdog", "Some Cover Band")]
        resolved, skipped = tl.resolve_tracklist(sp, entries, emit=quiet)
        self.assertEqual(resolved, [])
        self.assertEqual([s["reason"] for s in skipped], ["not_found", "no_confident_match"])

    def test_duplicate_line_is_skipped(self):
        sp = FakeSpotify(CATALOG)
        entries = [("Hotdog", "Simian Mobile Disco"), ("Hotdog", "Simian Mobile Disco")]
        resolved, skipped = tl.resolve_tracklist(sp, entries, emit=quiet)
        self.assertEqual(len(resolved), 1)
        self.assertEqual(skipped[0]["reason"], "duplicate")


class WritePlaylistTests(unittest.TestCase):
    def test_creates_playlist_and_adds_in_order_in_batches(self):
        sp = FakeSpotify([])
        ids = ["t%d" % i for i in range(250)]
        playlist_id = tl.write_playlist(sp, ids, title="Bike ride")
        self.assertEqual(playlist_id, "new-playlist")
        self.assertEqual(sp.created, [("test-user", "Bike ride")])
        self.assertEqual([len(batch) for _, batch in sp.added], [100, 100, 50])
        self.assertEqual([tid for _, batch in sp.added for tid in batch], ids)

    def test_append_to_existing_does_not_create_or_replace(self):
        sp = FakeSpotify([])
        tl.write_playlist(sp, ["a", "b"], playlist_id="existing")
        self.assertEqual(sp.created, [])
        self.assertEqual(sp.replaced, [])
        self.assertEqual(sp.added, [("existing", ["a", "b"])])

    def test_replace_overwrites_then_appends_the_rest(self):
        sp = FakeSpotify([])
        ids = ["t%d" % i for i in range(130)]
        tl.write_playlist(sp, ids, playlist_id="existing", replace=True)
        self.assertEqual(sp.replaced, [("existing", ids[:100])])
        self.assertEqual(sp.added, [("existing", ids[100:])])


class RunTests(unittest.TestCase):
    TEXT = "Pocket - Ariel Pink\nNonexistent Song - Nobody\nHotdog - Simian Mobile Disco\n"

    def test_end_to_end_new_playlist(self):
        sp = FakeSpotify(CATALOG)
        summary = tl.run(sp, self.TEXT, title="Mix", emit=quiet)
        self.assertEqual(summary["requested"], 3)
        self.assertEqual(summary["resolved"], 2)
        self.assertEqual(summary["playlist_id"], "new-playlist")
        self.assertEqual(sp.added, [("new-playlist", ["id-pocket", "id-hotdog"])])

    def test_dry_run_writes_nothing(self):
        sp = FakeSpotify(CATALOG)
        summary = tl.run(sp, self.TEXT, title="Mix", dry_run=True, emit=quiet)
        self.assertIsNone(summary["playlist_id"])
        self.assertEqual((sp.created, sp.added, sp.replaced), ([], [], []))

    def test_nothing_resolved_never_wipes_an_existing_playlist(self):
        sp = FakeSpotify(CATALOG)
        summary = tl.run(sp, "Nonexistent Song - Nobody", playlist_id="existing",
                         replace=True, emit=quiet)
        self.assertIsNone(summary["playlist_id"])
        self.assertEqual((sp.created, sp.added, sp.replaced), ([], [], []))

    def test_emits_structured_events(self):
        sp = FakeSpotify(CATALOG)
        events = []
        tl.run(sp, self.TEXT, title="Mix",
               emit=lambda kind, message, **data: events.append((kind, data)))
        kinds = [kind for kind, _ in events]
        self.assertEqual(kinds, ["search", "found", "miss", "found", "summary", "playlist"])
        self.assertEqual(events[-1][1], {"playlist_id": "new-playlist", "track_count": 2})

    def test_stop_writes_nothing(self):
        sp = FakeSpotify(CATALOG)
        summary = tl.run(sp, self.TEXT, title="Mix", emit=quiet, should_stop=lambda: True)
        self.assertEqual(summary["resolved"], 0)
        self.assertIsNone(summary["playlist_id"])
        self.assertEqual((sp.created, sp.added, sp.replaced), ([], [], []))


PLAYLIST_ID = "37i9dQZF1DXcBWIGoYBM5M"


class PlaylistIdTests(unittest.TestCase):
    def test_share_link_uri_and_bare_id(self):
        for link in (
            "https://open.spotify.com/playlist/%s?si=abc123" % PLAYLIST_ID,
            "https://open.spotify.com/intl-de/playlist/%s" % PLAYLIST_ID,
            "spotify:playlist:%s" % PLAYLIST_ID,
            "  %s  " % PLAYLIST_ID,
        ):
            self.assertEqual(tl.parse_playlist_id(link), PLAYLIST_ID, link)

    def test_rejects_non_playlist_input(self):
        for link in ("", "tennis", "https://open.spotify.com/track/%s" % PLAYLIST_ID,
                     "https://example.com/playlist/short"):
            self.assertIsNone(tl.parse_playlist_id(link), link)


class FetchPlaylistTests(unittest.TestCase):
    def test_reads_all_pages_in_order_and_skips_removed_entries(self):
        sp = FakeSpotify([])
        items = [{"track": track("t%d" % i, "Song %d" % i, "Band")} for i in range(150)]
        items.insert(3, {"track": None})  # an entry Spotify no longer has
        sp.playlists["pl"] = {"name": "tennis", "items": items}
        name, tracks = tl.fetch_playlist(sp, "pl")
        self.assertEqual(name, "tennis")
        self.assertEqual(len(tracks), 150)
        self.assertEqual(tracks[0], {"line": "Song 0 - Band", "id": "t0"})
        self.assertEqual([t["id"] for t in tracks], ["t%d" % i for i in range(150)])


class KnownTrackTests(unittest.TestCase):
    A, B = "A" * 22, "B" * 22

    def test_known_lines_use_exact_ids_without_searching(self):
        sp = FakeSpotify([])  # empty catalog: a search would find nothing
        known = {"Song - Club Mix - Band": self.A, "Song - Radio Edit - Band": self.B}
        # Reordered, and retyped with different case/punctuation.
        entries = tl.parse_tracklist("song - radio edit - BAND\nSong - Club Mix - Band!")
        resolved, skipped = tl.resolve_tracklist(sp, entries, emit=quiet, known=known)
        self.assertEqual([tid for tid, _ in resolved], [self.B, self.A])
        self.assertEqual(skipped, [])
        self.assertEqual(sp.searches, 0)

    def test_unknown_or_edited_lines_fall_back_to_search(self):
        sp = FakeSpotify(CATALOG)
        known = {"Song - Band": self.A}
        entries = tl.parse_tracklist("Song - Band\nHotdog - Simian Mobile Disco")
        resolved, _ = tl.resolve_tracklist(sp, entries, emit=quiet, known=known)
        self.assertEqual([tid for tid, _ in resolved], [self.A, "id-hotdog"])

    def test_malformed_known_ids_are_ignored(self):
        sp = FakeSpotify(CATALOG)
        known = {"Hotdog - Simian Mobile Disco": "not-a-real-id"}
        entries = tl.parse_tracklist("Hotdog - Simian Mobile Disco")
        resolved, _ = tl.resolve_tracklist(sp, entries, emit=quiet, known=known)
        self.assertEqual(resolved, [("id-hotdog", "Hotdog - Simian Mobile Disco")])


if __name__ == "__main__":
    unittest.main(verbosity=2)
