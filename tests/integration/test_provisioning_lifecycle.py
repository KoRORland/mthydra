"""Spec G — end-to-end provisioning lifecycle.

init → authority-migrate-placeholder → image-build (mocked) → cover-add +
cover-attest-verified → descriptor-sign-now → provision-seed → verify the
seed bundle via the pure-Python verifier → ru-box-mark-live →
ru-box-terminate → confirm SNI is burned + credentials revoked.
"""
import base64
import json
import shutil
import subprocess
from unittest.mock import MagicMock

import pytest

from mthydra.controller.bootstrap import init_state
from mthydra.controller.image.builder import build_image
from mthydra.controller.provisioning.seed import provision_box
from mthydra.controller.state.audit import recent_events
from mthydra.controller.state.authority import current_authority
from mthydra.controller.state.burned import is_burned
from mthydra.controller.state.cover_pool import add_candidate, attest_verified
from mthydra.controller.state.credentials import active_for_box
from mthydra.controller.state.db import connect
from mthydra.controller.state.ru_boxes import mark_live, mark_terminated
from mthydra.descriptor.authority import (
    generate_authority_keypair, verify_onward_credential,
)


@pytest.fixture
def recipient_fixture(tmp_path):
    if shutil.which("age-keygen") is None:
        pytest.skip("age-keygen not installed")
    keyfile = tmp_path / "identity"
    subprocess.run(["age-keygen", "-o", str(keyfile)], capture_output=True, check=True)
    for line in keyfile.read_text().splitlines():
        if line.startswith("# public key: "):
            return line.removeprefix("# public key: ").strip()
    raise RuntimeError("no public key line")


def test_provisioning_lifecycle_end_to_end(tmp_path, recipient_fixture):
    NOW = "2026-05-21T00:00:00Z"
    db = tmp_path / "state.sqlite"

    # 1. init DB with placeholder authority.
    init_state(
        db_path=db,
        age_recipient=recipient_fixture,
        provider_credentials={"b2": "id:secret"},
        obligation_timer_hours={"backup_restore_dryrun": 720},
        now=NOW,
        role="active",
    )

    # 2. Migrate authority to real Ed25519 (mimics CLI authority-migrate-placeholder).
    conn = connect(db)
    priv, pub = generate_authority_keypair()
    conn.execute(
        "UPDATE credential_authority SET privkey_pem=?, pubkey_pem=? "
        "WHERE retired_at IS NULL", (priv, pub),
    )
    conn.commit()
    conn.close()

    # 3. Sign a descriptor (must come before provision; uses spec-B helpers).
    from mthydra.descriptor.sign import sign_new_descriptor
    conn = connect(db)
    sign_new_descriptor(conn, now_iso=NOW, valid_until_iso="2026-05-22T00:00:00Z")
    conn.close()

    # 4. Build + promote an image (mocked HTTP + mocked B2).
    import hashlib
    asset_bytes = b"binary-bytes" * 100
    sha = hashlib.sha256(asset_bytes).hexdigest()
    checksum_text = f"{sha}  mtg-linux-amd64\n"
    release_json = {
        "tag_name": "v2.1.7",
        "assets": [
            {"name": "mtg-linux-amd64", "browser_download_url": "https://x/mtg-linux-amd64"},
            {"name": "SHA256SUMS", "browser_download_url": "https://x/SHA256SUMS"},
        ],
    }

    def _mock_http(url):
        resp = MagicMock()
        resp.status = 200
        if url.endswith("/releases/tags/v2.1.7"):
            resp.read.return_value = json.dumps(release_json).encode()
        elif url.endswith("/mtg-linux-amd64"):
            resp.read.return_value = asset_bytes
        elif url.endswith("/SHA256SUMS"):
            resp.read.return_value = checksum_text.encode()
        return resp

    b2 = MagicMock()
    b2.presigned_image_url.return_value = (
        f"https://b2.example/{sha}/mtg?sig=stub", "2026-05-21T01:00:00Z",
    )
    conn = connect(db)
    build_image(
        conn=conn, b2_destination=b2,
        upstream_repo="9seconds/mtg",
        upstream_release="v2.1.7",
        asset_filename="mtg-linux-amd64",
        github_api_url="https://api.github.com",
        tmp_dir=tmp_path,
        now=NOW,
        http_client=_mock_http,
    )
    from mthydra.controller.state.ru_images import promote
    promote(conn, sha, at=NOW, evidence="smoke")
    conn.close()

    # 5. Attest a cover domain.
    conn = connect(db)
    add_candidate(conn, "example.cover", added_at=NOW)
    attest_verified(conn, "example.cover", from_vantage="ru-vps-01", at=NOW)
    conn.close()

    # 6. provision_box.
    conn = connect(db)
    seed = provision_box(
        conn=conn, b2_destination=b2,
        provider="hetzner", region="fsn1",
        image_signed_url_ttl_seconds=3600,
        now=NOW,
        descriptor_refresh_url="https://b2.example/desc",
        agent_source_url="https://b2.example/agent.tgz",
        agent_source_sha256="deadbeef" * 8,
        telegram_dcs_v4=(),
        telegram_dcs_v6=(),
    )
    conn.close()

    # 7. Verify the embedded onward credential via the pure-Python verifier.
    cred = base64.b64decode(seed.onward_credential_b64)
    verified = verify_onward_credential(cred, seed.authority_pubkey_pem)
    assert verified.box_id == seed.box_id
    assert verified.authority_generation >= 1

    # 8. Verify the embedded initial descriptor.
    import struct
    desc_blob = base64.b64decode(seed.initial_descriptor_b64)
    assert len(desc_blob) >= 2 + 64
    n = struct.unpack(">H", desc_blob[:2])[0]
    desc_json = json.loads(desc_blob[2:2 + n])
    assert desc_json["generation"] == 1

    # 9. mark_live + terminate.
    conn = connect(db)
    mark_live(conn, seed.box_id, public_ip="203.0.113.7", at=NOW)
    conn.close()

    conn = connect(db)
    from mthydra.controller.state.credentials import revoke_credential
    from mthydra.controller.state.burned import mark_burned
    for c in active_for_box(conn, seed.box_id):
        revoke_credential(conn, c.cred_id, at=NOW)
    mark_burned(conn, seed.sni, "test", seed.box_id, NOW, None)
    mark_terminated(conn, seed.box_id, reason="test", at=NOW)
    assert is_burned(conn, seed.sni)
    remaining = active_for_box(conn, seed.box_id)
    assert remaining == []
    conn.close()

    # 10. Audit log has the full sequence.
    conn = connect(db)
    actions = {e.action for e in recent_events(conn, limit=50)}
    assert "box_provisioned" in actions
    assert "image_built" in actions
    assert "image_promoted" in actions
    conn.close()
