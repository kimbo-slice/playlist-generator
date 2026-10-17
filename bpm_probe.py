"""Coverage probe: how many of a playlist's tracks do the tempo/key services know?

Reads one of your Spotify playlists, asks ReccoBeats and GetSongBPM about every
track, and reports what each one knows and how often they agree. Read-only: it
changes nothing on Spotify.

    python3 bpm_probe.py PLAYLIST_LINK            # whole playlist
    python3 bpm_probe.py PLAYLIST_LINK --limit 30 # first 30 tracks

Needs GETSONGBPM_API_KEY for the GetSongBPM half (skipped with a note if unset).
"""

from __future__ import annotations

import argparse
import os
import time
from typing import Optional

import tempo_sources
import tracklist
import vibe_order

KEY_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
GETSONGBPM_PAUSE = 0.3  # seconds between lookups; their limit is 3000/hour


def describe(features: Optional[dict]) -> str:
    if not features:
        return "-"
    bpm = "{:.0f}".format(features["bpm"]) if features.get("bpm") else "?"
    if features.get("key") is None:
        key = "?"
    else:
        key = KEY_NAMES[features["key"]] + {1: "", 0: "m"}.get(features.get("mode"), "?")
    return "{} BPM {}".format(bpm, key)


def tempos_agree(a: float, b: float) -> bool:
    """Within 3%, counting double/half time as the same tempo."""
    return vibe_order.tempo_gap(a, b) <= 3.0


def compare(tracks: list[dict], api_key: Optional[str], log=print) -> dict:
    """``tracks`` are ``{"line": "Title - Artist", "id": spotify_id}`` dicts."""
    recco = tempo_sources.reccobeats_features([t["id"] for t in tracks if t.get("id")])
    counts = {"tracks": len(tracks), "reccobeats": 0, "getsongbpm": 0, "both": 0,
              "neither": 0, "tempo_agree": 0, "tempo_compared": 0,
              "key_agree": 0, "key_compared": 0}

    for track in tracks:
        from_recco = recco.get(track.get("id"))
        from_gsb = None
        if api_key:
            parsed = tracklist.parse_tracklist(track["line"])
            title, artist = parsed[0] if parsed else (track["line"], "")
            try:
                from_gsb = tempo_sources.getsongbpm_lookup(title, artist, api_key=api_key)
            except Exception as exc:  # noqa: BLE001 -- report and keep going
                log("  ! GetSongBPM error for {}: {}".format(track["line"], type(exc).__name__))
            time.sleep(GETSONGBPM_PAUSE)

        counts["reccobeats"] += bool(from_recco)
        counts["getsongbpm"] += bool(from_gsb)
        counts["both"] += bool(from_recco and from_gsb)
        counts["neither"] += not (from_recco or from_gsb)
        if from_recco and from_gsb:
            if from_recco.get("bpm") and from_gsb.get("bpm"):
                counts["tempo_compared"] += 1
                counts["tempo_agree"] += tempos_agree(from_recco["bpm"], from_gsb["bpm"])
            if from_recco.get("key") is not None and from_gsb.get("key") is not None:
                counts["key_compared"] += 1
                counts["key_agree"] += (from_recco["key"], from_recco.get("mode")) == \
                                       (from_gsb["key"], from_gsb.get("mode"))
        log("  {:<52.52}  ReccoBeats: {:<14}  GetSongBPM: {}".format(
            track["line"], describe(from_recco), describe(from_gsb)))
    return counts


def report(counts: dict, have_key: bool, log=print) -> None:
    total = counts["tracks"] or 1

    def pct(n):
        return "{} of {} ({:.0f}%)".format(n, counts["tracks"], 100.0 * n / total)

    log("\nCoverage")
    log("  ReccoBeats knows:   {}".format(pct(counts["reccobeats"])))
    if have_key:
        log("  GetSongBPM knows:   {}".format(pct(counts["getsongbpm"])))
        log("  Either one knows:   {}".format(pct(counts["tracks"] - counts["neither"])))
        log("  Neither knows:      {}".format(pct(counts["neither"])))
        if counts["tempo_compared"]:
            log("Where both know a track")
            log("  Same tempo (within 3%, double/half counted as same): {} of {}".format(
                counts["tempo_agree"], counts["tempo_compared"]))
            log("  Same key: {} of {}".format(counts["key_agree"], counts["key_compared"]))
    else:
        log("  GetSongBPM: skipped (GETSONGBPM_API_KEY is not set)")


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("playlist", help="Spotify playlist link, URI, or id")
    parser.add_argument("--limit", type=int, help="Only check the first N tracks")
    args = parser.parse_args(argv)

    playlist_id = tracklist.parse_playlist_id(args.playlist)
    if playlist_id is None:
        parser.error("that doesn't look like a Spotify playlist link")

    name, tracks = tracklist.fetch_playlist(tracklist.build_spotify_client(), playlist_id)
    if args.limit:
        tracks = tracks[:args.limit]
    api_key = os.environ.get("GETSONGBPM_API_KEY")
    print('Checking {} track(s) from "{}"...\n'.format(len(tracks), name))
    report(compare(tracks, api_key), have_key=bool(api_key))


if __name__ == "__main__":
    main()
