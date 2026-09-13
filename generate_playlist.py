"""Build a thematic Spotify playlist from songs whose lyrics mention a search term.

The pipeline:
    1. Search Spotify and/or Genius for candidate songs.
    2. Pull each candidate's lyrics from Genius.
    3. Keep a song only if the search term(s) make up enough of the lyric text
       (see :func:`match_percentage` / :func:`is_match`).
    4. Add matches to a Spotify playlist, skipping duplicates -- both the same
       Spotify track ID *and* the same underlying song released under a
       different ID (album vs. single vs. remaster).

The pure matching/de-duplication helpers at the top of this module have no
third-party or network dependencies, so they can be unit tested without ever
calling Spotify or Genius. Heavy imports (spotipy, lyricsgenius) are done
lazily inside the client factories for the same reason.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import string
import threading
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional


# --------------------------------------------------------------------------- #
# Pure helpers (no network, safe to unit test)
# --------------------------------------------------------------------------- #

_PUNCTUATION_TABLE = str.maketrans("", "", string.punctuation)

# Things that distinguish *releases* of a song but not the song itself.
_PAREN_RE = re.compile(r"[\(\[][^\)\]]*[\)\]]")          # "(Remastered 2011)", "[Live]"
_DASH_SUFFIX_RE = re.compile(r"\s-\s.*$")                # " - 2011 Remaster", " - Single Version"
_FEAT_RE = re.compile(r"\b(?:feat|ft|featuring|with)\b.*", re.IGNORECASE)
_WHITESPACE_RE = re.compile(r"\s+")


def normalize_text(text: Optional[str]) -> str:
    """Lowercase, strip punctuation, and collapse whitespace.

    Used for lyric/term comparison so matching is case- and punctuation
    insensitive.
    """
    if not text:
        return ""
    stripped = text.translate(_PUNCTUATION_TABLE).lower()
    return _WHITESPACE_RE.sub(" ", stripped).strip()


def clean_genius_lyrics(lyrics: Optional[str]) -> str:
    """Trim boilerplate Genius appends to lyric text (e.g. a trailing 'Embed')."""
    if not lyrics:
        return ""
    # Genius often ends the blob with "<contributor-count>Embed".
    return re.sub(r"\d*Embed$", "", lyrics.strip())


def match_percentage(lyrics: Optional[str], searches: Iterable[str]) -> float:
    """Percentage of the lyric text (by character count) made up of the terms.

    Both the lyrics and each search term are normalized the same way, so
    ``"Hotdogs"`` matches ``"hot dogs are great"`` occurrences of ``"hotdogs"``
    regardless of casing. Returns ``0.0`` for empty / restricted lyrics rather
    than raising.
    """
    normalized_lyrics = normalize_text(lyrics)
    total_chars = len(normalized_lyrics)
    if total_chars == 0:
        return 0.0

    matched_chars = 0
    for search in searches:
        term = normalize_text(search)
        if not term:
            continue
        matched_chars += normalized_lyrics.count(term) * len(term)
    return matched_chars / total_chars * 100.0


def is_match(lyrics: Optional[str], searches: Iterable[str], threshold: float) -> bool:
    """True if the search terms occupy at least ``threshold`` percent of the lyrics.

    Empty / restricted lyrics never match: a song we cannot read the lyrics for
    is not a themed hit, regardless of threshold.
    """
    if not normalize_text(lyrics):
        return False
    return match_percentage(lyrics, searches) >= threshold


def song_key(title: Optional[str], artist: Optional[str]) -> tuple[str, str]:
    """A normalized identity for a song, collapsing different releases of it.

    ``"Song (Remastered 2011)"`` by ``"Band feat. Guest"`` and ``"Song - Single
    Version"`` by ``"Band"`` produce the same key, so they de-duplicate against
    each other even though their Spotify IDs differ.
    """
    def norm(value: Optional[str]) -> str:
        value = value or ""
        value = _PAREN_RE.sub("", value)
        value = _DASH_SUFFIX_RE.sub("", value)
        value = _FEAT_RE.sub("", value)
        return normalize_text(value)

    primary_artist = (artist or "").split(",")[0]
    return (norm(title), norm(primary_artist))


def lyric_fingerprint(lyrics: Optional[str]) -> Optional[str]:
    """A stable hash of the normalized lyrics, or ``None`` for empty lyrics.

    Two songs with identical lyric text share a fingerprint, which catches
    duplicates whose titles/artists are formatted differently (or missing).
    Empty lyrics return ``None`` so restricted/unavailable lyrics never collapse
    unrelated songs together.
    """
    normalized = normalize_text(lyrics)
    if not normalized:
        return None
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()


def is_confident_match(
    candidate_title: Optional[str],
    candidate_artist: Optional[str],
    wanted_title: Optional[str],
    wanted_artist: Optional[str],
) -> bool:
    """True if a Spotify search result really is the song we were looking for.

    Guards the lossy Genius -> Spotify hand-off: a strict text query can return
    a cover, a live cut, or an unrelated track, so we verify before trusting it.
    Reuses ``song_key`` normalization, so release variants (``feat.``,
    remasters, parentheticals) and pure spacing differences (``"Hot Dog"`` vs
    ``"Hotdog"``) still count as a match, while a different title or artist does
    not.
    """
    cand_title, cand_artist = song_key(candidate_title, candidate_artist)
    want_title, want_artist = song_key(wanted_title, wanted_artist)

    if not cand_title or not want_title:
        return False

    title_ok = (
        cand_title == want_title
        or cand_title.replace(" ", "") == want_title.replace(" ", "")
    )
    if not title_ok:
        return False

    if cand_artist == want_artist:
        return True
    # One artist string containing the other handles "Band" vs "Band & Guests".
    if cand_artist and want_artist and (cand_artist in want_artist or want_artist in cand_artist):
        return True
    return False


# --------------------------------------------------------------------------- #
# Playlist building
# --------------------------------------------------------------------------- #

@dataclass
class PlaylistBuilder:
    """Collects matching, de-duplicated songs into a Spotify playlist.

    ``sp`` is a spotipy client and ``genius`` a lyricsgenius client. They are
    only invoked by the ``from_*`` search methods, so the de-duplication logic
    (:meth:`register` / :meth:`add_song`) can be exercised in tests with light
    fakes.
    """

    sp: object
    genius: object
    searches: list[str]
    threshold: float
    playlist_id: Optional[str] = None
    track_ids: set = field(default_factory=set)
    seen_keys: set = field(default_factory=set)
    seen_fingerprints: set = field(default_factory=set)
    # Songs found in a lyrics DB but not placeable on Spotify (title/artist + reason).
    misses: list = field(default_factory=list)
    # Seams for the web UI (both optional -> CLI behavior is unchanged):
    #   on_event: called with a structured event dict for every notable step;
    #             when None, events fall back to printing to stdout.
    #   stop_flag: set to request a cooperative stop between songs/pages.
    on_event: Optional[Callable[[dict], None]] = None
    stop_flag: threading.Event = field(default_factory=threading.Event)

    # -- progress + control seams ------------------------------------------ #
    def emit(self, kind: str, message: str = "", **data) -> None:
        """Report a step as a structured event (web UI) or a printed line (CLI)."""
        event = {"kind": kind, "message": message, **data}
        if self.on_event is not None:
            self.on_event(event)
        elif message:
            print(message)

    def request_stop(self) -> None:
        """Ask an in-progress run to stop at the next song/page boundary."""
        self.stop_flag.set()

    def should_stop(self) -> bool:
        return self.stop_flag.is_set()

    # -- de-duplication ---------------------------------------------------- #
    def is_new_song(
        self,
        track_id: Optional[str],
        title: str,
        artist: str,
        lyrics: Optional[str] = None,
    ) -> bool:
        """True if this song has not been seen before.

        A song is a duplicate if it matches an already-seen track on *any* of:
        Spotify track ID, normalized (title, artist), or lyric fingerprint.
        """
        if not track_id:
            return False
        if track_id in self.track_ids:
            return False
        if song_key(title, artist) in self.seen_keys:
            return False
        fingerprint = lyric_fingerprint(lyrics)
        if fingerprint is not None and fingerprint in self.seen_fingerprints:
            return False
        return True

    def register(
        self,
        track_id: Optional[str],
        title: str,
        artist: str,
        lyrics: Optional[str] = None,
    ) -> None:
        """Record a song as seen without adding it to the playlist (seeding)."""
        if track_id:
            self.track_ids.add(track_id)
        self.seen_keys.add(song_key(title, artist))
        fingerprint = lyric_fingerprint(lyrics)
        if fingerprint is not None:
            self.seen_fingerprints.add(fingerprint)

    def add_song(
        self,
        track_id: Optional[str],
        title: str,
        artist: str,
        lyrics: Optional[str] = None,
    ) -> bool:
        """Add a song to the playlist if it is new; return whether it was added."""
        if not self.is_new_song(track_id, title, artist, lyrics):
            self.emit("duplicate", "Skipping duplicate: {} - {}".format(title, artist),
                      title=title, artist=artist)
            return False
        self.register(track_id, title, artist, lyrics)
        try:
            self.sp.user_playlist_add_tracks(
                user=self.sp.me()["id"],
                playlist_id=self.playlist_id,
                tracks=[track_id],
            )
            self.emit("added", "Added: {} - {}".format(title, artist),
                      title=title, artist=artist, track_id=track_id)
            return True
        except Exception:
            self.emit("error", "Error adding song to playlist :( {} - {}".format(title, artist),
                      title=title, artist=artist)
            return False

    # -- lyric + spotify lookups ------------------------------------------ #
    def get_lyrics_from_genius(self, title: str, artist: str) -> str:
        try:
            song = self.genius.search_song(title, artist=artist, get_full_info=False)
        except Exception:
            self.emit("error", "Error fetching lyrics for {} - {}".format(title, artist))
            return ""
        if song is None:
            return ""
        return clean_genius_lyrics(song.lyrics)

    def resolve_spotify_track(
        self, title: str, artist: str
    ) -> tuple[Optional[str], Optional[str]]:
        """Find the Spotify track ID for a song discovered elsewhere (e.g. Genius).

        Returns ``(track_id, None)`` on a verified match, or ``(None, reason)``
        when the song can't be placed -- ``reason`` is one of ``"not_found"``
        (Spotify returned nothing across all queries), ``"no_confident_match"``
        (results came back but none passed verification), or ``"search_error"``.

        Tries progressively looser queries and stops at the first candidate that
        :func:`is_confident_match` accepts, so we neither lose real songs to an
        over-strict query nor grab the wrong track from a loose one. Escalates
        only when needed, to keep API calls (and rate-limit exposure) down.
        """
        norm_title, norm_artist = song_key(title, artist)
        queries = [
            "track:{} artist:{}".format(norm_title, norm_artist) if norm_artist else None,
            "{} {}".format(title, artist).strip(),
            norm_title or title,
        ]

        saw_any_results = False
        for query in queries:
            if not query:
                continue
            try:
                result = self.sp.search(q=query, type="track", limit=10)
            except Exception:
                self.emit("error", "Error searching Spotify for {} - {}".format(title, artist))
                return None, "search_error"
            items = result["tracks"]["items"]
            if items:
                saw_any_results = True
            for item in items:
                artists = item.get("artists") or [{}]
                if is_confident_match(item.get("name"), artists[0].get("name"), title, artist):
                    return item.get("id"), None

        return None, ("no_confident_match" if saw_any_results else "not_found")

    # -- match evaluation (with visible QA output) ------------------------- #
    def evaluate_match(self, title: str, artist: str, lyrics: Optional[str]) -> bool:
        """Score a song against the search terms and print the result; return whether it matches.

        Restores the per-song visibility the original script had, so you can
        watch the threshold decisions during a run. The scoring itself lives in
        the pure ``match_percentage`` / ``is_match`` helpers.
        """
        pct = match_percentage(lyrics, self.searches)
        matched = is_match(lyrics, self.searches, self.threshold)
        if not normalize_text(lyrics):
            verdict = "no lyrics"
        elif matched:
            verdict = "MATCH"
        else:
            verdict = "below"
        self.emit(
            "evaluate",
            "  {:6.2f}% / {:.2f}% threshold  [{}]  {} - {}".format(
                pct, self.threshold, verdict, title, artist),
            title=title, artist=artist, match_pct=pct,
            threshold=self.threshold, verdict=verdict,
        )
        return matched

    # -- search engines ---------------------------------------------------- #
    def from_spotify(self, main_search: str, start_offset: int = 0) -> None:
        """Use Spotify as the discovery engine, matching on Genius lyrics."""
        self.emit("search", "Searching Spotify for '{}'".format(main_search), engine="spotify")
        offset = start_offset
        limit = 50
        # Spotify caps search paging at offset+limit <= 1000.
        while offset < 1000:
            if self.should_stop():
                break
            response = self.sp.search(
                q="track:{}".format(main_search),
                type="track",
                limit=limit,
                offset=offset,
            )
            items = response["tracks"]["items"]
            if not items:
                break
            for song in items:
                if self.should_stop():
                    break
                title = song["name"]
                artist = song["artists"][0]["name"]
                lyrics = self.get_lyrics_from_genius(title, artist)
                if self.evaluate_match(title, artist, lyrics):
                    self.add_song(song["id"], title, artist, lyrics=lyrics)
            if self.should_stop():
                break
            offset += limit
            self.emit("progress", "Spotify offset now {}".format(offset), offset=offset)
        self.emit("progress", "Playlist size so far: {}".format(len(self.track_ids)),
                  playlist_size=len(self.track_ids))

    def from_genius(self, main_search: str, start_page: int = 1) -> None:
        """Use Genius as the discovery engine, matching on its lyrics."""
        self.emit("search", "Searching Genius for '{}'".format(main_search), engine="genius")
        page = start_page
        while True:
            if self.should_stop():
                break
            try:
                response = self.genius.search_songs(main_search, per_page=5, page=page)
            except Exception:
                self.emit("error", "Error searching Genius on page {}".format(page))
                break
            hits = response.get("hits", [])
            if not hits:
                break
            for hit in hits:
                if self.should_stop():
                    break
                result = hit["result"]
                title = result["title"]
                artist = result["primary_artist"].get("name")
                lyrics = clean_genius_lyrics(self.genius.lyrics(song_url=result["url"]))
                if not self.evaluate_match(title, artist, lyrics):
                    continue
                track_id, reason = self.resolve_spotify_track(title, artist)
                if track_id is None:
                    self.record_miss(title, artist, reason)
                    continue
                self.add_song(track_id, title, artist, lyrics=lyrics)
            if self.should_stop():
                break
            page += 1
            self.emit("progress", "Genius page now {}".format(page), page=page)
        self.emit("progress", "Playlist size so far: {}".format(len(self.track_ids)),
                  playlist_size=len(self.track_ids))

    # -- miss tracking ----------------------------------------------------- #
    def record_miss(self, title: str, artist: str, reason: Optional[str]) -> None:
        """Record a lyric-matched song we could not place on Spotify."""
        self.misses.append({"title": title, "artist": artist, "reason": reason})
        self.emit("miss", "Could not place on Spotify ({}): {} - {}".format(reason, title, artist),
                  title=title, artist=artist, reason=reason)

    def report_misses(self) -> None:
        """Report songs that matched the lyrics but never reached the playlist."""
        if not self.misses:
            self.emit("summary", "\nNo missed songs -- every lyric match was placed on Spotify.",
                      miss_count=0)
            return
        self.emit("summary",
                  "\n{} lyric match(es) could not be added to the playlist:".format(len(self.misses)),
                  miss_count=len(self.misses))
        for miss in self.misses:
            self.emit("summary", "  [{}] {} - {}".format(miss["reason"], miss["title"], miss["artist"]),
                      title=miss["title"], artist=miss["artist"], reason=miss["reason"])


# --------------------------------------------------------------------------- #
# Spotify / playlist plumbing (network side, not unit tested)
# --------------------------------------------------------------------------- #

def build_spotify_client():
    """Authenticate against Spotify. Imported lazily so the module stays import-safe."""
    import spotipy
    from spotipy.oauth2 import SpotifyOAuth

    print("Authenticating with Spotify")
    scope = "playlist-modify-public"
    return spotipy.Spotify(
        auth_manager=SpotifyOAuth(show_dialog=True, scope=scope, cache_path="cache.txt")
    )


def build_genius_client():
    import lyricsgenius

    token = os.environ.get("GENIUS_TOKEN")
    if not token:
        raise SystemExit("GENIUS_TOKEN environment variable is not set.")
    return lyricsgenius.Genius(token)


def _seed_from_playlist_tracks(sp, builder: "PlaylistBuilder", playlist_id: str) -> None:
    """Record the tracks already in a playlist so we don't re-add them."""
    tracks = sp.playlist_tracks(playlist_id)
    for item in tracks["items"]:
        track = item.get("track") or {}
        artists = track.get("artists") or [{}]
        builder.register(track.get("id"), track.get("name", ""), artists[0].get("name", ""))


