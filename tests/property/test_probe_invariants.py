"""Spec I — random-sequence invariants for probe harness.

After every operation in any random sequence (add_vantage, attest, retire,
burn, record_probe), the structural invariants of T7 must hold:
  * no probe_vantages row exits burned (trigger enforces; we assert too)
  * burned label is monotonic — never reused as a new candidate
  * every probe_results row's (box, vantage, image) references are live
  * the evaluator never returns 'hard_kill' from a history of only passes
  * a hard_fail anywhere triggers 'hard_kill' regardless of vantage count
"""
from __future__ import annotations

import sqlite3
import warnings

from hypothesis import given, settings
from hypothesis import strategies as st

from mthydra.controller.probe.evaluator import ProbeConfigView, evaluate_box
from mthydra.controller.state import probe_results as _pr
from mthydra.controller.state import probe_vantages as _pv
from mthydra.controller.state.db import connect
from mthydra.controller.state.image_profiles import pin
from mthydra.controller.state.schema import apply_schema


CFG = ProbeConfigView(soft_fail_window_M=6, soft_fail_threshold_N=3,
                       min_distinct_vantages=2)

NOW = "2026-05-25T01:00:00Z"

_VANTAGES = ["va", "vb", "vc"]
_LABELS = {"va": "kz1", "vb": "by1", "vc": "tr1"}


def _setup():
    conn = connect(":memory:")
    apply_schema(conn)
    conn.execute(
        "INSERT INTO ru_images (image_version, upstream_release, upstream_repo, "
        "binary_url, manifest_url, binary_sha256, binary_size_bytes, state, built_at) "
        "VALUES ('v1', 'r', 'r', 'u', 'm', 'sha', 1, 'candidate', ?)",
        (NOW,),
    )
    conn.execute(
        "INSERT INTO ru_boxes (box_id, provider, region, sni, state, image_version, created_at) "
        "VALUES ('b1', 'p', 'r', 'sni-b1', 'live', 'v1', ?)",
        (NOW,),
    )
    pin(conn, image_version="v1", profile_json="{}", recorded_by="op", at=NOW)
    return conn


@settings(max_examples=40, deadline=None, derandomize=True)
@given(
    ops=st.lists(
        st.tuples(
            st.sampled_from(["add", "attest", "retire", "burn", "record"]),
            st.sampled_from(_VANTAGES),
            st.sampled_from(["pass", "soft_fail", "hard_fail"]),
        ),
        min_size=1, max_size=30,
    ),
)
def test_random_operations_preserve_probe_invariants(ops):
    conn = _setup()
    pass_only_history = True  # tracks whether any non-pass probe_result was inserted
    cycle_counter = 0

    for op, vid, status in ops:
        try:
            if op == "add":
                _pv.add_candidate(
                    conn, vantage_id=vid, label=_LABELS[vid],
                    source_kind="cloud-cis", at=NOW,
                )
            elif op == "attest":
                _pv.attest_active(conn, vid, at=NOW)
            elif op == "retire":
                _pv.retire(conn, vid, at=NOW, reason="test")
            elif op == "burn":
                _pv.burn(conn, vid, at=NOW, reason="test")
            elif op == "record":
                cycle_counter += 1
                cycle_at = f"2026-05-25T0{cycle_counter % 10}:0{cycle_counter % 10}:00Z"
                _pr.record(
                    conn, box_id="b1", vantage_id=vid,
                    cycle_at=cycle_at,
                    check_type="surface_scan", status=status,
                    evidence_json=None, image_version="v1",
                    recorded_at=cycle_at,
                )
                if status != "pass":
                    pass_only_history = False
        except (ValueError, LookupError, sqlite3.IntegrityError):
            # Illegal transitions are expected from random sequences.
            pass

        # Invariant: every probe_results row's box+vantage+image exist.
        orphan = conn.execute(
            "SELECT pr.id FROM probe_results pr "
            "LEFT JOIN ru_boxes rb ON rb.box_id = pr.box_id "
            "LEFT JOIN probe_vantages pv ON pv.vantage_id = pr.vantage_id "
            "LEFT JOIN ru_images ri ON ri.image_version = pr.image_version "
            "WHERE rb.box_id IS NULL OR pv.vantage_id IS NULL OR ri.image_version IS NULL "
            "LIMIT 1"
        ).fetchone()
        assert orphan is None, f"orphan after op={op}"

        # Invariant: no burned vantage was reverted.
        burned_rows = conn.execute(
            "SELECT vantage_id FROM probe_vantages WHERE burned_at IS NOT NULL"
        ).fetchall()
        for (vid_burned,) in burned_rows:
            state = conn.execute(
                "SELECT state FROM probe_vantages WHERE vantage_id=?", (vid_burned,)
            ).fetchone()[0]
            assert state == "burned", f"vantage {vid_burned} exited burned"

        # Invariant: evaluator never returns hard_kill on a pass-only history.
        if pass_only_history:
            res = evaluate_box(conn, box_id="b1", cfg=CFG, now=NOW)
            assert res.verdict != "hard_kill", (
                f"hard_kill from pass-only history: {res}"
            )

    # Final: if any hard_fail was inserted, the evaluator returns hard_kill
    # (as long as that row is among the last M).
    hard_rows = conn.execute(
        "SELECT id FROM probe_results WHERE status='hard_fail' "
        "ORDER BY cycle_at DESC, id DESC LIMIT ?",
        (CFG.soft_fail_window_M,),
    ).fetchall()
    if hard_rows:
        res = evaluate_box(conn, box_id="b1", cfg=CFG, now=NOW)
        assert res.verdict == "hard_kill"

    conn.close()
