"""Re-key the lyric cache after a change to how song keys are normalized.

Song keys now strip Unicode punctuation (curly apostrophes, en dashes, CJK
marks), so a song cached as "when we’re together" is now looked up as "when
were together". Without re-keying, those cached lyrics become unreachable and
the app would try to fetch them again.

    python3 migrate_song_keys.py            # report only; changes nothing
    python3 migrate_song_keys.py --apply    # back up affected rows, then re-key

When two rows end up with the same key (the same song cached under both
spellings), the one with lyrics is kept, then the most recently fetched.
``--apply`` first writes every affected row to ``song_keys_backup.json``
(gitignored; it holds lyrics) and makes all changes in one transaction.
"""

from __future__ import annotations

import argparse
import json
import os
from typing import Callable, Dict, List, Tuple

from generate_playlist import song_key

BACKUP_PATH = "song_keys_backup.json"
TEMP_PREFIX = "\x1e"   # Postgres text can't hold NUL; no real key contains this


def new_key(title, artist) -> str:
    return "\x1f".join(song_key(title, artist))


def plan(rows: List[dict], key_fn: Callable = new_key) -> Tuple[Dict[str, str], List[str]]:
    """Work out the re-keying.

    ``rows`` are dicts with ``song_key``, ``title``, ``artist``, ``has_lyrics``
    and ``fetched_at``. Returns ``(renames, deletes)``: ``renames`` maps old key
    -> new key for rows that move; ``deletes`` lists keys of rows dropped
    because a better row holds the same song.
    """
    groups: Dict[str, List[dict]] = {}
    for row in rows:
        if "\x1f" not in row["song_key"]:
            continue          # not a song (e.g. a connection-test row)
        groups.setdefault(key_fn(row["title"], row["artist"]), []).append(row)

    renames: Dict[str, str] = {}
    deletes: List[str] = []
    for target, members in groups.items():
        if len(members) == 1 and members[0]["song_key"] == target:
            continue
        keep = max(members, key=lambda r: (bool(r["has_lyrics"]), str(r.get("fetched_at") or "")))
        for row in members:
            if row is not keep:
                deletes.append(row["song_key"])
        if keep["song_key"] != target:
            renames[keep["song_key"]] = target
    return renames, deletes


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--apply", action="store_true", help="back up affected rows, then re-key")
    args = parser.parse_args()

    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row) as conn:
        rows = conn.execute("SELECT song_key, title, artist, has_lyrics, fetched_at FROM songs").fetchall()
        renames, deletes = plan(rows)
        lost_lyrics = [k for k in deletes if next(r for r in rows if r["song_key"] == k)["has_lyrics"]]
        print("rows: %d | re-keyed: %d | merged away as duplicates: %d (%d of them had lyrics)"
              % (len(rows), len(renames), len(deletes), len(lost_lyrics)))
        for old, new in list(renames.items())[:10]:
            print("   %r -> %r" % (old, new))
        if not args.apply:
            print("Report only. Run with --apply to make these changes.")
            return

        affected = list(renames) + deletes
        full = conn.execute("SELECT * FROM songs WHERE song_key = ANY(%s)", (affected,)).fetchall()
        with open(BACKUP_PATH, "w") as f:
            json.dump(full, f, default=str, ensure_ascii=False, indent=1)
        print("Backed up %d rows to %s" % (len(full), BACKUP_PATH))

        with conn.transaction():
            if deletes:
                conn.execute("DELETE FROM songs WHERE song_key = ANY(%s)", (deletes,))
            # Two steps, so a row moving onto a key another row is leaving never collides.
            for old in renames:
                conn.execute("UPDATE songs SET song_key = %s WHERE song_key = %s", (TEMP_PREFIX + old, old))
            for old, new in renames.items():
                conn.execute("UPDATE songs SET song_key = %s WHERE song_key = %s", (new, TEMP_PREFIX + old))
        print("Done.")


if __name__ == "__main__":
    main()
