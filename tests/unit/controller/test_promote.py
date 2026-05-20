"""Spec F — promote-active atomic state replacement."""
import shutil
import subprocess

import pytest

from mthydra.controller.promote import PromotionError, promote_active
from mthydra.controller.state.db import connect
from mthydra.controller.state.node_state import current_node_state


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


def _seed_active_db(path, recipient):
    from mthydra.controller.bootstrap import init_state
    init_state(
        db_path=path,
        age_recipient=recipient,
        provider_credentials={"b2": "id:secret"},
        obligation_timer_hours={"backup_restore_dryrun": 720},
        now="2026-05-20T00:00:00Z",
        role="active",
    )


def _encrypt_db(src, dst, recipient):
    from mthydra.controller.backup.age_crypt import encrypt_file
    encrypt_file(src, recipient=recipient, out=dst)


def _seed_skeleton_db(path, recipient):
    from mthydra.controller.bootstrap import init_state
    init_state(
        db_path=path,
        age_recipient=recipient,
        provider_credentials={"b2": "id:secret"},
        obligation_timer_hours={},
        now="2026-05-20T01:00:00Z",
        role="standby",
    )


def test_promote_active_case_a_swaps_db(tmp_path, age_keypair):
    identity, recipient = age_keypair
    donor_db = tmp_path / "donor.sqlite"
    blob = tmp_path / "backup.age"
    target_db = tmp_path / "state.sqlite"

    _seed_active_db(donor_db, recipient)
    _encrypt_db(donor_db, blob, recipient)
    _seed_skeleton_db(target_db, recipient)

    case_b_checklist = promote_active(
        db_path=target_db,
        backup_blob=blob,
        age_identity=identity,
        case="A",
        node_id="eu-promoted-1",
        now="2026-05-20T01:00:00Z",
    )
    assert case_b_checklist is None

    conn = connect(target_db)
    ns = current_node_state(conn)
    assert ns.role == "active"
    assert ns.previous_role == "standby"
    assert ns.promotion_case == "A"
    cnt_auth = conn.execute(
        "SELECT COUNT(*) FROM credential_authority WHERE retired_at IS NULL"
    ).fetchone()[0]
    assert cnt_auth == 1
    conn.close()


def test_promote_active_case_b_returns_checklist(tmp_path, age_keypair):
    identity, recipient = age_keypair
    donor_db = tmp_path / "donor.sqlite"
    blob = tmp_path / "backup.age"
    target_db = tmp_path / "state.sqlite"
    _seed_active_db(donor_db, recipient)
    _encrypt_db(donor_db, blob, recipient)
    _seed_skeleton_db(target_db, recipient)

    checklist = promote_active(
        db_path=target_db,
        backup_blob=blob,
        age_identity=identity,
        case="B",
        node_id="eu-promoted-1",
        now="2026-05-20T01:00:00Z",
    )
    assert checklist is not None
    assert "authority-rotate" in checklist
    assert "signing-key-rotate" in checklist
    assert "rotate-provider-credential" in checklist


def test_promote_active_refuses_when_role_is_active(tmp_path, age_keypair):
    identity, recipient = age_keypair
    donor_db = tmp_path / "donor.sqlite"
    blob = tmp_path / "backup.age"
    target_db = tmp_path / "state.sqlite"
    _seed_active_db(donor_db, recipient)
    _encrypt_db(donor_db, blob, recipient)
    _seed_active_db(target_db, recipient)

    with pytest.raises(PromotionError, match="standby"):
        promote_active(
            db_path=target_db, backup_blob=blob, age_identity=identity,
            case="A", node_id="x", now="2026-05-20T01:00:00Z",
        )


def test_promote_active_refuses_invalid_case(tmp_path, age_keypair):
    identity, recipient = age_keypair
    donor_db = tmp_path / "donor.sqlite"
    blob = tmp_path / "backup.age"
    target_db = tmp_path / "state.sqlite"
    _seed_active_db(donor_db, recipient)
    _encrypt_db(donor_db, blob, recipient)
    _seed_skeleton_db(target_db, recipient)

    with pytest.raises(PromotionError, match="case"):
        promote_active(
            db_path=target_db, backup_blob=blob, age_identity=identity,
            case="C", node_id="x", now="2026-05-20T01:00:00Z",
        )


def test_promote_active_rolls_back_on_startup_check_failure(tmp_path, age_keypair, monkeypatch):
    """If startup-check fails on the new DB, the .bak is restored."""
    identity, recipient = age_keypair
    donor_db = tmp_path / "donor.sqlite"
    blob = tmp_path / "backup.age"
    target_db = tmp_path / "state.sqlite"
    _seed_active_db(donor_db, recipient)
    _encrypt_db(donor_db, blob, recipient)
    _seed_skeleton_db(target_db, recipient)

    from mthydra.controller.state.invariants import InvariantViolation
    def _fail(*_a, **_kw):
        raise InvariantViolation("simulated")
    monkeypatch.setattr("mthydra.controller.promote.check_all", _fail)

    with pytest.raises(PromotionError, match="invariant"):
        promote_active(
            db_path=target_db, backup_blob=blob, age_identity=identity,
            case="A", node_id="x", now="2026-05-20T01:00:00Z",
        )
    conn = connect(target_db)
    ns = current_node_state(conn)
    assert ns.role == "standby"
    conn.close()
