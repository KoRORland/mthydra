"""Tests for the controller CLI (spec A phase 6)."""
import shutil
import subprocess

import boto3
import pytest
from moto import mock_aws
from unittest.mock import patch

from mthydra.controller.cli import build_parser, run
from mthydra.controller.bootstrap import init_state
from mthydra.controller.state.db import connect
from mthydra.controller.state.obligations import list_obligations

FAKE_RECIPIENT = "age1ql3z7hjy54pw3hyww5ayyfg7zqgvc7w3j2elw8zmrj2kg5sfn9aqmcac8p"

_MIN_TOML = """\
[node]
role = "active"
hostname = "h"
[backup]
floor_interval_hours = 24
on_change_debounce_seconds = 30
endpoint = "https://example"
bucket = "b"
access_key_id = "k"
[backup.retention]
keep_daily = 30
keep_monthly = 12
object_lock_days = 365
[gap_monitor]
poll_interval_minutes = 30
alarm_threshold_hours = 48
recipient_email = "op@example.org"
[descriptor]
rotation_interval_hours = 1
validity_window_hours = 24
[obligations]
[obligations.timers_hours]
[cover_pool]
rotation_ttl_days = 14
reverify_after_days = 30
freeze_threshold = 2
reverify_sweep_interval = "1h"
rotation_sweep_interval = "1h"
replenishment_interval_days = 90
"""


def test_parser_knows_all_subcommands():
    p = build_parser()
    sub_help = p.format_help()
    for name in (
        "init",
        "startup-check",
        "backup-now",
        "restore",
        "adopt-restored-state",
        "obligation-proven",
    ):
        assert name in sub_help, f"subcommand {name!r} not in parser help"


def test_init_subcommand_runs(tmp_path):
    recipient_file = tmp_path / "age-recipient.txt"
    recipient_file.write_text(FAKE_RECIPIENT + "\n")
    db = tmp_path / "state.sqlite"
    exit_code = run([
        "init",
        "--db-path", str(db),
        "--age-recipient-file", str(recipient_file),
        "--provider-credential", "aws=AKID:SECRET",
        "--provider-credential", "b2=ID:SECRET",
    ])
    assert exit_code == 0
    assert db.exists()


def test_init_subcommand_fails_if_db_exists(tmp_path):
    db = tmp_path / "state.sqlite"
    db.write_bytes(b"")
    recipient_file = tmp_path / "age-recipient.txt"
    recipient_file.write_text(FAKE_RECIPIENT + "\n")
    exit_code = run([
        "init",
        "--db-path", str(db),
        "--age-recipient-file", str(recipient_file),
    ])
    assert exit_code != 0


def test_startup_check_returns_nonzero_when_db_missing(tmp_path):
    exit_code = run([
        "startup-check",
        "--db-path", str(tmp_path / "missing.sqlite"),
        "--age-recipient", FAKE_RECIPIENT,
    ])
    assert exit_code != 0


def test_backup_now_offline_mode_refused(tmp_path):
    """backup-now in offline mode must exit non-zero."""
    exit_code = run(["--mode", "offline", "--bucket-override", "x", "backup-now"])
    # offline mode is rejected at the backup-now level (not the global level)
    assert exit_code != 0


