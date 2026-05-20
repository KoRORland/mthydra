"""Spec C — random-sequence invariants for the cover-pool state machine.

After every operation in any random sequence of (add, attest, downgrade,
assign, rotate), the structural invariants of T5 must hold:
  * cover_domain_pool ∩ burned_domains = ∅
  * burned_domains row count is monotonically non-decreasing
  * No domain transitions to in_use twice without an intervening burn
"""
from __future__ import annotations

import sqlite3

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from mthydra.controller.state.cover_pool import (
    add_candidate, assign_to_box, attest_verified,
    downgrade_stale_verified, rotate_and_burn,
)
from mthydra.controller.state.db import connect
from mthydra.controller.state.ru_boxes import insert_box, mark_live
from mthydra.controller.state.schema import apply_schema

_DOMAINS = ["a.org", "b.org", "c.org", "d.org"]


def _setup(tmp_db_path):
    conn = connect(tmp_db_path)
    apply_schema(conn)
    insert_box(conn, "box-1", "aws", "eu-west-1", "10.0.0.1", "sni.invalid",
               "img-v1", "2026-04-01T00:00:00Z")
    mark_live(conn, "box-1", public_ip="10.0.0.1", at="2026-04-01T00:00:00Z")
    return conn


def _invariants_hold(conn) -> bool:
    overlap = conn.execute(
        "SELECT COUNT(*) FROM cover_domain_pool WHERE domain IN "
        "(SELECT domain FROM burned_domains)"
    ).fetchone()[0]
    return overlap == 0


@settings(max_examples=80, deadline=None)
@given(
    ops=st.lists(
        st.tuples(
            st.sampled_from(["add", "attest", "downgrade", "assign", "rotate"]),
            st.sampled_from(_DOMAINS),
        ),
        min_size=1, max_size=40,
    ),
)
def test_random_operations_preserve_invariants(ops):
    conn = _setup(":memory:")
    prev_burned = 0
    in_use_history: dict[str, int] = {}

    for op, dom in ops:
        try:
            if op == "add":
                add_candidate(conn, dom, added_at="2026-05-19T00:00:00Z")
            elif op == "attest":
                attest_verified(conn, dom, from_vantage="ru-vps-01", at="2026-05-19T00:00:00Z")
            elif op == "downgrade":
                downgrade_stale_verified(
                    conn, now="2099-01-01T00:00:00Z", reverify_after_days=30,
                )
            elif op == "assign":
                assign_to_box(conn, dom, box_id="box-1", at="2026-05-19T00:00:00Z")
                in_use_history[dom] = in_use_history.get(dom, 0) + 1
            elif op == "rotate":
                rotate_and_burn(
                    conn, dom, reason="manual_rotate", last_box_id="box-1",
                    at="2026-05-19T00:00:00Z",
                )
        except (ValueError, sqlite3.IntegrityError):
            # Illegal transitions are expected from random sequences.
            pass

        # Invariant 1: no overlap
        assert _invariants_hold(conn), f"overlap after op={op} dom={dom}"

        # Invariant 2: burned count is non-decreasing
        burned = conn.execute("SELECT COUNT(*) FROM burned_domains").fetchone()[0]
        assert burned >= prev_burned
        prev_burned = burned

    # Invariant 3: no domain entered in_use more than (burns_for_that_domain + 1) times
    for d, n in in_use_history.items():
        burns = conn.execute(
            "SELECT COUNT(*) FROM burned_domains WHERE domain=?", (d,)
        ).fetchone()[0]
        assert n <= burns + 1, f"{d} entered in_use {n} times with only {burns} burns"
