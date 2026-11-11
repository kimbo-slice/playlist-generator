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

``has_lyrics`` is ``None`` for a "pending" row: no definite answer yet, only a
note (``lrclib_miss_at``) that LRCLIB didn't have the song at that time, so
repeat runs can skip asking it again for a while (``note_lrclib_miss``).

``songs_mentioning(terms)`` returns cached songs whose lyrics might contain any
of the terms -- a cheap, generous pre-filter (one word fragment per term, case
-insensitive); callers score the results exactly with ``match_percentage``.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from typing import Iterable, List, Optional


def search_fragment(term: str) -> str:
    """The longest run of letters/digits in a term, for a substring pre-filter.

    "ain't" -> "ain", "hot dog" -> "hot". Any lyric containing the term, however
    it is punctuated or spaced, contains this fragment.
    """
    pieces = re.findall(r"\w+", term or "")
    return max(pieces, key=len).lower() if pieces else ""


class InMemoryLyricStore:
    """Dict-backed store for tests and ephemeral use."""

    def __init__(self):
        self._rows: dict[str, dict] = {}

    def get(self, song_key: str) -> Optional[dict]:
        row = self._rows.get(song_key)
        return dict(row) if row is not None else None

    def put(self, record: dict) -> None:
        previous = self._rows.get(record["song_key"], {})
        self._rows[record["song_key"]] = {"lrclib_miss_at": previous.get("lrclib_miss_at"), **record}

    def remember_track(self, song_key: str, spotify_id: str) -> None:
        if song_key in self._rows:
            self._rows[song_key]["spotify_id"] = spotify_id

    def note_lrclib_miss(self, song_key: str, title: str, artist: str, when: Optional[datetime] = None) -> None:
        row = self._rows.setdefault(song_key, {"song_key": song_key, "title": title, "artist": artist,
                                               "spotify_id": None, "lyrics": None, "has_lyrics": None})
        row["lrclib_miss_at"] = when or datetime.now(timezone.utc)

    def songs_mentioning(self, terms: Iterable[str]) -> List[dict]:
        fragments = [f for f in (search_fragment(t) for t in terms) if f]
        return [dict(r) for r in self._rows.values()
                if r.get("has_lyrics") and r.get("lyrics")
                and any(f in r["lyrics"].lower() for f in fragments)]

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
        );
        ALTER TABLE songs ADD COLUMN IF NOT EXISTS lrclib_miss_at TIMESTAMPTZ
    """

    _NOTE_MISS = """
        INSERT INTO songs (song_key, title, artist, lrclib_miss_at)
        VALUES (%s, %s, %s, now())
        ON CONFLICT (song_key) DO UPDATE SET lrclib_miss_at = now()
    """

    _COLUMNS = ("song_key", "title", "artist", "spotify_id", "lyrics", "has_lyrics", "lrclib_miss_at")

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
                "SELECT {} FROM songs WHERE song_key = %s".format(", ".join(self._COLUMNS)),
                (song_key,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return dict(zip(self._COLUMNS, row))

    def note_lrclib_miss(self, song_key: str, title: str, artist: str) -> None:
        """Remember that LRCLIB didn't have this song (leaves any answer untouched)."""
        with self._conn.cursor() as cur:
            cur.execute(self._NOTE_MISS, (song_key, title, artist))

    def remember_track(self, song_key: str, spotify_id: str) -> None:
        """Save the Spotify track a cached song was placed as (lyrics untouched)."""
        with self._conn.cursor() as cur:
            cur.execute("UPDATE songs SET spotify_id = %s WHERE song_key = %s", (spotify_id, song_key))

    def songs_mentioning(self, terms: Iterable[str]) -> List[dict]:
        fragments = [f for f in (search_fragment(t) for t in terms) if f]
        if not fragments:
            return []
        patterns = ["%" + f.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
                    for f in fragments]
        keys = ("song_key", "title", "artist", "spotify_id", "lyrics", "has_lyrics")
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT song_key, title, artist, spotify_id, lyrics, has_lyrics "
                "FROM songs WHERE has_lyrics AND lyrics ILIKE ANY(%s)",
                (patterns,),
            )
            return [dict(zip(keys, row)) for row in cur.fetchall()]

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
