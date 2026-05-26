"""Tests for state.probe_credentials — spec I2."""
from __future__ import annotations

import sqlite3

import pytest

from mthydra.controller.state.db import connect
from mthydra.controller.state.probe_credentials import (
    issue, list_active_for_box, list_active_for_vantage, list_all, revoke,
)
from mthydra.controller.state.schema import apply_schema


NOW = "2026-05-26T12:00:00Z"


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "state.sqlite")
    apply_schema(c)
    c.execute(
        "INSERT INTO credential_authority (generation, privkey_pem, pubkey_pem, created_at) "
        "VALUES (1, 'p', 'pk', ?)", (NOW,),
    )
    c.execute(
        "INSERT INTO ru_images (image_version, upstream_release, upstream_repo, "
        "binary_url, manifest_url, binary_sha256, binary_size_bytes, state, built_at) "
        "VALUES ('v1', 'r', 'r', 'u', 'm', 'sha', 1, 'candidate', ?)", (NOW,),
    )
    c.execute(
        "INSERT INTO ru_boxes (box_id, provider, region, sni, state, image_version, created_at) "
        "VALUES ('b1', 'p', 'r', 'sni-b1', 'live', 'v1', ?)", (NOW,),
    )
    c.execute(
        "INSERT INTO ru_boxes (box_id, provider, region, sni, state, image_version, created_at) "
        "VALUES ('b2', 'p', 'r', 'sni-b2', 'live', 'v1', ?)", (NOW,),
    )
    c.execute(
        "INSERT INTO probe_vantages (vantage_id, label, source_kind, state, added_at) "
        "VALUES ('vk', 'kz', 'cloud-cis', 'active', ?)", (NOW,),
    )
    c.execute(
        "INSERT INTO probe_vantages (vantage_id, label, source_kind, state, added_at) "
        "VALUES ('vb', 'by', 'cloud-cis', 'active', ?)", (NOW,),
    )
    c.commit()
    yield c
    c.close()


def test_issue_appends_and_audits(conn):
    issue(conn, cred_id="c1", box_id="b1", vantage_id="vk",
          authority_generation=1, credential=b"\x00\x11",
          issued_at=NOW, evidence="initial probe cred")
    row = conn.execute(
        "SELECT cred_id, revoked_at FROM probe_credentials WHERE cred_id='c1'"
    ).fetchone()
    assert row == ("c1", None)
    audits = conn.execute(
        "SELECT action, target FROM audit_log WHERE action='probe_credential_issue'"
    ).fetchall()
    assert audits == [("probe_credential_issue", "c1")]


def test_issue_double_active_refuses(conn):
    issue(conn, cred_id="c1", box_id="b1", vantage_id="vk",
          authority_generation=1, credential=b"\x00", issued_at=NOW)
    with pytest.raises(sqlite3.IntegrityError):
        issue(conn, cred_id="c2", box_id="b1", vantage_id="vk",
              authority_generation=1, credential=b"\x01", issued_at=NOW)


def test_issue_different_vantage_ok(conn):
    issue(conn, cred_id="c1", box_id="b1", vantage_id="vk",
          authority_generation=1, credential=b"\x00", issued_at=NOW)
    issue(conn, cred_id="c2", box_id="b1", vantage_id="vb",
          authority_generation=1, credential=b"\x01", issued_at=NOW)
    n = conn.execute(
        "SELECT COUNT(*) FROM probe_credentials WHERE box_id='b1'"
    ).fetchone()[0]
    assert n == 2


def test_revoke(conn):
    issue(conn, cred_id="c1", box_id="b1", vantage_id="vk",
          authority_generation=1, credential=b"\x00", issued_at=NOW)
    revoke(conn, "c1", at="2026-05-26T13:00:00Z", reason="rotation")
    row = conn.execute(
        "SELECT revoked_at FROM probe_credentials WHERE cred_id='c1'"
    ).fetchone()
    assert row[0] == "2026-05-26T13:00:00Z"


def test_revoke_missing_raises(conn):
    with pytest.raises(LookupError):
        revoke(conn, "nope", at=NOW, reason="x")


def test_revoke_already_raises(conn):
    issue(conn, cred_id="c1", box_id="b1", vantage_id="vk",
          authority_generation=1, credential=b"\x00", issued_at=NOW)
    revoke(conn, "c1", at="2026-05-26T13:00:00Z", reason="r1")
    with pytest.raises(ValueError, match="already revoked"):
        revoke(conn, "c1", at="2026-05-26T14:00:00Z", reason="r2")


def test_reissue_after_revoke(conn):
    issue(conn, cred_id="c1", box_id="b1", vantage_id="vk",
          authority_generation=1, credential=b"\x00", issued_at=NOW)
    revoke(conn, "c1", at="2026-05-26T13:00:00Z", reason="rotation")
    issue(conn, cred_id="c2", box_id="b1", vantage_id="vk",
          authority_generation=1, credential=b"\x99",
          issued_at="2026-05-26T13:01:00Z")
    active = list_active_for_box(conn, "b1")
    assert [c.cred_id for c in active] == ["c2"]


def test_list_active_for_box(conn):
    issue(conn, cred_id="c1", box_id="b1", vantage_id="vk",
          authority_generation=1, credential=b"\x00", issued_at=NOW)
    issue(conn, cred_id="c2", box_id="b1", vantage_id="vb",
          authority_generation=1, credential=b"\x01", issued_at=NOW)
    issue(conn, cred_id="c3", box_id="b2", vantage_id="vk",
          authority_generation=1, credential=b"\x02", issued_at=NOW)
    rows = list_active_for_box(conn, "b1")
    assert sorted(c.cred_id for c in rows) == ["c1", "c2"]


def test_list_active_for_vantage(conn):
    issue(conn, cred_id="c1", box_id="b1", vantage_id="vk",
          authority_generation=1, credential=b"\x00", issued_at=NOW)
    issue(conn, cred_id="c2", box_id="b2", vantage_id="vk",
          authority_generation=1, credential=b"\x01", issued_at=NOW)
    rows = list_active_for_vantage(conn, "vk")
    assert sorted(c.cred_id for c in rows) == ["c1", "c2"]


def test_list_all_include_revoked(conn):
    issue(conn, cred_id="c1", box_id="b1", vantage_id="vk",
          authority_generation=1, credential=b"\x00", issued_at=NOW)
    revoke(conn, "c1", at="2026-05-26T13:00:00Z", reason="rotation")
    assert list_all(conn) == []  # default excludes revoked
    rows = list_all(conn, include_revoked=True)
    assert [c.cred_id for c in rows] == ["c1"]
