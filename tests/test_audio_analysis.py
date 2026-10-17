"""Tests for audio_analysis.py. No downloads and no real librosa: the network
calls are faked and librosa is swapped for a stand-in where needed."""

import os
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import audio_analysis as aa

C_MAJOR_SCALE = [1, 0, 1, 0, 1, 1, 0, 1, 0, 1, 0, 1]       # C D E F G A B
A_MINOR_TRIAD = [0.3, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0]     # A, C, E with A loudest
A_MINOR_TRIAD[9], A_MINOR_TRIAD[0], A_MINOR_TRIAD[4] = 1.0, 0.7, 0.7


def deezer_item(title, artist, preview="https://cdn/preview.mp3"):
    return {"title": title, "artist": {"name": artist}, "preview": preview}


class EstimateKeyTests(unittest.TestCase):
    def test_c_major_scale_reads_as_c_major_or_its_relative_minor(self):
        key, mode, confidence = aa.estimate_key(C_MAJOR_SCALE)
        self.assertIn((key, mode), {(0, 1), (9, 0)})   # the same notes; either is defensible
        self.assertGreater(confidence, 0.5)

    def test_a_minor_triad_reads_as_a_minor(self):
        key, mode, _ = aa.estimate_key(A_MINOR_TRIAD)
        self.assertEqual((key, mode), (9, 0))

    def test_transposing_the_input_transposes_the_answer(self):
        key, mode, _ = aa.estimate_key(C_MAJOR_SCALE)
        up_a_fifth = C_MAJOR_SCALE[-7:] + C_MAJOR_SCALE[:-7]   # rotate up 7 semitones
        self.assertEqual(aa.estimate_key(up_a_fifth)[:2], ((key + 7) % 12, mode))

    def test_silence_or_bad_input(self):
        self.assertEqual(aa.estimate_key([0] * 12), (None, None, 0.0))
        self.assertEqual(aa.estimate_key([1, 2, 3]), (None, None, 0.0))


class DeezerPreviewTests(unittest.TestCase):
    def test_takes_the_first_result_that_really_matches(self):
        def http_get(url, params):
            return {"data": [deezer_item("Tennis Court", "Some Cover Band", "wrong"),
                             deezer_item("Tennis Court", "Lorde", "right")]}

        self.assertEqual(aa.deezer_preview_url("Tennis Court", "Lorde", http_get=http_get), "right")

    def test_skips_results_without_a_preview(self):
        def http_get(url, params):
            return {"data": [deezer_item("Tennis Court", "Lorde", preview="")]}

        self.assertIsNone(aa.deezer_preview_url("Tennis Court", "Lorde", http_get=http_get))

    def test_retries_with_release_decoration_removed(self):
        queries = []

        def http_get(url, params):
            queries.append(params["q"])
            if "Remastered" in params["q"]:
                return {"data": []}
            return {"data": [deezer_item("Song", "Band")]}

        self.assertIsNotNone(aa.deezer_preview_url("Song - Remastered 2011", "Band", http_get=http_get))
        self.assertEqual(queries, ["Band Song - Remastered 2011", "Band Song"])


class MeasureTests(unittest.TestCase):
    def test_finds_downloads_and_analyzes(self):
        result = aa.measure(
            "Tennis Court", "Lorde",
            http_get=lambda url, params: {"data": [deezer_item("Tennis Court", "Lorde", "u")]},
            download=lambda url: b"mp3:" + url.encode(),
            analyze=lambda data: {"bpm": 89.0, "seen": data})
        self.assertEqual(result, {"bpm": 89.0, "seen": b"mp3:u"})

    def test_no_preview_means_no_download(self):
        download = mock.Mock()
        self.assertIsNone(aa.measure("x", "y", http_get=lambda url, params: {"data": []},
                                     download=download))
        download.assert_not_called()


class AnalyzeAudioTests(unittest.TestCase):
    def test_temp_file_is_deleted_even_when_decoding_fails(self):
        seen = {}

        def failing_load(path, **kwargs):
            seen["path"] = path
            self.assertTrue(os.path.exists(path))
            raise RuntimeError("not audio")

        fake_librosa = types.SimpleNamespace(load=failing_load)
        with mock.patch.dict(sys.modules, {"librosa": fake_librosa}):
            with self.assertRaises(RuntimeError):
                aa.analyze_audio(b"definitely not an mp3")
        self.assertFalse(os.path.exists(seen["path"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
