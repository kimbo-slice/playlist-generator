"""Order tracks so each one flows into the next by tempo and key.

The rule is plain arithmetic, so the same list always gives the same order:

1. Score every possible transition: how far apart the tempos are (double and
   half time count as the same tempo, at a small fixed cost for the switch)
   plus how far apart the keys sit on the circle of fifths. Lower is smoother.
2. Start from the slowest track and keep adding whichever remaining track makes
   the smoothest next step.
3. Clean up: wherever reversing a stretch of the list lowers the total score,
   reverse it, so the end of the list isn't a run of leftover bad jumps.

Tracks with neither tempo nor key are returned separately, in their original
order, for the caller to place by hand.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

# One key step on the circle of fifths costs the same as this much tempo change.
TEMPO_PCT_PER_KEY_STEP = 3.0
# Neutral stand-ins when a track is missing one of the two measurements:
# worse than a good transition, better than a bad one.
UNKNOWN_TEMPO_GAP = 8.0   # percent
UNKNOWN_KEY_GAP = 2.0     # key steps
# Extra cost (in key steps) for moving between a track and one at roughly double
# or half its tempo. The tempos still count as matching, but the switch is felt,
# so it should be used sparingly rather than for free.
HALF_DOUBLE_SWITCH_COST = 2.0   # the same as a 6% tempo change
MAX_CLEANUP_PASSES = 50
# Tempo services often report the same pulse at half or double speed (74 on one,
# 149 on another). Folding every tempo into one range makes them comparable.
FOLD_LOW, FOLD_HIGH = 70.0, 140.0


@dataclass(frozen=True)
class Track:
    label: str                    # "Title - Artist"
    bpm: Optional[float] = None
    key: Optional[int] = None     # pitch class, 0 = C ... 11 = B
    mode: Optional[int] = None    # 1 major, 0 minor

    @property
    def measured(self) -> bool:
        return bool(self.bpm) or self.key is not None


def fold_bpm(bpm: Optional[float]) -> Optional[float]:
    """Double or halve a tempo until it lands in ``[FOLD_LOW, FOLD_HIGH)``."""
    if not bpm or bpm <= 0:
        return None
    while bpm < FOLD_LOW:
        bpm *= 2.0
    while bpm >= FOLD_HIGH:
        bpm /= 2.0
    return bpm


def tempo_gap(a: Optional[float], b: Optional[float]) -> Optional[float]:
    """Tempo difference in percent, treating double/half time as the same tempo."""
    if not a or not b:
        return None
    octaves = abs(math.log2(a / b)) % 1.0
    octaves = min(octaves, 1.0 - octaves)
    return (2.0 ** octaves - 1.0) * 100.0


def is_half_double_switch(a: Optional[float], b: Optional[float]) -> bool:
    """True when the closer match between two tempos is via doubling or halving."""
    if not a or not b:
        return False
    return round(abs(math.log2(a / b))) >= 1


def _fifths_position(key: int, mode: Optional[int]) -> int:
    """Position on the circle of fifths; a minor key sits with its relative major."""
    major_tonic = (key + 3) % 12 if mode == 0 else key
    return (major_tonic * 7) % 12


def key_gap(a: Track, b: Track) -> Optional[int]:
    """Steps apart on the circle of fifths (0 same key, 1 neighbor or relative major/minor)."""
    if a.key is None or b.key is None:
        return None
    apart = abs(_fifths_position(a.key, a.mode) - _fifths_position(b.key, b.mode))
    steps = min(apart, 12 - apart)
    if a.mode is not None and b.mode is not None and a.mode != b.mode:
        steps += 1
    return steps


def transition_cost(a: Track, b: Track) -> float:
    """How jarring it is to play ``b`` after ``a`` (symmetric; lower is smoother)."""
    tempo = tempo_gap(a.bpm, b.bpm)
    key = key_gap(a, b)
    return ((UNKNOWN_TEMPO_GAP if tempo is None else tempo) / TEMPO_PCT_PER_KEY_STEP
            + (UNKNOWN_KEY_GAP if key is None else key)
            + (HALF_DOUBLE_SWITCH_COST if is_half_double_switch(a.bpm, b.bpm) else 0.0))


def total_cost(tracks: list[Track]) -> float:
    return sum(transition_cost(a, b) for a, b in zip(tracks, tracks[1:]))


def order_tracks(tracks: list[Track]) -> tuple[list[Track], list[Track]]:
    """Return ``(ordered, unplaced)``.

    ``ordered`` holds every track with a tempo or key, sequenced for smooth
    transitions starting from the slowest. ``unplaced`` holds the rest in their
    original order.
    """
    pool = [t for t in tracks if t.measured]
    unplaced = [t for t in tracks if not t.measured]
    n = len(pool)
    if n < 3:
        return sorted(pool, key=lambda t: t.bpm or math.inf), unplaced

    cost = [[transition_cost(a, b) for b in pool] for a in pool]

    # Start from the slowest track (earliest in the list on a tie).
    start = min(range(n), key=lambda i: (pool[i].bpm or math.inf, i))
    path, remaining = [start], [i for i in range(n) if i != start]
    while remaining:
        nearest = min(remaining, key=lambda i: (cost[path[-1]][i], i))
        path.append(nearest)
        remaining.remove(nearest)

    # Clean-up: reverse any stretch (keeping the opener) that lowers the total.
    for _ in range(MAX_CLEANUP_PASSES):
        improved = False
        for i in range(1, n - 1):
            for j in range(i + 1, n):
                before = cost[path[i - 1]][path[i]]
                after = cost[path[i - 1]][path[j]]
                if j + 1 < n:
                    before += cost[path[j]][path[j + 1]]
                    after += cost[path[i]][path[j + 1]]
                if after < before - 1e-9:
                    path[i:j + 1] = reversed(path[i:j + 1])
                    improved = True
        if not improved:
            break

    return [pool[i] for i in path], unplaced
