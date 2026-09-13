"""Unit tests for the pure matching / de-duplication logic.

These tests deliberately use in-memory fakes and never construct a real
Spotify or Genius client, so running the suite makes ZERO network calls and
cannot trigger API rate limiting.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import generate_playlist as gp


class FakeSpotify:
    """Minimal stand-in for a spotipy client used by PlaylistBuilder.add_song."""

    def __init__(self):
        self.added = []

    def me(self):
        return {"id": "test-user"}

    def user_playlist_add_tracks(self, user, playlist_id, tracks):
        self.added.extend(tracks)


def make_builder(threshold=1.5, searches=("hotdog",)):
    return gp.PlaylistBuilder(
        sp=FakeSpotify(),
        genius=None,  # never called in these tests
        searches=list(searches),
        threshold=threshold,
        playlist_id="playlist-123",
    )


class NormalizeTextTests(unittest.TestCase):
    def test_lowercases_and_strips_punctuation(self):
        self.assertEqual(gp.normalize_text("Hot-Dog!!"), "hotdog")

    def test_collapses_whitespace(self):
        self.assertEqual(gp.normalize_text("  hot   dog  "), "hot dog")

    def test_handles_none_and_empty(self):
        self.assertEqual(gp.normalize_text(None), "")
        self.assertEqual(gp.normalize_text(""), "")


class CleanLyricsTests(unittest.TestCase):
    def test_strips_title_header(self):
        cleaned = gp.clean_genius_lyrics("Text Me Merry Christmas Lyrics\nthis holiday")
        self.assertEqual(cleaned, "this holiday")

    def test_strips_contributor_preamble(self):
        cleaned = gp.clean_genius_lyrics("9 ContributorsSong Lyrics\nreal words")
        self.assertEqual(cleaned, "real words")

    def test_strips_section_and_speaker_labels(self):
        cleaned = gp.clean_genius_lyrics("Duet Lyrics\n[Kristen:]\nsnow falls\n[Mike:]\nlights glow")
        self.assertNotIn("[", cleaned)
        self.assertNotIn("Kristen", cleaned)
        self.assertNotIn("Mike", cleaned)
        self.assertIn("snow falls", cleaned)

    def test_strips_trailing_embed(self):
        self.assertEqual(gp.clean_genius_lyrics("words here5Embed"), "words here")

    def test_featured_artist_name_not_counted(self):
        # The bug: a featured artist's name in a [Name:] label counted as a lyric hit.
        raw = "Holiday Duet Lyrics\n[Kristen:]\nsnow is falling\n[Mike:]\nlights are glowing"
        cleaned = gp.clean_genius_lyrics(raw)
        # Searching the featured performer's name now scores zero -- it's only a label.
        self.assertEqual(gp.match_percentage(cleaned, ["kristen"]), 0.0)
        # A real lyric word still matches.
        self.assertGreater(gp.match_percentage(cleaned, ["snow"]), 0.0)

    def test_handles_empty(self):
        self.assertEqual(gp.clean_genius_lyrics(""), "")
        self.assertEqual(gp.clean_genius_lyrics(None), "")


class LyricFetchErrorTests(unittest.TestCase):
    def test_error_surfaces_exception_type_and_returns_empty(self):
        class Boom:
            def search_song(self, *a, **k):
                raise TimeoutError("read timed out")

        events = []
        b = gp.PlaylistBuilder(sp=None, genius=Boom(), searches=["x"], threshold=3.0,
                               on_event=events.append)
        self.assertEqual(b.get_lyrics_from_genius("Song", "Artist"), "")
        err = next(e for e in events if e["kind"] == "error")
        self.assertEqual(err["error"], "TimeoutError")  # diagnosable, not swallowed


class RateLimitTests(unittest.TestCase):
    def test_429_triggers_backoff_not_error(self):
        class Resp:
            status_code = 429

        class RateLimited(Exception):
            response = Resp()

        class Boom:
            def search_song(self, *a, **k):
                raise RateLimited("429 Too Many Requests")

        events = []
        b = gp.PlaylistBuilder(sp=None, genius=Boom(), searches=["x"], threshold=3.0,
                               on_event=events.append)
        b.request_stop()  # so the cooldown records backoff but doesn't actually sleep
        self.assertEqual(b.get_lyrics_from_genius("S", "A"), "")
        self.assertEqual(b.genius_backoff, 10.0)          # backed off
        kinds = [e["kind"] for e in events]
        self.assertIn("progress", kinds)                  # a "pausing to recover" notice
        self.assertNotIn("error", kinds)                  # NOT logged as a hard error

    def test_is_rate_limited_detection(self):
        self.assertTrue(gp.PlaylistBuilder._is_rate_limited(Exception("429 Client Error")))
        self.assertFalse(gp.PlaylistBuilder._is_rate_limited(Exception("read timed out")))


class MatchPercentageTests(unittest.TestCase):
    def test_case_insensitive_match(self):
        # This is the original bug: a capitalized query matched nothing.
        lyrics = "hotdog hotdog hotdog and more"
        pct_lower = gp.match_percentage(lyrics, ["hotdog"])
        pct_upper = gp.match_percentage(lyrics, ["HOTDOG"])
        self.assertGreater(pct_lower, 0)
        self.assertEqual(pct_lower, pct_upper)

    def test_empty_lyrics_scores_zero_not_error(self):
        self.assertEqual(gp.match_percentage("", ["hotdog"]), 0.0)
        self.assertEqual(gp.match_percentage(None, ["hotdog"]), 0.0)

    def test_is_match_threshold_boundary(self):
        lyrics = "hotdog " * 10  # term-heavy
        self.assertTrue(gp.is_match(lyrics, ["hotdog"], threshold=1.5))
        self.assertFalse(gp.is_match("nothing relevant here", ["hotdog"], threshold=1.5))

    def test_empty_lyrics_never_matches(self):
        self.assertFalse(gp.is_match("", ["hotdog"], threshold=0.0))

    def test_multiple_terms_accumulate(self):
        # "cat dog cat dog" normalizes to length 15. Each term appears twice
        # (2 * 3 chars = 6 chars), so each alone is 6/15 = 40%, and both
        # together should sum to 80%.
        lyrics = "cat dog cat dog"
        cat_only = gp.match_percentage(lyrics, ["cat"])
        dog_only = gp.match_percentage(lyrics, ["dog"])
        both = gp.match_percentage(lyrics, ["cat", "dog"])

        self.assertAlmostEqual(cat_only, 40.0, places=6)
        self.assertAlmostEqual(dog_only, 40.0, places=6)
        self.assertAlmostEqual(both, 80.0, places=6)
        # The multi-term score is the sum of the per-term contributions.
        self.assertAlmostEqual(both, cat_only + dog_only, places=6)

    def test_absent_term_contributes_nothing(self):
        # A term that never appears must not change the score.
        lyrics = "cat dog cat dog"
        with_absent = gp.match_percentage(lyrics, ["cat", "zebra"])
        self.assertAlmostEqual(with_absent, 40.0, places=6)

    def test_multi_term_is_match_uses_all_terms(self):
        # Each term alone is below the threshold, but combined they clear it --
        # proving is_match considers every term, not just the first.
        lyrics = "cat dog cat dog"
        self.assertFalse(gp.is_match(lyrics, ["cat"], threshold=50.0))
        self.assertFalse(gp.is_match(lyrics, ["dog"], threshold=50.0))
        self.assertTrue(gp.is_match(lyrics, ["cat", "dog"], threshold=50.0))

    def test_multi_term_is_case_insensitive(self):
        lyrics = "cat dog cat dog"
        self.assertAlmostEqual(
            gp.match_percentage(lyrics, ["CAT", "Dog"]),
            gp.match_percentage(lyrics, ["cat", "dog"]),
            places=6,
        )


class SongKeyTests(unittest.TestCase):
    def test_collapses_release_variants(self):
        a = gp.song_key("Song (Remastered 2011)", "Band")
        b = gp.song_key("Song - 2011 Remaster", "Band")
        c = gp.song_key("Song", "Band feat. Guest")
        self.assertEqual(a, b)
        self.assertEqual(a, c)

    def test_different_songs_differ(self):
        self.assertNotEqual(gp.song_key("Song A", "Band"), gp.song_key("Song B", "Band"))


class LyricFingerprintTests(unittest.TestCase):
    def test_same_lyrics_same_fingerprint(self):
        self.assertEqual(
            gp.lyric_fingerprint("Hot dog, hot dog!"),
            gp.lyric_fingerprint("hot   dog hot dog"),
        )

    def test_empty_lyrics_fingerprint_is_none(self):
        self.assertIsNone(gp.lyric_fingerprint(""))
        self.assertIsNone(gp.lyric_fingerprint(None))


class DeDuplicationTests(unittest.TestCase):
    def test_adds_new_song(self):
        b = make_builder()
        self.assertTrue(b.add_song("id-1", "Song", "Band", lyrics="hotdog lyrics"))
        self.assertEqual(b.sp.added, ["id-1"])

    def test_same_track_id_rejected(self):
        b = make_builder()
        b.add_song("id-1", "Song", "Band", lyrics="hotdog lyrics")
        self.assertFalse(b.add_song("id-1", "Song", "Band", lyrics="hotdog lyrics"))
        self.assertEqual(b.sp.added, ["id-1"])

    def test_same_song_different_id_rejected_by_title_artist(self):
        b = make_builder()
        b.add_song("id-album", "Song", "Band", lyrics="")
        # Different Spotify ID (a single release), no lyrics to fingerprint on.
        self.assertFalse(b.add_song("id-single", "Song (Single Version)", "Band", lyrics=""))
        self.assertEqual(b.sp.added, ["id-album"])

    def test_same_lyrics_rejected_even_with_different_title(self):
        b = make_builder()
        lyrics = "identical lyric body goes here"
        b.add_song("id-1", "Weird Title", "Band", lyrics=lyrics)
        self.assertFalse(b.add_song("id-2", "Totally Different Title", "Other", lyrics=lyrics))
        self.assertEqual(b.sp.added, ["id-1"])

    def test_empty_lyrics_do_not_collapse_unrelated_songs(self):
        b = make_builder()
        b.add_song("id-1", "Song A", "Band A", lyrics="")
        # Different song, also missing lyrics -> must NOT be treated as duplicate.
        self.assertTrue(b.add_song("id-2", "Song B", "Band B", lyrics=""))
        self.assertEqual(b.sp.added, ["id-1", "id-2"])

    def test_missing_track_id_rejected(self):
        b = make_builder()
        self.assertFalse(b.add_song(None, "Song", "Band", lyrics="hotdog"))
        self.assertFalse(b.add_song(0, "Song", "Band", lyrics="hotdog"))
        self.assertEqual(b.sp.added, [])

    def test_seeding_prevents_re_add(self):
        b = make_builder()
        b.register("id-1", "Song", "Band", lyrics="hotdog lyrics")
        self.assertFalse(b.add_song("id-1", "Song", "Band", lyrics="hotdog lyrics"))
        self.assertEqual(b.sp.added, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
