"""Tempo (BPM) and musical key lookups from free services.

Spotify no longer gives this app audio features, so playlist ordering gets
tempo and key from elsewhere:

- **ReccoBeats** -- lookup by Spotify track id; no account or key.
- **GetSongBPM** -- lookup by title + artist; free key in ``GETSONGBPM_API_KEY``
  (their terms require the link back in the README).

Both return the same shape so callers don't care which answered::

    {"bpm": 89.0, "key": 0, "mode": 1, "source": "getsongbpm"}

``key`` is a pitch class (0 = C ... 11 = B) and ``mode`` is 1 major / 0 minor,
the same convention as the ``bpm`` / ``musical_key`` / ``mode`` columns reserved
in the lyric store. Either may be ``None`` when the service doesn't know.

Network access goes through an injectable ``http_get(url, params) -> json`` so
the parsing and matching here are unit tested without any real requests.
"""

from __future__ import annotations

import os
import re
import time
from typing import Callable, Optional

from generate_playlist import is_confident_match

RECCOBEATS_URL = "https://api.reccobeats.com/v1/audio-features"
RECCOBEATS_BATCH = 40
GETSONGBPM_URL = "https://api.getsong.co/search/"

_SPOTIFY_TRACK_RE = re.compile(r"track[/:]([A-Za-z0-9]{22})")
_OPEN_KEY_RE = re.compile(r"^(\d{1,2})([dm])$")
_BRACKETED_RE = re.compile(r"\s*[\(\[][^\)\]]*[\)\]]")   # "(Day-O)", "[Live]"
_DASH_SUFFIX_RE = re.compile(r"\s-\s.*$")                  # " - A-Trak Remix"
_FEATURING_RE = re.compile(r"\s+\b(?:feat|ft|featuring)\b.*", re.IGNORECASE)

HttpGet = Callable[[str, dict], object]


def _http_get(url: str, params: dict):
    """Default HTTP GET returning parsed JSON (requests imported lazily)."""
    import requests

    response = requests.get(url, params=params, timeout=15)
    response.raise_for_status()
    return response.json()


def _bpm(value) -> Optional[float]:
    try:
        bpm = float(value)
    except (TypeError, ValueError):
        return None
    return bpm if bpm > 0 else None


def parse_open_key(open_key: Optional[str]) -> tuple[Optional[int], Optional[int]]:
    """Open Key notation ("1d" = C major, "1m" = A minor) -> (pitch class, mode).

    The number walks the circle of fifths (2d = G, 3d = D ...); "d" is major and
    "m" is the relative minor of that major key.
    """
    match = _OPEN_KEY_RE.match((open_key or "").strip().lower())
    if not match or not 1 <= int(match.group(1)) <= 12:
        return None, None
    major_tonic = ((int(match.group(1)) - 1) * 7) % 12
    if match.group(2) == "d":
        return major_tonic, 1
    return (major_tonic + 9) % 12, 0


# --------------------------------------------------------------------------- #
# ReccoBeats (by Spotify id)
# --------------------------------------------------------------------------- #

def reccobeats_features(track_ids: list[str], http_get: HttpGet = _http_get) -> dict:
    """Look up many Spotify track ids at once; returns ``{spotify_id: features}``.

    Ids the service doesn't know are simply absent from the result.
    """
    found = {}
    unique = list(dict.fromkeys(i for i in track_ids if i))
    for start in range(0, len(unique), RECCOBEATS_BATCH):
        batch = unique[start:start + RECCOBEATS_BATCH]
        data = http_get(RECCOBEATS_URL, {"ids": ",".join(batch)})
        for item in (data.get("content") if isinstance(data, dict) else None) or []:
            match = _SPOTIFY_TRACK_RE.search(item.get("href") or "")
            if not match:
                continue
            key = item.get("key")
            mode = item.get("mode")
            found[match.group(1)] = {
                "bpm": _bpm(item.get("tempo")),
                "key": key if isinstance(key, int) and 0 <= key <= 11 else None,
                "mode": mode if mode in (0, 1) else None,
                "source": "reccobeats",
            }
    return found


# --------------------------------------------------------------------------- #
# GetSongBPM (by title + artist)
# --------------------------------------------------------------------------- #

def query_variants(title: str, artist: str) -> list[tuple[str, str]]:
    """Names to search with: as written first, then with release decoration removed.

    Punctuation is kept on purpose -- their search finds "Ain't No Rest for the
    Wicked" but not "aint no rest for the wicked".
    """
    plain_title = _DASH_SUFFIX_RE.sub("", _BRACKETED_RE.sub("", title)).strip() or title
    plain_artist = _FEATURING_RE.sub("", artist.split(",")[0]).strip() or artist
    variants = [(title.strip(), artist.strip()), (plain_title, plain_artist)]
    return list(dict.fromkeys(variants))


def _get_with_retry(http_get: HttpGet, url: str, params: dict):
    """One retry after a short pause; their API occasionally fails a request."""
    try:
        return http_get(url, params)
    except Exception:
        time.sleep(1.0)
        return http_get(url, params)


def getsongbpm_lookup(title: str, artist: str, api_key: Optional[str] = None,
                      http_get: HttpGet = _http_get) -> Optional[dict]:
    """Look up one song by name; ``None`` if unknown or no result really matches.

    Results are verified with the generator's ``is_confident_match`` so a cover
    or a different song with a similar name doesn't supply the wrong tempo.
    """
    api_key = api_key or os.environ.get("GETSONGBPM_API_KEY")
    if not api_key:
        raise RuntimeError("GETSONGBPM_API_KEY is not set.")
    for query_title, query_artist in query_variants(title, artist):
        data = _get_with_retry(http_get, GETSONGBPM_URL, {
            "api_key": api_key, "type": "both",
            "lookup": "song:{} artist:{}".format(query_title, query_artist),
        })
        results = data.get("search") if isinstance(data, dict) else None
        if not isinstance(results, list):   # {"search": {"error": "no result"}}
            continue
        for item in results:
            found_artist = (item.get("artist") or {}).get("name")
            if not is_confident_match(item.get("title"), found_artist, title, artist):
                continue
            key, mode = parse_open_key(item.get("open_key"))
            return {"bpm": _bpm(item.get("tempo")), "key": key, "mode": mode,
                    "source": "getsongbpm"}
    return None


# --------------------------------------------------------------------------- #
# Combining the two
# --------------------------------------------------------------------------- #

def best_features(reccobeats: Optional[dict], getsongbpm: Optional[dict]) -> Optional[dict]:
    """ReccoBeats wins (it is tied to the exact Spotify track); GetSongBPM fills gaps.

    When ReccoBeats is missing only the tempo or only the key, that part is
    taken from GetSongBPM. Returns ``None`` when neither knows the track.
    """
    if not reccobeats:
        return dict(getsongbpm) if getsongbpm else None
    merged = dict(reccobeats)
    if getsongbpm:
        if not merged.get("bpm"):
            merged["bpm"] = getsongbpm.get("bpm")
        if merged.get("key") is None:
            merged["key"], merged["mode"] = getsongbpm.get("key"), getsongbpm.get("mode")
    return merged
