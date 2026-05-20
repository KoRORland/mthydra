"""Spec C end-to-end lifecycle: add → attest → assign → rotate-due → burn.

Verifies that the burned row + audit log survive a backup + restore cycle.
"""
import shutil

import pytest

from mthydra.controller.backup.age_crypt import encrypt_file
from mthydra.controller.restore.decrypt import decrypt_blob
from mthydra.controller.state.audit import recent_events
from mthydra.controller.state.burned import is_burned
from mthydra.controller.state.cover_pool import (
    add_candidate, assign_to_box, attest_verified, list_by_state,
    list_due_for_rotation, rotate_and_burn,
)
from mthydra.controller.state.db import connect
from mthydra.controller.state.ru_boxes import insert_box, mark_live
from mthydra.controller.state.schema import apply_schema


@pytest.fixture
def age_keypair(tmp_path):
    if shutil.which("age-keygen") is None:
        pytest.skip("age-keygen not installed")
    import subprocess
    keyfile = tmp_path / "identity"
    subprocess.run(
        ["age-keygen", "-o", str(keyfile)],
        capture_output=True, text=True, check=True,
    )
    for line in keyfile.read_text().splitlines():
        if line.startswith("# public key: "):
            return keyfile, line.removeprefix("# public key: ").strip()
    raise RuntimeError("age-keygen produced no '# public key:' line")


def test_full_lifecycle_survives_backup_restore(tmp_path, age_keypair):
    identity, recipient = age_keypair
    db = tmp_path / "state.sqlite"
    conn = connect(db)
    apply_schema(conn)
    insert_box(conn, "box-1", "aws", "eu-west-1", "10.0.0.1", "sni.invalid",
               "img-v1", "2026-04-01T00:00:00Z")
    mark_live(conn, "box-1", public_ip="10.0.0.1", at="2026-04-01T00:00:00Z")
    add_candidate(conn, "live.org", added_at="2026-04-01T00:00:00Z")
    attest_verified(conn, "live.org", from_vantage="ru-vps-01",
                    at="2026-04-01T01:00:00Z")
    assign_to_box(conn, "live.org", box_id="box-1", at="2026-04-01T02:00:00Z")

    due = list_due_for_rotation(conn, now="2026-05-19T00:00:00Z", rotation_ttl_days=14)
    assert [d.domain for d in due] == ["live.org"]

    rotate_and_burn(conn, "live.org", reason="rotation_ttl",
                    last_box_id="box-1", at="2026-05-19T00:00:00Z",
                    details="14d TTL elapsed")

    assert is_burned(conn, "live.org")
    assert list_by_state(conn, "in_use") == []
    conn.close()

    enc = tmp_path / "state.sqlite.age"
    encrypt_file(db, recipient=recipient, out=enc)
    restored = tmp_path / "restored.sqlite"
    decrypt_blob(enc, identity_path=identity, out=restored)

    conn = connect(restored)
    assert is_burned(conn, "live.org")
    actions = {e.action for e in recent_events(conn, limit=50)}
    assert {"cover_added", "cover_attest_verified", "cover_assigned",
            "cover_rotated", "cover_burned"}.issubset(actions)
    conn.close()
