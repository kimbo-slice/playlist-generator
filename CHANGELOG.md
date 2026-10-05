# Changelog

## Track list -> playlist (`tracklist.py`)
- New `tracklist.py`: give it one track per line ("Title - Artist") and it finds
  each on Spotify with the generator's verified matching, then writes them to a
  playlist in the order given. First step toward vibe ordering.
- New playlist (`-t`), append to an existing one (`-p`), overwrite in place
  (`-p ... --replace`), or `--dry-run` to resolve without touching anything.
- Forgiving input: "Artist - Title" is tried as a fallback, artist is optional,
  numbering/bullets/comments are ignored. Unfound and duplicate lines are
  reported, and a list that resolves to nothing never creates or wipes a playlist.
- In the web UI: a "Track list" tab (paste tracks, name the playlist, "Create
  playlist" or "Check only") backed by `POST /api/tracklist`. It shares the
  console, Stop button and player with the lyric search; Stop cancels without
  creating anything. Append/replace on an existing playlist is CLI-only for now.
- Start from an existing playlist: paste a Spotify playlist link and "Load
  tracks" fills the box with its tracks in order (`POST /api/playlist-tracks`).
  Loaded lines remember their exact Spotify track, so a reordered list is
  written back as the same tracks with no name search; edited or new lines
  fall back to the normal lookup.
- +34 offline tests (tool + endpoints).

## Multi-term search + dedup pre-check
- Search several terms at once: comma-separate them in the UI query field, or
  pass `-q` + `-m` on the CLI. `assemble_terms()` flattens/splits/de-dupes them;
  discovery now runs once per term while matching scores against the whole set.
  So a term like "pocket" gets its own candidates, not just songs found under
  the main term.
- De-dup pre-check: known songs (by track ID or normalized title/artist) are now
  skipped *before* fetching lyrics -- cheaper, and no more misleading
  "[MATCH] then Skipping duplicate". +5 tests (67 total).

## Fetch-once lyric cache (Postgres corpus)
- New `lyric_store.py`: a `LyricStore` interface with `InMemoryLyricStore`
  (tests) and `PostgresLyricStore` (cloud corpus; psycopg imported lazily).
  `build_lyric_store()` returns Postgres when `DATABASE_URL` is set, else None.
- `PlaylistBuilder.cached_lyrics()` checks the store before any Genius fetch and
  writes results back -- including authoritative "no lyrics" (negative caching)
  so junk is never re-fetched. Transient errors/429s are NOT cached, so they can
  be retried. Keyed by `song_key`, so remixes/`feat.` variants share one entry.
- Re-enabled Genius discovery by default now that the cache makes both engines
  affordable.
- `SETUP_DATABASE.md`: one-time Neon/Supabase setup. `cache.db*` gitignored;
  `psycopg[binary]` added. +6 offline tests (62 total). No `DATABASE_URL` set ->
  unchanged behavior (no caching).

## Matching quality: strip Genius scaffolding
- `clean_genius_lyrics` now removes the "<Title> Lyrics" header, contributor
  preamble, and bracketed section/speaker labels (`[Chorus]`, `[Kristen:]`)
  before matching. Fixes false positives where a featured artist's name in a
  `[Name:]` label counted as a lyric hit (e.g. "kristen" scored 7.9% on a duet
  she only performs on -> now 0%). `build_genius_client` also sets
  `remove_section_headers=True`. +6 tests.

## Frontend v1 (local web UI)
- `app.py` (FastAPI) + `index.html` (vanilla JS): run the generator from the
  browser, watch a live console, Stop mid-run, and hear the playlist in an
  embedded Spotify player. Run with `uvicorn app:app --port 8000`.
- Added two hosting-ready seams to `PlaylistBuilder` (CLI behavior unchanged):
  an `on_event` callback (structured progress events; falls back to stdout) and
  a `stop_flag` (cooperative stop checked between songs/pages).
- Console updates by polling `/api/events` (swappable to SSE later).
- New offline tests: `tests/test_seams.py`, `tests/test_app.py` (TestClient +
  fake executor). Suite now 47, still zero live API calls.
- See `FRONTEND.md` for architecture and the hosting fast-follow plan.

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
