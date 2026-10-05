"""Put an ordered list of track names on a Spotify playlist.

Give it one track per line ("Title - Artist"); it finds each on Spotify using
the same verified matching as the generator and writes them to a playlist in
the order given. This is the output half of vibe ordering: whatever decides the
order only has to produce a list.

    python3 tracklist.py -t "Bike ride" tracks.txt        # new playlist
    pbpaste | python3 tracklist.py -t "Bike ride"         # list from stdin
    python3 tracklist.py -p PLAYLIST_ID --replace tracks.txt   # reorder in place
    python3 tracklist.py --dry-run tracks.txt             # resolve only

Line format is forgiving: "Artist - Title" also works (tried as a fallback),
the artist is optional, numbering/bullets are ignored, and blank lines or lines
starting with "#" are skipped.

The web UI drives the same ``run()`` with its own ``emit`` / ``should_stop``.
"""

from __future__ import annotations

import argparse
import re
import sys
from typing import Callable, Optional

from generate_playlist import (
    PlaylistBuilder, build_spotify_client, is_confident_match, normalize_text,
)

BATCH_SIZE = 100  # Spotify's per-request limit for playlist items

_LEADING_MARKER_RE = re.compile(r"^(?:\d+[.)]\s+|[-*•]\s+)")
_FANCY_DASH_RE = re.compile(r"\s+[–—]\s+")
_SPOTIFY_ID_RE = re.compile(r"^[A-Za-z0-9]{22}$")
_PLAYLIST_LINK_RE = re.compile(r"playlist[/:]([A-Za-z0-9]{22})")


def _print_event(kind: str, message: str, **data) -> None:
    """Default progress sink (CLI): just print the line."""
    print(message)


def _never() -> bool:
    return False


# --------------------------------------------------------------------------- #
# Parsing (pure)
# --------------------------------------------------------------------------- #

def parse_tracklist(text: str) -> list[tuple[str, str]]:
    """Turn pasted text into ordered (title, artist) pairs; artist may be ""."""
    entries = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        line = _LEADING_MARKER_RE.sub("", line)
        line = _FANCY_DASH_RE.sub(" - ", line)
        # Split on the LAST " - " so titles like "Song - Remix" stay intact.
        if " - " in line:
            title, artist = line.rsplit(" - ", 1)
        else:
            title, artist = line, ""
        title, artist = title.strip(), artist.strip()
        if title:
            entries.append((title, artist))
    return entries


# --------------------------------------------------------------------------- #
# Reading an existing playlist
# --------------------------------------------------------------------------- #

def parse_playlist_id(link: str) -> Optional[str]:
    """Playlist id from a share link, a spotify:playlist: URI, or a bare id."""
    link = (link or "").strip()
    if _SPOTIFY_ID_RE.match(link):
        return link
    match = _PLAYLIST_LINK_RE.search(link)
    return match.group(1) if match else None


def fetch_playlist(sp, playlist_id: str) -> tuple[str, list[dict]]:
    """Return ``(name, tracks)`` for a playlist, tracks in playlist order.

    Each track is ``{"line": "Title - Artist", "id": <track id or None>}``. The
    id lets a reordered list be written back as the exact same tracks, with no
    name search (see ``known`` in :func:`resolve_tracklist`).
    """
    name = (sp.playlist(playlist_id, fields="name") or {}).get("name") or ""
    tracks, offset = [], 0
    while True:
        page = sp.playlist_items(
            playlist_id, fields="items(track(id,name,artists(name))),next",
            limit=BATCH_SIZE, offset=offset, additional_types=("track",),
        )
        items = page.get("items") or []
        for item in items:
            track = item.get("track") or {}
            if not track.get("name"):
                continue  # removed/unavailable entry
            artist = ((track.get("artists") or [{}])[0] or {}).get("name") or ""
            line = "{} - {}".format(track["name"], artist) if artist else track["name"]
            tracks.append({"line": line, "id": track.get("id")})
        if not items or not page.get("next"):
            break
        offset += len(items)
    return name, tracks


# --------------------------------------------------------------------------- #
# Resolving names to Spotify tracks
# --------------------------------------------------------------------------- #

def _line_key(title: str, artist: str) -> tuple[str, str]:
    """Light normalization (case/punctuation only) so remixes stay distinct."""
    return (normalize_text(title), normalize_text(artist))


def _known_ids(known: Optional[dict]) -> dict:
    """Index a ``{line: track_id}`` map by normalized (title, artist)."""
    ids = {}
    for line, track_id in (known or {}).items():
        parsed = parse_tracklist(line)
        if parsed and isinstance(track_id, str) and _SPOTIFY_ID_RE.match(track_id):
            ids.setdefault(_line_key(*parsed[0]), track_id)
    return ids


