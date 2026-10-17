"""Tests for reorder.py and tempo folding. Offline: lookups are canned dicts."""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import reorder
import vibe_order as vo

ID_A, ID_B, ID_C, ID_D = "A" * 22, "B" * 22, "C" * 22, "D" * 22


def feat(bpm, key=None, mode=None, source="reccobeats"):
    return {"bpm": bpm, "key": key, "mode": mode, "source": source}


class FoldTests(unittest.TestCase):
    def test_half_and_double_readings_fold_to_the_same_tempo(self):
        self.assertAlmostEqual(vo.fold_bpm(149), 74.5)
        self.assertAlmostEqual(vo.fold_bpm(74.5), 74.5)
        self.assertAlmostEqual(vo.fold_bpm(219), 109.5)
        self.assertAlmostEqual(vo.fold_bpm(60), 120)
        self.assertAlmostEqual(vo.fold_bpm(35), 70)

    def test_range_edges(self):
        self.assertEqual(vo.fold_bpm(70), 70)
        self.assertEqual(vo.fold_bpm(140), 70)
        self.assertEqual(vo.fold_bpm(139.9), 139.9)

    def test_missing_tempo(self):
        for value in (None, 0, -5):
            self.assertIsNone(vo.fold_bpm(value))


class ChooseTempoTests(unittest.TestCase):
    def test_half_double_disagreement_is_not_flagged(self):
        self.assertEqual(reorder.choose_tempo(feat(149), feat(74.5)), (74.5, "reccobeats", ""))

    def test_real_disagreement_is_flagged_and_reccobeats_used(self):
        bpm, source, flag = reorder.choose_tempo(feat(100), feat(120))
        self.assertEqual((bpm, source), (100, "reccobeats"))
        self.assertEqual(flag, "sources disagree: 100 vs 120")

    def test_readings_either_side_of_the_fold_edge_agree(self):
        # 139 and 71 are 2% apart once double time is allowed for.
        self.assertEqual(reorder.choose_tempo(feat(139), feat(71))[2], "")

    def test_single_source_is_used_without_a_flag(self):
        self.assertEqual(reorder.choose_tempo(feat(100), None), (100, "reccobeats", ""))
        self.assertEqual(reorder.choose_tempo(None, feat(200)), (100, "getsongbpm", ""))

    def test_no_source(self):
        self.assertEqual(reorder.choose_tempo(None, None), (None, "", reorder.NOT_FOUND))
        self.assertEqual(reorder.choose_tempo(feat(None), None), (None, "", reorder.NOT_FOUND))


class PlanTests(unittest.TestCase):
    TRACKS = [{"line": "Fast - Band", "id": ID_A}, {"line": "Double - Band", "id": ID_B},
              {"line": "Unknown - Band", "id": ID_C}, {"line": "Mid - Band", "id": ID_D}]
    RECCO = {ID_A: feat(110), ID_B: feat(160), ID_D: feat(95)}
    GSB = {"Fast - Band": feat(110), "Double - Band": feat(80), "Mid - Band": feat(112)}

    def test_orders_by_folded_tempo_and_sets_unknowns_aside(self):
        ordered, unplaced = reorder.plan(self.TRACKS, self.RECCO, self.GSB)
        self.assertEqual([(e.line, e.was, e.bpm) for e in ordered],
                         [("Double - Band", 2, 80), ("Mid - Band", 4, 95), ("Fast - Band", 1, 110)])
        self.assertEqual([e.flag for e in ordered], ["", "sources disagree: 95 vs 112", ""])
        self.assertEqual([e.confirmed for e in ordered], [True, False, True])
        self.assertEqual([(e.line, e.flag) for e in unplaced], [("Unknown - Band", reorder.NOT_FOUND)])

    def test_duplicate_lines_are_both_kept(self):
        tracks = [{"line": "Same - Band", "id": ID_A}, {"line": "Same - Band", "id": ID_B},
                  {"line": "Other - Band", "id": ID_D}]
        ordered, _ = reorder.plan(tracks, self.RECCO, {})
        self.assertEqual(sorted(e.was for e in ordered), [1, 2, 3])

    def test_key_shapes_the_order_unless_turned_off(self):
        # Same tempo throughout; only key could tell these apart.
        tracks = [{"line": "%d - Band" % i, "id": str(i) * 22} for i in range(4)]
        recco = {str(i) * 22: feat(100 + i * 0.01, key, 1) for i, key in enumerate((0, 6, 7, 1))}
        plain, _ = reorder.plan(tracks, recco, {}, use_key=False)
        keyed, _ = reorder.plan(tracks, recco, {})
        self.assertEqual([e.was for e in plain], [1, 2, 3, 4])
        self.assertEqual([e.was for e in keyed], [1, 3, 2, 4])   # C, G, F#, C#

    def test_reccobeats_key_is_trusted_and_getsongbpm_only_fills_gaps(self):
        tracks = [{"line": "One - Band", "id": ID_A}, {"line": "Two - Band", "id": ID_B}]
        recco = {ID_A: feat(100, 0, 1), ID_B: feat(100)}
        gsb = {"One - Band": feat(100, 5, 0, "getsongbpm"), "Two - Band": feat(100, 7, 1, "getsongbpm")}
        ordered, _ = reorder.plan(tracks, recco, gsb)
        self.assertEqual({e.line: (e.key, e.mode) for e in ordered},
                         {"One - Band": (0, 1), "Two - Band": (7, 1)})

    def test_report_ends_with_a_paste_ready_list(self):
        lines = []
        reorder.report(*reorder.plan(self.TRACKS, self.RECCO, self.GSB), cross_checked=True,
                       log=lines.append)
        tail = lines[lines.index("\n# Paste-ready order") + 1:]
        self.assertEqual(tail, ["Double - Band", "Mid - Band", "Fast - Band",
                                reorder.PILE_HEADING, "Unknown - Band"])
        self.assertIn("\n4 track(s), 2 need a decision.", lines)
        self.assertIn("Tempo confirmed by both services: 2. From one service only: 0.", lines)


