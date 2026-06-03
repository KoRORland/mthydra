#!/usr/bin/env python3
"""Generate a real RU seed.json for the agent-boot harness.

Usage: make_seed.py <out_seed.json> <mtg_file_url> <mtg_sha256>

Seeds a throwaway DB with a real authority + signing key + signed descriptor
(carrying one EU exit so config_gen renders) + promoted image + verified cover,
runs the real provision_box, then rewrites image.url/sha256 to the local mtg
file the container serves over file://.
"""
import base64
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from mthydra.controller.provisioning.seed import provision_box
from mthydra.controller.state import cover_pool, eu_exit_set
from mthydra.controller.state.authority import insert_authority
from mthydra.controller.state.db import connect
from mthydra.controller.state.descriptor import insert_signing_key
from mthydra.controller.state.ru_images import insert_candidate, promote
from mthydra.controller.state.schema import apply_schema
from mthydra.descriptor.authority import generate_authority_keypair
from mthydra.descriptor.keys import generate_keypair
from mthydra.descriptor.sign import sign_new_descriptor

NOW = "2026-06-03T00:00:00Z"


def main() -> int:
    out_path, mtg_url, mtg_sha = sys.argv[1], sys.argv[2], sys.argv[3]
    db = Path(tempfile.mkdtemp()) / "harness.sqlite"
    conn = connect(str(db))
    apply_schema(conn)

    apriv, apub = generate_authority_keypair()
    insert_authority(conn, 1, apriv, apub, NOW)
    dpriv, dpub = generate_keypair()
    insert_signing_key(conn, 1, dpriv, dpub, NOW)
    # One EU exit so the signed descriptor carries an exit (dummy TEST-NET
    # endpoint). reality_pubkey must be a real x25519 key (sing-box validates
    # it: "invalid public_key"); base64url-no-pad, as `sing-box generate
    # reality-keypair` emits. Production gets this from data-exit-reality-keygen.
    _pub_raw = X25519PrivateKey.generate().public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    reality_pub = base64.urlsafe_b64encode(_pub_raw).rstrip(b"=").decode()
    eu_exit_set.add_exit(conn, "harness-fp", "192.0.2.1:443", 1, NOW,
                         cover_sni="www.cloudflare.com", reality_pubkey=reality_pub)
    sign_new_descriptor(conn, now_iso=NOW, valid_until_iso="2026-06-04T00:00:00Z")
    insert_candidate(conn, image_version="harnessimg", upstream_release="v0.0.0",
                     upstream_repo="9seconds/mtg", binary_url="images/x/mtg",
                     manifest_url="images/x/manifest.json", binary_sha256="harnessimg",
                     binary_size_bytes=1, built_at=NOW)
    promote(conn, "harnessimg", at=NOW, evidence="harness")
    cover_pool.add_candidate(conn, "www.cloudflare.com", added_at=NOW)
    cover_pool.attest_verified(conn, "www.cloudflare.com", from_vantage="h", at=NOW)

    b2 = MagicMock()
    b2.presigned_image_url.return_value = (mtg_url, "2026-06-04T00:00:00Z")
    seed = provision_box(
        conn=conn, b2_destination=b2, provider="harness", region="local",
        image_signed_url_ttl_seconds=3600, now=NOW,
        descriptor_refresh_url="file:///dev/null",
        agent_source_url="file:///dev/null", agent_source_sha256="0" * 64,
        telegram_dcs_v4=("149.154.160.0/20",), telegram_dcs_v6=(),
    )
    payload = json.loads(seed.to_json())
    payload["image"]["url"] = mtg_url
    payload["image"]["sha256"] = mtg_sha
    Path(out_path).write_text(json.dumps(payload, indent=2))
    print(f"wrote {out_path} (sni={payload['sni']}, "
          f"image.url={payload['image']['url']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
