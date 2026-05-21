"""Spec G — provisioning.seed unit tests."""
import base64
import json
from unittest.mock import MagicMock

import pytest

from mthydra.controller.provisioning.seed import (
    ProvisionError, SeedBundle, provision_box,
)
from mthydra.controller.state.db import connect
from mthydra.controller.state.schema import apply_schema


@pytest.fixture
def conn(tmp_db_path):
    c = connect(tmp_db_path)
    apply_schema(c)
    return c


NOW = "2026-05-21T00:00:00Z"


def _seed_authority(conn):
    """Insert a real Ed25519 authority row at generation 1."""
    from mthydra.controller.state.authority import insert_authority
    from mthydra.descriptor.authority import generate_authority_keypair
    priv, pub = generate_authority_keypair()
    insert_authority(conn, 1, priv, pub, NOW)


def _seed_placeholder_authority(conn):
    from mthydra.controller.state.authority import insert_authority
    insert_authority(conn, 1, "PRIV-BOOTSTRAP-xx", "PUB-BOOTSTRAP-xx", NOW)


def _seed_descriptor(conn):
    """Insert a signing key + sign one descriptor (touches descriptor_history)."""
    from mthydra.descriptor.keys import generate_keypair
    from mthydra.controller.state.descriptor import insert_signing_key
    priv, pub = generate_keypair()
    insert_signing_key(conn, 1, priv, pub, NOW)
    from mthydra.descriptor.sign import sign_new_descriptor
    sign_new_descriptor(conn, now_iso=NOW, valid_until_iso="2026-05-22T00:00:00Z")


def _seed_image(conn):
    from mthydra.controller.state.ru_images import insert_candidate, promote
    insert_candidate(
        conn,
        image_version="abc123",
        upstream_release="v2.1.7",
        upstream_repo="9seconds/mtg",
        binary_url="images/abc123/mtg",
        manifest_url="images/abc123/manifest.json",
        binary_sha256="abc123",
        binary_size_bytes=10485760,
        built_at=NOW,
    )
    promote(conn, "abc123", at=NOW, evidence="smoke")


def _seed_cover(conn, domain="example.cover"):
    from mthydra.controller.state.cover_pool import add_candidate, attest_verified
    add_candidate(conn, domain, added_at=NOW)
    attest_verified(conn, domain, from_vantage="ru-vps-01", at=NOW)


def _b2_mock():
    b2 = MagicMock()
    b2.presigned_image_url.return_value = (
        "https://b2.example/abc123/mtg?sig=zzz",
        "2026-05-21T01:00:00Z",
    )
    return b2


def test_provision_box_happy_path(conn):
    _seed_authority(conn)
    _seed_descriptor(conn)
    _seed_image(conn)
    _seed_cover(conn, "example.cover")
    b2 = _b2_mock()

    seed = provision_box(
        conn=conn, b2_destination=b2,
        provider="hetzner", region="fsn1",
        image_signed_url_ttl_seconds=3600,
        now=NOW,
    )
    assert isinstance(seed, SeedBundle)
    assert seed.sni == "example.cover"
    assert seed.transport_role == "ru_relay"
    assert seed.image_version == "abc123"
    assert "abc123" in seed.image_url
    assert len(seed.descriptor_trust_anchors_b64) == 1

    rows = conn.execute("SELECT box_id, state, sni FROM ru_boxes").fetchall()
    assert len(rows) == 1
    assert rows[0][1] == "provisioning"
    assert rows[0][2] == "example.cover"

    state = conn.execute(
        "SELECT state, assigned_box_id FROM cover_domain_pool WHERE domain='example.cover'"
    ).fetchone()
    assert state[0] == "in_use"
    assert state[1] == rows[0][0]

    cred_row = conn.execute(
        "SELECT credential FROM onward_credentials WHERE box_id=?", (rows[0][0],)
    ).fetchone()
    assert cred_row is not None
    cred_blob = bytes(cred_row[0])

    from mthydra.descriptor.authority import verify_onward_credential
    from mthydra.controller.state.authority import current_authority
    pub_pem = current_authority(conn).pubkey_pem
    payload = verify_onward_credential(cred_blob, pub_pem)
    assert payload.box_id == rows[0][0]


def test_provision_box_refuses_placeholder_authority(conn):
    _seed_placeholder_authority(conn)
    _seed_descriptor(conn)
    _seed_image(conn)
    _seed_cover(conn, "example.cover")

    with pytest.raises(ProvisionError, match="authority"):
        provision_box(
            conn=conn, b2_destination=_b2_mock(),
            provider="hetzner", region="fsn1",
            image_signed_url_ttl_seconds=3600,
            now=NOW,
        )


def test_provision_box_refuses_no_promoted_image(conn):
    _seed_authority(conn)
    _seed_descriptor(conn)
    _seed_cover(conn, "example.cover")
    with pytest.raises(ProvisionError, match="image"):
        provision_box(
            conn=conn, b2_destination=_b2_mock(),
            provider="hetzner", region="fsn1",
            image_signed_url_ttl_seconds=3600,
            now=NOW,
        )


def test_provision_box_refuses_no_cover_domain(conn):
    _seed_authority(conn)
    _seed_descriptor(conn)
    _seed_image(conn)
    with pytest.raises(ProvisionError, match="cover"):
        provision_box(
            conn=conn, b2_destination=_b2_mock(),
            provider="hetzner", region="fsn1",
            image_signed_url_ttl_seconds=3600,
            now=NOW,
        )


def test_provision_box_refuses_no_descriptor(conn):
    _seed_authority(conn)
    _seed_image(conn)
    _seed_cover(conn, "example.cover")
    with pytest.raises(ProvisionError, match="descriptor"):
        provision_box(
            conn=conn, b2_destination=_b2_mock(),
            provider="hetzner", region="fsn1",
            image_signed_url_ttl_seconds=3600,
            now=NOW,
        )


def test_seed_bundle_to_json_round_trips(conn):
    _seed_authority(conn)
    _seed_descriptor(conn)
    _seed_image(conn)
    _seed_cover(conn, "example.cover")
    seed = provision_box(
        conn=conn, b2_destination=_b2_mock(),
        provider="hetzner", region="fsn1",
        image_signed_url_ttl_seconds=3600,
        now=NOW,
    )
    payload = json.loads(seed.to_json())
    assert payload["schema"] == "mthydra.ru_seed.v1"
    assert payload["sni"] == "example.cover"
    assert "onward_credential" in payload
    assert "initial_descriptor" in payload
    cred_bytes = base64.b64decode(payload["onward_credential"])
    from mthydra.descriptor.authority import verify_onward_credential
    verified = verify_onward_credential(cred_bytes, payload["authority_pubkey_pem"])
    assert verified.box_id == payload["box_id"]


def test_seed_bundle_to_cloud_init_wraps_json(conn):
    _seed_authority(conn)
    _seed_descriptor(conn)
    _seed_image(conn)
    _seed_cover(conn, "example.cover")
    seed = provision_box(
        conn=conn, b2_destination=_b2_mock(),
        provider="hetzner", region="fsn1",
        image_signed_url_ttl_seconds=3600,
        now=NOW,
    )
    yaml_text = seed.to_cloud_init().decode("utf-8")
    assert yaml_text.startswith("#cloud-config")
    assert "write_files:" in yaml_text
    assert "/run/mthydra/seed.json" in yaml_text
    assert "example.cover" in yaml_text