class ClipFallbackTests(unittest.TestCase):
    CLIP = {"bpm": 196.0, "key": 3, "mode": 0, "source": "audio"}

    def test_clip_is_used_only_when_neither_service_has_a_tempo(self):
        self.assertEqual(reorder.choose_tempo(None, None, self.CLIP), (98.0, reorder.CLIP, ""))
        self.assertEqual(reorder.choose_tempo(feat(110), None, self.CLIP)[1], "reccobeats")
        self.assertEqual(reorder.choose_tempo(None, feat(110), self.CLIP)[1], "getsongbpm")

    def test_clip_tracks_are_placed_by_tempo_with_their_key_ignored(self):
        tracks = [{"line": "Known - Band", "id": ID_A}, {"line": "Clip - Band", "id": ID_B}]
        ordered, unplaced = reorder.plan(tracks, {ID_A: feat(110)}, {},
                                         clips={"Clip - Band": self.CLIP})
        clip_entry = next(e for e in ordered if e.line == "Clip - Band")
        self.assertEqual((clip_entry.bpm, clip_entry.source, clip_entry.flag), (98.0, reorder.CLIP, ""))
        self.assertEqual((clip_entry.key, clip_entry.mode), (None, None))
        self.assertEqual(unplaced, [])

    def test_report_labels_clip_tempos_and_counts_them(self):
        tracks = [{"line": "Known - Band", "id": ID_A}, {"line": "Clip - Band", "id": ID_B}]
        lines = []
        reorder.report(*reorder.plan(tracks, {ID_A: feat(110)}, {}, clips={"Clip - Band": self.CLIP}),
                       cross_checked=True, log=lines.append)
        self.assertTrue(any("Clip - Band" in l and "from preview clip" in l for l in lines))
        self.assertIn("Measured from a preview clip (least reliable, tempo only): 1.", lines)
        self.assertIn("Tempo confirmed by both services: 0. From one service only: 1.", lines)


class MeasureMissingTests(unittest.TestCase):
    TRACKS = [{"line": "Known - Band", "id": ID_A}, {"line": "Gsb Only - Band", "id": ID_B},
              {"line": "Nobody - Band", "id": ID_C}, {"line": "No Clip - Band", "id": ID_D}]
    RECCO = {ID_A: feat(110)}
    GSB = {"Gsb Only - Band": feat(120)}

    def run_measure(self, measure, cache=None, should_stop=None):
        with mock.patch.object(reorder.audio_analysis, "measure", measure), \
             mock.patch.object(reorder.time, "sleep"):
            return reorder.measure_missing(self.TRACKS, self.RECCO, self.GSB, log=lambda m: None,
                                           progress=lambda m: None, cache=cache,
                                           should_stop=should_stop or (lambda: False))

    def test_measures_only_tracks_without_a_tempo(self):
        measure = mock.Mock(side_effect=lambda title, artist:
                            {"bpm": 100.0, "source": "audio"} if title == "Nobody" else None)
        clips = self.run_measure(measure)
        self.assertEqual([c.args[0] for c in measure.call_args_list], ["Nobody", "No Clip"])
        self.assertEqual(clips, {"Nobody - Band": {"bpm": 100.0, "source": "audio"}})

    def test_remembers_results_including_no_clip(self):
        cache = {}
        measure = mock.Mock(return_value=None)
        self.run_measure(measure, cache=cache)
        self.run_measure(measure, cache=cache)
        self.assertEqual(measure.call_count, 2)    # two tracks, measured once each

    def test_failures_are_not_remembered_so_they_are_retried(self):
        cache = {}
        measure = mock.Mock(side_effect=RuntimeError("download failed"))
        self.run_measure(measure, cache=cache)
        self.run_measure(measure, cache=cache)
        self.assertEqual(measure.call_count, 4)

    def test_stop_halts_measuring(self):
        measure = mock.Mock(return_value=None)
        self.run_measure(measure, should_stop=lambda: True)
        measure.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
