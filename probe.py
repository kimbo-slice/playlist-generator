"""One-off sanity probe -- run this BEFORE the full generator.

It authenticates with Spotify and Genius, then for a handful of songs it:
  * confirms Spotify search works,
  * confirms Genius lyrics fetch works and computes the match percentage,
  * checks whether this Spotify app can read audio-features (BPM / key).

It creates NO playlist and adds NO tracks -- it only reads, and only a few
items, so it is cheap on the APIs. Run it in YOUR terminal (the first Spotify
call opens a browser for you to approve).

    python3 probe.py "hotdog"        # or any search term
"""

import os
import sys

import spotipy
from spotipy.oauth2 import SpotifyOAuth
import lyricsgenius

import generate_playlist as gp

SAMPLE_SIZE = 5


def main() -> None:
    query = sys.argv[1] if len(sys.argv) > 1 else "hotdog"
    print("Probe query: {!r}\n".format(query))

    # --- auth -------------------------------------------------------------
    print("Authenticating with Spotify (a browser may open the first time)...")
    sp = spotipy.Spotify(
        auth_manager=SpotifyOAuth(
            scope="playlist-modify-public",
            cache_path="cache.txt",
            show_dialog=True,
        )
    )
    me = sp.me()
    print("  OK -- logged in as: {}\n".format(me.get("display_name") or me.get("id")))

    token = os.environ.get("GENIUS_TOKEN")
    if not token:
        print("GENIUS_TOKEN not set -- aborting.")
        sys.exit(1)
    genius = lyricsgenius.Genius(token, verbose=False, remove_section_headers=True)
    print("Genius client ready.\n")

    # --- small spotify search --------------------------------------------
    results = sp.search(q="track:{}".format(query), type="track", limit=SAMPLE_SIZE)
    items = results["tracks"]["items"]
    print("Spotify returned {} track(s) for '{}'.\n".format(len(items), query))
    if not items:
        print("No tracks found; try another query.")
        return

    # --- audio-features availability check -------------------------------
    track_ids = [t["id"] for t in items]
    audio_features = None
    features_ok = False
    try:
        audio_features = sp.audio_features(track_ids)
        # Deprecated apps return a list of Nones rather than raising.
        features_ok = any(f is not None for f in (audio_features or []))
    except spotipy.SpotifyException as exc:
        print("audio-features request FAILED: HTTP {} ({})".format(exc.http_status, exc.msg))
    except Exception as exc:  # noqa: BLE001 -- surface anything unexpected
        print("audio-features request errored: {}".format(exc))

    if features_ok:
        print(">>> BPM/key ARE available for this app. Vibe ordering can use Spotify.\n")
    else:
        print(">>> BPM/key are NOT available (deprecated for this app). "
              "Vibe ordering will need a fallback source.\n")

    # --- per-song report --------------------------------------------------
    print("-" * 60)
    for i, track in enumerate(items):
        title = track["name"]
        artist = track["artists"][0]["name"]
        print("{}. {} - {}".format(i + 1, title, artist))

        song = genius.search_song(title, artist=artist, get_full_info=False)
        lyrics = gp.clean_genius_lyrics(song.lyrics) if song else ""
        pct = gp.match_percentage(lyrics, [query])
        if lyrics:
            print("   lyrics: {} chars | '{}' match = {:.2f}%".format(len(lyrics), query, pct))
        else:
            print("   lyrics: (none found)")

        if features_ok and audio_features and audio_features[i]:
            feat = audio_features[i]
            print("   bpm={:.0f}  key={}  mode={}".format(
                feat["tempo"], feat["key"], feat["mode"]))
        print()

    print("-" * 60)
    print("Probe complete. No playlist was created or modified.")


if __name__ == "__main__":
    main()
