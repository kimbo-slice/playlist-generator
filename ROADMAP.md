# Roadmap

Open work, roughly in dependency order. Done work lives in CHANGELOG.md.

## 1. Frontend to view collected songs  *(original goal #1)*
A UI to see a playlist's songs and which ones were added. Simplest version reads
the Spotify playlist directly; a richer version reads the local index (#5) to
also show match %, BPM/key, and misses. Local web app is the natural first step
and is ~90% of the structure a hosted version would need.

## 2. Vibe ORDERING by BPM + key  *(original goal #4)*
Reorder a finished playlist so tracks transition well — linear-ish BPM steps and
circle-of-fifths key moves (they don't have to match, just not be jarring).
- **Blocked on a data source:** this app gets HTTP 403 on Spotify's
  `audio-features` (deprecated for post-2024 apps — confirmed via probe.py).
- Fallback candidate: **GetSongBPM API** (free, gives BPM *and* key, needs an
  attribution backlink). Deezer gives BPM only.
- Store bpm/key on the per-song record (see #5) so ordering is a pure local read.

## 3. Own fetch-once index / datastore  *(original goal #5, the enabler)*
A local store (SQLite) so each song's lyrics are fetched once, ever.
- **`songs`**: song_id, title, artist, song_key, lyric_fingerprint, bpm,
  musical_key, mode, total_length, fetched_at.
- **`term_index`**: term, song_id, occurrences, match_pct.
- Store DERIVED stats, not raw lyric text (better legally — lyrics are
  input-only, never displayed — and far faster to search).
- Payoff: hours-long re-runs skip already-checked songs; search runs against
  local data instead of hammering Genius; feeds both #1 and #2.

## 4. Claude vibe-based discovery  *(new idea)*
Discover songs by *vibe/theme/mood* instead of literal lyric keywords — e.g.
"my birthday", "morning bike ride, building energy". Fills the gap lexical
matching can't reach (a perfect bike-ride song may never say "bike").

- **Slots in as a third discovery engine** next to `from_spotify` / `from_genius`:
  Claude returns `(title, artist)` pairs -> existing `resolve_spotify_track` ->
  dedup -> add. No new plumbing; this is the payoff of pluggable engines.
- **The verified resolver is already the hallucination guard:** invented songs
  simply won't resolve on Spotify and get logged as misses. `is_confident_match`
  + `record_miss` (built for "don't lose songs") double as "reject fake songs".
- **Bypasses the lyric-threshold gate** on purpose — it's a different mode
  ("trust Claude's taste" vs. "count the words"), selected per run
  (e.g. `--vibe "my birthday"`). Additive, not a replacement for lexical search.
- **New dependency:** `ANTHROPIC_API_KEY` (same env-var pattern as the others);
  small per-call cost. Batch requests (~25 at a time) and re-prompt for more on
  long runs; dedup handles repeats.
- **Open decision:** Claude-suggests-then-verify-on-Spotify (safest, recommended)
  vs. also lyric-checking the suggestions (stricter, but filters out great
  non-literal picks).

## Also noted
- Long runs don't need custom rate-limit backoff — spotipy retries 429s (retries=3)
  and lyricsgenius paces requests (sleep_time=0.2). The slowness IS the protection.
- `generate-playlist.py` (original, hyphenated) is superseded by
  `generate_playlist.py` and can be deleted whenever.
- Eventual hosting: per-user Spotify OAuth, server-side callback, Extended Quota
  Mode beyond 25 users. The pure logic already ports cleanly to a web backend.
