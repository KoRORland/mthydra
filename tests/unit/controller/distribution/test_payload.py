"""Tests for distribution.payload — pure subset builder + hash."""
from __future__ import annotations

import base64
import json

import pytest

from mthydra.controller.distribution.payload import (
    SubsetBox,
    build_subset,
    hash_subset,
    payload_to_json,
)
from mthydra.controller.state.db import connect
from mthydra.controller.state.schema import apply_schema


def _seed_user_assigned(conn, user_id="u1", shard_id="s1", *, box_ids):
    """Seed an active shard with the listed (box_id, public_ip, sni, has_cred) tuples."""
    import json as _json
    conn.execute(
        "INSERT INTO shards (shard_id, members_json, target_size, last_reshuffled_at, created_at) "
        "VALUES (?, ?, 2, '2026-05-25T00:00:00Z', '2026-05-25T00:00:00Z')",
        (shard_id, _json.dumps([user_id])),
    )
    conn.execute(
        "INSERT INTO users (user_id, display_name, out_of_band_channel, "
        "current_shard_id, added_at) "
        "VALUES (?, NULL, 'email', ?, '2026-05-25T00:00:00Z')",
        (user_id, shard_id),
    )
    # Need an authority + image for the FK constraints.
    conn.execute(
        "INSERT OR IGNORE INTO credential_authority (generation, privkey_pem, "
        "pubkey_pem, created_at) "
        "VALUES (1, 'priv', 'pub', '2026-05-25T00:00:00Z')"
    )
    conn.execute(
        "INSERT OR IGNORE INTO ru_images (image_version, upstream_release, upstream_repo, "
        "binary_url, manifest_url, binary_sha256, binary_size_bytes, state, built_at) "
        "VALUES ('v1', 'r', 'r', 'u', 'm', 'sha', 1, 'candidate', '2026-05-25T00:00:00Z')"
    )
    for box_id, public_ip, sni, has_cred in box_ids:
        conn.execute(
            "INSERT INTO ru_boxes (box_id, provider, region, public_ip, sni, "
            "shard_id, state, image_version, created_at) "
            "VALUES (?, 'p', 'r', ?, ?, ?, 'live', 'v1', '2026-05-25T00:00:00Z')",
            (box_id, public_ip, sni, shard_id),
        )
        if has_cred:
            conn.execute(
                "INSERT INTO onward_credentials (cred_id, box_id, credential, "
                "issued_at, authority_generation) "
                "VALUES (?, ?, ?, '2026-05-25T00:00:01Z', 1)",
                (f"c-{box_id}", box_id, b"\x00\x11\x22"),
            )
    conn.commit()


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "state.sqlite")
    apply_schema(c)
    yield c
    c.close()


def test_build_subset_returns_none_for_unassigned(conn):
    conn.execute(
        "INSERT INTO users (user_id, display_name, out_of_band_channel, added_at) "
        "VALUES ('u1', NULL, 'email', '2026-05-25T00:00:00Z')"
    )
    conn.commit()
    payload = build_subset(conn, "u1", now="2026-05-25T01:00:00Z")
    assert payload is None


def test_build_subset_returns_none_for_unknown_user(conn):
    payload = build_subset(conn, "nope", now="2026-05-25T01:00:00Z")
    assert payload is None


def test_build_subset_assigned_user_with_two_live_boxes(conn):
    _seed_user_assigned(conn, box_ids=[
        ("b1", "10.0.0.1", "sni-b1", True),
        ("b2", "10.0.0.2", "sni-b2", True),
    ])
    payload = build_subset(conn, "u1", now="2026-05-25T01:00:00Z")
    assert payload is not None
    assert payload.user_id == "u1"
    assert payload.shard_id == "s1"
    assert len(payload.boxes) == 2
    box_ids = {b.box_id for b in payload.boxes}
    assert box_ids == {"b1", "b2"}
    # credential_b64 is base64 of the credential blob.
    for b in payload.boxes:
        assert base64.b64decode(b.credential_b64) == b"\x00\x11\x22"
        assert b.port == 443


def test_build_subset_skips_box_without_credential(conn):
    _seed_user_assigned(conn, box_ids=[
        ("b1", "10.0.0.1", "sni-b1", True),
        ("b2", "10.0.0.2", "sni-b2", False),
    ])
    payload = build_subset(conn, "u1", now="2026-05-25T01:00:00Z")
    assert {b.box_id for b in payload.boxes} == {"b1"}


def test_build_subset_skips_box_with_revoked_credential(conn):
    _seed_user_assigned(conn, box_ids=[
        ("b1", "10.0.0.1", "sni-b1", True),
    ])
    conn.execute(
        "UPDATE onward_credentials SET revoked_at='2026-05-25T01:00:00Z' "
        "WHERE box_id='b1'"
    )
    conn.commit()
    payload = build_subset(conn, "u1", now="2026-05-25T02:00:00Z")
    assert payload.boxes == ()


def test_hash_subset_is_deterministic_and_order_independent():
    boxes = [
        SubsetBox(box_id="b1", public_ip="1.1.1.1", port=443,
                  sni="s1", credential_b64="aa"),
        SubsetBox(box_id="b2", public_ip="2.2.2.2", port=443,
                  sni="s2", credential_b64="bb"),
    ]
    h1 = hash_subset(boxes)
    h2 = hash_subset(list(reversed(boxes)))
    assert h1 == h2


def test_hash_subset_changes_on_member_change():
    base = [SubsetBox("b1", "1.1.1.1", 443, "s1", "aa")]
    h1 = hash_subset(base)
    h2 = hash_subset(base + [SubsetBox("b2", "2.2.2.2", 443, "s2", "bb")])
    assert h1 != h2


def test_payload_to_json_round_trip(conn):
    _seed_user_assigned(conn, box_ids=[("b1", "10.0.0.1", "sni-b1", True)])
    payload = build_subset(conn, "u1", now="2026-05-25T01:00:00Z")
    body = payload_to_json(payload)
    obj = json.loads(body)
    assert obj["user_id"] == "u1"
    assert obj["shard_id"] == "s1"
    assert len(obj["boxes"]) == 1
    assert obj["boxes"][0]["box_id"] == "b1"
    # Hash matches the boxes embedded.
    assert obj["subset_hash"] == payload.subset_hash


def test_build_subset_empty_boxes_when_all_terminated(conn):
    _seed_user_assigned(conn, box_ids=[
        ("b1", "10.0.0.1", "sni-b1", True),
    ])
    conn.execute(
        "UPDATE ru_boxes SET state='terminated', "
        "terminated_at='2026-05-25T01:00:00Z' WHERE box_id='b1'"
    )
    conn.commit()
    payload = build_subset(conn, "u1", now="2026-05-25T02:00:00Z")
    assert payload.boxes == ()
    # Empty subset still has a deterministic hash.
    assert payload.subset_hash == hash_subset([])
