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
        BackupConfig, Config, GapMonitorConfig, NodeConfig, ObligationsConfig, RetentionConfig
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
    )

    dest_prod = _build_destination(cfg, "secret", mode="production", bucket_override="override-bucket")
    assert dest_prod.bucket == "prod-bucket"

    dest_dry = _build_destination(cfg, "secret", mode="dryrun", bucket_override="override-bucket")
    assert dest_dry.bucket == "override-bucket"

    dest_dry_no_override = _build_destination(cfg, "secret", mode="dryrun", bucket_override=None)
    assert dest_dry_no_override.bucket == "prod-bucket"
