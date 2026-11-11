"""Reorder a playlist by tempo and key and print the new order for review.

Reads one of your Spotify playlists (or a "Title - Artist" text list), gets each
track's tempo, and prints them so each tempo steps smoothly to the next, starting
from the slowest. Read-only: it changes
nothing on Spotify. Paste the list at the bottom into the Track list tab (or
``tracklist.py``) to write it.

    python3 reorder.py PLAYLIST_LINK
    python3 reorder.py tracks.txt
    python3 reorder.py PLAYLIST_LINK --no-key      # tempo only

How the tempo numbers are made trustworthy:

- Every tempo is folded into 70-140 BPM, so a song one service calls 74 and
  another calls 149 is the same number either way.
- ReccoBeats supplies the tempo (it is tied to the exact Spotify track).
  GetSongBPM fills in songs ReccoBeats doesn't know and otherwise only checks it.
- A track is flagged when the two still disagree after folding. Tempos only
  one of them could supply are used as they are and counted in the summary.
- For tracks neither service knows, the tempo is measured from the track's
  30-second preview clip (``audio_analysis``) and labelled as such: the least
  reliable source, used only as a fallback. Tracks with no clip either go last,
  to place by hand. ``--no-clips`` skips this step.

Key comes from ReccoBeats too, with GetSongBPM filling in the songs it doesn't
know. The two are not compared on key: when they differ, ReccoBeats is trusted.
Key measured from a clip is too unreliable to use, so clip-measured tracks are
ordered by tempo alone.

Needs GETSONGBPM_API_KEY for the cross-check (skipped with a note if unset).
"""

from __future__ import annotations

import argparse
import os
import time
from dataclasses import dataclass
from typing import Optional

import audio_analysis
import tempo_sources
import tracklist
import vibe_order

YOURS = "you"           # Entry.source for a tempo the user typed in
CLIP = "preview clip"   # Entry.source for a tempo measured from the preview clip
AGREE_PCT = 3.0         # folded tempos this close count as the same
PROGRESS_EVERY = 10     # report "done/total" this often during lookups and clips
GETSONGBPM_PAUSE = 0.3  # seconds between lookups; their limit is 3000/hour
CLIP_PAUSE = 0.2        # seconds between preview-clip measurements


NOT_FOUND = "no tempo found (not in either service, no preview clip)"
PILE_HEADING = "# No tempo found -- place these by hand"


@dataclass(frozen=True)
class Entry:
    line: str                     # "Title - Artist"
    was: int                      # 1-based position in the original order
    bpm: Optional[float] = None   # folded tempo used for ordering
    source: str = ""              # which service supplied it
    flag: str = ""                # why this one needs a decision ("" = fine)
    confirmed: bool = False       # both services gave the same tempo
    readings: tuple = ()          # the differing tempos, when the services disagree
    key: Optional[int] = None
    mode: Optional[int] = None


# --------------------------------------------------------------------------- #
# Choosing a tempo per track (pure)
# --------------------------------------------------------------------------- #

def choose_tempo(recco: Optional[dict], gsb: Optional[dict],
                 clip: Optional[dict] = None) -> tuple[Optional[float], str, str]:
    """Return ``(folded bpm, source, flag)`` from the services' answers.

    ``clip`` (a preview-clip measurement) is used only when neither service has
    a tempo.
    """
    recco_bpm = vibe_order.fold_bpm((recco or {}).get("bpm"))
    gsb_bpm = vibe_order.fold_bpm((gsb or {}).get("bpm"))
    if recco_bpm and gsb_bpm and vibe_order.tempo_gap(recco_bpm, gsb_bpm) > AGREE_PCT:
        return recco_bpm, "reccobeats", "sources disagree: {:.0f} vs {:.0f}".format(
            recco_bpm, gsb_bpm)
    if recco_bpm:
        return recco_bpm, "reccobeats", ""
    if gsb_bpm:
        return gsb_bpm, "getsongbpm", ""
    clip_bpm = vibe_order.fold_bpm((clip or {}).get("bpm"))
    if clip_bpm:
        return clip_bpm, CLIP, ""
    return None, "", NOT_FOUND


