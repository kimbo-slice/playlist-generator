"""Tests for tempo_sources.py and the coverage probe's comparison logic.

Offline: every lookup goes through a fake ``http_get`` returning canned JSON in
the services' real response shapes, so no requests are made.
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bpm_probe
import tempo_sources as ts

ID_A, ID_B, ID_C = "A" * 22, "B" * 22, "C" * 22


def recco_item(spotify_id, tempo, key, mode):
    return {"id": "some-uuid", "href": "https://open.spotify.com/track/" + spotify_id,
            "tempo": tempo, "key": key, "mode": mode, "energy": 0.5}


def gsb_item(title, artist, tempo, open_key):
    return {"id": "x", "title": title, "tempo": tempo, "key_of": "?", "open_key": open_key,
            "artist": {"name": artist}}


class OpenKeyTests(unittest.TestCase):
    def test_major_keys_walk_the_circle_of_fifths(self):
        self.assertEqual(ts.parse_open_key("1d"), (0, 1))    # C major
        self.assertEqual(ts.parse_open_key("2d"), (7, 1))    # G major
        self.assertEqual(ts.parse_open_key("12d"), (5, 1))   # F major

    def test_minor_is_the_relative_minor(self):
        self.assertEqual(ts.parse_open_key("1m"), (9, 0))    # A minor
        self.assertEqual(ts.parse_open_key("2m"), (4, 0))    # E minor

    def test_unparseable_values(self):
        for value in (None, "", "13d", "0m", "Em", "7x"):
            self.assertEqual(ts.parse_open_key(value), (None, None), value)


class ReccoBeatsTests(unittest.TestCase):
    def test_maps_results_back_to_spotify_ids(self):
        def http_get(url, params):
            return {"content": [recco_item(ID_B, 184.821, 5, 0)]}

        found = ts.reccobeats_features([ID_A, ID_B], http_get=http_get)
        self.assertEqual(found, {ID_B: {"bpm": 184.821, "key": 5, "mode": 0, "source": "reccobeats"}})

    def test_unknown_key_and_zero_tempo_become_none(self):
        found = ts.reccobeats_features(
            [ID_A], http_get=lambda url, params: {"content": [recco_item(ID_A, 0, -1, None)]})
        self.assertEqual(found[ID_A], {"bpm": None, "key": None, "mode": None, "source": "reccobeats"})

    def test_batches_requests_and_skips_missing_ids(self):
        calls = []

        def http_get(url, params):
            calls.append(params["ids"].split(","))
            return {"content": []}

        ids = ["%022d" % i for i in range(95)] + [None, ""]
        self.assertEqual(ts.reccobeats_features(ids, http_get=http_get), {})
        self.assertEqual([len(c) for c in calls], [40, 40, 15])


class GetSongBpmTests(unittest.TestCase):
    def test_returns_tempo_and_key_for_a_matching_song(self):
        seen = {}

        def http_get(url, params):
            seen.update(params)
            return {"search": [gsb_item("Tennis Court", "Lorde", "89", "1d")]}

        result = ts.getsongbpm_lookup("Tennis Court", "Lorde", api_key="k", http_get=http_get)
        self.assertEqual(result, {"bpm": 89.0, "key": 0, "mode": 1, "source": "getsongbpm"})
        self.assertEqual(seen["lookup"], "song:Tennis Court artist:Lorde")
        self.assertEqual(seen["type"], "both")

    def test_keeps_apostrophes_in_the_search(self):
        # Their search finds "Ain't ..." but not "aint ...".
        lookups = []

        def http_get(url, params):
            lookups.append(params["lookup"])
            return {"search": [gsb_item("Ain't No Rest for the Wicked", "Cage the Elephant", "76", "2d")]}

        result = ts.getsongbpm_lookup("Ain't No Rest for the Wicked", "Cage The Elephant",
                                      api_key="k", http_get=http_get)
        self.assertEqual(lookups, ["song:Ain't No Rest for the Wicked artist:Cage The Elephant"])
        self.assertEqual(result["bpm"], 76.0)

    def test_falls_back_to_the_title_without_release_decoration(self):
        lookups = []

        def http_get(url, params):
            lookups.append(params["lookup"])
            if "Remastered" in params["lookup"]:
                return {"search": {"error": "no result"}}
            return {"search": [gsb_item("Song", "Band", "120", "1m")]}

        result = ts.getsongbpm_lookup("Song - Remastered 2011", "Band feat. Guest",
                                      api_key="k", http_get=http_get)
        self.assertEqual(lookups, ["song:Song - Remastered 2011 artist:Band feat. Guest",
                                   "song:Song artist:Band"])
        self.assertEqual((result["bpm"], result["key"], result["mode"]), (120.0, 9, 0))

    def test_retries_once_after_a_failed_request(self):
        calls = []

        def http_get(url, params):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("HTTP 500")
            return {"search": [gsb_item("Tennis Court", "Lorde", "89", "1d")]}

        with mock.patch.object(ts.time, "sleep"):
            result = ts.getsongbpm_lookup("Tennis Court", "Lorde", api_key="k", http_get=http_get)
        self.assertEqual((len(calls), result["bpm"]), (2, 89.0))

    def test_skips_a_different_artists_song_and_takes_the_right_one(self):
        def http_get(url, params):
            return {"search": [gsb_item("Hotdog", "Some Cover Band", "100", "1d"),
                               gsb_item("Hotdog", "Simian Mobile Disco", "128", "2m")]}

        result = ts.getsongbpm_lookup("Hotdog", "Simian Mobile Disco", api_key="k", http_get=http_get)
        self.assertEqual(result["bpm"], 128.0)

    def test_no_result_and_no_confident_match_return_none(self):
        no_result = lambda url, params: {"search": {"error": "no result"}}
        wrong = lambda url, params: {"search": [gsb_item("Hotdog", "Some Cover Band", "100", "1d")]}
        self.assertIsNone(ts.getsongbpm_lookup("Hotdog", "Simian Mobile Disco", api_key="k", http_get=no_result))
        self.assertIsNone(ts.getsongbpm_lookup("Hotdog", "Simian Mobile Disco", api_key="k", http_get=wrong))

    def test_missing_key_is_a_clear_error(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(RuntimeError):
                ts.getsongbpm_lookup("a", "b", http_get=lambda url, params: {})


class BestFeaturesTests(unittest.TestCase):
    RB = {"bpm": 174.0, "key": 10, "mode": 0, "source": "reccobeats"}
    GSB = {"bpm": 110.0, "key": 5, "mode": 1, "source": "getsongbpm"}

    def test_reccobeats_wins_a_disagreement(self):
        self.assertEqual(ts.best_features(self.RB, self.GSB), self.RB)

    def test_getsongbpm_used_when_reccobeats_has_nothing(self):
        self.assertEqual(ts.best_features(None, self.GSB), self.GSB)
        self.assertIsNone(ts.best_features(None, None))

    def test_getsongbpm_fills_only_the_missing_part(self):
        no_key = {"bpm": 174.0, "key": None, "mode": None, "source": "reccobeats"}
        merged = ts.best_features(no_key, self.GSB)
        self.assertEqual((merged["bpm"], merged["key"], merged["mode"]), (174.0, 5, 1))
        no_tempo = {"bpm": None, "key": 10, "mode": 0, "source": "reccobeats"}
        merged = ts.best_features(no_tempo, self.GSB)
        self.assertEqual((merged["bpm"], merged["key"], merged["mode"]), (110.0, 10, 0))


class ProbeComparisonTests(unittest.TestCase):
    def test_counts_coverage_and_agreement(self):
        tracks = [{"line": "One - Band", "id": ID_A},     # both know it, and agree
                  {"line": "Two - Band", "id": ID_B},     # only ReccoBeats
                  {"line": "Three - Band", "id": ID_C}]   # neither
        recco = {ID_A: {"bpm": 178.0, "key": 0, "mode": 1, "source": "reccobeats"},
                 ID_B: {"bpm": 100.0, "key": 2, "mode": 1, "source": "reccobeats"}}

        def gsb(title, artist, api_key=None):
            if title == "One":
                return {"bpm": 89.0, "key": 0, "mode": 1, "source": "getsongbpm"}
            return None

        with mock.patch.object(bpm_probe.tempo_sources, "reccobeats_features", return_value=recco), \
             mock.patch.object(bpm_probe.tempo_sources, "getsongbpm_lookup", side_effect=gsb), \
             mock.patch.object(bpm_probe.time, "sleep"):
            counts = bpm_probe.compare(tracks, api_key="k", log=lambda message: None)

        self.assertEqual((counts["reccobeats"], counts["getsongbpm"], counts["both"], counts["neither"]),
                         (2, 1, 1, 1))
        # 178 vs 89 is double time: counted as the same tempo.
        self.assertEqual((counts["tempo_agree"], counts["tempo_compared"]), (1, 1))
        self.assertEqual((counts["key_agree"], counts["key_compared"]), (1, 1))

    def test_tempo_agreement_rule(self):
        self.assertTrue(bpm_probe.tempos_agree(120, 122))
        self.assertTrue(bpm_probe.tempos_agree(70, 140))
        self.assertFalse(bpm_probe.tempos_agree(100, 120))


if __name__ == "__main__":
    unittest.main(verbosity=2)