def resolve_playlist(sp, builder: "PlaylistBuilder", args) -> str:
    """Find or create the target playlist and seed its existing tracks."""
    if args.playlistId:
        try:
            sp.playlist_tracks(args.playlistId)
            print("Using existing playlist {}".format(args.playlistId))
            _seed_from_playlist_tracks(sp, builder, args.playlistId)
            return args.playlistId
        except Exception:
            print("Could not find playlist {}; creating one instead".format(args.playlistId))

    title = args.title or args.query
    # Reuse an existing public playlist of the same name if there is one.
    for playlist in sp.current_user_playlists()["items"]:
        if playlist["public"] and playlist["name"] == title:
            print("Public playlist '{}' already exists -- adding to it".format(title))
            _seed_from_playlist_tracks(sp, builder, playlist["id"])
            return playlist["id"]

    print("Creating playlist '{}' for search term '{}'".format(title, args.query))
    response = sp.user_playlist_create(
        sp.me()["id"],
        title,
        public=True,
        description="Smart playlist supplied by beast infection",
    )
    return response["id"]


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--query", "-q", required=True, help="Search term for your playlist")
    parser.add_argument("--matches", "-m", nargs="*", help="Additional terms to match on")
    parser.add_argument("--title", "-t", help="Title of your playlist")
    parser.add_argument("--playlistId", "-p", help="Existing Spotify playlist ID to add to")
    parser.add_argument(
        "--bangerThreshold", "-bt", default=3.0, type=float,
        help="Min percent of lyric text the term must occupy to count as a match",
    )
    parser.add_argument("--spotify", "-sp", default="True", help="Search using Spotify")
    parser.add_argument("--genius", "-g", default="True", help="Search using Genius")
    parser.add_argument(
        "--spotifyPage", "-spp", default=0, type=int,
        help="Starting Spotify search offset (for resuming an interrupted run)",
    )
    parser.add_argument(
        "--geniusPage", "-gp", default=1, type=int,
        help="Starting Genius search page (for resuming an interrupted run)",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> None:
    args = build_parser().parse_args(argv)

    searches = args.matches if args.matches else [args.query]
    print("Using searches: {}".format(searches))

    sp = build_spotify_client()
    genius = build_genius_client()

    builder = PlaylistBuilder(
        sp=sp,
        genius=genius,
        searches=searches,
        threshold=args.bangerThreshold,
    )

    builder.playlist_id = resolve_playlist(sp, builder, args)

    if args.spotify == "True":
        builder.from_spotify(args.query, start_offset=args.spotifyPage)
    if args.genius == "True":
        builder.from_genius(args.query, start_page=args.geniusPage)

    builder.report_misses()
    print("Done. Songs in playlist: {}".format(len(builder.track_ids)))


if __name__ == "__main__":
    main()