def line_key(line: str) -> str:
    """How a typed-in tempo is matched to its track: the line, ignoring case and spacing."""
    return " ".join(line.lower().split())


def plan(tracks: list[dict], recco: dict, gsb: dict, use_key: bool = True,
         overrides: Optional[dict] = None,
         clips: Optional[dict] = None) -> tuple[list[Entry], list[Entry]]:
    """Return ``(ordered, unplaced)`` entries.

    ``tracks`` are ``{"line", "id"}`` dicts in the current order; ``recco`` maps
    Spotify id to features and ``gsb`` maps line to features. Key is ReccoBeats'
    where it has one, else GetSongBPM's; ``use_key=False`` orders by tempo alone.
    ``overrides`` maps a line to a tempo the user supplied; it beats both services.
    ``clips`` maps a line to a preview-clip measurement, used only when neither
    service has a tempo; its key is ignored.
    """
    yours = {line_key(line): vibe_order.fold_bpm(bpm) for line, bpm in (overrides or {}).items()}
    entries = []
    for position, track in enumerate(tracks, start=1):
        from_recco, from_gsb = recco.get(track.get("id")), gsb.get(track["line"])
        bpm, source, flag = choose_tempo(from_recco, from_gsb, (clips or {}).get(track["line"]))
        best = tempo_sources.best_features(from_recco, from_gsb) or {}
        both = [vibe_order.fold_bpm((f or {}).get("bpm")) for f in (from_recco, from_gsb)]
        confirmed, readings = bool(not flag and all(both)), ()
        if yours.get(line_key(track["line"])):
            bpm, source, flag, confirmed = yours[line_key(track["line"])], YOURS, "", False
        elif flag and all(both):
            readings = tuple(both)
        entries.append(Entry(track["line"], position, bpm, source, flag, confirmed, readings,
                             best.get("key"), best.get("mode")))

    # Label each track by its index so duplicate lines stay distinct.
    ordered, _ = vibe_order.order_tracks([
        vibe_order.Track(str(i), bpm=e.bpm, key=e.key if use_key else None,
                         mode=e.mode if use_key else None)
        for i, e in enumerate(entries) if e.bpm])
    return ([entries[int(t.label)] for t in ordered], [e for e in entries if not e.bpm])


# --------------------------------------------------------------------------- #
# Looking tempos up
# --------------------------------------------------------------------------- #

def _never() -> bool:
    return False


def _counter(label: str, total: int, progress):
    """Calls ``progress("  <label> n/total")`` every PROGRESS_EVERY items and at the end."""
    def tick(done: int) -> None:
        if done == total or done % PROGRESS_EVERY == 0:
            progress("  {} {}/{}".format(label, done, total))
    return tick


def lookup(tracks: list[dict], api_key: Optional[str], log=print, progress=print,
           should_stop=_never, cache: Optional[dict] = None) -> tuple[dict, dict]:
    """Ask both services about every track; returns ``(recco, gsb)`` for :func:`plan`.

    ``cache`` (any dict, reused between calls) remembers every answer, including
    "unknown", so reordering the same tracks again asks the services nothing.
    """
    cache = {} if cache is None else cache
    seen_ids, seen_lines = cache.setdefault("recco", {}), cache.setdefault("gsb", {})

    new_ids = [t["id"] for t in tracks if t.get("id") and t["id"] not in seen_ids]
    if new_ids:
        found = tempo_sources.reccobeats_features(new_ids)
        seen_ids.update({track_id: found.get(track_id) for track_id in new_ids})

    if api_key:
        to_ask = [t for t in tracks if t["line"] not in seen_lines]
        tick = _counter("GetSongBPM", len(to_ask), progress)
        for done, track in enumerate(to_ask, 1):
            if should_stop():
                break
            parsed = tracklist.parse_tracklist(track["line"])
            title, artist = parsed[0] if parsed else (track["line"], "")
            try:
                seen_lines[track["line"]] = tempo_sources.getsongbpm_lookup(
                    title, artist, api_key=api_key)
            except Exception as exc:  # noqa: BLE001 -- report and keep going
                log("  ! GetSongBPM error for {}: {}".format(track["line"], type(exc).__name__))
            tick(done)
            time.sleep(GETSONGBPM_PAUSE)

    recco = {t["id"]: seen_ids[t["id"]] for t in tracks if seen_ids.get(t.get("id"))}
    gsb = {t["line"]: seen_lines[t["line"]] for t in tracks if seen_lines.get(t["line"])}
    return recco, gsb


