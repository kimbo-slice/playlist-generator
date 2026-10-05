"""Tests for the web UI's track list endpoint (/api/tracklist).

These run the real endpoint -> executor -> tracklist.run path; only the Spotify
client is replaced with an in-memory fake, so there are zero network calls.
"""

import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient
import app as webapp


class FakeSpotify:
    """Search returns catalog tracks whose name appears in the query; records writes."""

    def __init__(self, catalog, search_delay=0.0):
        self.catalog = catalog
        self.search_delay = search_delay
        self.searches = 0
        self.playlists = {}
        self.created = []
        self.added = []

    def me(self):
        return {"id": "test-user"}

    def search(self, q, type="track", limit=10, offset=0):
        self.searches += 1
        if self.search_delay:
            time.sleep(self.search_delay)
        query = q.lower()
        return {"tracks": {"items": [t for t in self.catalog if t["name"].lower() in query]}}

    def user_playlist_create(self, user, name, public=True, description=""):
        self.created.append(name)
        return {"id": "new-playlist"}

    def playlist(self, playlist_id, fields=None):
        return {"name": self.playlists[playlist_id]["name"]}

    def playlist_items(self, playlist_id, fields=None, limit=100, offset=0, additional_types=("track",)):
        items = self.playlists[playlist_id]["items"]
        return {"items": items[offset:offset + limit],
                "next": "more" if offset + limit < len(items) else None}

    def playlist_add_items(self, playlist_id, items):
        self.added.append((playlist_id, list(items)))


def track(track_id, name, artist):
    return {"id": track_id, "name": name, "artists": [{"name": artist}]}


CATALOG = [
    track("id-hotdog", "Hotdog", "Simian Mobile Disco"),
    track("id-tennis", "Tennis Court", "Lorde"),
    track("id-pocket", "Pocket", "Ariel Pink"),
]
TEXT = "Pocket - Ariel Pink\nNonexistent Song - Nobody\nHotdog - Simian Mobile Disco\n"


def drain(client, timeout=3.0):
    """Poll /api/events until a 'done' event arrives; return (events, last response)."""
    cursor, events, deadline = 0, [], time.time() + timeout
    while time.time() < deadline:
        data = client.get("/api/events", params={"cursor": cursor}).json()
        cursor = data["cursor"]
        events.extend(data["events"])
        if any(e["kind"] == "done" for e in events):
            return events, data
        time.sleep(0.02)
    return events, {"status": "timeout"}


class TracklistEndpointTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(webapp.app)
        webapp.manager.run = None

    def use_spotify(self, fake):
        patcher = mock.patch.object(webapp, "_spotify_client", return_value=fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        return fake

    def test_creates_playlist_in_the_order_given(self):
        sp = self.use_spotify(FakeSpotify(CATALOG))
        r = self.client.post("/api/tracklist", json={"text": TEXT, "title": "Bike ride"})
        self.assertEqual(r.status_code, 200)

        events, data = drain(self.client)
        self.assertEqual([e["kind"] for e in events],
                         ["search", "found", "miss", "found", "summary", "playlist", "done"])
        self.assertEqual(sp.created, ["Bike ride"])
        self.assertEqual(sp.added, [("new-playlist", ["id-pocket", "id-hotdog"])])
        # The UI needs these to mount the player.
        playlist_event = next(e for e in events if e["kind"] == "playlist")
        self.assertEqual(playlist_event["playlist_id"], "new-playlist")
        self.assertEqual(playlist_event["track_count"], 2)
        self.assertEqual(data["playlist_id"], "new-playlist")
        self.assertEqual(data["status"], "complete")

    def test_check_only_creates_nothing(self):
        sp = self.use_spotify(FakeSpotify(CATALOG))
        r = self.client.post("/api/tracklist", json={"text": TEXT, "dry_run": True})
        self.assertEqual(r.status_code, 200)

        events, data = drain(self.client)
        kinds = [e["kind"] for e in events]
        self.assertIn("found", kinds)
        self.assertNotIn("playlist", kinds)
        self.assertEqual((sp.created, sp.added), ([], []))
        self.assertIsNone(data["playlist_id"])

    def test_rejects_empty_list_and_missing_title_without_starting_a_run(self):
        self.use_spotify(FakeSpotify(CATALOG))
        empty = self.client.post("/api/tracklist", json={"text": "  \n# just a comment\n", "title": "x"})
        self.assertEqual(empty.status_code, 400)
        untitled = self.client.post("/api/tracklist", json={"text": TEXT})
        self.assertEqual(untitled.status_code, 400)
        self.assertIsNone(webapp.manager.run)

    def test_stop_cancels_without_creating_a_playlist(self):
        sp = self.use_spotify(FakeSpotify(CATALOG, search_delay=0.02))
        many = "\n".join("Hotdog - Simian Mobile Disco" for _ in range(300))
        self.client.post("/api/tracklist", json={"text": many, "title": "Never made"})
        time.sleep(0.1)
        self.assertTrue(self.client.post("/api/stop").json()["stopped"])

        events, data = drain(self.client)
        self.assertEqual(data["status"], "stopped")
        self.assertEqual((sp.created, sp.added), ([], []))

    def test_rejected_while_another_run_is_in_progress(self):
        self.use_spotify(FakeSpotify(CATALOG, search_delay=0.02))
        many = "\n".join("Hotdog - Simian Mobile Disco" for _ in range(300))
        self.client.post("/api/tracklist", json={"text": many, "dry_run": True})
        time.sleep(0.05)
        second = self.client.post("/api/tracklist", json={"text": TEXT, "title": "x"})
        self.assertEqual(second.status_code, 409)
        self.client.post("/api/stop")
        drain(self.client)

    def test_spotify_failure_surfaces_as_error_status(self):
        with mock.patch.object(webapp, "_spotify_client", side_effect=RuntimeError("no auth")):
            self.client.post("/api/tracklist", json={"text": TEXT, "title": "x"})
            events, data = drain(self.client)
        self.assertEqual(data["status"], "error")
        self.assertTrue(any("no auth" in (e.get("message") or "") for e in events))


class PlaylistSourceEndpointTests(unittest.TestCase):
    PLAYLIST_ID = "37i9dQZF1DXcBWIGoYBM5M"
    LINK = "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M?si=abc"
    A, B = "A" * 22, "B" * 22

    def setUp(self):
        self.client = TestClient(webapp.app)
        webapp.manager.run = None
        self.sp = FakeSpotify([])
        self.sp.playlists[self.PLAYLIST_ID] = {"name": "tennis", "items": [
            {"track": track(self.A, "Song - Club Mix", "Band")},
            {"track": track(self.B, "Song - Radio Edit", "Band")},
        ]}
        patcher = mock.patch.object(webapp, "_spotify_client", return_value=self.sp)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_loads_tracks_in_playlist_order(self):
        r = self.client.post("/api/playlist-tracks", json={"link": self.LINK})
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["name"], "tennis")
        self.assertEqual(data["tracks"], [
            {"line": "Song - Club Mix - Band", "id": self.A},
            {"line": "Song - Radio Edit - Band", "id": self.B},
        ])

    def test_bad_link_is_rejected(self):
        r = self.client.post("/api/playlist-tracks", json={"link": "not a link"})
        self.assertEqual(r.status_code, 400)

    def test_spotify_failure_is_reported(self):
        r = self.client.post("/api/playlist-tracks",
                             json={"link": "spotify:playlist:" + "Z" * 22})  # not in the fake
        self.assertEqual(r.status_code, 502)
        self.assertIn("Spotify", r.json()["detail"])

    def test_load_reorder_create_round_trip_keeps_exact_tracks(self):
        loaded = self.client.post("/api/playlist-tracks", json={"link": self.LINK}).json()["tracks"]
        known = {t["line"]: t["id"] for t in loaded}
        reordered = "\n".join(t["line"] for t in reversed(loaded))
        self.client.post("/api/tracklist",
                         json={"text": reordered, "title": "tennis, reordered", "known": known})
        events, data = drain(self.client)
        self.assertEqual(data["status"], "complete")
        self.assertEqual(self.sp.added, [("new-playlist", [self.B, self.A])])
        self.assertEqual(self.sp.searches, 0)  # exact ids, no name lookups


if __name__ == "__main__":
    unittest.main(verbosity=2)
