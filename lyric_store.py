"""Persistent lyric cache -- fetch each song's lyrics once, ever.

A ``LyricStore`` is a tiny key/value store keyed by a normalized song key
(``"<title>\\x1f<artist>"``). ``PlaylistBuilder`` checks it before any Genius
fetch and writes results back (including "no lyrics", so junk is never
re-fetched). This turns "re-scrape Genius every run" into a growing local corpus.

Two implementations:
- ``InMemoryLyricStore`` -- for tests (no network, no DB).
- ``PostgresLyricStore`` -- the real cloud-backed corpus (psycopg imported
  lazily so this module loads without it).

``build_lyric_store()`` returns a Postgres store when ``DATABASE_URL`` is set,
else ``None`` (no caching -> unchanged behavior).

A record is a plain dict: {song_key, title, artist, spotify_id, lyrics,
has_lyrics}. bpm/musical_key/mode columns are reserved for vibe ordering.
"""

from __future__ import annotations

import os
from typing import Optional


class InMemoryLyricStore:
    """Dict-backed store for tests and ephemeral use."""

    def __init__(self):
        self._rows: dict[str, dict] = {}

    def get(self, song_key: str) -> Optional[dict]:
        row = self._rows.get(song_key)
        return dict(row) if row is not None else None

    def put(self, record: dict) -> None:
        self._rows[record["song_key"]] = dict(record)

    def close(self) -> None:
        pass


class PostgresLyricStore:
    """Cloud Postgres corpus. Concurrency-safe (MVCC + upsert on song_key)."""

    _SCHEMA = """
        CREATE TABLE IF NOT EXISTS songs (
            song_key    TEXT PRIMARY KEY,
            title       TEXT,
            artist      TEXT,
            spotify_id  TEXT,
            lyrics      TEXT,
            has_lyrics  BOOLEAN,
            bpm         REAL,
            musical_key INTEGER,
            mode        INTEGER,
            fetched_at  TIMESTAMPTZ DEFAULT now()
        )
    """

    _UPSERT = """
        INSERT INTO songs (song_key, title, artist, spotify_id, lyrics, has_lyrics)
        VALUES (%(song_key)s, %(title)s, %(artist)s, %(spotify_id)s, %(lyrics)s, %(has_lyrics)s)
        ON CONFLICT (song_key) DO UPDATE SET
            title      = EXCLUDED.title,
            artist     = EXCLUDED.artist,
            spotify_id = COALESCE(EXCLUDED.spotify_id, songs.spotify_id),
            lyrics     = EXCLUDED.lyrics,
            has_lyrics = EXCLUDED.has_lyrics,
            fetched_at = now()
    """

    def __init__(self, dsn: str):
        import psycopg  # lazy: module stays importable without the driver/DB

        self._conn = psycopg.connect(dsn, autocommit=True)
        with self._conn.cursor() as cur:
            cur.execute(self._SCHEMA)

    def get(self, song_key: str) -> Optional[dict]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT song_key, title, artist, spotify_id, lyrics, has_lyrics "
                "FROM songs WHERE song_key = %s",
                (song_key,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        keys = ("song_key", "title", "artist", "spotify_id", "lyrics", "has_lyrics")
        return dict(zip(keys, row))

    def put(self, record: dict) -> None:
        params = {
            "song_key": record["song_key"],
            "title": record.get("title"),
            "artist": record.get("artist"),
            "spotify_id": record.get("spotify_id"),
            "lyrics": record.get("lyrics"),
            "has_lyrics": record.get("has_lyrics"),
        }
        with self._conn.cursor() as cur:
            cur.execute(self._UPSERT, params)

    def close(self) -> None:
        self._conn.close()


def build_lyric_store():
    """Return a Postgres store if DATABASE_URL is set, else None (no caching)."""
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        return None
    return PostgresLyricStore(dsn)
