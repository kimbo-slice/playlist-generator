"""Lyrics from LRCLIB (https://lrclib.net), a free, open lyrics database.

The backup lyrics source for when Genius won't serve lyrics (Genius began
refusing this app's website requests with 403 in November 2026). LRCLIB
publishes a public API with no key; it asks clients to identify themselves
(``USER_AGENT``), to send requests one at a time with a short pause between
them, and to honor ``Retry-After`` on a 429 -- ignoring it can get a client
temporarily banned. ``_http_get`` does all three.

Lookups try the name as given, then without release details ("(Walmart
Performance)", "feat. X", "(효린)"), which LRCLIB's search often doesn't match.
LRCLIB often holds an instrumental entry next to the sung version of a song,
so a record with lyrics always wins over an instrumental one. Every result is
verified with the generator's ``is_confident_match`` against the name we were
asked for, so a different song with a similar name doesn't supply the wrong
lyrics.

Tested against 300 songs Genius had lyrics for: see CHANGELOG.md. Network
access goes through an injectable ``http_get`` so this is tested offline.
"""

from __future__ import annotations

import random
import threading
import time
from typing import Callable, List, Optional, Tuple

from generate_playlist import is_confident_match, strip_release_info

LRCLIB_URL = "https://lrclib.net/api/"
USER_AGENT = "playlist-generator (personal project; https://github.com/kimbo-slice/playlist-generator)"

# LRCLIB allows identified clients about 90 requests/minute and asks for a
# 200-500 ms pause between requests; 0.7 s between request starts stays under 90/min.
MIN_INTERVAL = 0.7
MAX_RETRIES = 3          # 429s honored per request before giving up (the song is retried next run)

HttpGet = Callable[[str, dict], object]

_lock = threading.Lock()
_last_request = 0.0


class RateLimited(Exception):
    """LRCLIB kept answering 429 after we waited as told."""


def _http_get(path: str, params: dict, get=None, sleep=time.sleep, clock=time.monotonic):
    """GET an LRCLIB endpoint, paced and honoring Retry-After; ``None`` for 404."""
    global _last_request
    if get is None:
        import requests
        get = requests.get
    for attempt in range(MAX_RETRIES + 1):
        with _lock:
            wait = _last_request + MIN_INTERVAL - clock()
            if wait > 0:
                sleep(wait)
            _last_request = clock()
        response = get(LRCLIB_URL + path, params=params, timeout=20,
                       headers={"User-Agent": USER_AGENT})
        if response.status_code == 429:
            if attempt == MAX_RETRIES:
                raise RateLimited("LRCLIB still rate limiting after %d waits" % MAX_RETRIES)
            try:
                delay = float(response.headers.get("Retry-After", 60))
            except ValueError:
                delay = 60.0
            sleep(delay * (1 + random.random() * 0.3))   # jitter, as LRCLIB asks
            continue
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()


def query_variants(title: str, artist: str) -> List[Tuple[str, str]]:
    """Names to search LRCLIB for: as given, then without release details."""
    artist = artist or ""
    bare = (strip_release_info(title), strip_release_info(artist.split(",")[0]))
    variants: List[Tuple[str, str]] = [(title, artist)]
    if bare[0] and bare != variants[0]:
        variants.append(bare)
    return variants


def _matches(record, title: str, artist: str) -> bool:
    return isinstance(record, dict) and is_confident_match(
        record.get("trackName"), record.get("artistName"), title, artist)


def lookup_lyrics(title: str, artist: str, http_get: HttpGet = _http_get) -> Optional[str]:
    """A song's plain lyrics from LRCLIB.

    Returns the lyric text, ``""`` for a track LRCLIB marks instrumental (an
    authoritative "no lyrics"), or ``None`` when LRCLIB doesn't have the song.
    Network errors propagate so the caller can treat them as "try again later".

    Requests: one ``get``; when that has no lyrics, one ``search``, plus a
    second only when the name has release details to drop.
    """
    instrumental = False
    record = http_get("get", {"track_name": title, "artist_name": artist})
    if _matches(record, title, artist):
        if record.get("plainLyrics"):
            return record["plainLyrics"]
        instrumental = bool(record.get("instrumental"))
    for query_title, query_artist in query_variants(title, artist):
        results = http_get("search", {"track_name": query_title,
                                      "artist_name": query_artist}) or []
        matching = [r for r in results if _matches(r, title, artist)]
        sung = next((r for r in matching if r.get("plainLyrics")), None)
        if sung is not None:
            return sung["plainLyrics"]
        instrumental = instrumental or any(r.get("instrumental") for r in matching)
    return "" if instrumental else None
