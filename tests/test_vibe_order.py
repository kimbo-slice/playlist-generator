"""Tests for vibe_order.py. Pure arithmetic: no network, no fakes needed."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import vibe_order as vo
from vibe_order import Track

C, D, E, F, G, A = 0, 2, 4, 5, 7, 9
MAJOR, MINOR = 1, 0


class TempoGapTests(unittest.TestCase):
    def test_same_and_close_tempos(self):
        self.assertEqual(vo.tempo_gap(120, 120), 0.0)
        self.assertAlmostEqual(vo.tempo_gap(120, 126), 5.0, places=6)

    def test_double_and_half_time_count_as_the_same(self):
        self.assertAlmostEqual(vo.tempo_gap(70, 140), 0.0, places=6)
        self.assertAlmostEqual(vo.tempo_gap(185, 92.5), 0.0, places=6)
        self.assertLess(vo.tempo_gap(95, 185), 3.0)   # really 95 vs 92.5

    def test_symmetric_and_bounded(self):
        self.assertAlmostEqual(vo.tempo_gap(100, 131), vo.tempo_gap(131, 100), places=9)
        self.assertLessEqual(vo.tempo_gap(100, 141.4), 41.5)  # the worst case, half an octave

    def test_missing_tempo(self):
        self.assertIsNone(vo.tempo_gap(None, 120))
        self.assertIsNone(vo.tempo_gap(120, 0))


class HalfDoubleSwitchTests(unittest.TestCase):
    def test_detects_a_switch_between_half_and_double_time(self):
        self.assertTrue(vo.is_half_double_switch(89, 179))
        self.assertTrue(vo.is_half_double_switch(145, 73))
        self.assertFalse(vo.is_half_double_switch(120, 126))
        self.assertFalse(vo.is_half_double_switch(100, 135))
        self.assertFalse(vo.is_half_double_switch(None, 120))

    def test_a_switch_costs_extra_even_when_the_tempos_match(self):
        slow = Track("slow", bpm=90, key=C, mode=MAJOR)
        same_range = Track("same", bpm=90, key=C, mode=MAJOR)
        double_time = Track("double", bpm=180, key=C, mode=MAJOR)
        self.assertEqual(vo.transition_cost(slow, same_range), 0.0)
        self.assertEqual(vo.transition_cost(slow, double_time), vo.HALF_DOUBLE_SWITCH_COST)

    def test_ordering_keeps_switches_rare(self):
        # Three slow tracks and their double-time twins in matching keys. Pairing
        # purely by key would bounce between the two tempo ranges every step.
        tracks = [Track("%d %d" % (bpm, key), bpm=bpm, key=key, mode=MAJOR)
                  for bpm, key in ((88, C), (176, C), (90, G), (180, G), (92, D), (184, D))]
        ordered, _ = vo.order_tracks(tracks)
        switches = sum(vo.is_half_double_switch(a.bpm, b.bpm) for a, b in zip(ordered, ordered[1:]))
        self.assertEqual(switches, 1)


class KeyGapTests(unittest.TestCase):
    def gap(self, a, b):
        return vo.key_gap(Track("a", key=a[0], mode=a[1]), Track("b", key=b[0], mode=b[1]))

    def test_same_key(self):
        self.assertEqual(self.gap((C, MAJOR), (C, MAJOR)), 0)

    def test_neighbors_on_the_circle_of_fifths(self):
        self.assertEqual(self.gap((C, MAJOR), (G, MAJOR)), 1)
        self.assertEqual(self.gap((C, MAJOR), (F, MAJOR)), 1)
        self.assertEqual(self.gap((F, MINOR), (C, MINOR)), 1)

    def test_relative_major_and_minor_are_one_step(self):
        self.assertEqual(self.gap((C, MAJOR), (A, MINOR)), 1)

    def test_far_keys(self):
        self.assertEqual(self.gap((C, MAJOR), (6, MAJOR)), 6)   # C to F#
        self.assertEqual(self.gap((C, MAJOR), (C, MINOR)), 4)   # same note, other mode

    def test_missing_key_or_mode(self):
        self.assertIsNone(vo.key_gap(Track("a", key=None), Track("b", key=C, mode=MAJOR)))
        self.assertEqual(vo.key_gap(Track("a", key=C), Track("b", key=G, mode=MAJOR)), 1)


class TransitionCostTests(unittest.TestCase):
    def test_exchange_rate_between_tempo_and_key(self):
        base = Track("a", bpm=100, key=C, mode=MAJOR)
        six_percent_faster = Track("b", bpm=106, key=C, mode=MAJOR)
        two_keys_away = Track("c", bpm=100, key=D, mode=MAJOR)
        self.assertAlmostEqual(vo.transition_cost(base, six_percent_faster),
                               vo.transition_cost(base, two_keys_away), places=6)

    def test_symmetric(self):
        a, b = Track("a", bpm=100, key=C, mode=MAJOR), Track("b", bpm=131, key=E, mode=MINOR)
        self.assertAlmostEqual(vo.transition_cost(a, b), vo.transition_cost(b, a), places=9)

    def test_missing_data_is_neutral(self):
        known = Track("a", bpm=100, key=C, mode=MAJOR)
        perfect = vo.transition_cost(known, Track("b", bpm=100, key=C, mode=MAJOR))
        tempo_only = vo.transition_cost(known, Track("c", bpm=100))
        terrible = vo.transition_cost(known, Track("d", bpm=141, key=6, mode=MAJOR))
        self.assertLess(perfect, tempo_only)
        self.assertLess(tempo_only, terrible)


class OrderTests(unittest.TestCase):
    def test_tempo_only_tracks_come_out_slowest_to_fastest(self):
        tracks = [Track(str(bpm), bpm=bpm) for bpm in (120, 100, 131, 108, 125, 112, 104)]
        ordered, unplaced = vo.order_tracks(tracks)
        self.assertEqual([t.bpm for t in ordered], [100, 104, 108, 112, 120, 125, 131])
        self.assertEqual(unplaced, [])

    def test_same_tempo_tracks_walk_the_circle_of_fifths(self):
        tracks = [Track(name, bpm=bpm, key=key, mode=MAJOR)
                  for name, bpm, key in (("slow C", 99, C), ("A", 100, A), ("G", 100, G),
                                         ("D", 100, D), ("E", 100, E))]
        ordered, _ = vo.order_tracks(tracks)
        self.assertEqual([t.label for t in ordered], ["slow C", "G", "D", "A", "E"])

    def test_double_time_track_sits_with_its_half_time_twin(self):
        tracks = [Track("70", bpm=70), Track("100", bpm=100), Track("141", bpm=141),
                  Track("102", bpm=102), Track("72", bpm=72)]
        ordered, _ = vo.order_tracks(tracks)
        labels = [t.label for t in ordered]
        self.assertEqual(labels[0], "70")                       # starts from the slowest
        self.assertLessEqual(abs(labels.index("141") - labels.index("72")), 2)
        self.assertEqual(abs(labels.index("100") - labels.index("102")), 1)

    def test_unmeasured_tracks_are_returned_separately_in_original_order(self):
        tracks = [Track("no data 1"), Track("b", bpm=120), Track("no data 2"),
                  Track("key only", key=G, mode=MAJOR), Track("a", bpm=100), Track("c", bpm=110)]
        ordered, unplaced = vo.order_tracks(tracks)
        self.assertEqual([t.label for t in unplaced], ["no data 1", "no data 2"])
        self.assertEqual(sorted(t.label for t in ordered), ["a", "b", "c", "key only"])
        self.assertEqual(ordered[0].label, "a")   # slowest with a tempo starts, not the key-only one

    def test_is_a_permutation_and_never_worse_than_the_input_order(self):
        # A fixed, scrambled set of 40 tracks (no randomness: arithmetic only).
        tracks = [Track("t%d" % i, bpm=70 + (i * 37) % 95, key=(i * 5) % 12, mode=i % 2)
                  for i in range(40)]
        ordered, unplaced = vo.order_tracks(tracks)
        self.assertEqual(sorted(t.label for t in ordered), sorted(t.label for t in tracks))
        self.assertEqual(unplaced, [])
        self.assertLess(vo.total_cost(ordered), vo.total_cost(tracks))

    def test_same_input_gives_the_same_order(self):
        tracks = [Track("t%d" % i, bpm=80 + (i * 13) % 60, key=(i * 7) % 12, mode=MAJOR)
                  for i in range(25)]
        first, _ = vo.order_tracks(tracks)
        second, _ = vo.order_tracks(list(tracks))
        self.assertEqual([t.label for t in first], [t.label for t in second])

    def test_tiny_inputs(self):
        self.assertEqual(vo.order_tracks([]), ([], []))
        one = [Track("only", bpm=100)]
        self.assertEqual(vo.order_tracks(one), (one, []))
        ordered, _ = vo.order_tracks([Track("fast", bpm=140), Track("slow", bpm=90)])
        self.assertEqual([t.label for t in ordered], ["slow", "fast"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
