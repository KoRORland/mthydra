"""Tests for startup self-check runner (spec A §10 + plan §16.2 mode extensions)."""
import shutil

import pytest

from mthydra.controller.bootstrap import init_state
from mthydra.controller.startup import StartupCheckResult, run_startup_checks
from mthydra.controller.state.authority import insert_authority
from mthydra.controller.state.db import connect
from mthydra.controller.state.schema import apply_schema

# A fake-but-valid age public key (age1 + >=32 chars)
FAKE_RECIPIENT = "age1ql3z7hjy54pw3hyww5ayyfg7zqgvc7w3j2elw8zmrj2kg5sfn9aqmcac8p"


@pytest.fixture
def initialized_db(tmp_path):
    db = tmp_path / "state.sqlite"
    init_state(
        db_path=db,
        age_recipient=FAKE_RECIPIENT,
        provider_credentials={"aws": "x", "b2": "y"},
        obligation_timer_hours={"backup_restore_dryrun": 720},
        now="2026-05-18T00:00:00Z",
    )
    return db


def test_run_startup_checks_succeeds_on_clean_init(initialized_db):
    result = run_startup_checks(db_path=initialized_db, age_recipient=FAKE_RECIPIENT)
    # age binary may or may not be installed; accept either pass or age_binary fail
    assert result.failed_check in (None, "age_binary")
    if result.failed_check == "age_binary":
        pytest.skip("age binary not installed")
    assert result.ok


def test_run_startup_checks_fails_when_db_missing(tmp_path):
    result = run_startup_checks(
        db_path=tmp_path / "missing.sqlite", age_recipient=FAKE_RECIPIENT
    )
    assert not result.ok
    assert "db" in result.failed_check


def test_run_startup_checks_fails_on_bad_recipient(initialized_db):
    result = run_startup_checks(db_path=initialized_db, age_recipient="not-age")
    # If age binary missing, we hit age_binary check first
    assert not result.ok
    assert result.failed_check in ("age_binary", "age_recipient")


def test_run_startup_checks_fails_on_invariant_violation(tmp_path):
    """A DB with two active credential_authority rows fails the invariant check."""
    if shutil.which("age") is None:
        pytest.skip("age binary not installed")
    db = tmp_path / "state.sqlite"
    conn = connect(db)
    apply_schema(conn)
    insert_authority(conn, 1, "P1", "K1", "2026-05-18T00:00:00Z")
    insert_authority(conn, 2, "P2", "K2", "2026-05-18T00:00:00Z")
    conn.close()
    result = run_startup_checks(db_path=db, age_recipient=FAKE_RECIPIENT)
    assert not result.ok
    assert result.failed_check == "invariant"


# ---------------------------------------------------------------------------
# Mode tests (plan §16.2 G4)
# ---------------------------------------------------------------------------

def test_dryrun_mode_rejects_when_no_bucket_override(initialized_db):
    if shutil.which("age") is None:
        pytest.skip("age binary not installed")
    result = run_startup_checks(
        db_path=initialized_db,
        age_recipient=FAKE_RECIPIENT,
        mode="dryrun",
        bucket_override=None,
    )
    assert not result.ok
    assert result.failed_check == "dryrun_bucket_override"


def test_dryrun_mode_rejects_when_override_matches_prod(initialized_db):
    if shutil.which("age") is None:
        pytest.skip("age binary not installed")
    result = run_startup_checks(
        db_path=initialized_db,
        age_recipient=FAKE_RECIPIENT,
        mode="dryrun",
        bucket_override="my-prod-bucket",
        prod_bucket="my-prod-bucket",
    )
    assert not result.ok
    assert result.failed_check == "dryrun_bucket_override"


def test_offline_mode_skips_network_checks(initialized_db):
    """offline mode should pass checks 1–9 without any network calls."""
    if shutil.which("age") is None:
        pytest.skip("age binary not installed")
    result = run_startup_checks(
        db_path=initialized_db,
        age_recipient=FAKE_RECIPIENT,
        mode="offline",
        destination=None,
    )
    assert result.ok
