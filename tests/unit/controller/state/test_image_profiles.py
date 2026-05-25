"""Tests for state.image_profiles — spec I §5."""
from __future__ import annotations

import pytest

from mthydra.controller.state.db import connect
from mthydra.controller.state.image_profiles import (
    get_profile,
    list_pinned,
    pin,
)
from mthydra.controller.state.schema import apply_schema


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "state.sqlite")
    apply_schema(c)
    c.execute(
        "INSERT INTO ru_images (image_version, upstream_release, upstream_repo, "
        "binary_url, manifest_url, binary_sha256, binary_size_bytes, state, built_at) "
        "VALUES ('v1', 'r', 'r', 'u', 'm', 'sha', 1, 'candidate', ?)",
        ("2026-05-25T00:00:00Z",),
    )
    c.execute(
        "INSERT INTO ru_images (image_version, upstream_release, upstream_repo, "
        "binary_url, manifest_url, binary_sha256, binary_size_bytes, state, built_at) "
        "VALUES ('v2', 'r', 'r', 'u', 'm', 'sha2', 1, 'candidate', ?)",
        ("2026-05-25T00:00:00Z",),
    )
    c.commit()
    yield c
    c.close()


def test_pin_inserts_row_and_audits(conn):
    pin(conn, image_version="v1", profile_json='{"k":1}',
        recorded_by="op", at="2026-05-25T00:01:00Z",
        notes="initial pin")
    p = get_profile(conn, "v1")
    assert p is not None
    assert p.profile_json == '{"k":1}'
    assert p.recorded_by == "op"
    assert p.notes == "initial pin"
    audits = conn.execute(
        "SELECT action, target FROM audit_log WHERE action='image_profile_pin'"
    ).fetchall()
    assert audits == [("image_profile_pin", "v1")]


def test_pin_overwrites(conn):
    pin(conn, image_version="v1", profile_json='{"k":1}', recorded_by="op",
        at="2026-05-25T00:01:00Z")
    pin(conn, image_version="v1", profile_json='{"k":2}', recorded_by="op2",
        at="2026-05-25T00:02:00Z", notes="updated")
    p = get_profile(conn, "v1")
    assert p.profile_json == '{"k":2}'
    assert p.recorded_by == "op2"
    assert p.notes == "updated"


def test_pin_refuses_unknown_image(conn):
    with pytest.raises(LookupError):
        pin(conn, image_version="nope", profile_json='{}', recorded_by="op",
            at="2026-05-25T00:00:00Z")


def test_pin_refuses_empty_profile(conn):
    with pytest.raises(ValueError):
        pin(conn, image_version="v1", profile_json="", recorded_by="op",
            at="2026-05-25T00:00:00Z")


def test_get_profile_missing_returns_none(conn):
    assert get_profile(conn, "v1") is None
    assert get_profile(conn, "nope") is None


def test_list_pinned(conn):
    pin(conn, image_version="v1", profile_json='{"k":1}', recorded_by="op",
        at="2026-05-25T00:01:00Z")
    pin(conn, image_version="v2", profile_json='{"k":2}', recorded_by="op",
        at="2026-05-25T00:02:00Z")
    rows = list_pinned(conn)
    assert [p.image_version for p in rows] == ["v1", "v2"]
