"""Spec F — end-to-end promotion lifecycle.

Active DB created + populated → backup encrypted → decrypted + promoted onto
a separately-init'd standby skeleton DB → state survives → startup-check passes.
"""
import shutil
import subprocess

import pytest

from mthydra.controller.bootstrap import init_state
from mthydra.controller.promote import promote_active
from mthydra.controller.state.audit import recent_events
from mthydra.controller.state.cover_pool import add_candidate, attest_verified
from mthydra.controller.state.db import connect
from mthydra.controller.state.invariants import check_all
from mthydra.controller.state.node_state import current_node_state
from mthydra.controller.state.schema import SCHEMA_VERSION


@pytest.fixture
def age_keypair(tmp_path):
    if shutil.which("age-keygen") is None:
        pytest.skip("age-keygen not installed")
    keyfile = tmp_path / "identity"
    subprocess.run(["age-keygen", "-o", str(keyfile)], capture_output=True, check=True)
    for line in keyfile.read_text().splitlines():
        if line.startswith("# public key: "):
            return keyfile, line.removeprefix("# public key: ").strip()
    raise RuntimeError("no public key line")


def test_promotion_lifecycle(tmp_path, age_keypair):
    identity, recipient = age_keypair

    # 1. Set up the active "donor" DB with some real state.
    active_db = tmp_path / "active.sqlite"
    init_state(
        db_path=active_db,
        age_recipient=recipient,
        provider_credentials={"b2": "id:secret"},
        obligation_timer_hours={"backup_restore_dryrun": 720},
        now="2026-05-20T00:00:00Z",
        role="active",
    )
    conn = connect(active_db)
    add_candidate(conn, "live.org", added_at="2026-05-20T00:30:00Z")
    attest_verified(conn, "live.org", from_vantage="ru-vps-01",
                    at="2026-05-20T00:35:00Z")
    conn.close()

    # 2. Encrypt the active DB (simulating a backup blob).
    blob = tmp_path / "backup.age"
    from mthydra.controller.backup.age_crypt import encrypt_file
    encrypt_file(active_db, recipient=recipient, out=blob)

    # 3. Set up a separate standby skeleton DB.
    standby_db = tmp_path / "standby.sqlite"
    init_state(
        db_path=standby_db,
        age_recipient=recipient,
        provider_credentials={"b2": "id:secret"},
        obligation_timer_hours={},
        now="2026-05-20T01:00:00Z",
        role="standby",
    )
    conn = connect(standby_db)
    ns = current_node_state(conn)
    assert ns.role == "standby"
    conn.close()

    # 4. Promote the standby with Case A.
    checklist = promote_active(
        db_path=standby_db,
        backup_blob=blob,
        age_identity=identity,
        case="A",
        node_id="eu-promoted-1",
        now="2026-05-20T02:00:00Z",
    )
    assert checklist is None  # Case A — no checklist

    # 5. Verify the promoted DB carries the active's full state.
    conn = connect(standby_db)
    ns = current_node_state(conn)
    assert ns.role == "active"
    assert ns.previous_role == "standby"
    assert ns.promotion_case == "A"

    cnt = conn.execute(
        "SELECT COUNT(*) FROM credential_authority WHERE retired_at IS NULL"
    ).fetchone()[0]
    assert cnt == 1

    pool = conn.execute(
        "SELECT domain, state FROM cover_domain_pool WHERE domain='live.org'"
    ).fetchone()
    assert pool == ("live.org", "candidate_verified")

    # startup-check passes on the new active
    check_all(conn, expected_schema_version=SCHEMA_VERSION,
              now_iso="2026-05-20T02:01:00Z")

    # Audit log contains the promotion event + role transition
    actions = {e.action for e in recent_events(conn, limit=50)}
    assert "eu_node_promoted" in actions
    assert "node_role_set" in actions

    conn.close()
