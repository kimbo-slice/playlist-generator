"""Measure tempo and key from a track's 30-second preview clip.

The fallback for tracks no tempo service knows, and a single consistent method
when we want one: find the track's preview on Deezer (public API, no key; their
terms allow private, non-commercial use), download the clip into memory,
estimate tempo and key with librosa (open source), and delete the clip.

Returns the same shape as ``tempo_sources``::

    {"bpm": 118.0, "key": 9, "mode": 0, "source": "audio"}

Limits: a 30-second clip from somewhere in the song, so intros, outros and key
changes are invisible; tempo can come out at half or double; key detection
confuses neighbouring and relative keys more often than the databases do.

``estimate_key`` is pure Python and the network/audio steps are injectable, so
the logic is unit tested without downloads or librosa.
"""

from __future__ import annotations

import math
import os
import tempfile
from typing import Callable, Optional

from generate_playlist import is_confident_match
import tempo_sources

DEEZER_SEARCH_URL = "https://api.deezer.com/search"

# Krumhansl-Kessler key profiles: how strongly each scale degree (from the
# tonic upward, in semitones) is felt in a major or minor key.
_MAJOR_PROFILE = [6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88]
_MINOR_PROFILE = [6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17]


def _correlation(a: list[float], b: list[float]) -> float:
    mean_a, mean_b = sum(a) / len(a), sum(b) / len(b)
    da, db = [x - mean_a for x in a], [y - mean_b for y in b]
    denominator = math.sqrt(sum(x * x for x in da) * sum(y * y for y in db))
    return sum(x * y for x, y in zip(da, db)) / denominator if denominator else 0.0


def estimate_key(chroma: list[float]) -> tuple[Optional[int], Optional[int], float]:
    """``(pitch class, mode, confidence)`` from 12 average pitch-class energies.

    ``chroma[0]`` is C. Compares the energies with every major and minor key
    profile and picks the best fit; confidence is that fit's correlation.
    """
    if len(chroma) != 12 or not any(chroma):
        return None, None, 0.0
    best = (None, None, -2.0)
    for tonic in range(12):
        for mode, profile in ((1, _MAJOR_PROFILE), (0, _MINOR_PROFILE)):
            rotated = [profile[(pitch - tonic) % 12] for pitch in range(12)]
            score = _correlation(chroma, rotated)
            if score > best[2]:
                best = (tonic, mode, score)
    return best


def deezer_preview_url(title: str, artist: str,
                       http_get=tempo_sources._http_get) -> Optional[str]:
    """URL of the track's 30-second Deezer preview, or None if not found/verified."""
    for query_title, query_artist in tempo_sources.query_variants(title, artist):
        data = http_get(DEEZER_SEARCH_URL, {"q": "{} {}".format(query_artist, query_title)})
        for item in (data.get("data") if isinstance(data, dict) else None) or []:
            found_artist = (item.get("artist") or {}).get("name")
            if item.get("preview") and is_confident_match(item.get("title"), found_artist,
                                                          title, artist):
                return item["preview"]
    return None


def _download(url: str) -> bytes:
    import requests

    response = requests.get(url, timeout=20)
    response.raise_for_status()
    return response.content


def analyze_audio(data: bytes) -> dict:
    """Tempo and key from audio bytes (librosa imported lazily)."""
    import librosa
    import numpy

    # The MP3 decoder needs a real file to recognise the format, so the clip
    # sits in a temp file only while it is decoded.
    handle = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
    try:
        handle.write(data)
        handle.close()
        samples, rate = librosa.load(handle.name, sr=22050, mono=True)
    finally:
        os.remove(handle.name)
    # Finer tempo steps (hop 128 instead of librosa's 512, which only resolves
    # ~6 BPM steps near 120) and a weaker pull toward 120 BPM. Chosen by testing
    # against tempos two databases agree on: 32 of 36 within 2%, up from 27.
    tempo = float(numpy.atleast_1d(librosa.feature.tempo(
        y=samples, sr=rate, hop_length=128, std_bpm=2.0))[0])
    chroma = librosa.feature.chroma_cqt(y=samples, sr=rate).mean(axis=1)
    key, mode, confidence = estimate_key([float(value) for value in chroma])
    return {"bpm": tempo if tempo > 0 else None, "key": key, "mode": mode,
            "key_confidence": confidence, "source": "audio"}


def measure(title: str, artist: str, http_get=tempo_sources._http_get,
            download: Callable[[str], bytes] = _download,
            analyze: Callable[[bytes], dict] = analyze_audio) -> Optional[dict]:
    """Find, fetch and analyze a track's preview; None if there's no preview to use."""
    url = deezer_preview_url(title, artist, http_get=http_get)
    if url is None:
        return None
    return analyze(download(url))