@pytest.mark.skipif(shutil.which("age") is None, reason="age binary not installed")
def test_backup_now_performs_real_backup(tmp_path):
    """backup-now wires to a real BackupPipeline call (moto S3)."""
    import subprocess as _sp
    # Generate a real age keypair
    keyfile = tmp_path / "id.key"
    r = _sp.run(["age-keygen", "-o", str(keyfile)], capture_output=True, text=True, check=True)
    recipient = next(
        line.removeprefix("# public key: ").strip()
        for line in r.stderr.splitlines()
        if line.startswith("# public key: ")
    )
    recipient_file = tmp_path / "age-recipient.txt"
    recipient_file.write_text(recipient + "\n")

    # Write a minimal controller.toml
    toml = tmp_path / "controller.toml"
    toml.write_text(
        "[node]\nrole = \"active\"\nhostname = \"test\"\n"
        "[backup]\nfloor_interval_hours = 24\non_change_debounce_seconds = 30\n"
        "endpoint = \"\"\nbucket = \"mthydra-test\"\naccess_key_id = \"x\"\n"
        "[backup.retention]\nkeep_daily = 30\nkeep_monthly = 12\nobject_lock_days = 30\n"
        "[gap_monitor]\npoll_interval_minutes = 30\nalarm_threshold_hours = 48\n"
        "recipient_email = \"op@example.org\"\n"
    )

    db = tmp_path / "state.sqlite"
    init_rc = run([
        "init", "--db-path", str(db),
        "--age-recipient-file", str(recipient_file),
        "--provider-credential", "b2=secret",
    ])
    assert init_rc == 0

    tmp_dir = tmp_path / "bak"
    tmp_dir.mkdir()
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="mthydra-test")
        client.put_bucket_versioning(
            Bucket="mthydra-test", VersioningConfiguration={"Status": "Enabled"}
        )
        client.put_object_lock_configuration(
            Bucket="mthydra-test",
            ObjectLockConfiguration={
                "ObjectLockEnabled": "Enabled",
                "Rule": {"DefaultRetention": {"Mode": "COMPLIANCE", "Days": 365}},
            },
        )
        # Patch DEFAULT_RECIPIENT_FILE and S3Destination._client
        with patch("mthydra.controller.cli.DEFAULT_RECIPIENT_FILE", str(recipient_file)):
            from mthydra.controller.backup.s3_dest import S3Destination
            orig_init = S3Destination.__init__
            def patched_init(self, *a, **kw):
                orig_init(self, *a, **kw)
                self._client = client
            with patch.object(S3Destination, "__init__", patched_init):
                exit_code = run([
                    "backup-now",
                    "--db-path", str(db),
                    "--config", str(toml),
                    "--tmp-dir", str(tmp_dir),
                ])
        assert exit_code == 0
        # Verify blob appeared in S3
        objs = client.list_objects_v2(Bucket="mthydra-test").get("Contents", [])
        keys = [o["Key"] for o in objs]
        assert any(k.startswith("gen-") for k in keys)


def test_obligation_proven_updates_clock(tmp_path):
    db = tmp_path / "state.sqlite"
    recipient_file = tmp_path / "age-recipient.txt"
    recipient_file.write_text(FAKE_RECIPIENT + "\n")
    # First init to create the DB with obligation rows
    init_rc = run([
        "init",
        "--db-path", str(db),
        "--age-recipient-file", str(recipient_file),
        "--provider-credential", "aws=x",
    ])
    assert init_rc == 0

    exit_code = run([
        "obligation-proven", "backup_restore_dryrun",
        "--db-path", str(db),
        "--details", "dry-run gen-1 → vm-test at 2026-05-18T00:00:00Z",
    ])
    assert exit_code == 0


def test_obligation_proven_fails_for_unknown_id(tmp_path):
    db = tmp_path / "state.sqlite"
    recipient_file = tmp_path / "age-recipient.txt"
    recipient_file.write_text(FAKE_RECIPIENT + "\n")
    run(["init", "--db-path", str(db), "--age-recipient-file", str(recipient_file)])
    exit_code = run([
        "obligation-proven", "nonexistent_obligation",
        "--db-path", str(db),
    ])
    assert exit_code != 0


def test_obligation_proven_cadence_from_config(tmp_path):
    """obligation-proven should read cadence from controller.toml, not hardcode 720h."""
    from mthydra.controller.state.obligations import list_obligations
    db = tmp_path / "state.sqlite"
    recipient_file = tmp_path / "age-recipient.txt"
    recipient_file.write_text(FAKE_RECIPIENT + "\n")
    # Use t1_dormant_health which has a 168h cadence in the spec
    run(["init", "--db-path", str(db), "--age-recipient-file", str(recipient_file)])

    toml = tmp_path / "controller.toml"
    toml.write_text(
        "[node]\nrole = \"active\"\nhostname = \"test\"\n"
        "[backup]\nfloor_interval_hours = 24\non_change_debounce_seconds = 30\n"
        "endpoint = \"\"\nbucket = \"b\"\naccess_key_id = \"x\"\n"
        "[backup.retention]\nkeep_daily = 30\nkeep_monthly = 12\nobject_lock_days = 30\n"
        "[gap_monitor]\npoll_interval_minutes = 30\nalarm_threshold_hours = 48\n"
        "recipient_email = \"op@example.org\"\n"
        "[obligations.timers_hours]\nt1_dormant_health = 168\n"
    )

    exit_code = run([
        "obligation-proven", "t1_dormant_health",
        "--db-path", str(db),
        "--config", str(toml),
    ])
    assert exit_code == 0
    conn = connect(db)
    obs = {o.obligation_id: o for o in list_obligations(conn)}
    # next_due_at should be last_proven_at + 168h, not + 720h
    from datetime import datetime, timedelta, timezone
    proven = datetime.fromisoformat(obs["t1_dormant_health"].last_proven_at.replace("Z", "+00:00"))
    due = datetime.fromisoformat(obs["t1_dormant_health"].next_due_at.replace("Z", "+00:00"))
    delta_hours = (due - proven).total_seconds() / 3600
    assert abs(delta_hours - 168) < 1, f"expected ~168h, got {delta_hours}h"


