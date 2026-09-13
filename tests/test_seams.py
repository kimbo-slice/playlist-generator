"""Tests for the web-UI seams: structured event emission and cooperative stop.

Offline as always -- fake clients, zero API calls.
"""

import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import generate_playlist as gp


def spotify_track(track_id, name, artist):
    return {"id": track_id, "name": name, "artists": [{"name": artist}]}


class FakeGenius:
    def __init__(self, songs_map):
        self.songs_map = songs_map

    def search_song(self, title, artist=None, get_full_info=False):
        text = self.songs_map.get(title)
        return types.SimpleNamespace(lyrics=text) if text is not None else None


class FakeDiscoverySpotify:
    def __init__(self, pages):
        self.pages = pages
        self.added = []

    def me(self):
        return {"id": "u"}

    def search(self, q, type="track", limit=50, offset=0):
        idx = offset // 50
        items = self.pages[idx] if idx < len(self.pages) else []
        return {"tracks": {"items": items, "total": 999}}

    def user_playlist_add_tracks(self, user, playlist_id, tracks):
        self.added.extend(tracks)


class EventEmissionTests(unittest.TestCase):
    def test_events_captured_via_callback(self):
        events = []
        pages = [[spotify_track("a", "Hotdog", "Band A")], []]
        builder = gp.PlaylistBuilder(
            sp=FakeDiscoverySpotify(pages),
            genius=FakeGenius({"Hotdog": "hotdog hotdog hotdog hotdog"}),
            searches=["hotdog"],
            threshold=1.5,
            playlist_id="p",
            on_event=events.append,
        )
        builder.from_spotify("hotdog")

        kinds = [e["kind"] for e in events]
        self.assertIn("search", kinds)
        self.assertIn("evaluate", kinds)
        self.assertIn("added", kinds)

        # The 'evaluate' event carries structured fields, not just a string.
        ev = next(e for e in events if e["kind"] == "evaluate")
        self.assertEqual(ev["title"], "Hotdog")
        self.assertIn("match_pct", ev)
        self.assertEqual(ev["threshold"], 1.5)

        added = next(e for e in events if e["kind"] == "added")
        self.assertEqual(added["track_id"], "a")

    def test_callback_suppresses_stdout(self):
        # With a callback set, emit() routes to it instead of printing.
        events = []
        b = gp.PlaylistBuilder(sp=FakeDiscoverySpotify([[]]), genius=FakeGenius({}),
                               searches=["x"], threshold=1.5, playlist_id="p",
                               on_event=events.append)
        b.emit("test", "should not print", value=1)
        self.assertEqual(events[-1], {"kind": "test", "message": "should not print", "value": 1})


class CooperativeStopTests(unittest.TestCase):
    def test_stop_flag_halts_before_processing(self):
        # Stop is requested up front, so no songs get evaluated or added.
        events = []
        pages = [[spotify_track("a", "Hotdog", "Band A"),
                  spotify_track("b", "Hotdog Two", "Band B")], []]
        builder = gp.PlaylistBuilder(
            sp=FakeDiscoverySpotify(pages),
            genius=FakeGenius({"Hotdog": "hotdog hotdog hotdog"}),
            searches=["hotdog"],
            threshold=1.5,
            playlist_id="p",
            on_event=events.append,
        )
        builder.request_stop()
        builder.from_spotify("hotdog")

        self.assertEqual(builder.sp.added, [])
        self.assertNotIn("evaluate", [e["kind"] for e in events])

    def test_stop_midway_stops_adding_more(self):
        # A callback that trips the stop flag after the first add -> only one song added.
        pages = [[spotify_track("a", "Hotdog", "Band A"),
                  spotify_track("b", "Chili Dog", "Band B")], []]
        songs = {"Hotdog": "hotdog hotdog hotdog", "Chili Dog": "hotdog hotdog hotdog"}
        builder = gp.PlaylistBuilder(
            sp=FakeDiscoverySpotify(pages),
            genius=FakeGenius(songs),
            searches=["hotdog"],
            threshold=1.5,
            playlist_id="p",
        )

        def stop_after_first_add(event):
            if event["kind"] == "added":
                builder.request_stop()

        builder.on_event = stop_after_first_add
        builder.from_spotify("hotdog")

        self.assertEqual(builder.sp.added, ["a"])  # stopped before the second


if __name__ == "__main__":
    unittest.main(verbosity=2)
