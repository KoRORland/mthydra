"""Tests for state.probe_vantages — spec I §5 lifecycle + audit."""
from __future__ import annotations

import json
import sqlite3

import pytest

from mthydra.controller.state.db import connect
from mthydra.controller.state.probe_vantages import (
    ProbeVantage,
    add_candidate,
    attest_active,
    burn,
    get_vantage,
    list_by_state,
    list_due_for_rotation,
    retire,
)
from mthydra.controller.state.schema import apply_schema


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "state.sqlite")
    apply_schema(c)
    yield c
    c.close()


def test_add_candidate_inserts_row_and_audit(conn):
    add_candidate(
        conn, vantage_id="v1", label="kz1", source_kind="cloud-cis",
        at="2026-05-25T00:00:00Z", region_hint="KZ-almaty", notes="seed",
    )
    v = get_vantage(conn, "v1")
    assert v.state == "candidate"
    assert v.label == "kz1"
    audits = conn.execute(
        "SELECT action, target FROM audit_log WHERE action='vantage_add'"
    ).fetchall()
    assert audits == [("vantage_add", "v1")]


def test_add_candidate_refuses_duplicate_label(conn):
    add_candidate(conn, vantage_id="v1", label="kz1", source_kind="x",
                  at="2026-05-25T00:00:00Z")
    with pytest.raises(sqlite3.IntegrityError):
        add_candidate(conn, vantage_id="v2", label="kz1", source_kind="x",
                      at="2026-05-25T00:00:00Z")


def test_attest_active_transitions_and_audits(conn):
    add_candidate(conn, vantage_id="v1", label="kz1", source_kind="x",
                  at="2026-05-25T00:00:00Z")
    attest_active(conn, "v1", at="2026-05-25T00:01:00Z", evidence="ssh log")
    v = get_vantage(conn, "v1")
    assert v.state == "active"
    assert v.attested_at == "2026-05-25T00:01:00Z"
    audits = conn.execute(
        "SELECT action, details_json FROM audit_log WHERE action='vantage_attest_active'"
    ).fetchall()
    assert len(audits) == 1
    assert json.loads(audits[0][1])["evidence"] == "ssh log"


def test_attest_active_refuses_non_candidate(conn):
    add_candidate(conn, vantage_id="v1", label="kz1", source_kind="x",
                  at="2026-05-25T00:00:00Z")
    attest_active(conn, "v1", at="2026-05-25T00:01:00Z")
    with pytest.raises(ValueError):
        attest_active(conn, "v1", at="2026-05-25T00:02:00Z")


def test_attest_active_missing_raises(conn):
    with pytest.raises(LookupError):
        attest_active(conn, "nope", at="2026-05-25T00:00:00Z")


def test_retire_transitions(conn):
    add_candidate(conn, vantage_id="v1", label="kz1", source_kind="x",
                  at="2026-05-25T00:00:00Z")
    attest_active(conn, "v1", at="2026-05-25T00:01:00Z")
    retire(conn, "v1", at="2026-05-25T01:00:00Z", reason="no longer needed")
    v = get_vantage(conn, "v1")
    assert v.state == "retired"
    assert v.retired_at == "2026-05-25T01:00:00Z"


def test_retire_refuses_burned(conn):
    add_candidate(conn, vantage_id="v1", label="kz1", source_kind="x",
                  at="2026-05-25T00:00:00Z")
    burn(conn, "v1", at="2026-05-25T00:01:00Z", reason="leaked")
    with pytest.raises(ValueError):
        retire(conn, "v1", at="2026-05-25T00:02:00Z")


def test_burn_from_active(conn):
    add_candidate(conn, vantage_id="v1", label="kz1", source_kind="x",
                  at="2026-05-25T00:00:00Z")
    attest_active(conn, "v1", at="2026-05-25T00:01:00Z")
    burn(conn, "v1", at="2026-05-25T02:00:00Z", reason="egress in blocklist")
    v = get_vantage(conn, "v1")
    assert v.state == "burned"
    assert v.burned_at == "2026-05-25T02:00:00Z"
    assert v.burn_reason == "egress in blocklist"


def test_burn_idempotent_refuse(conn):
    add_candidate(conn, vantage_id="v1", label="kz1", source_kind="x",
                  at="2026-05-25T00:00:00Z")
    burn(conn, "v1", at="2026-05-25T00:01:00Z", reason="r1")
    with pytest.raises(ValueError):
        burn(conn, "v1", at="2026-05-25T00:02:00Z", reason="r2")


def test_list_by_state(conn):
    add_candidate(conn, vantage_id="v1", label="kz1", source_kind="x",
                  at="2026-05-25T00:00:00Z")
    add_candidate(conn, vantage_id="v2", label="by1", source_kind="x",
                  at="2026-05-25T00:00:00Z")
    attest_active(conn, "v2", at="2026-05-25T00:01:00Z")
    active = list_by_state(conn, "active")
    assert [v.vantage_id for v in active] == ["v2"]
    everything = list_by_state(conn)
    assert sorted(v.vantage_id for v in everything) == ["v1", "v2"]


def test_get_vantage_missing(conn):
    with pytest.raises(LookupError):
        get_vantage(conn, "nope")


def test_list_due_for_rotation(conn):
    add_candidate(conn, vantage_id="v1", label="kz1", source_kind="x",
                  at="2026-05-01T00:00:00Z")
    attest_active(conn, "v1", at="2026-05-01T00:00:00Z")
    add_candidate(conn, vantage_id="v2", label="by1", source_kind="x",
                  at="2026-05-25T00:00:00Z")
    attest_active(conn, "v2", at="2026-05-25T00:01:00Z")
    overdue = list_due_for_rotation(
        conn, now="2026-05-25T00:02:00Z", ttl_days=14,
    )
    assert overdue == ["v1"]
