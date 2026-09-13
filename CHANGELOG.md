# Changelog

## Refactor + bug fixes

Rewrote the original `generate-playlist.py` (buggy, ran everything at import time,
un-importable name) into `generate_playlist.py` — an importable module with pure,
testable helpers, a `PlaylistBuilder` class, and a `main()` guard. Same CLI flags
and "add songs as you go" behavior are preserved. Third-party imports (spotipy,
lyricsgenius) are lazy so the logic loads — and tests run — without them.

### Matching (the "filters out everything" bug)
- **Case-insensitive matching.** Search terms are now normalized the same way as
  the lyrics, so a capitalized query (`Hotdog`) no longer matches zero songs.
- **Two independent gates.** A song must (1) clear the lyric threshold *and*
  (2) not already be in the set.
- **Empty/restricted lyrics never match** — a song we can't read isn't a themed hit.
- Scoring lives in pure `match_percentage` / `is_match`; multi-term queries
  accumulate correctly.

### De-duplication
A song is a duplicate if it matches an already-seen track on **any** of:
- Spotify track ID,
- normalized `(title, artist)` (`song_key`) — collapses `(Remastered)`,
  `- Single Version`, `feat. X`, punctuation, casing,
- lyric fingerprint (hash of normalized lyrics; zero extra API calls).

Existing playlist tracks are seeded on startup, so re-runs skip what's already in.

### Genius → Spotify resolution (stop losing songs)
- `resolve_spotify_track()` replaced the fragile single strict query with a
  cascade (strict → free-text → title-only) that scans **all** candidates and
  accepts only a **verified** one (`is_confident_match` — tolerant of
  feat./remaster/spacing, strict on wrong title/artist). Stops early to limit
  API calls.
- **Miss reporting** — a lyric match that can't be placed on Spotify is recorded
  with a reason (`not_found` / `no_confident_match` / `search_error`) and printed
  in a summary, instead of silently vanishing.

### Run-time visibility
- `evaluate_match()` prints a per-song QA line during a run:
  `  4.99% / 3.00% threshold  [MATCH]  Title - Artist` (verdicts: MATCH / below /
  no lyrics). Restores the original script's threshold visibility.

### Other
- Fixed Spotify pagination (was incrementing offset by 1 and looping forever;
  now pages by 50 up to Spotify's 1000 cap).
- Default `--bangerThreshold` changed from 1.5 to **3.0** (the empirically good ~3%).

## Tests (39, all offline — no live API calls, cannot cause rate limiting)
- `tests/test_matching.py` — normalize, match %, multi-term, dedup, fingerprint.
- `tests/test_resolver.py` — `is_confident_match`, resolver cascade/verification,
  miss tracking.
- `tests/test_pipeline.py` — end-to-end `from_genius` / `from_spotify` loops
  against fake clients (real logic, fake network).

Run: `python3 -m unittest discover -s tests`

## Tooling
- `probe.py` — a small read-only sanity script (auth check + BPM/key availability
  check). Confirmed this Spotify app gets **403 on audio-features** (BPM/key
  deprecated) — vibe ordering will need a fallback source.

## Known leftovers
- `generate-playlist.py` (original, hyphenated) is superseded by
  `generate_playlist.py` and can be deleted.
- Vibe ordering (#4), a frontend (#1), and a fetch-once index (#5) are not built yet.