def test_rotate_provider_credential_updates_db(tmp_path):
    from mthydra.controller.state.tokens import get_provider_credential
    db = tmp_path / "state.sqlite"
    recipient_file = tmp_path / "age-recipient.txt"
    recipient_file.write_text(FAKE_RECIPIENT + "\n")
    run(["init", "--db-path", str(db), "--age-recipient-file", str(recipient_file),
         "--provider-credential", "b2=old_secret"])
    exit_code = run([
        "rotate-provider-credential", "b2",
        "--db-path", str(db),
        "--credential", "new_secret",
    ])
    assert exit_code == 0
    conn = connect(db)
    assert get_provider_credential(conn, "b2") == "new_secret"


def test_rotate_provider_credential_writes_audit_row(tmp_path):
    from mthydra.controller.state.audit import recent_events
    db = tmp_path / "state.sqlite"
    recipient_file = tmp_path / "age-recipient.txt"
    recipient_file.write_text(FAKE_RECIPIENT + "\n")
    run(["init", "--db-path", str(db), "--age-recipient-file", str(recipient_file),
         "--provider-credential", "b2=old_secret"])
    run(["rotate-provider-credential", "b2", "--db-path", str(db), "--credential", "new"])
    conn = connect(db)
    events = [e for e in recent_events(conn) if e.action == "rotate_provider_credential"]
    assert len(events) == 1
    assert events[0].target == "b2"


def test_dryrun_mode_without_bucket_override_fails():
    exit_code = run([
        "--mode", "dryrun",
        "startup-check",
        "--db-path", "/nonexistent/state.sqlite",
        "--age-recipient", FAKE_RECIPIENT,
    ])
    assert exit_code != 0


def test_build_destination_uses_override_bucket_in_dryrun(tmp_path):
    """In dryrun mode, _build_destination must point at bucket_override, not cfg.backup.bucket."""
    from mthydra.controller.cli import _build_destination
    from mthydra.controller.config import (
        BackupConfig, Config, CoverPoolConfig, DescriptorConfig, GapMonitorConfig,
        NodeConfig, ObligationsConfig, RetentionConfig,
    )

    cfg = Config(
        node=NodeConfig(role="active", hostname="h"),
        backup=BackupConfig(
            floor_interval_hours=24,
            on_change_debounce_seconds=30,
            endpoint="",
            bucket="prod-bucket",
            access_key_id="id",
            retention=RetentionConfig(keep_daily=30, keep_monthly=12, object_lock_days=365),
        ),
        gap_monitor=GapMonitorConfig(
            poll_interval_minutes=30, alarm_threshold_hours=48, recipient_email="op@example.org"
        ),
        obligations=ObligationsConfig(),
        descriptor=DescriptorConfig(rotation_interval_hours=1, validity_window_hours=24),
        cover_pool=CoverPoolConfig(
            rotation_ttl_days=14,
            reverify_after_days=30,
            freeze_threshold=2,
            reverify_sweep_interval_seconds=3600,
            rotation_sweep_interval_seconds=3600,
            replenishment_interval_days=90,
        ),
    )

    dest_prod = _build_destination(cfg, "secret", mode="production", bucket_override="override-bucket")
    assert dest_prod.bucket == "prod-bucket"

    dest_dry = _build_destination(cfg, "secret", mode="dryrun", bucket_override="override-bucket")
    assert dest_dry.bucket == "override-bucket"

    dest_dry_no_override = _build_destination(cfg, "secret", mode="dryrun", bucket_override=None)
    assert dest_dry_no_override.bucket == "prod-bucket"


