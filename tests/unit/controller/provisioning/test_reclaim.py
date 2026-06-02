"""Reclaim a never-live provisioning box without burning its cover domain.

A box stuck in state='provisioning' never went live (only mark_live flips it to
'live' and sets public_ip), so its SNI was never exposed. Unlike ru-box-terminate
— which BURNS the SNI (correct for a box that was actually live) — reclaiming a
never-live orphan must return its cover domain in_use -> candidate_verified so it
can be reused. This is the cleanup path for orphans left behind when provisioning
crashes after provision_box commits (e.g. the ru-bringup Path(None) crash that
stranded box c1a72a8d holding www.cloudflare.com on 2026-06-01).
"""
from unittest.mock import MagicMock

import pytest

from mthydra.controller.provisioning.reclaim import ReclaimError, reclaim_box
from mthydra.controller.provisioning.seed import provision_box
from mthydra.controller.state import cover_pool, ru_boxes
from mthydra.controller.state.db import connect
from mthydra.controller.state.schema import apply_schema

NOW = "2026-06-01T00:00:00Z"
LATER = "2026-06-02T00:00:00Z"


@pytest.fixture
def conn(tmp_db_path):
    c = connect(tmp_db_path)
    apply_schema(c)
    return c


def _b2_mock():
    b2 = MagicMock()
    b2.presigned_image_url.return_value = (
        "https://b2.example/abc123/mtg?sig=zzz",
        "2026-06-01T01:00:00Z",
    )
    return b2


def _seed_prereqs(conn, domain="example.cover"):
    from mthydra.controller.state.authority import insert_authority
    from mthydra.descriptor.authority import generate_authority_keypair
    priv, pub = generate_authority_keypair()
    insert_authority(conn, 1, priv, pub, NOW)

    from mthydra.controller.state.descriptor import insert_signing_key
    from mthydra.descriptor.keys import generate_keypair
    dpriv, dpub = generate_keypair()
    insert_signing_key(conn, 1, dpriv, dpub, NOW)
    from mthydra.descriptor.sign import sign_new_descriptor
    sign_new_descriptor(conn, now_iso=NOW, valid_until_iso="2026-06-02T00:00:00Z")

    from mthydra.controller.state.ru_images import insert_candidate, promote
    insert_candidate(
        conn, image_version="abc123", upstream_release="v2.1.7",
        upstream_repo="9seconds/mtg", binary_url="images/abc123/mtg",
        manifest_url="images/abc123/manifest.json", binary_sha256="abc123",
        binary_size_bytes=10485760, built_at=NOW,
    )
    promote(conn, "abc123", at=NOW, evidence="smoke")

    cover_pool.add_candidate(conn, domain, added_at=NOW)
    cover_pool.attest_verified(conn, domain, from_vantage="ru-vps-01", at=NOW)


_V2_KWARGS = dict(
    descriptor_refresh_url="https://b2.example/descriptors/current",
    agent_source_url="https://b2.example/agent/v0.1.0.tar.gz",
    agent_source_sha256="deadbeef" * 8,
    telegram_dcs_v4=("149.154.160.0/20",),
    telegram_dcs_v6=("2001:b28:f23d::/48",),
)


def _provision(conn, domain="example.cover"):
    return provision_box(
        conn=conn, b2_destination=_b2_mock(), provider="hetzner", region="fsn1",
        image_signed_url_ttl_seconds=3600, now=NOW, **_V2_KWARGS,
    )


def test_reclaim_returns_cover_to_candidate_verified(conn):
    _seed_prereqs(conn, "example.cover")
    seed = _provision(conn, "example.cover")
    # Sanity: provisioning consumed the cover domain.
    assert cover_pool.list_by_state(conn, "in_use")[0].domain == "example.cover"
    assert cover_pool.list_by_state(conn, "candidate_verified") == []

    domain = reclaim_box(conn, seed.box_id, now=LATER)

    assert domain == "example.cover"
    # Cover domain is reusable again — NOT burned.
    verified = cover_pool.list_by_state(conn, "candidate_verified")
    assert [c.domain for c in verified] == ["example.cover"]
    assert cover_pool.list_by_state(conn, "in_use") == []
    burned = conn.execute("SELECT COUNT(*) FROM burned_domains").fetchone()[0]
    assert burned == 0
    # The reclaimed cover row has no lingering box assignment.
    row = verified[0]
    assert row.assigned_box_id is None
    assert row.entered_in_use_at is None
    # Its verification attestation is preserved (it was never actually exposed).
    assert row.last_verified_at == NOW
    assert row.verified_from_vantage == "ru-vps-01"


def test_reclaim_marks_box_terminated(conn):
    _seed_prereqs(conn, "example.cover")
    seed = _provision(conn, "example.cover")

    reclaim_box(conn, seed.box_id, now=LATER, reason="orphan cleanup")

    state = conn.execute(
        "SELECT state, terminated_at, termination_reason FROM ru_boxes WHERE box_id=?",
        (seed.box_id,),
    ).fetchone()
    assert state[0] == "terminated"
    assert state[1] == LATER
    assert "orphan cleanup" in state[2]


def test_reclaim_refuses_live_box(conn):
    """A live box exposed its SNI on a real VM. Reclaim must refuse — the
    operator has to ru-box-terminate (which burns the SNI) instead."""
    _seed_prereqs(conn, "example.cover")
    seed = _provision(conn, "example.cover")
    ru_boxes.mark_live(conn, seed.box_id, public_ip="203.0.113.7", at=NOW)

    with pytest.raises(ReclaimError, match="live"):
        reclaim_box(conn, seed.box_id, now=LATER)

    # Nothing changed: still live, cover still in_use.
    assert ru_boxes.list_live(conn)[0].box_id == seed.box_id
    assert cover_pool.list_by_state(conn, "in_use")[0].domain == "example.cover"


def test_reclaim_unknown_box_raises(conn):
    _seed_prereqs(conn, "example.cover")
    with pytest.raises(ReclaimError, match="not found"):
        reclaim_box(conn, "no-such-box", now=LATER)


def test_reclaim_writes_audit_row(conn):
    _seed_prereqs(conn, "example.cover")
    seed = _provision(conn, "example.cover")

    reclaim_box(conn, seed.box_id, now=LATER)

    row = conn.execute(
        "SELECT actor, target FROM audit_log WHERE action='box_reclaimed'"
    ).fetchone()
    assert row is not None
    assert row[1] == seed.box_id
