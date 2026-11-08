"""Tests for re-keying the lyric cache (pure planning; no database)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import migrate_song_keys as m


def row(key, title, artist, has_lyrics=True, fetched_at="2026-11-01"):
    return {"song_key": key, "title": title, "artist": artist,
            "has_lyrics": has_lyrics, "fetched_at": fetched_at}


class PlanTests(unittest.TestCase):
    def test_up_to_date_rows_are_left_alone(self):
        self.assertEqual(m.plan([row("tennis court\x1florde", "Tennis Court", "Lorde")]), ({}, []))

    def test_rows_that_are_not_songs_are_left_alone(self):
        self.assertEqual(m.plan([row("__test__", "T", "A")]), ({}, []))

    def test_curly_apostrophe_key_is_renamed(self):
        old = "when we’re together\x1fkristen bell"
        renames, deletes = m.plan([row(old, "When We’re Together", "Kristen Bell")])
        self.assertEqual(renames, {old: "when were together\x1fkristen bell"})
        self.assertEqual(deletes, [])

    def test_same_song_under_both_spellings_keeps_the_one_with_lyrics(self):
        curly = row("when we’re together\x1fkristen bell", "When We’re Together", "Kristen Bell")
        straight = row("when were together\x1fkristen bell", "When We're Together", "Kristen Bell",
                       has_lyrics=False, fetched_at="2026-11-07")
        renames, deletes = m.plan([curly, straight])
        self.assertEqual(deletes, [straight["song_key"]])
        self.assertEqual(renames, {curly["song_key"]: straight["song_key"]})

    def test_when_both_have_lyrics_the_newest_wins(self):
        curly = row("lovin’ it\x1fband", "Lovin’ It", "Band", fetched_at="2026-10-01")
        straight = row("lovin it\x1fband", "Lovin' It", "Band", fetched_at="2026-11-01")
        renames, deletes = m.plan([curly, straight])
        self.assertEqual((renames, deletes), ({}, [curly["song_key"]]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