def _resolve_title_only(sp, title: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """No artist given: take the first result whose title really matches."""
    try:
        result = sp.search(q="track:{}".format(title), type="track", limit=10)
    except Exception:
        return None, None, "search_error"
    for item in result["tracks"]["items"]:
        artist = (item.get("artists") or [{}])[0].get("name")
        if is_confident_match(item.get("name"), artist, title, artist):
            return item.get("id"), "{} - {}".format(item.get("name"), artist), None
    return None, None, "not_found"


def resolve_tracklist(sp, entries: list[tuple[str, str]],
                      emit: Callable[..., None] = _print_event,
                      should_stop: Callable[[], bool] = _never,
                      known: Optional[dict] = None):
    """Resolve entries in order (stopping early if ``should_stop()``).

    ``known`` maps lines to track ids already known (e.g. loaded from an existing
    playlist); matching lines use that exact track and skip the Spotify search.

    Returns ``(resolved, skipped)``: ``resolved`` is an ordered list of
    ``(track_id, label)``; ``skipped`` is a list of ``{"line", "reason"}`` where
    reason is not_found / no_confident_match / search_error / duplicate.
    Emits a "found" or "miss" event per line.
    """
    # Borrow the generator's verified resolver; no Genius or matching needed here.
    builder = PlaylistBuilder(sp=sp, genius=None, searches=[], threshold=0.0,
                              on_event=lambda event: None)
    resolved, skipped, seen = [], [], set()
    known_ids = _known_ids(known)

    for title, artist in entries:
        if should_stop():
            break
        line = "{} - {}".format(title, artist) if artist else title
        label = line
        reason = None
        track_id = known_ids.get(_line_key(title, artist))
        if track_id is not None:
            pass  # exact track from the source playlist; nothing to look up
        elif artist:
            track_id, reason = builder.resolve_spotify_track(title, artist)
            if track_id is None and reason != "search_error":
                # Maybe it was written "Artist - Title".
                swapped_id, _ = builder.resolve_spotify_track(artist, title)
                if swapped_id is not None:
                    track_id, reason = swapped_id, None
                    label = "{} - {}".format(artist, title)
        else:
            track_id, found_label, reason = _resolve_title_only(sp, title)
            label = found_label or line

        if track_id is not None and track_id in seen:
            track_id, reason = None, "duplicate"
        if track_id is None:
            skipped.append({"line": line, "reason": reason})
            emit("miss", "  x {}  ({})".format(line, reason), line=line, reason=reason)
        else:
            seen.add(track_id)
            resolved.append((track_id, label))
            emit("found", "  + {}".format(label), track_id=track_id)
    return resolved, skipped


# --------------------------------------------------------------------------- #
# Writing the playlist
# --------------------------------------------------------------------------- #

def write_playlist(sp, track_ids: list[str], title: Optional[str] = None,
                   playlist_id: Optional[str] = None, replace: bool = False) -> str:
    """Write tracks in order; return the playlist id.

    No ``playlist_id`` -> create a new public playlist named ``title``.
    With ``playlist_id`` -> append, or overwrite its contents when ``replace``.
    """
    remaining = list(track_ids)
    if playlist_id is None:
        playlist_id = sp.user_playlist_create(
            sp.me()["id"], title, public=True,
            description="Ordered playlist built from a track list",
        )["id"]
    elif replace:
        sp.playlist_replace_items(playlist_id, remaining[:BATCH_SIZE])
        remaining = remaining[BATCH_SIZE:]

    for start in range(0, len(remaining), BATCH_SIZE):
        sp.playlist_add_items(playlist_id, remaining[start:start + BATCH_SIZE])
    return playlist_id


def run(sp, text: str, title: Optional[str] = None, playlist_id: Optional[str] = None,
        replace: bool = False, dry_run: bool = False,
        emit: Callable[..., None] = _print_event,
        should_stop: Callable[[], bool] = _never,
        known: Optional[dict] = None) -> dict:
    """Parse, resolve, and (unless dry_run or stopped) write. Returns a summary dict."""
    entries = parse_tracklist(text)
    emit("search", "Looking up {} track(s) on Spotify...".format(len(entries)))
    resolved, skipped = resolve_tracklist(sp, entries, emit=emit, should_stop=should_stop,
                                          known=known)
    summary = {"requested": len(entries), "resolved": len(resolved),
               "skipped": skipped, "playlist_id": None}

    if should_stop():
        # A partial list would be a wrong playlist, so stopping writes nothing.
        emit("summary", "\nStopped -- no playlist was created or changed.")
        return summary

    emit("summary", "\n{} of {} found.".format(len(resolved), len(entries)))
    if dry_run:
        emit("summary", "Check only -- no playlist was created or changed.")
    elif not resolved:
        # Never create an empty playlist or wipe an existing one over a bad list.
        emit("summary", "Nothing found -- no playlist was created or changed.")
    else:
        summary["playlist_id"] = write_playlist(
            sp, [track_id for track_id, _ in resolved],
            title=title, playlist_id=playlist_id, replace=replace,
        )
        emit("playlist",
             "Playlist: https://open.spotify.com/playlist/{}".format(summary["playlist_id"]),
             playlist_id=summary["playlist_id"], track_count=len(resolved))
    return summary


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("file", nargs="?", help="Track list file (default: read stdin)")
    parser.add_argument("--title", "-t", help="Create a new playlist with this title")
    parser.add_argument("--playlistId", "-p", help="Existing playlist to add to")
    parser.add_argument("--replace", action="store_true",
                        help="With -p: overwrite the playlist's contents instead of appending")
    parser.add_argument("--dry-run", action="store_true",
                        help="Resolve and report only; don't touch any playlist")
    return parser


def main(argv: Optional[list[str]] = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.dry_run and not (args.title or args.playlistId):
        parser.error("give --title for a new playlist, --playlistId for an existing one, or --dry-run")
    if args.replace and not args.playlistId:
        parser.error("--replace needs --playlistId")

    if args.file:
        with open(args.file, encoding="utf-8") as handle:
            text = handle.read()
    else:
        text = sys.stdin.read()

    if not parse_tracklist(text):
        parser.error("the track list is empty")
    run(build_spotify_client(), text, title=args.title, playlist_id=args.playlistId,
        replace=args.replace, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