# ---------------------------------------------------------------------------
# Spec B CLI subcommands
# ---------------------------------------------------------------------------

def _init_db(tmp_path):
    """Helper: init a DB and return (db_path, toml_path)."""
    db = tmp_path / "state.sqlite"
    recipient_file = tmp_path / "age-recipient.txt"
    recipient_file.write_text(FAKE_RECIPIENT + "\n")
    toml = tmp_path / "controller.toml"
    toml.write_text(
        "[node]\nrole = \"active\"\nhostname = \"test\"\n"
        "[backup]\nfloor_interval_hours = 24\non_change_debounce_seconds = 30\n"
        "endpoint = \"\"\nbucket = \"b\"\naccess_key_id = \"x\"\n"
        "[backup.retention]\nkeep_daily = 30\nkeep_monthly = 12\nobject_lock_days = 30\n"
        "[gap_monitor]\npoll_interval_minutes = 30\nalarm_threshold_hours = 48\n"
        "recipient_email = \"op@example.org\"\n"
        "[descriptor]\nrotation_interval_hours = 1\nvalidity_window_hours = 24\n"
    )
    rc = run(["init", "--db-path", str(db), "--age-recipient-file", str(recipient_file)])
    assert rc == 0
    return db, toml


def test_descriptor_sign_now_creates_generation(tmp_path):
    db, toml = _init_db(tmp_path)
    rc = run(["descriptor-sign-now", "--db-path", str(db), "--config", str(toml)])
    assert rc == 0


def test_descriptor_show_empty_db_returns_nonzero(tmp_path):
    db, toml = _init_db(tmp_path)
    rc = run(["descriptor-show", "--db-path", str(db)])
    assert rc != 0  # no descriptors yet


def test_descriptor_show_after_sign(tmp_path):
    db, toml = _init_db(tmp_path)
    run(["descriptor-sign-now", "--db-path", str(db), "--config", str(toml)])
    rc = run(["descriptor-show", "--db-path", str(db)])
    assert rc == 0


def test_descriptor_verify_on_fresh_signed(tmp_path):
    db, toml = _init_db(tmp_path)
    run(["descriptor-sign-now", "--db-path", str(db), "--config", str(toml)])
    # Extract payload and sig from DB
    conn = connect(db)
    row = conn.execute(
        "SELECT payload, signature FROM descriptor_history ORDER BY generation DESC LIMIT 1"
    ).fetchone()
    payload_file = tmp_path / "payload.json"
    sig_file = tmp_path / "sig.bin"
    payload_file.write_bytes(row[0].encode("utf-8"))
    sig_file.write_bytes(bytes(row[1]))
    rc = run([
        "descriptor-verify", str(payload_file), str(sig_file),
        "--db-path", str(db),
    ])
    assert rc == 0


def test_eu_add_and_retire(tmp_path):
    db, toml = _init_db(tmp_path)
    rc_add = run([
        "eu-add", "aabbcc", "eu1.example.org:443",
        "--db-path", str(db), "--config", str(toml),
    ])
    assert rc_add == 0
    # Verify exit appears in latest descriptor
    conn = connect(db)
    row = conn.execute(
        "SELECT payload FROM descriptor_history ORDER BY generation DESC LIMIT 1"
    ).fetchone()
    import json
    p = json.loads(row[0])
    assert any(e["fingerprint"] == "aabbcc" for e in p["eu_exit_set"])

    rc_retire = run([
        "eu-retire", "aabbcc",
        "--db-path", str(db), "--config", str(toml),
    ])
    assert rc_retire == 0
    row2 = conn.execute(
        "SELECT payload FROM descriptor_history ORDER BY generation DESC LIMIT 1"
    ).fetchone()
    p2 = json.loads(row2[0])
    assert not any(e["fingerprint"] == "aabbcc" for e in p2["eu_exit_set"])


def test_signing_key_rotate(tmp_path):
    db, toml = _init_db(tmp_path)
    rc = run(["signing-key-rotate", "--db-path", str(db), "--config", str(toml)])
    assert rc == 0
    conn = connect(db)
    rows = conn.execute("SELECT generation FROM descriptor_signing_key ORDER BY generation").fetchall()
    assert len(rows) == 2  # original + new


