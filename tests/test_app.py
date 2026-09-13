"""Tests for the web plumbing (start / stop / events polling).

Uses FastAPI's TestClient and a FAKE executor injected into the Manager, so
the run emits canned events on a thread with no Spotify/Genius calls. Zero
network, no rate-limit risk.
"""

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient
import app as webapp


def drain(client, timeout=3.0):
    """Poll /api/events until a 'done' event arrives; return all events."""
    cursor, events, deadline = 0, [], time.time() + timeout
    while time.time() < deadline:
        data = client.get("/api/events", params={"cursor": cursor}).json()
        cursor = data["cursor"]
        events.extend(data["events"])
        if any(e["kind"] == "done" for e in events):
            return events, data
        time.sleep(0.02)
    return events, {"status": "timeout"}


class WebPlumbingTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(webapp.app)
        # Reset manager state between tests.
        webapp.manager.run = None
        webapp.manager.executor = webapp.default_executor

    def test_run_streams_events_and_playlist(self):
        def fake_executor(run):
            run.playlist_id = "fake-playlist-123"
            run.emit({"kind": "playlist", "message": "Playlist ready.",
                      "playlist_id": "fake-playlist-123"})
            for i in range(3):
                if run.should_stop():
                    break
                run.emit({"kind": "added", "message": "Added song %d" % i, "track_id": "t%d" % i})

        webapp.manager.executor = fake_executor
        r = self.client.post("/api/run", json={"query": "hotdog"})
        self.assertEqual(r.status_code, 200)

        events, data = drain(self.client)
        kinds = [e["kind"] for e in events]
        self.assertIn("playlist", kinds)
        self.assertEqual(kinds.count("added"), 3)
        self.assertIn("done", kinds)
        self.assertEqual(data["playlist_id"], "fake-playlist-123")
        self.assertEqual(data["status"], "complete")

    def test_stop_halts_a_run(self):
        # Executor loops until asked to stop.
        def looping_executor(run):
            n = 0
            while not run.should_stop() and n < 10000:
                run.emit({"kind": "added", "message": "song %d" % n, "track_id": str(n)})
                n += 1
                time.sleep(0.005)

        webapp.manager.executor = looping_executor
        self.client.post("/api/run", json={"query": "x"})
        time.sleep(0.1)
        stop = self.client.post("/api/stop").json()
        self.assertTrue(stop["stopped"])

        events, data = drain(self.client)
        self.assertEqual(webapp.manager.run.status, "stopped")
        self.assertEqual(events[-1]["kind"], "done")

    def test_second_run_while_running_is_rejected(self):
        def slow(run):
            while not run.should_stop():
                time.sleep(0.01)

        webapp.manager.executor = slow
        self.client.post("/api/run", json={"query": "a"})
        time.sleep(0.05)
        second = self.client.post("/api/run", json={"query": "b"})
        self.assertEqual(second.status_code, 409)
        self.client.post("/api/stop")  # cleanup

    def test_executor_error_becomes_error_status(self):
        def boom(run):
            raise RuntimeError("kaboom")

        webapp.manager.executor = boom
        self.client.post("/api/run", json={"query": "a"})
        events, data = drain(self.client)
        self.assertEqual(webapp.manager.run.status, "error")
        self.assertTrue(any("kaboom" in (e.get("message") or "") for e in events))


if __name__ == "__main__":
    unittest.main(verbosity=2)
