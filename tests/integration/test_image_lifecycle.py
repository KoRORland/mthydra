"""Spec D — end-to-end image lifecycle.

upstream-check (no rows) -> image-build (mocked HTTP + mocked B2) ->
image-list (candidate) -> image-promote -> image-current ->
image-retire (different candidate) -> image-list (mixed states).
"""
import hashlib
import json
import shutil
import subprocess
from unittest.mock import MagicMock

import pytest

from mthydra.controller.bootstrap import init_state
from mthydra.controller.image.builder import build_image
from mthydra.controller.image.upstream_tracker import UpstreamReleaseTracker
from mthydra.controller.state.audit import recent_events
from mthydra.controller.state.db import connect
from mthydra.controller.state.obligations import list_obligations
from mthydra.controller.state.ru_images import (
    current_promoted, list_images, promote, retire,
)


def _mock_http(tag, asset_bytes):
    sha = hashlib.sha256(asset_bytes).hexdigest()
    checksum_text = f"{sha}  mtg-linux-amd64\n"
    release_latest_json = {"tag_name": tag}
    release_tag_json = {
        "tag_name": tag,
        "assets": [
            {"name": "mtg-linux-amd64", "browser_download_url": f"https://example/{tag}/mtg-linux-amd64"},
            {"name": "SHA256SUMS", "browser_download_url": f"https://example/{tag}/SHA256SUMS"},
        ],
    }
    def _get(url):
        resp = MagicMock()
        resp.status = 200
        if url.endswith("/releases/latest"):
            resp.read.return_value = json.dumps(release_latest_json).encode()
        elif "/releases/tags/" in url:
            resp.read.return_value = json.dumps(release_tag_json).encode()
        elif url.endswith("mtg-linux-amd64"):
            resp.read.return_value = asset_bytes
        elif url.endswith("SHA256SUMS"):
            resp.read.return_value = checksum_text.encode()
        else:
            resp.status = 404
            resp.read.return_value = b""
        return resp
    return _get, sha


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


def test_image_lifecycle_end_to_end(tmp_path, recipient_fixture):
    db = tmp_path / "state.sqlite"
    init_state(
        db_path=db,
        age_recipient=recipient_fixture,
        provider_credentials={"b2": "id:secret"},
        obligation_timer_hours={"backup_restore_dryrun": 720},
        now="2026-05-21T00:00:00Z",
        role="active",
    )

    asset_v217 = b"binary-v217" * 100
    asset_v218 = b"binary-v218" * 100
    http_217, sha_217 = _mock_http("v2.1.7", asset_v217)
    http_218, sha_218 = _mock_http("v2.1.8", asset_v218)

    # 1. Upstream check before any builds: anti-obligation appears.
    tracker = UpstreamReleaseTracker(
        db_path=db, upstream_repo="9seconds/mtg",
        github_api_url="https://api.github.com",
        poll_interval_seconds=3600, mode="offline",
        clock=lambda: "2026-05-21T00:30:00Z",
        http_client=http_217,
    )
    latest = tracker.run_once()
    assert latest == "v2.1.7"
    conn = connect(db)
    obs = {o.obligation_id for o in list_obligations(conn)}
    assert "t4_upstream_release_available::v2.1.7" in obs
    conn.close()

    # 2. Build v2.1.7 (mock B2).
    b2 = MagicMock()
    conn = connect(db)
    iv_217 = build_image(
        conn=conn, b2_destination=b2,
        upstream_repo="9seconds/mtg",
        upstream_release="v2.1.7",
        asset_filename="mtg-linux-amd64",
        github_api_url="https://api.github.com",
        tmp_dir=tmp_path,
        now="2026-05-21T01:00:00Z",
        http_client=http_217,
    )
    assert iv_217 == sha_217
    conn.close()

    # 3. Promote it.
    conn = connect(db)
    promote(conn, iv_217, at="2026-05-21T01:30:00Z", evidence="manual smoke")
    conn.close()

    # 4. Anti-obligation cleared; image is current.
    conn = connect(db)
    obs = {o.obligation_id for o in list_obligations(conn)}
    assert "t4_upstream_release_available::v2.1.7" not in obs
    assert "t4_image_promoted" in obs
    n = current_promoted(conn)
    assert n is not None
    assert n.image_version == iv_217
    conn.close()

    # 5. Build v2.1.8 as a candidate (do NOT promote yet).
    conn = connect(db)
    iv_218 = build_image(
        conn=conn, b2_destination=b2,
        upstream_repo="9seconds/mtg",
        upstream_release="v2.1.8",
        asset_filename="mtg-linux-amd64",
        github_api_url="https://api.github.com",
        tmp_dir=tmp_path,
        now="2026-05-21T02:00:00Z",
        http_client=http_218,
    )
    conn.close()

    # 6. Retire the unpromoted candidate.
    conn = connect(db)
    retire(conn, iv_218, at="2026-05-21T02:30:00Z", reason="superseded")
    images = list_images(conn)
    by_state = {im.state for im in images}
    assert by_state == {"promoted", "retired"}
    conn.close()

    # 7. Audit log shows the full sequence.
    conn = connect(db)
    actions = [e.action for e in recent_events(conn, limit=50)]
    assert "image_built" in actions
    assert "image_promoted" in actions
    assert "image_retired" in actions
    conn.close()
