# Changelog

## Remember LRCLIB misses for a month
- While Genius is refusing, a song LRCLIB doesn't have can't be cached as "no
  lyrics" (Genius may know it later), so every run asked LRCLIB about it
  again -- ~1 s per song, and load on a donation-run server.
- Now the miss is noted (`lrclib_miss_at`, a new column added automatically)
  without marking the song empty, and LRCLIB isn't asked about it again for
  30 days (`LRCLIB_RECHECK_DAYS`). Genius is still asked once it's back; after
  30 days LRCLIB gets another try, in case it has added the song.
- Live check: second lookup of a missing song 0.10 s instead of 1.46 s, no
  LRCLIB request. Saved-lyrics search ignores these "pending" rows.
- The console now says why a song has no lyric text: "not found" (no source
  has it), "instrumental", "no lyrics" (the song exists but has none), or
  "couldn't check" (an error or refusal), instead of "no lyrics" for all four.
  On 124 empty songs from a "cordial" run: 123 not found, 1 instrumental.
- Reorder reports progress during the slow steps ("GetSongBPM 30/75",
  "clips 20/45"), in the terminal and the web console, so a long lookup no
  longer looks stuck.
- Tempo and key lookups are saved (`tempo_store.py`, a `tempo_lookups` table in
  the same database) and reused by the web app and `reorder.py`. "Not known"
  answers are rechecked after 30 days. Live: reordering a 75-track playlist
  took 2 min 39 s the first time, 2.9 s the second.
- `reorder.py <playlist> --table`: each track's BPM (as measured) and Camelot
  key in the playlist's current order, with where each came from. Changes
  nothing.
- +22 offline tests (241 total, passing with the network blocked; the app's
  tests use an in-memory store, never the real database).

## Search your saved lyrics
- New first discovery step, "Saved lyrics" (checkbox in the UI, on by
  default; `--saved False` on the CLI): every song in the lyric cache is
  scored against all the search terms at once, strongest matches first.
  It makes no Genius or LRCLIB requests at all, so it can't be rate limited or
  blocked; Spotify is only asked to place matches, and the track found is saved
  back to the cache so the next search skips that lookup too.
- The cache pre-filters by one word fragment per term (`songs_mentioning`),
  then scores exactly with the same `match_percentage` as everything else.
  Tried on the real cache (3,387 songs with lyrics): "pocket" 89 matches,
  "blue" 144, "kristen, kristin" 16, each in under a second.
- Online discovery (Spotify, Genius) still runs after it and skips songs the
  saved search already added.
- +8 offline tests (220 total, passing with the network blocked).

## LRCLIB lyrics backup; respect a Genius refusal
- Genius began refusing this app's website requests (HTTP 403) after Nov 4,
  2026. The app no longer keeps asking: on the first 403 it stops using Genius
  for the rest of the run, with one console message instead of an error per song.
- New `lrclib.py`: lyrics from LRCLIB (lrclib.net), a free, open lyrics
  database with a public API (no key; the app identifies itself). Used when
  Genius has no lyrics or refuses. Lookups are verified with the same
  title/artist matching as everything else, retried without release details
  ("(Walmart Performance)", "feat. X"), and prefer a sung version over an
  instrumental entry with the same name.
- Polite client, per LRCLIB's docs: requests spaced 0.7 s apart (under the
  ~90/min allowed for identified clients) and `Retry-After` honored on a 429
  (they may ban clients that ignore it).
- Cache rules: lyrics from either source are cached. "No lyrics" is cached only
  when every source gave a definite answer, so songs missed while Genius is
  refusing get retried later, when Genius may answer again.
- Measured on 300 cached songs with Genius lyrics (295 real songs, 5 Genius
  translation pages). Of the 256 that are on Spotify: LRCLIB has the same
  lyrics for 189 (74%), different text for 12 (5%; about half are the same song
  transcribed differently), and nothing for 54 (21%). About 1.4 requests per
  song, 1 s per song.

## Curly apostrophes no longer lose songs
- `normalize_text` stripped only ASCII punctuation, so Genius's curly
  apostrophes and quotes (’ “ ”) survived: "You’re Not Alone" didn't match
  Spotify's "You're Not Alone". This hit Genius → Spotify placement,
  de-duplication, and searches with an apostrophe ("ain't"). It now strips all
  Unicode punctuation, and " – " (en dash) release suffixes are dropped like " - ".
- Measured on 40 cached songs with a curly apostrophe: placed on Spotify by the
  app's resolver 5 times before, 29 after.
- Cache keys change for 254 cached songs; `migrate_song_keys.py` re-keys them
  (report by default, `--apply` backs up and changes in one transaction). It
  also merges 15 songs cached twice under both spellings. Run it before using
  this version, or those songs' lyrics are fetched again.
- +23 offline tests (212 total, passing with the network blocked).

## Reorder by tempo and key
- "Reorder by tempo + key" button in the Track list tab (`POST /api/reorder`) and
  `reorder.py` on the command line: reads a playlist or list, gets each track's
  tempo and key, and puts the list in an order that steps smoothly. Changes only
  the box (or prints a list); nothing on Spotify until you create a playlist.
- Tempo and key come from ReccoBeats (by Spotify id, no key needed), cross-checked
  against GetSongBPM (by name, free key, backlink in the README) in
  `tempo_sources.py`. Every tempo is folded into 70-140 BPM so half/double
  readings compare equal; tracks where the two still disagree are flagged, and
  you can pick a reading or type a tempo (remembered in your browser).
- Fallback for tracks neither service knows (`audio_analysis.py`): tempo measured
  from the track's 30-second Deezer preview with librosa; the clip is deleted
  after measuring. Tempo only -- key from a clip proved too unreliable (right
  about 4 times in 10). Tested against tempos both databases agree on: 32 of 36
  within 2%. Tracks with no data at all go to a "place by hand" pile.
- Ordering rule (`vibe_order.py`): transition cost = tempo gap + steps apart on
  the circle of fifths (1 key step = 3% tempo), with a small cost for switching
  between half- and double-time; greedy chain from the slowest track, then a
  clean-up pass. Deterministic.
- `bpm_probe.py` reports how many of a playlist's tracks each service knows.
- +86 offline tests (178 total; the suite passes with the network blocked).

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