def measure_missing(tracks: list[dict], recco: dict, gsb: dict, log=print, progress=print,
                    should_stop=_never, cache: Optional[dict] = None) -> dict:
    """Measure, from preview clips, the tempo of tracks neither service knows.

    Returns ``{line: features}`` for the ones that could be measured, for
    :func:`plan`. ``cache`` remembers results, including "no preview clip", so
    the same track is never downloaded twice; failed attempts are retried.
    """
    cache = {} if cache is None else cache
    seen = cache.setdefault("clips", {})
    missing = [t for t in tracks
               if not (recco.get(t.get("id")) or {}).get("bpm")
               and not (gsb.get(t["line"]) or {}).get("bpm")]
    to_measure = [t for t in missing if t["line"] not in seen]
    if to_measure:
        progress("\nMeasuring {} track(s) from preview clips (a few seconds each)...".format(
            len(to_measure)))
    tick = _counter("clips", len(to_measure), progress)
    for done, track in enumerate(to_measure, 1):
        if should_stop():
            break
        parsed = tracklist.parse_tracklist(track["line"])
        title, artist = parsed[0] if parsed else (track["line"], "")
        try:
            seen[track["line"]] = audio_analysis.measure(title, artist)
        except Exception as exc:  # noqa: BLE001 -- report, leave uncached, keep going
            log("  ! Preview clip error for {}: {}".format(track["line"], type(exc).__name__))
        tick(done)
        time.sleep(CLIP_PAUSE)
    return {t["line"]: seen[t["line"]] for t in missing if (seen.get(t["line"]) or {}).get("bpm")}


def camelot(key: Optional[int], mode: Optional[int]) -> str:
    """Pitch class + mode -> Camelot notation ("8B" = C major, "8A" = A minor); "?" if unknown."""
    if key is None or mode not in (0, 1):
        return "?"
    fifths = (key * 7) % 12
    return "{}B".format((fifths + 7) % 12 + 1) if mode == 1 else "{}A".format((fifths + 4) % 12 + 1)


def table(tracks: list[dict], recco: dict, gsb: dict, clips: dict, log=print) -> None:
    """Every track in its current order with BPM (as measured, not folded), Camelot key and source."""
    log("{:>3}  {:<60} {:>5}  {:<4}  {}".format("#", "track", "BPM", "key", "source"))
    for number, track in enumerate(tracks, 1):
        r, g, c = recco.get(track.get("id")), gsb.get(track["line"]), clips.get(track["line"])
        r_bpm, g_bpm, c_bpm = ((x or {}).get("bpm") for x in (r, g, c))
        bpm = r_bpm or g_bpm or c_bpm
        source = ("both" if r_bpm and g_bpm else "reccobeats" if r_bpm else "getsongbpm" if g_bpm
                  else "preview clip" if c_bpm else "none")
        features = tempo_sources.best_features(r, g) or {}
        log("{:>3}  {:<60.60} {:>5}  {:<4}  {}".format(
            number, track["line"], "{:.0f}".format(bpm) if bpm else "?",
            camelot(features.get("key"), features.get("mode")), source))


