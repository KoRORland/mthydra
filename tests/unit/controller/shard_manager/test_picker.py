"""Tests for shard_manager.picker — pure SystemRandom chunking."""
from __future__ import annotations

import random
from collections import Counter

import pytest

from mthydra.controller.shard_manager.picker import pick_new_rosters


def test_chunks_by_target_size():
    rosters = pick_new_rosters(
        current_members=["a", "b", "c", "d", "e"],
        unassigned=["f"],
        target_size=2,
        rng=random.Random(42),
    )
    flat = [u for r in rosters for u in r]
    assert sorted(flat) == ["a", "b", "c", "d", "e", "f"]
    assert all(1 <= len(r) <= 2 for r in rosters)
    # 6 users / target 2 = exactly 3 full chunks.
    assert len(rosters) == 3
    assert sum(len(r) == 2 for r in rosters) == 3


def test_last_chunk_may_be_smaller():
    rosters = pick_new_rosters(
        current_members=["a", "b", "c", "d", "e"],
        unassigned=[],
        target_size=2,
        rng=random.Random(0),
    )
    assert len(rosters) == 3  # 2 + 2 + 1
    assert sorted(len(r) for r in rosters) == [1, 2, 2]


def test_empty_pool_returns_empty_list():
    assert pick_new_rosters(
        current_members=[], unassigned=[], target_size=2,
        rng=random.Random(0),
    ) == []


def test_target_size_must_be_positive():
    with pytest.raises(ValueError, match="target_size"):
        pick_new_rosters(
            current_members=["a"], unassigned=[], target_size=0,
        )


def test_target_size_1_yields_singletons():
    rosters = pick_new_rosters(
        current_members=["a", "b", "c"], unassigned=[], target_size=1,
        rng=random.Random(0),
    )
    assert len(rosters) == 3
    assert all(len(r) == 1 for r in rosters)


def test_deterministic_given_seeded_rng():
    a = pick_new_rosters(
        current_members=["x", "y", "z", "w"], unassigned=[], target_size=2,
        rng=random.Random(1234),
    )
    b = pick_new_rosters(
        current_members=["x", "y", "z", "w"], unassigned=[], target_size=2,
        rng=random.Random(1234),
    )
    assert a == b


def test_anti_correlation_over_many_shuffles():
    """Spec H §7.2: pair co-occurrence must be bounded. With 6 users + size 2,
    each pair co-occurs in ~1/5 of trials over many runs. Allow a wide band."""
    users = ["u1", "u2", "u3", "u4", "u5", "u6"]
    pair_counts: Counter[frozenset[str]] = Counter()
    trials = 500
    for seed in range(trials):
        rosters = pick_new_rosters(
            current_members=users, unassigned=[], target_size=2,
            rng=random.Random(seed),
        )
        for r in rosters:
            if len(r) == 2:
                pair_counts[frozenset(r)] += 1
    # Each of 15 possible pairs should appear with frequency around trials/5.
    # Loose bounds: 80..220 to absorb seed variance.
    assert len(pair_counts) == 15
    for count in pair_counts.values():
        assert 80 <= count <= 220, f"pair count {count} outside [80, 220]"
