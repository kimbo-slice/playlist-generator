"""Tests for saved tempo lookups (in-memory store; no database)."""

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tempo_store as ts

NOW = datetime(2026, 11, 10, tzinfo=timezone.utc)
FEAT = {"bpm": 120.0, "key": 9, "mode": 0, "source": "reccobeats"}


class FreshnessTests(unittest.TestCase):
    def test_known_answers_never_expire(self):
        store = ts.InMemoryTempoStore()
        store.save("recco", {"id1": FEAT}, when=NOW - timedelta(days=400))
        self.assertEqual(store.load("recco", ["id1"], now=NOW), {"id1": FEAT})

    def test_unknown_answers_expire_after_the_recheck_period(self):
        store = ts.InMemoryTempoStore()
        store.save("gsb", {"recent": None}, when=NOW - timedelta(days=ts.RECHECK_DAYS - 1))
        store.save("gsb", {"old": None}, when=NOW - timedelta(days=ts.RECHECK_DAYS + 1))
        self.assertEqual(store.load("gsb", ["recent", "old"], now=NOW), {"recent": None})


class PreloadSaveTests(unittest.TestCase):
    TRACKS = [{"line": "A - Band", "id": "id-a"}, {"line": "B - Band", "id": "id-b"}]

    def test_preload_fills_the_cache_and_save_new_skips_what_was_loaded(self):
        store = ts.InMemoryTempoStore()
        store.save("recco", {"id-a": FEAT})
        cache = {}
        loaded = ts.preload(store, self.TRACKS, cache)
        self.assertEqual(cache["recco"], {"id-a": FEAT})
        cache["recco"]["id-b"] = None            # found during this run
        cache["clips"] = {"B - Band": {"bpm": 100.0}}
        self.assertEqual(ts.save_new(store, cache, loaded), 2)
        self.assertEqual(store.load("recco", ["id-b"]), {"id-b": None})
        self.assertEqual(store.load("clips", ["B - Band"]), {"B - Band": {"bpm": 100.0}})

    def test_no_store_is_a_no_op(self):
        cache = {}
        loaded = ts.preload(None, self.TRACKS, cache)
        self.assertEqual(ts.save_new(None, {"recco": {"x": FEAT}}, loaded), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