def load_tracks(sp, source: str, log=print) -> tuple[str, list[dict]]:
    """``source`` is a playlist link/id, or a path to a "Title - Artist" list."""
    if os.path.isfile(source):
        with open(source, encoding="utf-8") as handle:
            entries = tracklist.parse_tracklist(handle.read())
        resolved, skipped = tracklist.resolve_tracklist(sp, entries, emit=lambda *a, **k: None)
        for miss in skipped:
            log("  ! not on Spotify, left out: {} ({})".format(miss["line"], miss["reason"]))
        return source, [{"line": label, "id": track_id} for track_id, label in resolved]
    playlist_id = tracklist.parse_playlist_id(source)
    if playlist_id is None:
        raise SystemExit("That is neither a file nor a Spotify playlist link: " + source)
    return tracklist.fetch_playlist(sp, playlist_id)


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #

def paste_lines(ordered: list[Entry], unplaced: list[Entry]) -> list[str]:
    """The new order as track-list text; unplaced tracks follow a comment line."""
    lines = [entry.line for entry in ordered]
    if unplaced:
        lines.append(PILE_HEADING)
        lines.extend(entry.line for entry in unplaced)
    return lines


def report(ordered: list[Entry], unplaced: list[Entry], cross_checked: bool, log=print,
           paste: bool = True) -> None:
    log("{:>3}  {:>4}  {:>4}  {:<52}  {}".format("new", "was", "BPM", "track", "check"))
    for position, entry in enumerate(ordered, start=1):
        log("{:>3}  {:>4}  {:>4.0f}  {:<52.52}  {}".format(
            position, entry.was, entry.bpm, entry.line,
            entry.flag or {YOURS: "your tempo", CLIP: "from preview clip"}.get(entry.source, "")))
    for entry in unplaced:
        log("{:>3}  {:>4}  {:>4}  {:<52.52}  {}".format("-", entry.was, "?", entry.line, entry.flag))

    everything = ordered + unplaced
    log("\n{} track(s), {} need a decision.".format(
        len(everything), sum(bool(e.flag) for e in everything)))
    if cross_checked:
        log("Tempo confirmed by both services: {}. From one service only: {}.".format(
            sum(e.confirmed for e in everything),
            sum(bool(e.bpm) and not e.confirmed and not e.flag and e.source not in (YOURS, CLIP)
                for e in everything)))
    else:
        log("GetSongBPM cross-check skipped (GETSONGBPM_API_KEY is not set).")
    from_clips = sum(e.source == CLIP for e in everything)
    if from_clips:
        log("Measured from a preview clip (least reliable, tempo only): {}.".format(from_clips))

    if paste:
        log("\n# Paste-ready order")
        for text in paste_lines(ordered, unplaced):
            log(text)


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("source", help="Spotify playlist link/id, or a track list file")
    parser.add_argument("--no-key", action="store_true",
                        help="Order by tempo alone, ignoring key")
    parser.add_argument("--no-clips", action="store_true",
                        help="Don't measure missing tempos from preview clips")
    parser.add_argument("--table", action="store_true",
                        help="Just list each track's BPM and Camelot key, in the current order")
    args = parser.parse_args(argv)

    name, tracks = load_tracks(tracklist.build_spotify_client(), args.source)
    api_key = os.environ.get("GETSONGBPM_API_KEY")
    import tempo_store
    store = tempo_store.build_tempo_store()   # saved lookups, if DATABASE_URL is set
    cache: dict = {}
    loaded = tempo_store.preload(store, tracks, cache)
    saved = sum(1 for t in tracks if t.get("id") in loaded["recco"]
                or t["line"] in loaded["gsb"] or t["line"] in loaded["clips"])
    print('Getting tempos for {} track(s) from "{}" ({} already saved)...\n'.format(
        len(tracks), name, saved))
    try:
        recco, gsb = lookup(tracks, api_key, cache=cache)
        clips = {} if args.no_clips else measure_missing(tracks, recco, gsb, cache=cache)
    finally:
        tempo_store.save_new(store, cache, loaded)   # keep what was found, even if stopped
    if args.table:
        table(tracks, recco, gsb, clips)
        return
    ordered, unplaced = plan(tracks, recco, gsb, use_key=not args.no_key, clips=clips)
    report(ordered, unplaced, cross_checked=bool(api_key))


if __name__ == "__main__":
    main()
