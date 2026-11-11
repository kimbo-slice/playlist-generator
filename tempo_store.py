"""Saved tempo and key lookups, so each track is only worked out once.

Spotify no longer gives this app tempo or key, so ``reorder`` pieces them
together from ReccoBeats (by Spotify id), GetSongBPM (by name) and preview
clips -- minutes for a big playlist. This keeps every answer in the same
Postgres database as the lyric cache, so reordering a playlist again, or
another playlist sharing tracks, asks nothing for the tracks already known.

Answers are stored per source, exactly as ``reorder``'s in-memory cache holds
them: ``("recco", spotify_id)``, ``("gsb", line)``, ``("clips", line)`` ->
features dict, or ``None`` for "this source doesn't know the track". A
"doesn't know" answer is trusted for ``RECHECK_DAYS``, then asked again, since
the services add tracks over time.

Two implementations, like ``lyric_store``: ``InMemoryTempoStore`` for tests,
``PostgresTempoStore`` for real (psycopg imported lazily).
``build_tempo_store()`` returns the Postgres one when ``DATABASE_URL`` is set.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, Optional

RECHECK_DAYS = 30
SECTIONS = ("recco", "gsb", "clips")


def _fresh(features, fetched_at: Optional[datetime], now: datetime) -> bool:
    """Known answers never expire; "doesn't know" answers expire after RECHECK_DAYS."""
    if features is not None:
        return True
    if fetched_at is None:
        return False
    if fetched_at.tzinfo is None:
        fetched_at = fetched_at.replace(tzinfo=timezone.utc)
    return now - fetched_at < timedelta(days=RECHECK_DAYS)


class InMemoryTempoStore:
    def __init__(self):
        self.rows: Dict[tuple, tuple] = {}

    def load(self, source: str, keys: Iterable[str], now: Optional[datetime] = None) -> dict:
        now = now or datetime.now(timezone.utc)
        out = {}
        for key in keys:
            row = self.rows.get((source, key))
            if row is not None and _fresh(row[0], row[1], now):
                out[key] = row[0]
        return out

    def save(self, source: str, entries: dict, when: Optional[datetime] = None) -> None:
        when = when or datetime.now(timezone.utc)
        for key, features in entries.items():
            self.rows[(source, key)] = (features, when)

    def close(self) -> None:
        pass


class PostgresTempoStore:
    _SCHEMA = """
        CREATE TABLE IF NOT EXISTS tempo_lookups (
            source      TEXT NOT NULL,
            lookup_key  TEXT NOT NULL,
            features    JSONB,
            fetched_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (source, lookup_key)
        )
    """

    def __init__(self, dsn: str):
        import psycopg  # lazy: module stays importable without the driver/DB

        self._conn = psycopg.connect(dsn, autocommit=True)
        self._conn.execute(self._SCHEMA)

    def load(self, source: str, keys: Iterable[str], now: Optional[datetime] = None) -> dict:
        keys = list(dict.fromkeys(k for k in keys if k))
        if not keys:
            return {}
        now = now or datetime.now(timezone.utc)
        rows = self._conn.execute(
            "SELECT lookup_key, features, fetched_at FROM tempo_lookups "
            "WHERE source = %s AND lookup_key = ANY(%s)", (source, keys)).fetchall()
        return {key: features for key, features, fetched_at in rows if _fresh(features, fetched_at, now)}

    def save(self, source: str, entries: dict) -> None:
        if not entries:
            return
        from psycopg.types.json import Jsonb

        with self._conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO tempo_lookups (source, lookup_key, features) VALUES (%s, %s, %s) "
                "ON CONFLICT (source, lookup_key) DO UPDATE SET "
                "features = EXCLUDED.features, fetched_at = now()",
                [(source, key, Jsonb(features) if features is not None else None)
                 for key, features in entries.items()])

    def close(self) -> None:
        self._conn.close()


def build_tempo_store():
    """Postgres store if DATABASE_URL is set, else None (nothing is saved)."""
    dsn = os.environ.get("DATABASE_URL")
    return PostgresTempoStore(dsn) if dsn else None


def preload(store, tracks: list[dict], cache: dict) -> Dict[str, set]:
    """Fill ``cache`` (reorder's per-source dicts) with saved answers for these tracks.

    Returns which keys were loaded per source, for :func:`save_new`.
    """
    if store is None:
        return {s: set() for s in SECTIONS}
    ids = [t["id"] for t in tracks if t.get("id")]
    lines = [t["line"] for t in tracks]
    loaded = {"recco": store.load("recco", ids), "gsb": store.load("gsb", lines),
              "clips": store.load("clips", lines)}
    for source, found in loaded.items():
        cache.setdefault(source, {}).update(found)
    return {source: set(found) for source, found in loaded.items()}


def save_new(store, cache: dict, loaded: Dict[str, set]) -> int:
    """Save every answer in ``cache`` that didn't come from the store; returns how many."""
    if store is None:
        return 0
    saved = 0
    for source in SECTIONS:
        new = {k: v for k, v in cache.get(source, {}).items() if k not in loaded.get(source, set())}
        store.save(source, new)
        saved += len(new)
    return saved
