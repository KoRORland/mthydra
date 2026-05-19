"""Tests for first-run bootstrap (spec A §10.1 + spec B Ed25519 key generation)."""
import shutil

import pytest

from mthydra.controller.bootstrap import BootstrapError, init_state
from mthydra.controller.state.authority import current_authority
from mthydra.controller.state.db import connect
from mthydra.controller.state.descriptor import current_signing_key
from mthydra.controller.state.obligations import list_obligations
from mthydra.controller.state.tokens import get_provider_credential
from mthydra.descriptor.keys import is_placeholder

# Use a real-looking but fake age public key (age1 + bech32 chars, >=32 chars long)
FAKE_RECIPIENT = "age1ql3z7hjy54pw3hyww5ayyfg7zqgvc7w3j2elw8zmrj2kg5sfn9aqmcac8p"


def test_init_creates_db_with_seed_rows(tmp_path):
    db = tmp_path / "state.sqlite"
    init_state(
        db_path=db,
        age_recipient=FAKE_RECIPIENT,
        provider_credentials={"aws": "AKID:SECRET", "b2": "ID:SECRET"},
        obligation_timer_hours={"backup_restore_dryrun": 720, "t2_dryrun_caseA": 720},
        now="2026-05-18T00:00:00Z",
    )
    assert db.exists()
    conn = connect(db)
    assert current_authority(conn).generation == 1
    assert current_signing_key(conn).generation == 1
    assert get_provider_credential(conn, "aws") == "AKID:SECRET"
    assert get_provider_credential(conn, "b2") == "ID:SECRET"
    obs = {o.obligation_id for o in list_obligations(conn)}
    assert "backup_restore_dryrun" in obs
    assert "t2_dryrun_caseA" in obs


def test_init_refuses_when_db_exists(tmp_path):
    db = tmp_path / "state.sqlite"
    db.write_bytes(b"")
    with pytest.raises(BootstrapError, match="exists"):
        init_state(
            db_path=db,
            age_recipient=FAKE_RECIPIENT,
            provider_credentials={"aws": "x"},
            obligation_timer_hours={},
            now="2026-05-18T00:00:00Z",
        )


def test_init_rejects_bad_recipient(tmp_path):
    db = tmp_path / "state.sqlite"
    with pytest.raises(BootstrapError, match="recipient"):
        init_state(
            db_path=db,
            age_recipient="not-an-age-key",
            provider_credentials={"aws": "x"},
            obligation_timer_hours={},
            now="2026-05-18T00:00:00Z",
        )


def test_init_obligation_next_due_computed(tmp_path):
    db = tmp_path / "state.sqlite"
    init_state(
        db_path=db,
        age_recipient=FAKE_RECIPIENT,
        provider_credentials={},
        obligation_timer_hours={"t2_dryrun_caseA": 720},
        now="2026-05-18T00:00:00Z",
    )
    conn = connect(db)
    obs = {o.obligation_id: o for o in list_obligations(conn)}
    assert obs["t2_dryrun_caseA"].next_due_at == "2026-06-17T00:00:00Z"


def test_init_no_provider_creds(tmp_path):
    db = tmp_path / "state.sqlite"
    init_state(
        db_path=db,
        age_recipient=FAKE_RECIPIENT,
        provider_credentials={},
        obligation_timer_hours={},
        now="2026-05-18T00:00:00Z",
    )
    assert db.exists()


def test_init_generates_real_ed25519_signing_key(tmp_path):
    """spec B: init must produce a real Ed25519 key, not a placeholder."""
    db = tmp_path / "state.sqlite"
    init_state(
        db_path=db,
        age_recipient=FAKE_RECIPIENT,
        provider_credentials={},
        obligation_timer_hours={},
        now="2026-05-18T00:00:00Z",
    )
    conn = connect(db)
    key = current_signing_key(conn)
    assert len(bytes(key.pubkey)) == 32, "pubkey must be 32 bytes"
    assert len(bytes(key.privkey)) == 32, "privkey must be 32 bytes"
    assert not is_placeholder(bytes(key.privkey)), "must not be a spec A placeholder"


def test_init_sets_file_mode_0600(tmp_path):
    import stat
    db = tmp_path / "state.sqlite"
    init_state(
        db_path=db,
        age_recipient=FAKE_RECIPIENT,
        provider_credentials={},
        obligation_timer_hours={},
        now="2026-05-18T00:00:00Z",
    )
    mode = stat.S_IMODE(db.stat().st_mode)
    assert mode == 0o600, f"expected 0o600, got {oct(mode)}"


def test_init_sets_parent_dir_mode_0700(tmp_path):
    import stat
    db = tmp_path / "state.sqlite"
    init_state(
        db_path=db,
        age_recipient=FAKE_RECIPIENT,
        provider_credentials={},
        obligation_timer_hours={},
        now="2026-05-18T00:00:00Z",
    )
    mode = stat.S_IMODE(tmp_path.stat().st_mode)
    assert mode == 0o700, f"expected 0o700, got {oct(mode)}"