def test_descriptor_migrate_placeholder_on_real_key(tmp_path):
    """On a post-spec-B DB, migration is a no-op."""
    db, toml = _init_db(tmp_path)  # init now uses real Ed25519
    rc = run(["descriptor-migrate-placeholder", "--db-path", str(db), "--config", str(toml)])
    assert rc == 0  # prints "nothing to do"


def test_descriptor_migrate_placeholder_on_placeholder_key(tmp_path):
    """On a spec-A legacy DB, migration mints a real key and signs."""
    db, toml = _init_db(tmp_path)
    # Manually replace the signing key with a placeholder to simulate spec-A state
    conn = connect(db)
    conn.execute("UPDATE descriptor_signing_key SET privkey=?, pubkey=? WHERE generation=1",
                 (b"PRIV-DESC-" + b"\x00" * 22, b"PUB-DESC-" + b"\x00" * 23))
    conn.commit()
    conn.close()
    rc = run(["descriptor-migrate-placeholder", "--db-path", str(db), "--config", str(toml)])
    assert rc == 0
    conn = connect(db)
    rows = conn.execute("SELECT generation FROM descriptor_signing_key ORDER BY generation").fetchall()
    assert len(rows) == 2  # placeholder (retired) + new real key


def test_init_seeds_cover_pool_obligations_via_cli(tmp_path, age_recipient):
    db = tmp_path / "state.sqlite"
    rc = run([
        "init",
        "--db-path", str(db),
        "--age-recipient", age_recipient,
        "--provider-credential", "b2=id:secret",
    ])
    assert rc == 0
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.obligations import list_obligations
    conn = connect(db)
    ids = {o.obligation_id for o in list_obligations(conn)}
    assert "cover_pool_reverify_pass_proven" in ids
    assert "cover_pool_replenishment_proven" in ids


# ===== Task 14: cover-add =====

def test_cover_add_creates_unverified_row(tmp_path, age_recipient):
    from mthydra.controller.cli import run
    db = tmp_path / "state.sqlite"
    run([
        "init", "--db-path", str(db),
        "--age-recipient", age_recipient,
        "--provider-credential", "b2=id:secret",
    ])
    rc = run(["cover-add", "fresh.org", "--db-path", str(db),
              "--notes", "fall-2026 batch"])
    assert rc == 0
    from mthydra.controller.state.cover_pool import list_by_state
    from mthydra.controller.state.db import connect
    conn = connect(db)
    rows = list_by_state(conn, "candidate_unverified")
    assert [r.domain for r in rows] == ["fresh.org"]
    assert rows[0].notes == "fall-2026 batch"


def test_cover_add_proves_replenishment_obligation(tmp_path, age_recipient):
    from mthydra.controller.cli import run
    db = tmp_path / "state.sqlite"
    run([
        "init", "--db-path", str(db),
        "--age-recipient", age_recipient,
        "--provider-credential", "b2=id:secret",
    ])
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.obligations import list_obligations, set_obligation
    conn = connect(db)
    set_obligation(conn, "cover_pool_replenishment_proven",
                   last_proven_at="2025-01-01T00:00:00Z",
                   proven_by="bootstrap",
                   next_due_at="2025-04-01T00:00:00Z")
    conn.close()
    run(["cover-add", "fresh.org", "--db-path", str(db)])
    conn = connect(db)
    obs = {o.obligation_id: o for o in list_obligations(conn)}
    assert obs["cover_pool_replenishment_proven"].last_proven_at > "2025-01-01T00:00:00Z"
    conn.close()


def test_cover_add_refuses_burned(tmp_path, age_recipient, capsys):
    from mthydra.controller.cli import run
    db = tmp_path / "state.sqlite"
    run([
        "init", "--db-path", str(db),
        "--age-recipient", age_recipient,
        "--provider-credential", "b2=id:secret",
    ])
    from mthydra.controller.state.db import connect
    conn = connect(db)
    conn.execute(
        "INSERT INTO burned_domains (domain, burned_at, reason) "
        "VALUES ('burned.org', '2026-05-19T00:00:00Z', 'manual')"
    )
    conn.commit()
    conn.close()
    rc = run(["cover-add", "burned.org", "--db-path", str(db)])
    assert rc == 2
    err = capsys.readouterr().err
    assert "burned_domains" in err
