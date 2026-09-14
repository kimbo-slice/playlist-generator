"""Local web UI for the playlist generator (v1).

Runs the pipeline in-process on a background thread, streams its progress events
to the browser (polled via /api/events), embeds a Spotify player for the
resulting playlist, and exposes a Stop button.

Designed as the on-ramp to a hosted version: the pieces that would change when
hosting (who Spotify authenticates as, and how the run executes) are isolated
behind `Manager.executor` and the client factories, so hosting swaps those
without touching the console/player/stop plumbing.

Run it:
    uvicorn app:app --reload --port 8000
    # then open http://127.0.0.1:8000
"""

from __future__ import annotations

import threading
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

HERE = Path(__file__).parent


# --------------------------------------------------------------------------- #
# Run state
# --------------------------------------------------------------------------- #

class RunParams(BaseModel):
    query: str
    title: Optional[str] = None
    threshold: float = 3.0
    spotify: bool = True    # Spotify title search as the discovery engine
    genius: bool = True     # Genius as a discovery engine; lyrics always use Genius
    matches: Optional[List[str]] = None


class Run:
    """A single playlist-building run and its collected progress events."""

    def __init__(self, run_id: str, params: RunParams):
        self.id = run_id
        self.params = params
        self.events: List[dict] = []
        self.status = "running"  # running | complete | stopped | error
        self.playlist_id: Optional[str] = None
        self.stop_flag = threading.Event()
        self.thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    def emit(self, event: dict) -> None:
        """on_event sink: append a structured event (thread-safe)."""
        with self._lock:
            self.events.append(event)

    def should_stop(self) -> bool:
        return self.stop_flag.is_set()

    def slice(self, cursor: int) -> List[dict]:
        with self._lock:
            return self.events[cursor:]


def default_executor(run: Run) -> None:
    """Real execution: build clients, resolve the playlist, run the engines.

    Imported lazily so the web app (and its tests) load without spotipy/genius.
    """
    from generate_playlist import (
        PlaylistBuilder, build_spotify_client, build_genius_client, resolve_playlist,
    )
    from lyric_store import build_lyric_store

    p = run.params
    searches = p.matches if p.matches else [p.query]
    sp = build_spotify_client()
    genius = build_genius_client()
    store = build_lyric_store()  # Postgres if DATABASE_URL is set, else None

    builder = PlaylistBuilder(
        sp=sp, genius=genius, searches=searches, threshold=p.threshold,
        on_event=run.emit, stop_flag=run.stop_flag, lyric_store=store,
    )
    args = SimpleNamespace(playlistId=None, title=p.title, query=p.query)
    builder.playlist_id = resolve_playlist(sp, builder, args)
    run.playlist_id = builder.playlist_id
    # track_count reflects songs already in the playlist (seeded) -> lets the UI
    # mount the player immediately when re-running against a non-empty playlist.
    run.emit({"kind": "playlist", "message": "Playlist ready.",
              "playlist_id": builder.playlist_id, "track_count": len(builder.track_ids)})

    if p.spotify:
        builder.from_spotify(p.query)
    if p.genius:
        builder.from_genius(p.query)
    builder.report_misses()


def _run_thread(run: Run, executor) -> None:
    try:
        executor(run)
        run.status = "stopped" if run.should_stop() else "complete"
        run.emit({"kind": "done", "message": "Stopped." if run.should_stop() else "Done.",
                  "status": run.status})
    except Exception as exc:  # noqa: BLE001 -- surface any failure to the console
        run.status = "error"
        run.emit({"kind": "error", "message": "Run failed: {}".format(exc)})
        run.emit({"kind": "done", "message": "Ended with error.", "status": "error"})


class Manager:
    """Owns the single active run (local, single-user)."""

    def __init__(self):
        self.run: Optional[Run] = None
        self.executor = default_executor  # swappable (tests, hosting)

    def start(self, params: RunParams) -> Run:
        if self.run is not None and self.run.status == "running":
            raise HTTPException(status_code=409, detail="A run is already in progress.")
        run = Run(uuid.uuid4().hex, params)
        run.thread = threading.Thread(target=_run_thread, args=(run, self.executor), daemon=True)
        self.run = run
        run.thread.start()
        return run

    def stop(self) -> bool:
        if self.run is None or self.run.status != "running":
            return False
        self.run.stop_flag.set()
        return True


manager = Manager()


# --------------------------------------------------------------------------- #
# App
# --------------------------------------------------------------------------- #

app = FastAPI(title="Playlist Generator")
app.state.manager = manager


@app.get("/")
def index():
    return FileResponse(HERE / "index.html")


@app.post("/api/run")
def start_run(params: RunParams):
    run = manager.start(params)
    return {"run_id": run.id, "status": run.status}


@app.post("/api/stop")
def stop_run():
    return {"stopped": manager.stop()}


@app.get("/api/events")
def get_events(cursor: int = 0):
    run = manager.run
    if run is None:
        return {"events": [], "cursor": 0, "status": "idle", "playlist_id": None}
    events = run.slice(cursor)
    return {
        "events": events,
        "cursor": cursor + len(events),
        "status": run.status,
        "playlist_id": run.playlist_id,
    }
