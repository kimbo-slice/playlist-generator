# Frontend (v1)

A local web UI to run the generator, watch it work in a live console, control it
with a Stop button, and hear the resulting playlist in an embedded Spotify player.
Built hosting-ready — the seams that change when hosting are isolated.

## Run it
```bash
pip install -r requirements.txt          # once
uvicorn app:app --reload --port 8000     # then open http://127.0.0.1:8000
```
Starting a run reuses the cached Spotify auth (`cache.txt`); if it's missing, run
`probe.py` once first to authenticate.

## What it does
- **Controls:** search term, playlist title, threshold %, Spotify/Genius toggles.
- **Live console:** every pipeline event streams in, colored by kind (match =
  green, miss = orange, below-threshold/duplicate = grey, error = red).
- **Stop:** cooperatively halts the run at the next song; the playlist keeps
  everything added so far.
- **Spotify player:** an iframe embed of the playlist, appearing once it's
  created and refreshable to pull newly added tracks.

## Architecture
```
Browser (index.html, vanilla JS)
  │  POST /api/run        → start a run (background thread)
  │  GET  /api/events     → poll for new progress events (+ status, playlist_id)
  │  POST /api/stop       → set the run's stop flag
  ▼
FastAPI (app.py)
  Manager → Run (events list, status, stop_flag, thread)
          → executor(run)         ← the injectable seam
  ▼
PlaylistBuilder (generate_playlist.py)
  on_event  → run.emit(...)       ← structured progress events
  stop_flag → checked per song/page
```

### Pipeline seams (added this iteration, CLI behavior unchanged)
- **`on_event`** callback: every step emits a structured event dict; when unset,
  events print to stdout (so the CLI is untouched).
- **`stop_flag`** (`threading.Event`): `from_spotify`/`from_genius` check it
  between songs and pages for a clean, non-destructive stop.

### Why polling, not SSE
The console polls `/api/events` every 400ms. For a local single-user console the
UX is identical to SSE, but it's simpler and trivially testable. Swappable to SSE
later without touching the pipeline.

## Hosting fast-follow (not built yet, but not blocked)
The only pieces that change:
- **Auth:** `build_spotify_client()` (cached single token) → per-user OAuth with a
  server-side callback + token storage.
- **Execution:** `Manager.executor` runs in a thread today → a background worker /
  queue for multiple concurrent users.
- **Run identity:** already keyed by id (one local run → N per user).

The console, player, Stop, event stream, and all pipeline logic stay put.

## Tests
`tests/test_seams.py` (event emission + cooperative stop) and `tests/test_app.py`
(start/stop/events via TestClient with a fake executor). Both offline — no API
calls, no rate-limit risk.

## Known limits
- Playback is Spotify's iframe embed: 30s previews unless you're logged into
  Spotify Premium in that browser. Full in-page playback (Web Playback SDK) is
  deferred.
- One run at a time (single-user local).
