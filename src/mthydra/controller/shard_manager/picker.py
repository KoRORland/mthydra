"""Pure shard membership picker — spec H §7.2.

No I/O, no DB access. Given a pool of users (current shard members +
unassigned) and a target shard size, produce a shuffled chunk layout.
Tests inject a seeded `random.Random` for determinism; production calls
fall through to `random.SystemRandom()`.
"""
from __future__ import annotations

import random


def pick_new_rosters(
    *,
    current_members: list[str],
    unassigned: list[str],
    target_size: int,
    rng: random.Random | None = None,
) -> list[list[str]]:
    """Shuffle the union and chunk into groups of `target_size`.

    The last chunk may be smaller. Empty input -> empty output (no group with
    zero members is returned). target_size must be >= 1.
    """
    if target_size < 1:
        raise ValueError(f"target_size must be >= 1 (got {target_size})")
    pool = list(current_members) + list(unassigned)
    if not pool:
        return []
    rng = rng or random.SystemRandom()
    rng.shuffle(pool)
    return [pool[i : i + target_size] for i in range(0, len(pool), target_size)]
