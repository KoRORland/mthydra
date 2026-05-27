"""Tests for the controller CLI (spec A phase 6)."""
import json
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
[data_exit]
listen_port = 443
sing_box_socket = "/run/mthydra/sing-box.sock"
config_path = "/etc/mthydra/sing-box.json"
reality_key_path = "/etc/mthydra/reality.key"
[data_exit.telegram_dcs]
v4 = ["149.154.160.0/20"]
v6 = ["2001:b28:f23d::/48"]
[data_exit.cover_sni]
default = "www.example-cover-domain.invalid"
[observability.telegram]
bot_token = "test-token"
chat_id = "12345"
[observability.email]
smtp_host = "smtp.example.org"
smtp_port = 587
from_addr = "ops@example.org"
to_addr = "op@example.org"
username = "ops@example.org"
password = "app-pw"
[distribution.telegram]
bot_token = "dist-token"
[distribution.email]
smtp_host = "smtp.example.org"
smtp_port = 587
from_addr = "dist@example.org"
username = "dist@example.org"
password = "app-pw"
"""

_PROVISION_V2_ARGS = [
    "--agent-source-url", "https://b2.example/agent/v0.1.0.tar.gz",
    "--agent-source-sha256", "deadbeef" * 8,
    "--descriptor-refresh-url", "https://b2.example/descriptors/current",
]


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


def test_init_provider_credential_env_reads_secret_from_environment(tmp_path, monkeypatch):
    """--provider-credential-env keeps the secret off argv (H3)."""
    from mthydra.controller.state.tokens import get_provider_credential
    recipient_file = tmp_path / "age-recipient.txt"
    recipient_file.write_text(FAKE_RECIPIENT + "\n")
    db = tmp_path / "state.sqlite"
    monkeypatch.setenv("MY_B2_CRED", "ID:SECRET-FROM-ENV")
    exit_code = run([
        "init",
        "--db-path", str(db),
        "--age-recipient-file", str(recipient_file),
        "--provider-credential-env", "b2=MY_B2_CRED",
    ])
    assert exit_code == 0
    conn = connect(db)
    assert get_provider_credential(conn, "b2") == "ID:SECRET-FROM-ENV"


def test_init_provider_credential_env_missing_var_errors(tmp_path, monkeypatch):
    recipient_file = tmp_path / "age-recipient.txt"
    recipient_file.write_text(FAKE_RECIPIENT + "\n")
    db = tmp_path / "state.sqlite"
    monkeypatch.delenv("NOPE_VAR", raising=False)
    exit_code = run([
        "init",
        "--db-path", str(db),
        "--age-recipient-file", str(recipient_file),
        "--provider-credential-env", "b2=NOPE_VAR",
    ])
    assert exit_code != 0
    assert not db.exists()


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
        for line in keyfile.read_text().splitlines()
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
        BackupConfig, Config, CoverPoolConfig, DescriptorConfig,
        DistributionConfig, GapMonitorConfig, ImageCanaryConfig, ImageConfig,
        NodeConfig, ObligationsConfig, ObservabilityConfig, ProbeConfig,
        RetentionConfig, ShardManagerConfig, StandbyConfig,
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
        standby=StandbyConfig(
            node_id="",
            heartbeat_interval_seconds=60,
            heartbeat_poll_interval_seconds=300,
            staleness_alert_seconds=600,
        ),
        image=ImageConfig(
            upstream_repo="9seconds/mtg",
            upstream_release_asset="mtg-linux-amd64",
            upstream_check_interval_seconds=168 * 3600,
            github_api_url="https://api.github.com",
            build_tmp_dir="/var/lib/mthydra/tmp",
            canary=ImageCanaryConfig(min_boxes=1, min_cycles_per_box=4),
        ),
        shard_manager=ShardManagerConfig(
            target_size=2,
            max_size=3,
            reshuffle_interval_days=14,
            reshuffle_sweep_interval_seconds=3600,
        ),
        probe=ProbeConfig(
            soft_fail_window_M=4,
            soft_fail_threshold_N=3,
            min_distinct_vantages=2,
            coverage_window_seconds=3600,
            probe_vantage_ttl_days=14,
            probe_audit_sweep_interval_seconds=300,
        ),
        observability=ObservabilityConfig(
            alerter_sweep_interval_seconds=120,
            heartbeat_interval_seconds=3600,
            heartbeat_breach_threshold=3,
            alert_dedupe_window_warn_seconds=3600,
            alert_dedupe_window_crit_seconds=900,
            alert_dedupe_window_info_seconds=21600,
            telegram=None,
            email=None,
        ),
        distribution=DistributionConfig(
            publish_sweep_interval_seconds=300,
            user_heartbeat_interval_seconds=86400,
            heartbeat_breach_threshold=3,
            telegram=None,
            email=None,
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


# ===== Task 15: cover-attest-verified =====

def test_cover_attest_verified_transitions_state(tmp_path, age_recipient):
    from mthydra.controller.cli import run
    db = tmp_path / "state.sqlite"
    run([
        "init", "--db-path", str(db),
        "--age-recipient", age_recipient,
        "--provider-credential", "b2=id:secret",
    ])
    run(["cover-add", "fresh.org", "--db-path", str(db)])
    rc = run([
        "cover-attest-verified", "fresh.org",
        "--vantage", "ru-vps-01",
        "--evidence", "curl + cert match",
        "--db-path", str(db),
    ])
    assert rc == 0
    from mthydra.controller.state.cover_pool import list_by_state
    from mthydra.controller.state.db import connect
    conn = connect(db)
    rows = list_by_state(conn, "candidate_verified")
    assert rows[0].verified_from_vantage == "ru-vps-01"


def test_cover_attest_verified_proves_reverify_pass_obligation(tmp_path, age_recipient):
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
    set_obligation(conn, "cover_pool_reverify_pass_proven",
                   last_proven_at="2025-01-01T00:00:00Z",
                   proven_by="bootstrap",
                   next_due_at="2025-03-01T00:00:00Z")
    conn.close()
    run(["cover-add", "fresh.org", "--db-path", str(db)])
    run([
        "cover-attest-verified", "fresh.org",
        "--vantage", "ru-vps-01",
        "--db-path", str(db),
    ])
    conn = connect(db)
    obs = {o.obligation_id: o for o in list_obligations(conn)}
    assert obs["cover_pool_reverify_pass_proven"].last_proven_at > "2025-01-01T00:00:00Z"
    conn.close()


def test_cover_attest_verified_rejects_missing_domain(tmp_path, age_recipient, capsys):
    from mthydra.controller.cli import run
    db = tmp_path / "state.sqlite"
    run([
        "init", "--db-path", str(db),
        "--age-recipient", age_recipient,
        "--provider-credential", "b2=id:secret",
    ])
    rc = run([
        "cover-attest-verified", "ghost.org",
        "--vantage", "ru-vps-01",
        "--db-path", str(db),
    ])
    assert rc == 2


# ===== Task 16: cover-list =====

def test_cover_list_default_shows_all_states(tmp_path, age_recipient, capsys):
    from mthydra.controller.cli import run
    db = tmp_path / "state.sqlite"
    run([
        "init", "--db-path", str(db),
        "--age-recipient", age_recipient,
        "--provider-credential", "b2=id:secret",
    ])
    run(["cover-add", "a.org", "--db-path", str(db)])
    run(["cover-add", "b.org", "--db-path", str(db)])
    capsys.readouterr()
    rc = run(["cover-list", "--db-path", str(db)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "a.org" in out
    assert "b.org" in out


def test_cover_list_json_output_schema(tmp_path, age_recipient, capsys):
    import json
    from mthydra.controller.cli import run
    db = tmp_path / "state.sqlite"
    run([
        "init", "--db-path", str(db),
        "--age-recipient", age_recipient,
        "--provider-credential", "b2=id:secret",
    ])
    run(["cover-add", "a.org", "--db-path", str(db)])
    capsys.readouterr()
    rc = run(["cover-list", "--db-path", str(db), "--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert isinstance(data, list)
    assert any(row["domain"] == "a.org" for row in data)
    assert all(set(row.keys()) >= {"domain", "state", "added_at"} for row in data)


# ===== Task 17: cover-rotate =====

def test_cover_rotate_burns_in_use_domain(tmp_path, age_recipient):
    from mthydra.controller.cli import run
    db = tmp_path / "state.sqlite"
    run([
        "init", "--db-path", str(db),
        "--age-recipient", age_recipient,
        "--provider-credential", "b2=id:secret",
    ])
    from mthydra.controller.state.cover_pool import (
        add_candidate, assign_to_box, attest_verified,
    )
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.ru_boxes import insert_box, mark_live
    conn = connect(db)
    insert_box(conn, "box-1", "aws", "eu-west-1", "10.0.0.1", "sni.invalid",
               "img-v1", "2026-05-19T00:00:00Z")
    mark_live(conn, "box-1", public_ip="10.0.0.1", at="2026-05-19T00:00:00Z")
    add_candidate(conn, "rot.org", added_at="2026-05-19T00:00:00Z")
    attest_verified(conn, "rot.org", from_vantage="ru-vps-01", at="2026-05-19T01:00:00Z")
    assign_to_box(conn, "rot.org", box_id="box-1", at="2026-05-19T02:00:00Z")
    conn.close()
    rc = run([
        "cover-rotate", "rot.org",
        "--reason", "manual_rotate",
        "--db-path", str(db),
    ])
    assert rc == 0
    from mthydra.controller.state.burned import is_burned
    conn = connect(db)
    assert is_burned(conn, "rot.org")


def test_cover_rotate_refuses_non_in_use(tmp_path, age_recipient, capsys):
    from mthydra.controller.cli import run
    db = tmp_path / "state.sqlite"
    run([
        "init", "--db-path", str(db),
        "--age-recipient", age_recipient,
        "--provider-credential", "b2=id:secret",
    ])
    run(["cover-add", "newborn.org", "--db-path", str(db)])
    rc = run(["cover-rotate", "newborn.org", "--db-path", str(db)])
    assert rc == 2
    err = capsys.readouterr().err
    assert "is not in_use" in err


# ===== Task 18: cover-due =====

def test_cover_due_lists_overdue_and_stale(tmp_path, age_recipient, capsys):
    import json

    from mthydra.controller.cli import run
    db = tmp_path / "state.sqlite"
    run([
        "init", "--db-path", str(db),
        "--age-recipient", age_recipient,
        "--provider-credential", "b2=id:secret",
    ])

    from mthydra.controller.state.cover_pool import (
        add_candidate, assign_to_box, attest_verified,
    )
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.ru_boxes import insert_box, mark_live
    conn = connect(db)
    old = "2026-04-01T00:00:00Z"
    insert_box(conn, "box-1", "aws", "eu-west-1", "10.0.0.1", "sni.invalid", "img-v1", old)
    mark_live(conn, "box-1", public_ip="10.0.0.1", at=old)
    add_candidate(conn, "old.org", added_at=old)
    attest_verified(conn, "old.org", from_vantage="ru-vps-01", at=old)
    assign_to_box(conn, "old.org", box_id="box-1", at=old)
    conn.close()

    cfg_path = tmp_path / "controller.toml"
    cfg_path.write_text(_MIN_TOML)

    capsys.readouterr()
    rc = run([
        "cover-due", "--db-path", str(db),
        "--config", str(cfg_path),
        "--json",
    ])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert "due_for_rotation" in out
    assert any(r["domain"] == "old.org" for r in out["due_for_rotation"])
    assert "pool_health" in out


# ===== Task 19: cover-pool-stats =====

def test_cover_pool_stats_json(tmp_path, age_recipient, capsys):
    import json
    from mthydra.controller.cli import run
    db = tmp_path / "state.sqlite"
    cfg_path = tmp_path / "controller.toml"
    cfg_path.write_text(_MIN_TOML)
    run([
        "init", "--db-path", str(db),
        "--age-recipient", age_recipient,
        "--provider-credential", "b2=id:secret",
    ])
    run(["cover-add", "a.org", "--db-path", str(db)])
    capsys.readouterr()
    rc = run([
        "cover-pool-stats", "--db-path", str(db),
        "--config", str(cfg_path),
        "--json",
    ])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["candidate_unverified"] == 1
    assert payload["candidate_verified"] == 0
    assert payload["in_use"] == 0
    assert "rotation_frozen" in payload


# ===== Task 10: authority-rotate =====

def test_authority_rotate_adds_new_generation(tmp_path, age_recipient):
    from mthydra.controller.cli import run
    db = tmp_path / "state.sqlite"
    cfg_path = tmp_path / "controller.toml"
    cfg_path.write_text(_MIN_TOML)
    run(["init", "--db-path", str(db),
         "--age-recipient", age_recipient,
         "--provider-credential", "b2=id:secret"])
    rc = run(["authority-rotate", "--db-path", str(db), "--config", str(cfg_path)])
    assert rc == 0
    from mthydra.controller.state.authority import list_authorities
    from mthydra.controller.state.db import connect
    conn = connect(db)
    auths = list_authorities(conn)
    assert len(auths) == 2
    assert sum(1 for a in auths if a.retired_at is None) == 1


def test_authority_rotate_refuses_on_standby(tmp_path, age_recipient, capsys):
    from mthydra.controller.cli import run
    db = tmp_path / "state.sqlite"
    cfg_path = tmp_path / "controller.toml"
    cfg_path.write_text(_MIN_TOML)
    run(["init", "--role", "standby", "--db-path", str(db),
         "--age-recipient", age_recipient,
         "--provider-credential", "b2=id:secret"])
    rc = run(["authority-rotate", "--db-path", str(db), "--config", str(cfg_path)])
    assert rc == 2
    err = capsys.readouterr().err.lower()
    assert "active" in err or "standby" in err


def test_eu_node_add_default_standby(tmp_path, age_recipient):
    from mthydra.controller.cli import run
    db = tmp_path / "state.sqlite"
    run(["init", "--db-path", str(db),
         "--age-recipient", age_recipient,
         "--provider-credential", "b2=id:secret"])
    rc = run(["eu-node-add", "eu-standby-de-1",
              "--hostname", "standby.example",
              "--provider", "hetzner",
              "--region", "de",
              "--db-path", str(db)])
    assert rc == 0
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.eu_nodes import get_eu_node
    conn = connect(db)
    n = get_eu_node(conn, "eu-standby-de-1")
    assert n.role == "standby"


def test_eu_node_add_refuses_second_active(tmp_path, age_recipient, capsys):
    from mthydra.controller.cli import run
    db = tmp_path / "state.sqlite"
    run(["init", "--db-path", str(db),
         "--age-recipient", age_recipient,
         "--provider-credential", "b2=id:secret"])
    run(["eu-node-add", "eu-active-1", "--hostname", "h",
         "--provider", "aws", "--region", "fr",
         "--role", "active", "--db-path", str(db)])
    rc = run(["eu-node-add", "eu-active-2", "--hostname", "h",
              "--provider", "aws", "--region", "fr",
              "--role", "active", "--db-path", str(db)])
    assert rc == 2
    assert "only one active" in capsys.readouterr().err


def test_eu_node_retire_happy(tmp_path, age_recipient):
    from mthydra.controller.cli import run
    db = tmp_path / "state.sqlite"
    run(["init", "--db-path", str(db),
         "--age-recipient", age_recipient,
         "--provider-credential", "b2=id:secret"])
    run(["eu-node-add", "eu-standby-de-1", "--hostname", "h",
         "--provider", "hetzner", "--region", "de", "--db-path", str(db)])
    rc = run(["eu-node-retire", "eu-standby-de-1", "--db-path", str(db)])
    assert rc == 0
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.eu_nodes import get_eu_node
    conn = connect(db)
    n = get_eu_node(conn, "eu-standby-de-1")
    assert n.role == "retired"


def test_eu_node_list_json(tmp_path, age_recipient, capsys):
    import json
    from mthydra.controller.cli import run
    db = tmp_path / "state.sqlite"
    run(["init", "--db-path", str(db),
         "--age-recipient", age_recipient,
         "--provider-credential", "b2=id:secret"])
    run(["eu-node-add", "eu-standby-de-1", "--hostname", "h",
         "--provider", "hetzner", "--region", "de", "--db-path", str(db)])
    capsys.readouterr()
    rc = run(["eu-node-list", "--db-path", str(db), "--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert any(r["node_id"] == "eu-standby-de-1" for r in data)


def test_eu_node_add_refused_on_standby(tmp_path, age_recipient, capsys):
    from mthydra.controller.cli import run
    db = tmp_path / "state.sqlite"
    run(["init", "--role", "standby", "--db-path", str(db),
         "--age-recipient", age_recipient,
         "--provider-credential", "b2=id:secret"])
    rc = run(["eu-node-add", "eu-anything", "--hostname", "h",
              "--provider", "p", "--region", "r", "--db-path", str(db)])
    assert rc == 2
    assert "active" in capsys.readouterr().err.lower()


def test_serve_arms_cover_pool_sweeps_in_offline_mode(tmp_path, age_recipient, monkeypatch):
    """Smoke: serve with --mode offline arms the sweeps as no-ops and returns 0 quickly."""
    import signal
    import pathlib
    from mthydra.controller.cli import run
    db = tmp_path / "state.sqlite"
    cfg_path = tmp_path / "controller.toml"
    cfg_path.write_text(_MIN_TOML)
    # Write a recipient file at the location _cmd_serve reads from
    recipient_file = tmp_path / "age-recipient.txt"
    recipient_file.write_text(age_recipient + "\n")
    monkeypatch.setattr("mthydra.controller.cli.DEFAULT_RECIPIENT_FILE", str(recipient_file))

    run([
        "init", "--db-path", str(db),
        "--age-recipient", age_recipient,
        "--provider-credential", "b2=id:secret",
    ])

    # _cmd_serve tries to mkdir /var/lib/mthydra/tmp which requires root.
    # Redirect that hardcoded path to tmp_path.
    _real_mkdir = pathlib.Path.mkdir
    _serve_tmp = tmp_path / "serve_tmp"

    def _patched_mkdir(self, mode=0o777, parents=False, exist_ok=False):
        if str(self) == "/var/lib/mthydra/tmp":
            _serve_tmp.mkdir(parents=True, exist_ok=True)
            return
        _real_mkdir(self, mode=mode, parents=parents, exist_ok=exist_ok)

    monkeypatch.setattr(pathlib.Path, "mkdir", _patched_mkdir)

    # Cause stop_event to fire on the first iteration so the daemon exits.
    # _cmd_serve does `stop_event.wait(timeout=60)` after arming — we patch
    # threading.Event.wait to return True (event "set") immediately.
    import threading as _t
    def _fast_wait(self, timeout=None):
        self.set()
        return True
    monkeypatch.setattr(_t.Event, "wait", _fast_wait)

    rc = run([
        "--mode", "offline",
        "--bucket-override", "off-bucket",
        "serve",
        "--db-path", str(db),
        "--config", str(cfg_path),
    ])
    assert rc == 0


# ===== Task 13: standby-drill-proven =====

def test_standby_drill_proven_case_a_proves_both_obligations(tmp_path, age_recipient):
    from mthydra.controller.cli import run
    db = tmp_path / "state.sqlite"
    run(["init", "--db-path", str(db),
         "--age-recipient", age_recipient,
         "--provider-credential", "b2=id:secret"])
    run(["eu-node-add", "eu-standby-de-1", "--hostname", "h",
         "--provider", "hetzner", "--region", "de", "--db-path", str(db)])

    from mthydra.controller.state.db import connect
    from mthydra.controller.state.obligations import list_obligations, set_obligation
    conn = connect(db)
    set_obligation(conn, "t2_dryrun_caseA",
                   last_proven_at="2025-01-01T00:00:00Z",
                   proven_by="bootstrap",
                   next_due_at="2025-02-01T00:00:00Z")
    conn.close()

    rc = run(["standby-drill-proven", "--node-id", "eu-standby-de-1",
              "--case", "A", "--notes", "test drill",
              "--db-path", str(db)])
    assert rc == 0
    conn = connect(db)
    obs = {o.obligation_id: o for o in list_obligations(conn)}
    assert obs["t2_dryrun_caseA"].last_proven_at > "2025-01-01T00:00:00Z"
    assert "eu_standby_drill_proven::eu-standby-de-1" in obs
    conn.close()


def test_standby_drill_proven_case_b_proves_caseB(tmp_path, age_recipient):
    from mthydra.controller.cli import run
    db = tmp_path / "state.sqlite"
    run(["init", "--db-path", str(db),
         "--age-recipient", age_recipient,
         "--provider-credential", "b2=id:secret"])
    run(["eu-node-add", "eu-standby-de-1", "--hostname", "h",
         "--provider", "hetzner", "--region", "de", "--db-path", str(db)])

    from mthydra.controller.state.db import connect
    from mthydra.controller.state.obligations import list_obligations, set_obligation
    conn = connect(db)
    # pin to a known-old timestamp so the drill's "now" is guaranteed newer
    set_obligation(conn, "t2_dryrun_caseB",
                   last_proven_at="2025-01-01T00:00:00Z",
                   proven_by="bootstrap",
                   next_due_at="2025-02-01T00:00:00Z")
    pre = next((o for o in list_obligations(conn) if o.obligation_id == "t2_dryrun_caseB"), None)
    pre_at = pre.last_proven_at if pre else None
    conn.close()

    run(["standby-drill-proven", "--node-id", "eu-standby-de-1",
         "--case", "B", "--db-path", str(db)])

    conn = connect(db)
    obs = {o.obligation_id: o for o in list_obligations(conn)}
    assert "t2_dryrun_caseB" in obs
    if pre_at is not None:
        assert obs["t2_dryrun_caseB"].last_proven_at > pre_at
    conn.close()


# ===== Task 14: role-gated serve =====

def test_serve_standby_arms_publisher_not_orchestrator(tmp_path, age_recipient, monkeypatch):
    """Standby serve loop: heartbeat publisher armed; backup/descriptor/cover-pool NOT."""
    import threading as _t
    from mthydra.controller.cli import run

    db = tmp_path / "state.sqlite"
    cfg_path = tmp_path / "controller.toml"
    # _MIN_TOML has no [standby] section — append it with node_id required for standby serve.
    cfg_path.write_text(
        _MIN_TOML
        + "\n[standby]\n"
        + "node_id = \"eu-standby-de-1\"\n"
        + "heartbeat_interval_seconds = 60\n"
        + "heartbeat_poll_interval = \"5m\"\n"
        + "staleness_alert_seconds = 600\n"
    )
    run(["init", "--role", "standby", "--db-path", str(db),
         "--age-recipient", age_recipient,
         "--provider-credential", "b2=id:secret"])

    recipient_file = tmp_path / "age-recipient.txt"
    recipient_file.write_text(age_recipient + "\n")
    monkeypatch.setattr("mthydra.controller.cli.DEFAULT_RECIPIENT_FILE",
                        str(recipient_file))

    def _fast_wait(self, timeout=None):
        self.set()
        return True
    monkeypatch.setattr(_t.Event, "wait", _fast_wait)

    rc = run([
        "--mode", "offline",
        "--bucket-override", "off-bucket",
        "serve",
        "--db-path", str(db),
        "--config", str(cfg_path),
    ])
    assert rc == 0


# ===== Task 8: spec D image subcommands =====

def test_image_build_happy_path(tmp_path, age_recipient, monkeypatch):
    """image-build delegates to build_image; happy path returns 0."""
    from mthydra.controller.cli import run
    db = tmp_path / "state.sqlite"
    cfg_path = tmp_path / "controller.toml"
    cfg_path.write_text(_MIN_TOML)
    run(["init", "--db-path", str(db),
         "--age-recipient", age_recipient,
         "--provider-credential", "b2=id:secret"])

    captured = {}
    def _stub_build_image(**kwargs):
        captured.update(kwargs)
        from mthydra.controller.state.ru_images import insert_candidate
        insert_candidate(
            kwargs["conn"],
            image_version="iv-stub",
            upstream_release=kwargs["upstream_release"],
            upstream_repo=kwargs["upstream_repo"],
            binary_url="images/iv-stub/mtg",
            manifest_url="images/iv-stub/manifest.json",
            binary_sha256="iv-stub",
            binary_size_bytes=100,
            built_at=kwargs["now"],
        )
        return "iv-stub"
    monkeypatch.setattr("mthydra.controller.cli.build_image", _stub_build_image)

    profile_path = tmp_path / "profile.json"
    profile_path.write_text('{"surface":":443"}')
    rc = run(["image-build", "--release", "v2.1.7",
              "--profile-json", str(profile_path),
              "--db-path", str(db), "--config", str(cfg_path)])
    assert rc == 0
    # Spec D2: profile pinned atomically with the image.
    from mthydra.controller.state.db import connect
    conn = connect(db)
    row = conn.execute(
        "SELECT profile_json FROM image_profiles WHERE image_version='iv-stub'"
    ).fetchone()
    assert row is not None and '"surface"' in row[0]
    conn.close()
    assert captured["upstream_release"] == "v2.1.7"
    assert captured["upstream_repo"] == "9seconds/mtg"


def test_image_build_refused_on_standby(tmp_path, age_recipient, capsys, monkeypatch):
    from mthydra.controller.cli import run
    db = tmp_path / "state.sqlite"
    cfg_path = tmp_path / "controller.toml"
    cfg_path.write_text(_MIN_TOML)
    run(["init", "--role", "standby", "--db-path", str(db),
         "--age-recipient", age_recipient,
         "--provider-credential", "b2=id:secret"])
    profile_path = tmp_path / "profile.json"
    profile_path.write_text('{}')
    rc = run(["image-build", "--release", "v2.1.7",
              "--profile-json", str(profile_path),
              "--db-path", str(db), "--config", str(cfg_path)])
    assert rc == 2
    assert "active-only" in capsys.readouterr().err.lower()


def test_image_build_refuses_without_profile_json(tmp_path, age_recipient, capsys):
    """Spec D2: --profile-json is mandatory."""
    from mthydra.controller.cli import run
    db = tmp_path / "state.sqlite"
    cfg_path = tmp_path / "controller.toml"
    cfg_path.write_text(_MIN_TOML)
    run(["init", "--db-path", str(db),
         "--age-recipient", age_recipient,
         "--provider-credential", "b2=id:secret"])
    # argparse exits 2 with SystemExit.
    import pytest as _pt
    with _pt.raises(SystemExit) as exc:
        run(["image-build", "--release", "v2.1.7",
             "--db-path", str(db), "--config", str(cfg_path)])
    assert exc.value.code == 2


def test_image_build_refuses_empty_profile_json(tmp_path, age_recipient, capsys, monkeypatch):
    from mthydra.controller.cli import run
    db = tmp_path / "state.sqlite"
    cfg_path = tmp_path / "controller.toml"
    cfg_path.write_text(_MIN_TOML)
    run(["init", "--db-path", str(db),
         "--age-recipient", age_recipient,
         "--provider-credential", "b2=id:secret"])
    profile_path = tmp_path / "empty.json"
    profile_path.write_text("   \n   ")
    rc = run(["image-build", "--release", "v2.1.7",
              "--profile-json", str(profile_path),
              "--db-path", str(db), "--config", str(cfg_path)])
    assert rc == 2
    assert "non-empty" in capsys.readouterr().err


def test_image_list_json(tmp_path, age_recipient, capsys):
    import json
    from mthydra.controller.cli import run
    db = tmp_path / "state.sqlite"
    cfg_path = tmp_path / "controller.toml"
    cfg_path.write_text(_MIN_TOML)
    run(["init", "--db-path", str(db),
         "--age-recipient", age_recipient,
         "--provider-credential", "b2=id:secret"])
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.ru_images import insert_candidate
    conn = connect(db)
    insert_candidate(
        conn, image_version="iv1",
        upstream_release="v2.1.7", upstream_repo="9seconds/mtg",
        binary_url="x", manifest_url="x", binary_sha256="iv1",
        binary_size_bytes=100, built_at="2026-05-21T00:00:00Z",
    )
    conn.close()
    capsys.readouterr()
    rc = run(["image-list", "--db-path", str(db), "--config", str(cfg_path), "--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert any(r["image_version"] == "iv1" for r in data)


def test_image_promote_requires_evidence(tmp_path, age_recipient):
    from mthydra.controller.cli import run
    db = tmp_path / "state.sqlite"
    run(["init", "--db-path", str(db),
         "--age-recipient", age_recipient,
         "--provider-credential", "b2=id:secret"])
    import pytest as _pt
    with _pt.raises(SystemExit) as exc:
        run(["image-promote", "iv1", "--db-path", str(db)])
    assert exc.value.code == 2


def test_image_promote_clears_upstream_release_obligation(tmp_path, age_recipient):
    from mthydra.controller.cli import run
    db = tmp_path / "state.sqlite"
    cfg_path = tmp_path / "controller.toml"
    cfg_path.write_text(_MIN_TOML)
    run(["init", "--db-path", str(db),
         "--age-recipient", age_recipient,
         "--provider-credential", "b2=id:secret"])
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.image_profiles import pin
    from mthydra.controller.state.obligations import list_obligations, set_obligation
    from mthydra.controller.state.probe_results import record
    from mthydra.controller.state.probe_vantages import add_candidate, attest_active
    from mthydra.controller.state.ru_boxes import insert_box, mark_live
    from mthydra.controller.state.ru_images import insert_candidate
    conn = connect(db)
    insert_candidate(
        conn, image_version="iv1", upstream_release="v2.1.7",
        upstream_repo="9seconds/mtg", binary_url="x", manifest_url="x",
        binary_sha256="iv1", binary_size_bytes=100, built_at="2026-05-21T00:00:00Z",
    )
    set_obligation(conn, "t4_upstream_release_available::v2.1.7",
                   last_proven_at="2026-05-21T00:00:00Z",
                   proven_by="tracker", next_due_at="2026-05-21T00:00:00Z")
    # Spec D2: satisfy the gate — pin profile, 1 canary, 4 cycles, 2 vantages.
    pin(conn, image_version="iv1", profile_json='{}',
        recorded_by="op", at="2026-05-21T00:00:00Z")
    add_candidate(conn, vantage_id="vk", label="kz1", source_kind="x",
                  at="2026-05-21T00:00:00Z")
    attest_active(conn, "vk", at="2026-05-21T00:00:00Z")
    add_candidate(conn, vantage_id="vb", label="by1", source_kind="x",
                  at="2026-05-21T00:00:00Z")
    attest_active(conn, "vb", at="2026-05-21T00:00:00Z")
    insert_box(conn, "b-canary", "p", "r", "10.0.0.1", "sni-canary",
               "iv1", "2026-05-21T00:00:00Z", is_canary=True)
    mark_live(conn, "b-canary", public_ip="10.0.0.1",
              at="2026-05-21T00:01:00Z")
    for i, vid in enumerate(["vk", "vk", "vb", "vb"]):
        record(conn, box_id="b-canary", vantage_id=vid,
               cycle_at=f"2026-05-21T0{i + 2}:00:00Z",
               check_type="surface_scan", status="pass",
               evidence_json=None, image_version="iv1",
               recorded_at=f"2026-05-21T0{i + 2}:00:01Z")
    conn.close()
    rc = run(["image-promote", "iv1", "--evidence", "smoke",
              "--db-path", str(db), "--config", str(cfg_path)])
    assert rc == 0
    conn = connect(db)
    obs = {o.obligation_id for o in list_obligations(conn)}
    assert "t4_upstream_release_available::v2.1.7" not in obs
    assert "t4_image_promoted" in obs
    conn.close()


def test_image_promote_refused_by_gate(tmp_path, age_recipient, capsys):
    """Spec D2: image-promote refuses with each gate-failure reason on stderr."""
    from mthydra.controller.cli import run
    db = tmp_path / "state.sqlite"
    cfg_path = tmp_path / "controller.toml"
    cfg_path.write_text(_MIN_TOML)
    run(["init", "--db-path", str(db),
         "--age-recipient", age_recipient,
         "--provider-credential", "b2=id:secret"])
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.ru_images import insert_candidate
    conn = connect(db)
    insert_candidate(
        conn, image_version="iv1", upstream_release="v2.1.7",
        upstream_repo="9seconds/mtg", binary_url="x", manifest_url="x",
        binary_sha256="iv1", binary_size_bytes=100,
        built_at="2026-05-21T00:00:00Z",
    )
    conn.close()
    # No profile, no canary — gate should refuse.
    rc = run(["image-promote", "iv1", "--evidence", "smoke",
              "--db-path", str(db), "--config", str(cfg_path)])
    assert rc == 2
    err = capsys.readouterr().err
    assert "image_profiles row missing" in err
    assert "insufficient canary boxes" in err
    # Audit row recorded.
    conn = connect(db)
    rows = conn.execute(
        "SELECT details_json FROM audit_log WHERE action='image_promote_refused'"
    ).fetchall()
    assert len(rows) == 1
    conn.close()


def test_image_current_works_on_standby(tmp_path, age_recipient, capsys):
    """image-current is the one read-only command callable on standby."""
    from mthydra.controller.cli import run
    db = tmp_path / "state.sqlite"
    cfg_path = tmp_path / "controller.toml"
    cfg_path.write_text(_MIN_TOML)
    run(["init", "--role", "standby", "--db-path", str(db),
         "--age-recipient", age_recipient,
         "--provider-credential", "b2=id:secret"])
    capsys.readouterr()
    rc = run(["image-current", "--db-path", str(db), "--config", str(cfg_path)])
    assert rc == 0
    out = capsys.readouterr().out.strip()
    assert "none" in out.lower() or "no" in out.lower()


def test_image_retire_promoted_warns(tmp_path, age_recipient, capsys):
    from mthydra.controller.cli import run
    db = tmp_path / "state.sqlite"
    run(["init", "--db-path", str(db),
         "--age-recipient", age_recipient,
         "--provider-credential", "b2=id:secret"])
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.ru_images import insert_candidate, promote
    conn = connect(db)
    insert_candidate(
        conn, image_version="iv1", upstream_release="v2.1.7",
        upstream_repo="9seconds/mtg", binary_url="x", manifest_url="x",
        binary_sha256="iv1", binary_size_bytes=100,
        built_at="2026-05-21T00:00:00Z",
    )
    promote(conn, "iv1", at="2026-05-21T01:00:00Z", evidence="x")
    conn.close()
    capsys.readouterr()
    rc = run(["image-retire", "iv1", "--reason", "regression",
              "--db-path", str(db)])
    assert rc == 0
    cap = capsys.readouterr()
    out = (cap.out + cap.err).lower()
    assert "no" in out or "promote" in out or "default" in out


def test_upstream_check_invokes_tracker(tmp_path, age_recipient, capsys, monkeypatch):
    from mthydra.controller.cli import run
    db = tmp_path / "state.sqlite"
    cfg_path = tmp_path / "controller.toml"
    cfg_path.write_text(_MIN_TOML)
    run(["init", "--db-path", str(db),
         "--age-recipient", age_recipient,
         "--provider-credential", "b2=id:secret"])

    called = {"latest": None}
    class _StubTracker:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
        def run_once(self):
            called["latest"] = "v2.1.7"
            return "v2.1.7"
    monkeypatch.setattr("mthydra.controller.cli.UpstreamReleaseTracker", _StubTracker)

    rc = run(["upstream-check", "--db-path", str(db), "--config", str(cfg_path)])
    assert rc == 0
    assert called["latest"] == "v2.1.7"
    assert "v2.1.7" in capsys.readouterr().out


# ===== Task 3 (Spec G): authority-migrate-placeholder + authority-rotate real Ed25519 =====

def test_authority_migrate_placeholder_noop_when_already_real(tmp_path, age_recipient):
    """Spec G's bootstrap mints real Ed25519 directly, so migrate is a no-op
    on freshly-init'd DBs. Exercising the idempotent path."""
    from mthydra.controller.cli import run
    db = tmp_path / "state.sqlite"
    cfg_path = tmp_path / "controller.toml"
    cfg_path.write_text(_MIN_TOML)
    run(["init", "--db-path", str(db),
         "--age-recipient", age_recipient,
         "--provider-credential", "b2=id:secret"])

    from mthydra.controller.state.authority import current_authority
    from mthydra.controller.state.db import connect
    conn = connect(db)
    before = current_authority(conn)
    assert before.privkey_pem.startswith("-----BEGIN PRIVATE KEY-----")
    conn.close()

    rc = run(["authority-migrate-placeholder",
              "--db-path", str(db), "--config", str(cfg_path)])
    assert rc == 0

    conn = connect(db)
    after = current_authority(conn)
    assert after.generation == before.generation
    assert after.privkey_pem.startswith("-----BEGIN PRIVATE KEY-----")


def test_authority_migrate_placeholder_replaces_placeholder(tmp_path, age_recipient):
    """Migration path: simulate a pre-spec-G DB by forcing a PRIV-BOOTSTRAP- row,
    then run the migration and confirm it converts to real Ed25519."""
    from mthydra.controller.cli import run
    db = tmp_path / "state.sqlite"
    cfg_path = tmp_path / "controller.toml"
    cfg_path.write_text(_MIN_TOML)
    run(["init", "--db-path", str(db),
         "--age-recipient", age_recipient,
         "--provider-credential", "b2=id:secret"])

    # Simulate a pre-spec-G deployment by overwriting the authority with a placeholder.
    from mthydra.controller.state.authority import current_authority
    from mthydra.controller.state.db import connect
    conn = connect(db)
    conn.execute(
        "UPDATE credential_authority SET privkey_pem='PRIV-BOOTSTRAP-x', "
        "pubkey_pem='PUB-BOOTSTRAP-x' WHERE retired_at IS NULL"
    )
    conn.commit()
    before = current_authority(conn)
    assert before.privkey_pem.startswith("PRIV-BOOTSTRAP-")
    conn.close()

    rc = run(["authority-migrate-placeholder",
              "--db-path", str(db), "--config", str(cfg_path)])
    assert rc == 0

    conn = connect(db)
    after = current_authority(conn)
    assert after.generation == before.generation  # in-place
    assert after.privkey_pem.startswith("-----BEGIN PRIVATE KEY-----")
    assert after.pubkey_pem.startswith("-----BEGIN PUBLIC KEY-----")
    conn.close()
    assert after.pubkey_pem.startswith("-----BEGIN PUBLIC KEY-----")
    conn.close()


def test_authority_migrate_placeholder_idempotent(tmp_path, age_recipient, capsys):
    from mthydra.controller.cli import run
    db = tmp_path / "state.sqlite"
    cfg_path = tmp_path / "controller.toml"
    cfg_path.write_text(_MIN_TOML)
    run(["init", "--db-path", str(db),
         "--age-recipient", age_recipient,
         "--provider-credential", "b2=id:secret"])
    run(["authority-migrate-placeholder", "--db-path", str(db), "--config", str(cfg_path)])
    capsys.readouterr()
    rc = run(["authority-migrate-placeholder", "--db-path", str(db), "--config", str(cfg_path)])
    assert rc == 0


def test_authority_migrate_placeholder_refused_on_standby(tmp_path, age_recipient, capsys):
    from mthydra.controller.cli import run
    db = tmp_path / "state.sqlite"
    cfg_path = tmp_path / "controller.toml"
    cfg_path.write_text(_MIN_TOML)
    run(["init", "--role", "standby", "--db-path", str(db),
         "--age-recipient", age_recipient,
         "--provider-credential", "b2=id:secret"])
    rc = run(["authority-migrate-placeholder", "--db-path", str(db), "--config", str(cfg_path)])
    assert rc == 2
    assert "active-only" in capsys.readouterr().err.lower()


def test_authority_rotate_uses_real_ed25519(tmp_path, age_recipient):
    """authority-rotate now uses generate_authority_keypair() — not PRIV-BOOTSTRAP-."""
    from mthydra.controller.cli import run
    db = tmp_path / "state.sqlite"
    cfg_path = tmp_path / "controller.toml"
    cfg_path.write_text(_MIN_TOML)
    run(["init", "--db-path", str(db),
         "--age-recipient", age_recipient,
         "--provider-credential", "b2=id:secret"])
    run(["authority-migrate-placeholder", "--db-path", str(db), "--config", str(cfg_path)])
    rc = run(["authority-rotate", "--db-path", str(db), "--config", str(cfg_path)])
    assert rc == 0

    from mthydra.controller.state.authority import current_authority
    from mthydra.controller.state.db import connect
    conn = connect(db)
    cur = current_authority(conn)
    assert cur.generation == 2
    assert cur.privkey_pem.startswith("-----BEGIN PRIVATE KEY-----")
    conn.close()


def test_serve_arms_upstream_tracker(tmp_path, age_recipient, monkeypatch):
    """Active serve constructs and arms an UpstreamReleaseTracker alongside the
    cover-pool sweeps + heartbeat poller."""
    import pathlib
    import threading as _t
    from mthydra.controller.cli import run
    db = tmp_path / "state.sqlite"
    cfg_path = tmp_path / "controller.toml"
    cfg_path.write_text(_MIN_TOML)
    run(["init", "--db-path", str(db),
         "--age-recipient", age_recipient,
         "--provider-credential", "b2=id:secret"])
    recipient_file = tmp_path / "age-recipient.txt"
    recipient_file.write_text(age_recipient + "\n")
    monkeypatch.setattr("mthydra.controller.cli.DEFAULT_RECIPIENT_FILE",
                         str(recipient_file))

    armed = {"tracker": 0}
    class _StubTracker:
        def __init__(self, **kwargs): pass
        def arm(self): armed["tracker"] += 1
        def disarm(self): pass
        def run_once(self): return None
    monkeypatch.setattr("mthydra.controller.cli.UpstreamReleaseTracker", _StubTracker)

    # Redirect hardcoded /var/lib/mthydra/tmp to tmp_path (requires root otherwise).
    _real_mkdir = pathlib.Path.mkdir
    _serve_tmp = tmp_path / "serve_tmp"

    def _patched_mkdir(self, mode=0o777, parents=False, exist_ok=False):
        if str(self) == "/var/lib/mthydra/tmp":
            _serve_tmp.mkdir(parents=True, exist_ok=True)
            return
        _real_mkdir(self, mode=mode, parents=parents, exist_ok=exist_ok)

    monkeypatch.setattr(pathlib.Path, "mkdir", _patched_mkdir)

    def _fast_wait(self, timeout=None):
        self.set()
        return True
    monkeypatch.setattr(_t.Event, "wait", _fast_wait)

    rc = run([
        "serve",
        "--db-path", str(db),
        "--config", str(cfg_path),
    ])
    assert rc == 0
    assert armed["tracker"] == 1


def test_serve_refuses_when_startup_check_fails(tmp_path, age_recipient, monkeypatch):
    """M12: serve validates local state and refuses (rc 10) instead of arming
    wheels against a broken config — here, a corrupted age recipient."""
    import pathlib
    from mthydra.controller.cli import run
    db = tmp_path / "state.sqlite"
    cfg_path = tmp_path / "controller.toml"
    cfg_path.write_text(_MIN_TOML)
    run(["init", "--db-path", str(db),
         "--age-recipient", age_recipient,
         "--provider-credential", "b2=id:secret"])
    # Recipient file holds a recipient with a broken bech32 checksum (last
    # char flipped, still lowercase + valid charset).
    bad = "age1ql3z7hjy54pw3hyww5ayyfg7zqgvc7w3j2elw8zmrj2kg5sfn9aqmcac8q"
    recipient_file = tmp_path / "age-recipient.txt"
    recipient_file.write_text(bad + "\n")
    monkeypatch.setattr("mthydra.controller.cli.DEFAULT_RECIPIENT_FILE",
                         str(recipient_file))
    _real_mkdir = pathlib.Path.mkdir

    def _patched_mkdir(self, mode=0o777, parents=False, exist_ok=False):
        if str(self) == "/var/lib/mthydra/tmp":
            (tmp_path / "serve_tmp").mkdir(parents=True, exist_ok=True)
            return
        _real_mkdir(self, mode=mode, parents=parents, exist_ok=exist_ok)

    monkeypatch.setattr(pathlib.Path, "mkdir", _patched_mkdir)
    rc = run(["serve", "--db-path", str(db), "--config", str(cfg_path)])
    assert rc == 10


# ----- spec G: provision-seed -----


def _setup_provision_prereqs(db, age_recipient, cfg_path):
    """Build a DB that's ready for provision-seed: migrate authority,
    promote image, attest cover-domain, sign descriptor."""
    from mthydra.controller.cli import run
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.ru_images import insert_candidate, promote
    from mthydra.controller.state.cover_pool import add_candidate, attest_verified

    run(["init", "--db-path", str(db),
         "--age-recipient", age_recipient,
         "--provider-credential", "b2=id:secret"])
    run(["authority-migrate-placeholder", "--db-path", str(db), "--config", str(cfg_path)])
    conn = connect(db)
    insert_candidate(
        conn,
        image_version="abc123",
        upstream_release="v2.1.7",
        upstream_repo="9seconds/mtg",
        binary_url="images/abc123/mtg",
        manifest_url="images/abc123/manifest.json",
        binary_sha256="abc123",
        binary_size_bytes=10485760,
        built_at="2026-05-21T00:00:00Z",
    )
    promote(conn, "abc123", at="2026-05-21T00:01:00Z", evidence="smoke")
    add_candidate(conn, "example.cover", added_at="2026-05-21T00:02:00Z")
    attest_verified(conn, "example.cover", from_vantage="ru-vps-01",
                     at="2026-05-21T00:03:00Z")
    conn.close()
    # Sign a descriptor.
    run(["descriptor-sign-now", "--db-path", str(db), "--config", str(cfg_path)])


def test_provision_seed_cloud_init_default(tmp_path, age_recipient, capsys, monkeypatch):
    from mthydra.controller.cli import run
    db = tmp_path / "state.sqlite"
    cfg_path = tmp_path / "controller.toml"
    cfg_path.write_text(_MIN_TOML)

    # Stub presigned_image_url so we don't need a real B2.
    from mthydra.controller.backup.s3_dest import S3Destination
    monkeypatch.setattr(
        S3Destination, "presigned_image_url",
        lambda self, *, image_version, ttl_seconds=3600: (
            f"https://b2.example/{image_version}/mtg?sig=stub",
            "2026-05-21T01:00:00Z",
        ),
    )
    _setup_provision_prereqs(db, age_recipient, cfg_path)
    capsys.readouterr()
    rc = run(["provision-seed",
              "--provider", "hetzner", "--region", "fsn1",
              "--db-path", str(db), "--config", str(cfg_path),
              *_PROVISION_V2_ARGS])
    assert rc == 0
    out = capsys.readouterr().out
    assert out.startswith("#cloud-config")
    assert "write_files" in out
    assert "example.cover" in out


def test_provision_seed_json_format(tmp_path, age_recipient, capsys, monkeypatch):
    import json
    from mthydra.controller.cli import run
    db = tmp_path / "state.sqlite"
    cfg_path = tmp_path / "controller.toml"
    cfg_path.write_text(_MIN_TOML)

    from mthydra.controller.backup.s3_dest import S3Destination
    monkeypatch.setattr(
        S3Destination, "presigned_image_url",
        lambda self, *, image_version, ttl_seconds=3600: (
            f"https://b2.example/{image_version}/mtg?sig=stub",
            "2026-05-21T01:00:00Z",
        ),
    )
    _setup_provision_prereqs(db, age_recipient, cfg_path)
    capsys.readouterr()
    rc = run(["provision-seed", "--format", "json",
              "--provider", "hetzner", "--region", "fsn1",
              "--db-path", str(db), "--config", str(cfg_path),
              *_PROVISION_V2_ARGS])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == "mthydra.ru_seed.v2"
    assert payload["sni"] == "example.cover"
    assert payload["transport_role"] == "ru_relay"


def test_provision_seed_refused_on_standby(tmp_path, age_recipient, capsys):
    from mthydra.controller.cli import run
    db = tmp_path / "state.sqlite"
    cfg_path = tmp_path / "controller.toml"
    cfg_path.write_text(_MIN_TOML)
    run(["init", "--role", "standby", "--db-path", str(db),
         "--age-recipient", age_recipient,
         "--provider-credential", "b2=id:secret"])
    rc = run(["provision-seed", "--provider", "p", "--region", "r",
              "--db-path", str(db), "--config", str(cfg_path),
              *_PROVISION_V2_ARGS])
    assert rc == 2
    assert "active-only" in capsys.readouterr().err.lower()


def test_provision_seed_refused_no_promoted_image(tmp_path, age_recipient, capsys):
    from mthydra.controller.cli import run
    db = tmp_path / "state.sqlite"
    cfg_path = tmp_path / "controller.toml"
    cfg_path.write_text(_MIN_TOML)
    run(["init", "--db-path", str(db),
         "--age-recipient", age_recipient,
         "--provider-credential", "b2=id:secret"])
    run(["authority-migrate-placeholder", "--db-path", str(db), "--config", str(cfg_path)])
    # No image promoted, no domain attested, no descriptor signed.
    rc = run(["provision-seed", "--provider", "p", "--region", "r",
              "--db-path", str(db), "--config", str(cfg_path),
              *_PROVISION_V2_ARGS])
    assert rc == 3
    err = capsys.readouterr().err.lower()
    assert "image" in err or "promoted" in err


# ----- spec G: ru-box-list / ru-box-mark-live / ru-box-terminate -----


def test_ru_box_list_empty_default(tmp_path, age_recipient, capsys):
    from mthydra.controller.cli import run
    db = tmp_path / "state.sqlite"
    run(["init", "--db-path", str(db),
         "--age-recipient", age_recipient,
         "--provider-credential", "b2=id:secret"])
    capsys.readouterr()
    rc = run(["ru-box-list", "--db-path", str(db)])
    assert rc == 0


def test_ru_box_list_json_after_provision(tmp_path, age_recipient, capsys, monkeypatch):
    import json
    from mthydra.controller.cli import run
    db = tmp_path / "state.sqlite"
    cfg_path = tmp_path / "controller.toml"
    cfg_path.write_text(_MIN_TOML)
    from mthydra.controller.backup.s3_dest import S3Destination
    monkeypatch.setattr(
        S3Destination, "presigned_image_url",
        lambda self, *, image_version, ttl_seconds=3600: (
            f"https://b2.example/{image_version}/mtg?sig=stub",
            "2026-05-21T01:00:00Z",
        ),
    )
    _setup_provision_prereqs(db, age_recipient, cfg_path)
    run(["provision-seed", "--provider", "hetzner", "--region", "fsn1",
         "--db-path", str(db), "--config", str(cfg_path),
         *_PROVISION_V2_ARGS])
    capsys.readouterr()
    rc = run(["ru-box-list", "--json", "--db-path", str(db)])
    assert rc == 0
    rows = json.loads(capsys.readouterr().out)
    assert len(rows) == 1
    assert rows[0]["state"] == "provisioning"


def test_ru_box_mark_live_happy_path(tmp_path, age_recipient, capsys, monkeypatch):
    from mthydra.controller.cli import run
    db = tmp_path / "state.sqlite"
    cfg_path = tmp_path / "controller.toml"
    cfg_path.write_text(_MIN_TOML)
    from mthydra.controller.backup.s3_dest import S3Destination
    monkeypatch.setattr(
        S3Destination, "presigned_image_url",
        lambda self, *, image_version, ttl_seconds=3600: (
            f"https://b2.example/{image_version}/mtg?sig=stub",
            "2026-05-21T01:00:00Z",
        ),
    )
    _setup_provision_prereqs(db, age_recipient, cfg_path)
    run(["provision-seed", "--provider", "hetzner", "--region", "fsn1",
         "--db-path", str(db), "--config", str(cfg_path),
         *_PROVISION_V2_ARGS])
    from mthydra.controller.state.db import connect
    conn = connect(db)
    box_id = conn.execute("SELECT box_id FROM ru_boxes LIMIT 1").fetchone()[0]
    conn.close()
    rc = run(["ru-box-mark-live", box_id, "--public-ip", "203.0.113.7",
              "--db-path", str(db)])
    assert rc == 0
    conn = connect(db)
    row = conn.execute("SELECT state, public_ip FROM ru_boxes WHERE box_id=?",
                        (box_id,)).fetchone()
    assert row == ("live", "203.0.113.7")
    conn.close()


def test_ru_box_terminate_burns_sni_and_revokes_credentials(tmp_path, age_recipient, monkeypatch):
    from mthydra.controller.cli import run
    db = tmp_path / "state.sqlite"
    cfg_path = tmp_path / "controller.toml"
    cfg_path.write_text(_MIN_TOML)
    from mthydra.controller.backup.s3_dest import S3Destination
    monkeypatch.setattr(
        S3Destination, "presigned_image_url",
        lambda self, *, image_version, ttl_seconds=3600: (
            f"https://b2.example/{image_version}/mtg?sig=stub",
            "2026-05-21T01:00:00Z",
        ),
    )
    _setup_provision_prereqs(db, age_recipient, cfg_path)
    run(["provision-seed", "--provider", "hetzner", "--region", "fsn1",
         "--db-path", str(db), "--config", str(cfg_path),
         *_PROVISION_V2_ARGS])
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.burned import is_burned
    conn = connect(db)
    box_id, sni = conn.execute("SELECT box_id, sni FROM ru_boxes LIMIT 1").fetchone()
    conn.close()
    rc = run(["ru-box-terminate", box_id, "--reason", "test",
              "--db-path", str(db)])
    assert rc == 0
    conn = connect(db)
    state = conn.execute(
        "SELECT state FROM ru_boxes WHERE box_id=?", (box_id,)
    ).fetchone()[0]
    assert state == "terminated"
    assert is_burned(conn, sni)
    revoked = conn.execute(
        "SELECT COUNT(*) FROM onward_credentials WHERE box_id=? AND revoked_at IS NULL",
        (box_id,),
    ).fetchone()[0]
    assert revoked == 0
    conn.close()


def test_bootstrap_seeds_provision_drill_obligation(tmp_path, age_recipient):
    from mthydra.controller.cli import run
    db = tmp_path / "state.sqlite"
    run(["init", "--db-path", str(db),
         "--age-recipient", age_recipient,
         "--provider-credential", "b2=id:secret"])
    conn = connect(db)
    obs = {o.obligation_id for o in list_obligations(conn)}
    assert "g_provision_drill_proven" in obs
    conn.close()


def test_bootstrap_seeds_spec_e_obligations(tmp_path, age_recipient):
    from mthydra.controller.cli import run
    db = tmp_path / "state.sqlite"
    run(["init", "--db-path", str(db),
         "--age-recipient", age_recipient,
         "--provider-credential", "b2=id:secret"])
    conn = connect(db)
    obs = {o.obligation_id: o for o in list_obligations(conn)}
    assert "e_ru_agent_provision_replace_drill_proven" in obs
    assert "e_data_exit_drill_proven" in obs
    conn.close()


# -----------------------------------------------------------------------------
# Task 10 (Spec E): data-exit CLI subcommands
# -----------------------------------------------------------------------------

_TOML_WITH_DATA_EXIT = """\
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
[data_exit]
listen_port = 443
sing_box_socket = "/run/sb.sock"
config_path = "{config_path}"
reality_key_path = "{reality_key_path}"
[data_exit.telegram_dcs]
v4 = ["149.154.160.0/20"]
v6 = []
[data_exit.cover_sni]
default = "c.example"
"""


def _setup_eu_node_with_identity(db, cfg_path_str, age_recipient, node_id="eu1"):
    """Helper: init DB + add eu_node with cover_sni + reality_pubkey."""
    from mthydra.controller.cli import run
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.eu_nodes import (
        add_eu_node, set_data_exit_identity,
    )
    run(["init", "--db-path", str(db),
         "--age-recipient", age_recipient,
         "--provider-credential", "b2=id:secret"])
    conn = connect(db)
    add_eu_node(conn, node_id=node_id, hostname=f"{node_id}.example",
                provider="p", region="r", role="active",
                added_at="2026-05-23T00:00:00Z")
    conn.execute("UPDATE eu_nodes SET public_ip='203.0.113.5' "
                 "WHERE node_id=?", (node_id,))
    set_data_exit_identity(conn, node_id, cover_sni="c.example",
                            reality_pubkey="PUB")
    conn.commit()
    conn.close()


def test_data_exit_config_show_emits_json(tmp_path, age_recipient, capsys, monkeypatch):
    """`data-exit-config-show` prints the rendered sing-box.json."""
    from mthydra.controller.cli import run
    db = tmp_path / "state.sqlite"
    cfg_path = tmp_path / "controller.toml"
    cfg_path.write_text(_TOML_WITH_DATA_EXIT.format(
        config_path=str(tmp_path / "sb.json"),
        reality_key_path=str(tmp_path / "r.key"),
    ))
    (tmp_path / "r.key").write_text("KEY")
    run(["init", "--db-path", str(db),
         "--age-recipient", age_recipient,
         "--provider-credential", "b2=id:secret"])
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.eu_nodes import (
        add_eu_node, set_data_exit_identity,
    )
    conn = connect(db)
    add_eu_node(conn, node_id="eu1", hostname="eu1.example",
                provider="p", region="r", role="active",
                added_at="2026-05-23T00:00:00Z")
    conn.execute("UPDATE eu_nodes SET public_ip='1.2.3.4'")
    set_data_exit_identity(conn, "eu1", cover_sni="c.example",
                            reality_pubkey="PUB")
    conn.commit()
    conn.close()
    capsys.readouterr()
    rc = run(["data-exit-config-show", "--node-id", "eu1",
              "--db-path", str(db), "--config", str(cfg_path)])
    assert rc == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["inbounds"][0]["tls"]["server_name"] == "c.example"


def test_data_exit_rewrite_writes_file_and_audits(tmp_path, age_recipient, capsys):
    """`data-exit-rewrite` forces a wheel tick now."""
    from mthydra.controller.cli import run
    db = tmp_path / "state.sqlite"
    cfg_path = tmp_path / "controller.toml"
    cfg_path.write_text(_TOML_WITH_DATA_EXIT.format(
        config_path=str(tmp_path / "sb.json"),
        reality_key_path=str(tmp_path / "r.key"),
    ))
    (tmp_path / "r.key").write_text("PRIVKEY")
    _setup_eu_node_with_identity(db, str(cfg_path), age_recipient)
    capsys.readouterr()
    rc = run(["data-exit-rewrite", "--node-id", "eu1",
              "--db-path", str(db), "--config", str(cfg_path)])
    assert rc == 0
    assert (tmp_path / "sb.json").exists()
    out = capsys.readouterr().out
    assert "config regenerated" in out


def test_data_exit_status_shows_config_summary(tmp_path, age_recipient, capsys):
    """`data-exit-status` prints node_id, last config write time, allowlist size."""
    from mthydra.controller.cli import run
    db = tmp_path / "state.sqlite"
    cfg_path = tmp_path / "controller.toml"
    cfg_path.write_text(_TOML_WITH_DATA_EXIT.format(
        config_path=str(tmp_path / "sb.json"),
        reality_key_path=str(tmp_path / "r.key"),
    ))
    (tmp_path / "r.key").write_text("PRIVKEY")
    _setup_eu_node_with_identity(db, str(cfg_path), age_recipient)
    capsys.readouterr()
    rc = run(["data-exit-status", "--node-id", "eu1",
              "--db-path", str(db), "--config", str(cfg_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "node_id:" in out
    assert "eu1" in out
    assert "cover_sni:" in out
    assert "c.example" in out
    assert "users_allowlist:" in out


def test_data_exit_reality_keygen_creates_keypair(tmp_path, age_recipient, monkeypatch):
    """`data-exit-reality-keygen` writes private + pubkey to disk + DB."""
    from mthydra.controller.cli import run
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.eu_nodes import add_eu_node
    db = tmp_path / "state.sqlite"
    cfg_path = tmp_path / "controller.toml"
    key_path = tmp_path / "r.key"
    cfg_path.write_text(_TOML_WITH_DATA_EXIT.format(
        config_path=str(tmp_path / "sb.json"),
        reality_key_path=str(key_path),
    ))
    # Stub `sing-box generate reality-keypair` output.
    import subprocess
    real_run = subprocess.run
    def fake_run(cmd, **kw):
        if cmd[:3] == ["sing-box", "generate", "reality-keypair"]:
            return type("R", (), {
                "returncode": 0,
                "stdout": "PrivateKey: TEST_PRIV\nPublicKey: TEST_PUB\n",
                "stderr": "",
            })()
        return real_run(cmd, **kw)
    monkeypatch.setattr(subprocess, "run", fake_run)

    run(["init", "--db-path", str(db),
         "--age-recipient", age_recipient,
         "--provider-credential", "b2=id:secret"])
    conn = connect(db)
    add_eu_node(conn, node_id="eu1", hostname="eu1.example",
                provider="p", region="r", role="active",
                added_at="2026-05-23T00:00:00Z")
    conn.commit()
    conn.close()

    rc = run(["data-exit-reality-keygen", "--node-id", "eu1",
              "--evidence", "initial-setup",
              "--db-path", str(db), "--config", str(cfg_path)])
    assert rc == 0
    assert key_path.read_text().strip() == "TEST_PRIV"
    conn = connect(db)
    pub = conn.execute(
        "SELECT reality_pubkey FROM eu_nodes WHERE node_id='eu1'"
    ).fetchone()[0]
    assert pub == "TEST_PUB"
    conn.close()


def test_data_exit_reality_keygen_refuses_if_already_present(tmp_path, age_recipient, capsys):
    """Pre-existing reality_pubkey on the node row causes refusal."""
    from mthydra.controller.cli import run
    db = tmp_path / "state.sqlite"
    cfg_path = tmp_path / "controller.toml"
    cfg_path.write_text(_TOML_WITH_DATA_EXIT.format(
        config_path=str(tmp_path / "sb.json"),
        reality_key_path=str(tmp_path / "r.key"),
    ))
    _setup_eu_node_with_identity(db, str(cfg_path), age_recipient)
    rc = run(["data-exit-reality-keygen", "--node-id", "eu1",
              "--evidence", "test",
              "--db-path", str(db), "--config", str(cfg_path)])
    assert rc == 3
    err = capsys.readouterr().err
    assert "already has reality_pubkey" in err



# ===== spec H: shard manager CLI =====


def _h_init(tmp_path, age_recipient, db_name="state.sqlite"):
    db = tmp_path / db_name
    run([
        "init", "--db-path", str(db),
        "--age-recipient", age_recipient,
        "--provider-credential", "b2=id:secret",
    ])
    return db


def _h_cfg(tmp_path, **overrides):
    """Write a minimal controller.toml with [shard_manager] for spec H tests."""
    p = tmp_path / "controller.toml"
    tgt = overrides.get("target_size", 2)
    mx = overrides.get("max_size", 3)
    interval = overrides.get("reshuffle_interval_days", 14)
    p.write_text(
        "[node]\nrole='active'\nhostname='h'\n"
        "[backup]\nfloor_interval_hours=24\non_change_debounce_seconds=30\n"
        "endpoint=''\nbucket='b'\naccess_key_id='k'\n"
        "[backup.retention]\nkeep_daily=30\nkeep_monthly=12\nobject_lock_days=365\n"
        "[gap_monitor]\npoll_interval_minutes=30\nalarm_threshold_hours=48\n"
        "recipient_email='op@example.org'\n"
        "[descriptor]\nrotation_interval_hours=1\nvalidity_window_hours=24\n"
        "[obligations]\n[obligations.timers_hours]\n"
        "[cover_pool]\nrotation_ttl_days=14\nreverify_after_days=30\n"
        "freeze_threshold=2\nreverify_sweep_interval='1h'\n"
        "rotation_sweep_interval='1h'\nreplenishment_interval_days=90\n"
        f"[shard_manager]\ntarget_size={tgt}\nmax_size={mx}\n"
        f"reshuffle_interval_days={interval}\nreshuffle_sweep_interval='1h'\n"
    )
    return p


def test_cli_user_add_and_user_list(tmp_path, age_recipient, capsys):
    db = _h_init(tmp_path, age_recipient)
    rc = run(["user-add", "alice",
              "--out-of-band-channel", "signal:+1555",
              "--display-name", "Alice",
              "--db-path", str(db)])
    assert rc == 0
    rc = run(["user-add", "bob",
              "--out-of-band-channel", "email:bob@example.org",
              "--db-path", str(db)])
    assert rc == 0
    capsys.readouterr()
    rc = run(["user-list", "--db-path", str(db), "--json"])
    assert rc == 0
    import json as _json
    out = _json.loads(capsys.readouterr().out)
    by_id = {u["user_id"]: u for u in out}
    assert by_id["alice"]["display_name"] == "Alice"
    assert by_id["alice"]["current_shard_id"] is None
    assert by_id["bob"]["out_of_band_channel"] == "email:bob@example.org"


def test_cli_user_add_refuses_duplicate(tmp_path, age_recipient, capsys):
    db = _h_init(tmp_path, age_recipient)
    rc1 = run(["user-add", "alice", "--out-of-band-channel", "signal", "--db-path", str(db)])
    assert rc1 == 0
    rc2 = run(["user-add", "alice", "--out-of-band-channel", "signal", "--db-path", str(db)])
    assert rc2 == 2


def test_cli_shard_create_assigns_users(tmp_path, age_recipient):
    db = _h_init(tmp_path, age_recipient)
    cfg = _h_cfg(tmp_path)
    for u in ["u1", "u2"]:
        run(["user-add", u, "--out-of-band-channel", "x", "--db-path", str(db)])
    rc = run(["shard-create", "s1", "--members", "u1,u2",
              "--db-path", str(db), "--config", str(cfg)])
    assert rc == 0
    # Users now reference s1.
    from mthydra.controller.state.db import connect
    conn = connect(db)
    rows = conn.execute(
        "SELECT user_id, current_shard_id FROM users ORDER BY user_id"
    ).fetchall()
    assert rows == [("u1", "s1"), ("u2", "s1")]


def test_cli_shard_create_refuses_already_in_active_shard(tmp_path, age_recipient, capsys):
    db = _h_init(tmp_path, age_recipient)
    cfg = _h_cfg(tmp_path)
    for u in ["u1", "u2"]:
        run(["user-add", u, "--out-of-band-channel", "x", "--db-path", str(db)])
    run(["shard-create", "s1", "--members", "u1", "--db-path", str(db), "--config", str(cfg)])
    rc = run(["shard-create", "s2", "--members", "u1,u2",
              "--db-path", str(db), "--config", str(cfg)])
    assert rc == 2
    err = capsys.readouterr().err
    assert "already in active shard" in err


def test_cli_shard_list_json(tmp_path, age_recipient, capsys):
    db = _h_init(tmp_path, age_recipient)
    cfg = _h_cfg(tmp_path)
    run(["user-add", "u1", "--out-of-band-channel", "x", "--db-path", str(db)])
    run(["shard-create", "s1", "--members", "u1",
         "--db-path", str(db), "--config", str(cfg)])
    capsys.readouterr()
    rc = run(["shard-list", "--json", "--db-path", str(db)])
    assert rc == 0
    import json as _json
    out = _json.loads(capsys.readouterr().out)
    assert len(out) == 1
    assert out[0]["shard_id"] == "s1"
    assert out[0]["members"] == ["u1"]
    assert out[0]["target_size"] == 2


def test_cli_shard_show(tmp_path, age_recipient, capsys):
    db = _h_init(tmp_path, age_recipient)
    cfg = _h_cfg(tmp_path)
    run(["user-add", "u1", "--out-of-band-channel", "x", "--db-path", str(db)])
    run(["shard-create", "s1", "--members", "u1",
         "--db-path", str(db), "--config", str(cfg)])
    capsys.readouterr()
    rc = run(["shard-show", "s1", "--json", "--db-path", str(db)])
    assert rc == 0
    import json as _json
    out = _json.loads(capsys.readouterr().out)
    assert out["shard_id"] == "s1"
    assert out["members"] == ["u1"]
    assert out["boxes"] == []


def test_cli_shard_show_refuses_missing(tmp_path, age_recipient, capsys):
    db = _h_init(tmp_path, age_recipient)
    rc = run(["shard-show", "nope", "--db-path", str(db)])
    assert rc == 2


def test_cli_shard_assign_box_provisioning_ok(tmp_path, age_recipient, capsys):
    db = _h_init(tmp_path, age_recipient)
    cfg = _h_cfg(tmp_path)
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.ru_boxes import insert_box
    run(["user-add", "u1", "--out-of-band-channel", "x", "--db-path", str(db)])
    run(["shard-create", "s1", "--members", "u1",
         "--db-path", str(db), "--config", str(cfg)])
    conn = connect(db)
    insert_box(conn, "b1", "p", "r", "10.0.0.1", "sni-b1.example",
               "img-v1", "2026-05-24T00:00:00Z")
    conn.close()
    rc = run(["shard-assign-box", "b1", "--shard", "s1", "--db-path", str(db)])
    assert rc == 0
    conn = connect(db)
    row = conn.execute("SELECT shard_id FROM ru_boxes WHERE box_id='b1'").fetchone()
    assert row[0] == "s1"


def test_cli_shard_assign_box_refuses_live(tmp_path, age_recipient, capsys):
    db = _h_init(tmp_path, age_recipient)
    cfg = _h_cfg(tmp_path)
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.ru_boxes import insert_box, mark_live
    run(["user-add", "u1", "--out-of-band-channel", "x", "--db-path", str(db)])
    run(["shard-create", "s1", "--members", "u1",
         "--db-path", str(db), "--config", str(cfg)])
    # Create s2 cleanly via SQL (a real shard-create would refuse since u1 is in s1).
    conn = connect(db)
    conn.execute(
        "INSERT INTO shards (shard_id, members_json, target_size, last_reshuffled_at, created_at) "
        "VALUES ('s2', '[]', 2, '2026-05-24T00:00:00Z', '2026-05-24T00:00:00Z')"
    )
    insert_box(conn, "b1", "p", "r", "10.0.0.1", "sni-b1.example",
               "img-v1", "2026-05-24T00:00:00Z")
    conn.execute("UPDATE ru_boxes SET shard_id='s1' WHERE box_id='b1'")
    mark_live(conn, "b1", public_ip="10.0.0.1", at="2026-05-24T00:01:00Z")
    conn.commit()
    conn.close()
    rc = run(["shard-assign-box", "b1", "--shard", "s2", "--db-path", str(db)])
    assert rc == 2


def test_cli_shard_reshuffle_creates_new_shard(tmp_path, age_recipient, capsys):
    db = _h_init(tmp_path, age_recipient)
    cfg = _h_cfg(tmp_path)
    for u in ["u1", "u2"]:
        run(["user-add", u, "--out-of-band-channel", "x", "--db-path", str(db)])
    run(["shard-create", "s1", "--members", "u1,u2",
         "--db-path", str(db), "--config", str(cfg)])
    rc = run(["shard-reshuffle", "s1", "--db-path", str(db), "--config", str(cfg)])
    assert rc == 0
    from mthydra.controller.state.db import connect
    conn = connect(db)
    # s1 retired
    row = conn.execute("SELECT retired_at FROM shards WHERE shard_id='s1'").fetchone()
    assert row[0] is not None
    # exactly one new active shard exists
    n_active = conn.execute(
        "SELECT COUNT(*) FROM shards WHERE retired_at IS NULL"
    ).fetchone()[0]
    assert n_active == 1


def test_cli_shard_stats_json(tmp_path, age_recipient, capsys):
    db = _h_init(tmp_path, age_recipient)
    cfg = _h_cfg(tmp_path)
    for u in ["u1", "u2", "u3"]:
        run(["user-add", u, "--out-of-band-channel", "x", "--db-path", str(db)])
    run(["shard-create", "s1", "--members", "u1",
         "--db-path", str(db), "--config", str(cfg)])
    capsys.readouterr()
    rc = run(["shard-stats", "--json", "--db-path", str(db), "--config", str(cfg)])
    assert rc == 0
    import json as _json
    out = _json.loads(capsys.readouterr().out)
    assert out["total_active"] == 1
    assert sorted(out["unassigned_users"]) == ["u2", "u3"]


def _make_live_box_in_shard(db, box_id, shard_id, target_size=2, members=None):
    """Helper: insert a live box bound to an existing shard, with credential + reality_uuid."""
    import json as _json
    from mthydra.controller.state.credentials import issue_credential
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.ru_boxes import insert_box, mark_live
    members = members if members is not None else []
    conn = connect(db)
    # Ensure shard exists.
    existing = conn.execute(
        "SELECT 1 FROM shards WHERE shard_id=?", (shard_id,)
    ).fetchone()
    if existing is None:
        conn.execute(
            "INSERT INTO shards (shard_id, members_json, target_size, last_reshuffled_at, created_at) "
            "VALUES (?, ?, ?, '2026-05-24T00:00:00Z', '2026-05-24T00:00:00Z')",
            (shard_id, _json.dumps(members), target_size),
        )
        for u in members:
            conn.execute(
                "INSERT OR IGNORE INTO users (user_id, display_name, out_of_band_channel, "
                "current_shard_id, added_at) "
                "VALUES (?, NULL, 'email', ?, '2026-05-24T00:00:00Z')",
                (u, shard_id),
            )
        conn.commit()
    insert_box(conn, box_id, "p", "r", "10.0.0.1", f"sni-{box_id}.example",
               "img-v1", "2026-05-24T00:00:00Z")
    conn.execute("UPDATE ru_boxes SET shard_id=? WHERE box_id=?", (shard_id, box_id))
    mark_live(conn, box_id, public_ip="10.0.0.1", at="2026-05-24T00:01:00Z")
    issue_credential(conn, box_id, b"\x00" * 10, "2026-05-24T00:01:00Z", authority_generation=1)
    conn.execute("UPDATE ru_boxes SET reality_uuid=? WHERE box_id=?",
                 (f"uuid-{box_id}", box_id))
    # Seed cover_domain_pool 'in_use' row for the SNI so mark_burned can find it.
    conn.execute(
        "INSERT INTO cover_domain_pool (domain, state, last_verified_at, verified_from_vantage, "
        "assigned_box_id, added_at, entered_in_use_at) "
        "VALUES (?, 'in_use', '2026-05-24T00:00:00Z', 'op', ?, '2026-05-24T00:00:00Z', "
        "'2026-05-24T00:00:00Z')",
        (f"sni-{box_id}.example", box_id),
    )
    conn.commit()
    conn.close()


def test_cli_ru_box_terminate_compromise_triggers_reshuffle(tmp_path, age_recipient, capsys):
    db = _h_init(tmp_path, age_recipient)
    _make_live_box_in_shard(db, "b1", "s1", target_size=2, members=["u1", "u2"])
    rc = run(["ru-box-terminate", "b1", "--reason", "compromise", "--db-path", str(db)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "compromise reshuffle" in out
    from mthydra.controller.state.db import connect
    conn = connect(db)
    retired_at = conn.execute(
        "SELECT retired_at FROM shards WHERE shard_id='s1'"
    ).fetchone()[0]
    assert retired_at is not None
    n_active = conn.execute(
        "SELECT COUNT(*) FROM shards WHERE retired_at IS NULL"
    ).fetchone()[0]
    assert n_active == 1
    audits = conn.execute(
        "SELECT details_json FROM audit_log WHERE action='shard_reshuffle'"
    ).fetchall()
    assert len(audits) == 1
    import json as _json
    details = _json.loads(audits[0][0])
    assert details["from"] == "s1"
    assert details["reason"] == "compromise"


def test_cli_ru_box_terminate_benign_reason_no_reshuffle(tmp_path, age_recipient, capsys):
    db = _h_init(tmp_path, age_recipient)
    _make_live_box_in_shard(db, "b1", "s1", target_size=2, members=["u1", "u2"])
    rc = run(["ru-box-terminate", "b1", "--reason", "aged_out", "--db-path", str(db)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "compromise reshuffle" not in out
    from mthydra.controller.state.db import connect
    conn = connect(db)
    retired_at = conn.execute(
        "SELECT retired_at FROM shards WHERE shard_id='s1'"
    ).fetchone()[0]
    assert retired_at is None  # shard untouched
    audits = conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE action='shard_reshuffle'"
    ).fetchone()[0]
    assert audits == 0


# ===== spec I: probe vantage harness CLI =====


def test_cli_vantage_add_and_list(tmp_path, age_recipient, capsys):
    db = _h_init(tmp_path, age_recipient)
    rc = run(["vantage-add", "v1",
              "--label", "kz1",
              "--source-kind", "cloud-cis",
              "--region-hint", "KZ-almaty",
              "--db-path", str(db)])
    assert rc == 0
    rc = run(["vantage-add", "v2",
              "--label", "by1",
              "--source-kind", "cloud-cis",
              "--db-path", str(db)])
    assert rc == 0
    capsys.readouterr()
    rc = run(["vantage-list", "--json", "--db-path", str(db)])
    assert rc == 0
    import json as _json
    out = _json.loads(capsys.readouterr().out)
    ids = {v["vantage_id"]: v for v in out}
    assert ids["v1"]["region_hint"] == "KZ-almaty"
    assert ids["v1"]["state"] == "candidate"


def test_cli_vantage_attest_active_and_burn(tmp_path, age_recipient, capsys):
    db = _h_init(tmp_path, age_recipient)
    run(["vantage-add", "v1", "--label", "kz1", "--source-kind", "x",
         "--db-path", str(db)])
    rc = run(["vantage-attest-active", "v1", "--evidence", "ssh-log",
              "--db-path", str(db)])
    assert rc == 0
    rc = run(["vantage-burn", "v1", "--reason", "leaked",
              "--db-path", str(db)])
    assert rc == 0
    from mthydra.controller.state.db import connect
    conn = connect(db)
    row = conn.execute(
        "SELECT state, burn_reason FROM probe_vantages WHERE vantage_id='v1'"
    ).fetchone()
    assert row == ("burned", "leaked")


def test_cli_vantage_burn_refuses_double_burn(tmp_path, age_recipient, capsys):
    db = _h_init(tmp_path, age_recipient)
    run(["vantage-add", "v1", "--label", "kz1", "--source-kind", "x",
         "--db-path", str(db)])
    run(["vantage-burn", "v1", "--reason", "r1", "--db-path", str(db)])
    rc = run(["vantage-burn", "v1", "--reason", "r2", "--db-path", str(db)])
    assert rc == 2


def test_cli_profile_pin_and_show(tmp_path, age_recipient, capsys):
    db = _h_init(tmp_path, age_recipient)
    # Seed an image via raw SQL.
    from mthydra.controller.state.db import connect
    conn = connect(db)
    conn.execute(
        "INSERT INTO ru_images (image_version, upstream_release, upstream_repo, "
        "binary_url, manifest_url, binary_sha256, binary_size_bytes, state, built_at) "
        "VALUES ('v1', 'r', 'r', 'u', 'm', 'sha', 1, 'candidate', '2026-05-25T00:00:00Z')"
    )
    conn.commit()
    conn.close()
    profile_path = tmp_path / "profile.json"
    profile_path.write_text('{"tls_handshake":"x"}')
    rc = run(["profile-pin", "v1",
              "--profile-json", str(profile_path),
              "--recorded-by", "op",
              "--db-path", str(db)])
    assert rc == 0
    capsys.readouterr()
    rc = run(["profile-show", "v1", "--json", "--db-path", str(db)])
    assert rc == 0
    import json as _json
    out = _json.loads(capsys.readouterr().out)
    assert out["image_version"] == "v1"
    assert '"tls_handshake"' in out["profile_json"]


def test_cli_profile_show_missing(tmp_path, age_recipient, capsys):
    db = _h_init(tmp_path, age_recipient)
    rc = run(["profile-show", "v1", "--db-path", str(db)])
    assert rc == 2


def test_cli_probe_record_refuses_inactive_vantage(tmp_path, age_recipient, capsys):
    db = _h_init(tmp_path, age_recipient)
    from mthydra.controller.state.db import connect
    conn = connect(db)
    conn.execute(
        "INSERT INTO ru_images (image_version, upstream_release, upstream_repo, "
        "binary_url, manifest_url, binary_sha256, binary_size_bytes, state, built_at) "
        "VALUES ('v1', 'r', 'r', 'u', 'm', 'sha', 1, 'candidate', '2026-05-25T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO ru_boxes (box_id, provider, region, sni, state, image_version, created_at) "
        "VALUES ('b1', 'p', 'r', 'sni-b1', 'live', 'v1', '2026-05-25T00:00:00Z')"
    )
    conn.commit()
    conn.close()
    run(["vantage-add", "v1", "--label", "kz1", "--source-kind", "x",
         "--db-path", str(db)])
    # state=candidate -> probe-record refuses
    rc = run(["probe-record",
              "--box-id", "b1",
              "--vantage", "v1",
              "--check", "surface_scan",
              "--status", "pass",
              "--cycle-at", "2026-05-25T01:00:00Z",
              "--db-path", str(db)])
    assert rc == 2


def test_cli_probe_record_happy_path(tmp_path, age_recipient, capsys):
    db = _h_init(tmp_path, age_recipient)
    from mthydra.controller.state.db import connect
    conn = connect(db)
    conn.execute(
        "INSERT INTO ru_images (image_version, upstream_release, upstream_repo, "
        "binary_url, manifest_url, binary_sha256, binary_size_bytes, state, built_at) "
        "VALUES ('v1', 'r', 'r', 'u', 'm', 'sha', 1, 'candidate', '2026-05-25T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO ru_boxes (box_id, provider, region, sni, state, image_version, created_at) "
        "VALUES ('b1', 'p', 'r', 'sni-b1', 'live', 'v1', '2026-05-25T00:00:00Z')"
    )
    conn.commit()
    conn.close()
    run(["vantage-add", "v1", "--label", "kz1", "--source-kind", "x",
         "--db-path", str(db)])
    run(["vantage-attest-active", "v1", "--db-path", str(db)])
    rc = run(["probe-record",
              "--box-id", "b1",
              "--vantage", "v1",
              "--check", "tls_fall_through",
              "--status", "pass",
              "--cycle-at", "2026-05-25T01:00:00Z",
              "--db-path", str(db)])
    assert rc == 0
    conn = connect(db)
    n = conn.execute("SELECT COUNT(*) FROM probe_results").fetchone()[0]
    assert n == 1


def test_cli_probe_evaluate_returns_verdict(tmp_path, age_recipient, capsys):
    db = _h_init(tmp_path, age_recipient)
    cfg = _h_cfg(tmp_path)
    # Extend the cfg file with [probe] section so probe-evaluate's config loads.
    cfg.write_text(cfg.read_text() + "\n[probe]\nsoft_fail_window_M=4\n"
                   "soft_fail_threshold_N=3\nmin_distinct_vantages=2\n"
                   "coverage_window_seconds=3600\nprobe_vantage_ttl_days=14\n"
                   "probe_audit_sweep_interval='5m'\n")
    from mthydra.controller.state.db import connect
    conn = connect(db)
    conn.execute(
        "INSERT INTO ru_images (image_version, upstream_release, upstream_repo, "
        "binary_url, manifest_url, binary_sha256, binary_size_bytes, state, built_at) "
        "VALUES ('v1', 'r', 'r', 'u', 'm', 'sha', 1, 'candidate', '2026-05-25T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO ru_boxes (box_id, provider, region, sni, state, image_version, created_at) "
        "VALUES ('b1', 'p', 'r', 'sni-b1', 'live', 'v1', '2026-05-25T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO image_profiles (image_version, profile_json, recorded_at, recorded_by) "
        "VALUES ('v1', '{}', '2026-05-25T00:00:00Z', 'op')"
    )
    conn.commit()
    conn.close()
    capsys.readouterr()
    rc = run(["probe-evaluate", "--box-id", "b1", "--json",
              "--db-path", str(db), "--config", str(cfg)])
    assert rc == 0
    import json as _json
    out = _json.loads(capsys.readouterr().out)
    assert out["verdict"] == "healthy"


def test_cli_probe_due_json(tmp_path, age_recipient, capsys):
    db = _h_init(tmp_path, age_recipient)
    from mthydra.controller.state.db import connect
    conn = connect(db)
    conn.execute(
        "INSERT INTO obligation_clocks (obligation_id, last_proven_at, proven_by, next_due_at) "
        "VALUES ('probe_kill_pending::b1', ?, 'probe_audit_sweep', ?)",
        ("2026-05-25T00:00:00Z", "2026-05-25T00:00:00Z"),
    )
    conn.commit()
    conn.close()
    capsys.readouterr()
    rc = run(["probe-due", "--json", "--db-path", str(db)])
    assert rc == 0
    import json as _json
    out = _json.loads(capsys.readouterr().out)
    assert out["kill_pending"] == ["probe_kill_pending::b1"]


# ===== spec J: observability CLI =====


def test_cli_obs_status_json(tmp_path, age_recipient, capsys):
    db = _h_init(tmp_path, age_recipient)
    capsys.readouterr()
    rc = run(["obs-status", "--json", "--db-path", str(db)])
    assert rc == 0
    import json as _json
    out = _json.loads(capsys.readouterr().out)
    assert "summary_line" in out
    assert "counts" in out


def test_cli_obs_alerts_recent_empty(tmp_path, age_recipient, capsys):
    db = _h_init(tmp_path, age_recipient)
    capsys.readouterr()
    rc = run(["obs-alerts-recent", "--json", "--db-path", str(db)])
    assert rc == 0
    import json as _json
    out = _json.loads(capsys.readouterr().out)
    assert out == []


def test_cli_obs_alerts_recent_with_rows(tmp_path, age_recipient, capsys):
    db = _h_init(tmp_path, age_recipient)
    from mthydra.controller.state.alert_log import append
    from mthydra.controller.state.db import connect
    conn = connect(db)
    for i in range(3):
        append(conn, attempted_at=f"2026-05-25T0{i}:00:00Z",
               delivered_at=f"2026-05-25T0{i}:00:01Z",
               sink="telegram", severity="warn", kind="x",
               target=None, dedupe_key=f"k{i}", payload="p", error=None)
    conn.close()
    capsys.readouterr()
    rc = run(["obs-alerts-recent", "--limit", "2", "--json", "--db-path", str(db)])
    assert rc == 0
    import json as _json
    out = _json.loads(capsys.readouterr().out)
    assert len(out) == 2
    assert out[0]["dedupe_key"] == "k2"


def test_cli_obs_alert_test_offline_dispatches(tmp_path, age_recipient, capsys):
    db = _h_init(tmp_path, age_recipient)
    cfg = _h_cfg(tmp_path)
    cfg.write_text(cfg.read_text() + """
[observability]
alerter_sweep_interval = "2m"
heartbeat_interval = "1h"
heartbeat_breach_threshold = 3
[observability.telegram]
bot_token = ""
chat_id = ""
[observability.email]
smtp_host = ""
smtp_port = 587
from_addr = ""
to_addr = ""
username = ""
password = ""
""")
    rc = run([
        "--mode", "offline",
        "obs-alert-test", "--severity", "crit",
        "--message", "deploy-time test",
        "--db-path", str(db), "--config", str(cfg),
    ])
    assert rc == 0
    from mthydra.controller.state.db import connect
    conn = connect(db)
    n = conn.execute(
        "SELECT COUNT(*) FROM alert_log WHERE kind='operator_test'"
    ).fetchone()[0]
    # crit -> two routes (telegram + email)
    assert n == 2
    conn.close()


def test_cli_obs_alert_test_info_refuses(tmp_path, age_recipient, capsys):
    db = _h_init(tmp_path, age_recipient)
    cfg = _h_cfg(tmp_path)
    cfg.write_text(cfg.read_text() + """
[observability]
[observability.telegram]
bot_token = ""
chat_id = ""
[observability.email]
smtp_host = ""
smtp_port = 587
from_addr = ""
to_addr = ""
username = ""
password = ""
""")
    rc = run([
        "--mode", "offline",
        "obs-alert-test", "--severity", "info",
        "--db-path", str(db), "--config", str(cfg),
    ])
    assert rc == 2


def test_cli_obs_heartbeat_now_offline_succeeds(tmp_path, age_recipient, capsys):
    db = _h_init(tmp_path, age_recipient)
    cfg = _h_cfg(tmp_path)
    cfg.write_text(cfg.read_text() + """
[observability]
[observability.telegram]
bot_token = ""
chat_id = ""
[observability.email]
smtp_host = ""
smtp_port = 587
from_addr = ""
to_addr = ""
username = ""
password = ""
""")
    rc = run([
        "--mode", "offline",
        "obs-heartbeat-now",
        "--db-path", str(db), "--config", str(cfg),
    ])
    assert rc == 0
    from mthydra.controller.state.db import connect
    conn = connect(db)
    n = conn.execute(
        "SELECT COUNT(*) FROM alert_log WHERE severity='heartbeat'"
    ).fetchone()[0]
    assert n == 1
    conn.close()


def test_serve_refuses_active_without_observability_credentials(
    tmp_path, age_recipient, capsys, monkeypatch,
):
    """Spec J J-D1: active mode without both sinks configured -> exit 2."""
    from mthydra.controller.cli import run as _run
    db = tmp_path / "state.sqlite"
    cfg_path = tmp_path / "controller.toml"
    cfg_path.write_text(
        "[node]\nrole='active'\nhostname='h'\n"
        "[backup]\nfloor_interval_hours=24\non_change_debounce_seconds=30\n"
        "endpoint='https://example'\nbucket='b'\naccess_key_id='k'\n"
        "[backup.retention]\nkeep_daily=30\nkeep_monthly=12\nobject_lock_days=365\n"
        "[gap_monitor]\npoll_interval_minutes=30\nalarm_threshold_hours=48\n"
        "recipient_email='op@example.org'\n"
        "[descriptor]\nrotation_interval_hours=1\nvalidity_window_hours=24\n"
        "[obligations]\n[obligations.timers_hours]\n"
        "[cover_pool]\nrotation_ttl_days=14\nreverify_after_days=30\n"
        "freeze_threshold=2\nreverify_sweep_interval='1h'\n"
        "rotation_sweep_interval='1h'\nreplenishment_interval_days=90\n"
    )
    _run(["init", "--db-path", str(db),
          "--age-recipient", age_recipient,
          "--provider-credential", "b2=id:secret"])
    recipient_file = tmp_path / "age-recipient.txt"
    recipient_file.write_text(age_recipient + "\n")
    monkeypatch.setattr("mthydra.controller.cli.DEFAULT_RECIPIENT_FILE",
                        str(recipient_file))
    capsys.readouterr()
    rc = _run(["serve", "--db-path", str(db), "--config", str(cfg_path)])
    assert rc == 2
    err = capsys.readouterr().err
    assert "[observability.telegram]" in err


# ===== spec K: distribution CLI =====


def _k_cfg(tmp_path, *, with_dist_creds=True):
    """Build a controller.toml that satisfies dist-publish-now config-load."""
    cfg = tmp_path / "k_cfg.toml"
    dist_block = ""
    if with_dist_creds:
        dist_block = (
            "[distribution.telegram]\nbot_token='dist-token'\n"
            "[distribution.email]\nsmtp_host='smtp.example.org'\nsmtp_port=587\n"
            "from_addr='dist@example.org'\nusername='dist@example.org'\npassword='pw'\n"
        )
    cfg.write_text(
        "[node]\nrole='active'\nhostname='h'\n"
        "[backup]\nfloor_interval_hours=24\non_change_debounce_seconds=30\n"
        "endpoint=''\nbucket='b'\naccess_key_id='k'\n"
        "[backup.retention]\nkeep_daily=30\nkeep_monthly=12\nobject_lock_days=365\n"
        "[gap_monitor]\npoll_interval_minutes=30\nalarm_threshold_hours=48\n"
        "recipient_email='op@example.org'\n"
        "[descriptor]\nrotation_interval_hours=1\nvalidity_window_hours=24\n"
        "[obligations]\n[obligations.timers_hours]\n"
        "[cover_pool]\nrotation_ttl_days=14\nreverify_after_days=30\n"
        "freeze_threshold=2\nreverify_sweep_interval='1h'\n"
        "rotation_sweep_interval='1h'\nreplenishment_interval_days=90\n"
        + dist_block
    )
    return cfg


def test_cli_user_channels_set_and_show(tmp_path, age_recipient, capsys):
    db = _h_init(tmp_path, age_recipient)
    from mthydra.controller.state.db import connect
    conn = connect(db)
    conn.execute(
        "INSERT INTO users (user_id, display_name, out_of_band_channel, added_at) "
        "VALUES ('u1', NULL, 'email', '2026-05-25T00:00:00Z')"
    )
    conn.commit()
    conn.close()
    rc = run(["user-channels-set", "u1",
              "--telegram", "12345",
              "--email", "u1@example.org",
              "--db-path", str(db)])
    assert rc == 0
    capsys.readouterr()
    rc = run(["user-channels-show", "u1", "--json", "--db-path", str(db)])
    assert rc == 0
    import json as _json
    out = _json.loads(capsys.readouterr().out)
    assert out["telegram_chat_id"] == "12345"
    assert out["email_addr"] == "u1@example.org"


def test_cli_user_channels_set_refuses_no_args(tmp_path, age_recipient, capsys):
    db = _h_init(tmp_path, age_recipient)
    rc = run(["user-channels-set", "u1", "--db-path", str(db)])
    assert rc == 2


def test_cli_user_channels_show_missing(tmp_path, age_recipient, capsys):
    db = _h_init(tmp_path, age_recipient)
    rc = run(["user-channels-show", "u-nope", "--db-path", str(db)])
    assert rc == 2


def test_cli_user_channels_list_json(tmp_path, age_recipient, capsys):
    db = _h_init(tmp_path, age_recipient)
    from mthydra.controller.state.db import connect
    conn = connect(db)
    conn.execute(
        "INSERT INTO users (user_id, display_name, out_of_band_channel, added_at) "
        "VALUES ('u1', NULL, 'email', '2026-05-25T00:00:00Z')"
    )
    conn.commit()
    conn.close()
    run(["user-channels-set", "u1", "--telegram", "t1", "--db-path", str(db)])
    capsys.readouterr()
    rc = run(["user-channels-list", "--json", "--db-path", str(db)])
    assert rc == 0
    import json as _json
    out = _json.loads(capsys.readouterr().out)
    assert len(out) == 1
    assert out[0]["telegram_chat_id"] == "t1"


def test_cli_dist_status_json(tmp_path, age_recipient, capsys):
    db = _h_init(tmp_path, age_recipient)
    capsys.readouterr()
    rc = run(["dist-status", "--json", "--db-path", str(db)])
    assert rc == 0
    import json as _json
    out = _json.loads(capsys.readouterr().out)
    assert out == []


def test_cli_dist_publish_now_offline(tmp_path, age_recipient, capsys):
    """dist-publish-now with no users -> succeeds with zero dispatched."""
    db = _h_init(tmp_path, age_recipient)
    cfg = _k_cfg(tmp_path)
    rc = run([
        "--mode", "offline",
        "dist-publish-now", "--user-id", "u1",
        "--db-path", str(db), "--config", str(cfg),
    ])
    assert rc == 0


def test_cli_dist_test_offline_dispatches(tmp_path, age_recipient, capsys):
    db = _h_init(tmp_path, age_recipient)
    cfg = _k_cfg(tmp_path)
    from mthydra.controller.state.db import connect
    conn = connect(db)
    conn.execute(
        "INSERT INTO users (user_id, display_name, out_of_band_channel, added_at) "
        "VALUES ('u1', NULL, 'email', '2026-05-25T00:00:00Z')"
    )
    conn.commit()
    conn.close()
    run(["user-channels-set", "u1",
         "--telegram", "12345", "--email", "u1@example.org",
         "--db-path", str(db)])
    rc = run([
        "--mode", "offline",
        "dist-test", "--user-id", "u1",
        "--db-path", str(db), "--config", str(cfg),
    ])
    assert rc == 0
    from mthydra.controller.state.db import connect
    conn = connect(db)
    n = conn.execute(
        "SELECT COUNT(*) FROM distribution_log WHERE kind='test'"
    ).fetchone()[0]
    assert n == 2
    conn.close()


def test_cli_dist_test_user_without_channels(tmp_path, age_recipient, capsys):
    db = _h_init(tmp_path, age_recipient)
    cfg = _k_cfg(tmp_path)
    from mthydra.controller.state.db import connect
    conn = connect(db)
    conn.execute(
        "INSERT INTO users (user_id, display_name, out_of_band_channel, added_at) "
        "VALUES ('u1', NULL, 'email', '2026-05-25T00:00:00Z')"
    )
    conn.commit()
    conn.close()
    rc = run([
        "--mode", "offline",
        "dist-test", "--user-id", "u1",
        "--db-path", str(db), "--config", str(cfg),
    ])
    assert rc == 2


def test_cli_dist_log_recent_json(tmp_path, age_recipient, capsys):
    db = _h_init(tmp_path, age_recipient)
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.distribution_log import append
    conn = connect(db)
    conn.execute(
        "INSERT INTO users (user_id, display_name, out_of_band_channel, added_at) "
        "VALUES ('u1', NULL, 'email', '2026-05-25T00:00:00Z')"
    )
    conn.commit()
    for i in range(3):
        append(conn, user_id="u1", channel="telegram", kind="subset_delta",
               attempted_at=f"2026-05-25T0{i}:00:00Z",
               delivered_at=f"2026-05-25T0{i}:00:01Z",
               subset_hash=f"h{i}", payload_json='[]', error=None)
    conn.close()
    capsys.readouterr()
    rc = run(["dist-log-recent", "--limit", "2", "--json", "--db-path", str(db)])
    assert rc == 0
    import json as _json
    out = _json.loads(capsys.readouterr().out)
    assert len(out) == 2
    assert out[0]["subset_hash"] == "h2"


def test_serve_refuses_active_without_distribution_credentials(
    tmp_path, age_recipient, capsys, monkeypatch,
):
    """Spec K K-D10: active mode without distribution sinks -> exit 2."""
    from mthydra.controller.cli import run as _run
    db = tmp_path / "state.sqlite"
    cfg_path = tmp_path / "controller.toml"
    # Has observability creds (so spec J refusal does NOT fire first) but no
    # distribution creds.
    cfg_path.write_text(
        "[node]\nrole='active'\nhostname='h'\n"
        "[backup]\nfloor_interval_hours=24\non_change_debounce_seconds=30\n"
        "endpoint='https://example'\nbucket='b'\naccess_key_id='k'\n"
        "[backup.retention]\nkeep_daily=30\nkeep_monthly=12\nobject_lock_days=365\n"
        "[gap_monitor]\npoll_interval_minutes=30\nalarm_threshold_hours=48\n"
        "recipient_email='op@example.org'\n"
        "[descriptor]\nrotation_interval_hours=1\nvalidity_window_hours=24\n"
        "[obligations]\n[obligations.timers_hours]\n"
        "[cover_pool]\nrotation_ttl_days=14\nreverify_after_days=30\n"
        "freeze_threshold=2\nreverify_sweep_interval='1h'\n"
        "rotation_sweep_interval='1h'\nreplenishment_interval_days=90\n"
        "[observability.telegram]\nbot_token='t'\nchat_id='c'\n"
        "[observability.email]\nsmtp_host='s'\nsmtp_port=587\n"
        "from_addr='f'\nto_addr='t'\nusername='u'\npassword='p'\n"
    )
    _run(["init", "--db-path", str(db),
          "--age-recipient", age_recipient,
          "--provider-credential", "b2=id:secret"])
    recipient_file = tmp_path / "age-recipient.txt"
    recipient_file.write_text(age_recipient + "\n")
    monkeypatch.setattr("mthydra.controller.cli.DEFAULT_RECIPIENT_FILE",
                        str(recipient_file))
    capsys.readouterr()
    rc = _run(["serve", "--db-path", str(db), "--config", str(cfg_path)])
    assert rc == 2
    err = capsys.readouterr().err
    assert "[distribution.telegram]" in err


# ===== spec D2: provision-seed --canary + canary clear =====


def test_provision_seed_canary_flag_marks_row(tmp_path, age_recipient, monkeypatch):
    """Spec D2: --canary flag plumbs through to ru_boxes.is_canary."""
    from mthydra.controller.cli import run
    db = tmp_path / "state.sqlite"
    cfg_path = tmp_path / "controller.toml"
    cfg_path.write_text(_MIN_TOML)

    from mthydra.controller.backup.s3_dest import S3Destination
    monkeypatch.setattr(
        S3Destination, "presigned_image_url",
        lambda self, *, image_version, ttl_seconds=3600: (
            f"https://b2.example/{image_version}/mtg?sig=stub",
            "2026-05-21T01:00:00Z",
        ),
    )
    _setup_provision_prereqs(db, age_recipient, cfg_path)
    rc = run(["provision-seed",
              "--provider", "hetzner", "--region", "fsn1",
              "--canary",
              "--db-path", str(db), "--config", str(cfg_path),
              *_PROVISION_V2_ARGS])
    assert rc == 0
    from mthydra.controller.state.db import connect
    conn = connect(db)
    row = conn.execute(
        "SELECT is_canary FROM ru_boxes ORDER BY box_id DESC LIMIT 1"
    ).fetchone()
    assert row[0] == 1
    conn.close()


def test_provision_seed_without_canary_marks_zero(tmp_path, age_recipient, monkeypatch):
    """Spec D2: without --canary, is_canary defaults to 0."""
    from mthydra.controller.cli import run
    db = tmp_path / "state.sqlite"
    cfg_path = tmp_path / "controller.toml"
    cfg_path.write_text(_MIN_TOML)

    from mthydra.controller.backup.s3_dest import S3Destination
    monkeypatch.setattr(
        S3Destination, "presigned_image_url",
        lambda self, *, image_version, ttl_seconds=3600: (
            f"https://b2.example/{image_version}/mtg?sig=stub",
            "2026-05-21T01:00:00Z",
        ),
    )
    _setup_provision_prereqs(db, age_recipient, cfg_path)
    rc = run(["provision-seed",
              "--provider", "hetzner", "--region", "fsn1",
              "--db-path", str(db), "--config", str(cfg_path),
              *_PROVISION_V2_ARGS])
    assert rc == 0
    from mthydra.controller.state.db import connect
    conn = connect(db)
    row = conn.execute(
        "SELECT is_canary FROM ru_boxes ORDER BY box_id DESC LIMIT 1"
    ).fetchone()
    assert row[0] == 0
    conn.close()


# ===== spec D2: image-promote-status, image-rollback, ru-box-canary-clear =====


def _seed_canary_cohort_for_image(conn, image_version):
    """Helper: pin profile, attest 2 vantages, provision + live one canary,
    record 4 passing probe rows (2 per vantage). Returns the canary box_id."""
    from mthydra.controller.state.image_profiles import pin
    from mthydra.controller.state.probe_results import record
    from mthydra.controller.state.probe_vantages import add_candidate, attest_active
    from mthydra.controller.state.ru_boxes import insert_box, mark_live
    pin(conn, image_version=image_version, profile_json='{}',
        recorded_by="op", at="2026-05-25T00:00:00Z")
    add_candidate(conn, vantage_id="vk", label="kz1", source_kind="x",
                  at="2026-05-25T00:00:00Z")
    attest_active(conn, "vk", at="2026-05-25T00:00:00Z")
    add_candidate(conn, vantage_id="vb", label="by1", source_kind="x",
                  at="2026-05-25T00:00:00Z")
    attest_active(conn, "vb", at="2026-05-25T00:00:00Z")
    insert_box(conn, "b-canary", "p", "r", "10.0.0.1", "sni-canary",
               image_version, "2026-05-25T00:00:00Z", is_canary=True)
    mark_live(conn, "b-canary", public_ip="10.0.0.1",
              at="2026-05-25T00:01:00Z")
    for i, vid in enumerate(["vk", "vk", "vb", "vb"]):
        record(conn, box_id="b-canary", vantage_id=vid,
               cycle_at=f"2026-05-25T0{i + 2}:00:00Z",
               check_type="surface_scan", status="pass",
               evidence_json=None, image_version=image_version,
               recorded_at=f"2026-05-25T0{i + 2}:00:01Z")
    return "b-canary"


def test_image_promote_status_failing_gate(tmp_path, age_recipient, capsys):
    from mthydra.controller.cli import run
    db = tmp_path / "state.sqlite"
    cfg_path = tmp_path / "controller.toml"
    cfg_path.write_text(_MIN_TOML)
    run(["init", "--db-path", str(db),
         "--age-recipient", age_recipient,
         "--provider-credential", "b2=id:secret"])
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.ru_images import insert_candidate
    conn = connect(db)
    insert_candidate(
        conn, image_version="iv1", upstream_release="v",
        upstream_repo="r", binary_url="x", manifest_url="x",
        binary_sha256="x", binary_size_bytes=1, built_at="2026-05-25T00:00:00Z",
    )
    conn.close()
    capsys.readouterr()
    rc = run(["image-promote-status", "iv1", "--json",
              "--db-path", str(db), "--config", str(cfg_path)])
    # Always exit 0 (read-only).
    assert rc == 0
    import json as _json
    out = _json.loads(capsys.readouterr().out)
    assert out["passed"] is False
    assert any("image_profiles row missing" in r for r in out["reasons"])


def test_image_promote_status_passing_gate(tmp_path, age_recipient, capsys):
    from mthydra.controller.cli import run
    db = tmp_path / "state.sqlite"
    cfg_path = tmp_path / "controller.toml"
    cfg_path.write_text(_MIN_TOML)
    run(["init", "--db-path", str(db),
         "--age-recipient", age_recipient,
         "--provider-credential", "b2=id:secret"])
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.ru_images import insert_candidate
    conn = connect(db)
    insert_candidate(
        conn, image_version="iv1", upstream_release="v",
        upstream_repo="r", binary_url="x", manifest_url="x",
        binary_sha256="x", binary_size_bytes=1, built_at="2026-05-25T00:00:00Z",
    )
    _seed_canary_cohort_for_image(conn, "iv1")
    conn.close()
    capsys.readouterr()
    rc = run(["image-promote-status", "iv1", "--json",
              "--db-path", str(db), "--config", str(cfg_path)])
    assert rc == 0
    import json as _json
    out = _json.loads(capsys.readouterr().out)
    assert out["passed"] is True
    assert out["canary_probe_rows"] == 4
    assert out["canary_distinct_vantages"] == 2


def test_image_rollback_happy_path(tmp_path, age_recipient, capsys):
    """Spec D2: rollback retires source, re-promotes target, emits per-box anti."""
    from mthydra.controller.cli import run
    db = tmp_path / "state.sqlite"
    cfg_path = tmp_path / "controller.toml"
    cfg_path.write_text(_MIN_TOML)
    run(["init", "--db-path", str(db),
         "--age-recipient", age_recipient,
         "--provider-credential", "b2=id:secret"])
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.ru_boxes import insert_box, mark_live
    from mthydra.controller.state.ru_images import insert_candidate, promote
    conn = connect(db)
    # Two images: iv0 (previously promoted, then retired) + iv1 (currently promoted).
    insert_candidate(conn, image_version="iv0", upstream_release="v0",
                     upstream_repo="r", binary_url="x", manifest_url="x",
                     binary_sha256="x", binary_size_bytes=1,
                     built_at="2026-05-25T00:00:00Z")
    promote(conn, "iv0", at="2026-05-25T00:01:00Z", evidence="iv0 first")
    insert_candidate(conn, image_version="iv1", upstream_release="v1",
                     upstream_repo="r", binary_url="x", manifest_url="x",
                     binary_sha256="x2", binary_size_bytes=1,
                     built_at="2026-05-25T01:00:00Z")
    promote(conn, "iv1", at="2026-05-25T01:01:00Z", evidence="iv1 promo")
    # Two live boxes on iv1 to be flagged.
    insert_box(conn, "b1", "p", "r", "10.0.0.1", "sni-b1", "iv1",
               "2026-05-25T01:02:00Z")
    insert_box(conn, "b2", "p", "r", "10.0.0.2", "sni-b2", "iv1",
               "2026-05-25T01:02:00Z")
    mark_live(conn, "b1", public_ip="10.0.0.1", at="2026-05-25T01:03:00Z")
    mark_live(conn, "b2", public_ip="10.0.0.2", at="2026-05-25T01:03:00Z")
    conn.close()
    rc = run(["image-rollback", "iv1",
              "--to", "iv0",
              "--evidence", "iv1 regressed on TLS handshake",
              "--db-path", str(db)])
    assert rc == 0
    conn = connect(db)
    # iv1 retired, iv0 re-promoted.
    iv1_state, iv0_state = (
        conn.execute(
            "SELECT state FROM ru_images WHERE image_version=?", (v,)
        ).fetchone()[0]
        for v in ("iv1", "iv0")
    )
    assert iv1_state == "retired"
    assert iv0_state == "promoted"
    # Per-box rollback_pending rows.
    pending = {
        r[0] for r in conn.execute(
            "SELECT obligation_id FROM obligation_clocks "
            "WHERE obligation_id LIKE 'image_rollback_pending::%'"
        ).fetchall()
    }
    assert pending == {
        "image_rollback_pending::b1",
        "image_rollback_pending::b2",
    }
    conn.close()


def test_image_rollback_refuses_target_never_promoted(tmp_path, age_recipient, capsys):
    """Spec D2: --to must reference a previously-promoted image."""
    from mthydra.controller.cli import run
    db = tmp_path / "state.sqlite"
    run(["init", "--db-path", str(db),
         "--age-recipient", age_recipient,
         "--provider-credential", "b2=id:secret"])
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.ru_images import insert_candidate, promote
    conn = connect(db)
    insert_candidate(conn, image_version="iv_candidate",
                     upstream_release="v", upstream_repo="r",
                     binary_url="x", manifest_url="x",
                     binary_sha256="x", binary_size_bytes=1,
                     built_at="2026-05-25T00:00:00Z")
    insert_candidate(conn, image_version="iv_promo",
                     upstream_release="v2", upstream_repo="r",
                     binary_url="x", manifest_url="x",
                     binary_sha256="x2", binary_size_bytes=1,
                     built_at="2026-05-25T00:00:00Z")
    promote(conn, "iv_promo", at="2026-05-25T00:01:00Z", evidence="x")
    conn.close()
    rc = run(["image-rollback", "iv_promo",
              "--to", "iv_candidate",
              "--evidence", "x",
              "--db-path", str(db)])
    assert rc == 2
    assert "never promoted" in capsys.readouterr().err


def test_image_rollback_refuses_same_source_and_target(tmp_path, age_recipient, capsys):
    from mthydra.controller.cli import run
    db = tmp_path / "state.sqlite"
    run(["init", "--db-path", str(db),
         "--age-recipient", age_recipient,
         "--provider-credential", "b2=id:secret"])
    rc = run(["image-rollback", "iv1", "--to", "iv1",
              "--evidence", "x", "--db-path", str(db)])
    assert rc == 2


def test_ru_box_canary_clear_demotes_and_audits(tmp_path, age_recipient, capsys):
    from mthydra.controller.cli import run
    db = tmp_path / "state.sqlite"
    run(["init", "--db-path", str(db),
         "--age-recipient", age_recipient,
         "--provider-credential", "b2=id:secret"])
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.ru_boxes import insert_box
    conn = connect(db)
    insert_box(conn, "b1", "p", "r", "10.0.0.1", "sni-b1",
               "v1", "2026-05-25T00:00:00Z", is_canary=True)
    conn.close()
    rc = run(["ru-box-canary-clear", "b1", "--reason", "soak done",
              "--db-path", str(db)])
    assert rc == 0
    conn = connect(db)
    row = conn.execute(
        "SELECT is_canary FROM ru_boxes WHERE box_id='b1'"
    ).fetchone()
    assert row[0] == 0
    audits = conn.execute(
        "SELECT action, target FROM audit_log WHERE action='ru_box_canary_clear'"
    ).fetchall()
    assert audits == [("ru_box_canary_clear", "b1")]
    conn.close()


def test_ru_box_canary_clear_refuses_non_canary(tmp_path, age_recipient, capsys):
    from mthydra.controller.cli import run
    db = tmp_path / "state.sqlite"
    run(["init", "--db-path", str(db),
         "--age-recipient", age_recipient,
         "--provider-credential", "b2=id:secret"])
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.ru_boxes import insert_box
    conn = connect(db)
    insert_box(conn, "b1", "p", "r", "10.0.0.1", "sni-b1",
               "v1", "2026-05-25T00:00:00Z", is_canary=False)
    conn.close()
    rc = run(["ru-box-canary-clear", "b1", "--reason", "x",
              "--db-path", str(db)])
    assert rc == 2


# ===== spec M: compact-logs CLI =====


def test_cli_compact_logs_dry_run_default(tmp_path, age_recipient, capsys):
    from mthydra.controller.cli import run
    from mthydra.controller.state.db import connect
    db = tmp_path / "state.sqlite"
    run(["init", "--db-path", str(db),
         "--age-recipient", age_recipient,
         "--provider-credential", "b2=id:secret"])
    conn = connect(db)
    for i in range(3):
        conn.execute(
            "INSERT INTO alert_log (attempted_at, delivered_at, sink, severity, "
            "kind, target, dedupe_key, payload) "
            "VALUES (?, ?, 'telegram', 'warn', 'k', NULL, ?, 'p')",
            (f"2026-05-2{i}T00:00:00Z", f"2026-05-2{i}T00:00:01Z", f"d{i}"),
        )
    conn.commit()
    conn.close()
    capsys.readouterr()
    # Default = dry-run; no --evidence required.
    rc = run(["compact-logs", "--table", "alert_log",
              "--before", "2026-05-22T00:00:00Z",
              "--db-path", str(db)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "would delete 2 row(s)" in out
    # Rows still present.
    conn = connect(db)
    n = conn.execute("SELECT COUNT(*) FROM alert_log").fetchone()[0]
    assert n == 3
    conn.close()


def test_cli_compact_logs_real_run_requires_evidence(tmp_path, age_recipient, capsys):
    from mthydra.controller.cli import run
    db = tmp_path / "state.sqlite"
    run(["init", "--db-path", str(db),
         "--age-recipient", age_recipient,
         "--provider-credential", "b2=id:secret"])
    rc = run(["compact-logs", "--table", "alert_log",
              "--before", "2026-05-22T00:00:00Z",
              "--no-dry-run",
              "--db-path", str(db)])
    assert rc == 2
    assert "--evidence required" in capsys.readouterr().err


def test_cli_compact_logs_real_run_deletes(tmp_path, age_recipient, capsys):
    from mthydra.controller.cli import run
    from mthydra.controller.state.db import connect
    db = tmp_path / "state.sqlite"
    run(["init", "--db-path", str(db),
         "--age-recipient", age_recipient,
         "--provider-credential", "b2=id:secret"])
    conn = connect(db)
    for i in range(3):
        conn.execute(
            "INSERT INTO alert_log (attempted_at, delivered_at, sink, severity, "
            "kind, target, dedupe_key, payload) "
            "VALUES (?, ?, 'telegram', 'warn', 'k', NULL, ?, 'p')",
            (f"2026-05-2{i}T00:00:00Z", f"2026-05-2{i}T00:00:01Z", f"d{i}"),
        )
    conn.commit()
    conn.close()
    rc = run(["compact-logs", "--table", "alert_log",
              "--before", "2026-05-22T00:00:00Z",
              "--no-dry-run",
              "--evidence", "monthly retention purge",
              "--db-path", str(db)])
    assert rc == 0
    conn = connect(db)
    n = conn.execute("SELECT COUNT(*) FROM alert_log").fetchone()[0]
    assert n == 1
    conn.close()


def test_cli_compact_logs_table_all_iterates(tmp_path, age_recipient, capsys):
    """--table all loops over alert_log + probe_results + distribution_log."""
    from mthydra.controller.cli import run
    db = tmp_path / "state.sqlite"
    run(["init", "--db-path", str(db),
         "--age-recipient", age_recipient,
         "--provider-credential", "b2=id:secret"])
    capsys.readouterr()
    rc = run(["compact-logs", "--table", "all",
              "--before", "2026-05-22T00:00:00Z",
              "--db-path", str(db)])
    assert rc == 0
    out = capsys.readouterr().out
    for t in ("alert_log", "probe_results", "distribution_log"):
        assert t in out


# ===== spec J2: obs-alert-ack CLI =====


def test_cli_obs_alert_ack_default_24h(tmp_path, age_recipient, capsys):
    from mthydra.controller.cli import run
    db = tmp_path / "state.sqlite"
    run(["init", "--db-path", str(db),
         "--age-recipient", age_recipient,
         "--provider-credential", "b2=id:secret"])
    rc = run(["obs-alert-ack", "probe_kill_pending::b1",
              "--evidence", "aware, replacing box",
              "--db-path", str(db)])
    assert rc == 0
    from mthydra.controller.state.alert_acks import list_active
    from mthydra.controller.state.db import connect
    conn = connect(db)
    active = list_active(conn, now="2030-01-01T00:00:00Z")
    # Default 24h => expires long before 2030; should be empty.
    assert active == []
    # But active in the near future:
    active_now = list_active(conn, now="2026-05-26T01:00:00Z")
    assert any(a.dedupe_key == "probe_kill_pending::b1" for a in active_now)
    conn.close()


def test_cli_obs_alert_ack_custom_expires(tmp_path, age_recipient, capsys):
    from mthydra.controller.cli import run
    db = tmp_path / "state.sqlite"
    run(["init", "--db-path", str(db),
         "--age-recipient", age_recipient,
         "--provider-credential", "b2=id:secret"])
    rc = run(["obs-alert-ack", "k",
              "--evidence", "ev",
              "--expires-in", "5d",
              "--db-path", str(db)])
    assert rc == 0


def test_cli_obs_alert_ack_refuses_excessive_duration(tmp_path, age_recipient, capsys):
    from mthydra.controller.cli import run
    db = tmp_path / "state.sqlite"
    run(["init", "--db-path", str(db),
         "--age-recipient", age_recipient,
         "--provider-credential", "b2=id:secret"])
    rc = run(["obs-alert-ack", "k",
              "--evidence", "ev",
              "--expires-in", "30d",
              "--db-path", str(db)])
    assert rc == 2
    assert "7d cap" in capsys.readouterr().err


def test_cli_obs_alert_ack_refuses_bad_suffix(tmp_path, age_recipient, capsys):
    from mthydra.controller.cli import run
    db = tmp_path / "state.sqlite"
    run(["init", "--db-path", str(db),
         "--age-recipient", age_recipient,
         "--provider-credential", "b2=id:secret"])
    rc = run(["obs-alert-ack", "k",
              "--evidence", "ev",
              "--expires-in", "5w",
              "--db-path", str(db)])
    assert rc == 2


def test_cli_obs_alert_ack_list_active_only(tmp_path, age_recipient, capsys):
    from mthydra.controller.cli import run
    db = tmp_path / "state.sqlite"
    run(["init", "--db-path", str(db),
         "--age-recipient", age_recipient,
         "--provider-credential", "b2=id:secret"])
    # Two acks: one short-lived (will expire by listing time), one not.
    run(["obs-alert-ack", "long",
         "--evidence", "ev", "--expires-in", "7d",
         "--db-path", str(db)])
    run(["obs-alert-ack", "short",
         "--evidence", "ev", "--expires-in", "1s",
         "--db-path", str(db)])
    # Wait for the 1s ack to expire — list_active uses _now() which advances.
    import time
    time.sleep(2)
    capsys.readouterr()
    rc = run(["obs-alert-ack-list", "--json", "--db-path", str(db)])
    assert rc == 0
    import json as _json
    out = _json.loads(capsys.readouterr().out)
    keys = {r["dedupe_key"] for r in out}
    assert "long" in keys
    assert "short" not in keys


def test_cli_obs_alert_ack_list_include_expired(tmp_path, age_recipient, capsys):
    from mthydra.controller.cli import run
    db = tmp_path / "state.sqlite"
    run(["init", "--db-path", str(db),
         "--age-recipient", age_recipient,
         "--provider-credential", "b2=id:secret"])
    run(["obs-alert-ack", "short",
         "--evidence", "ev", "--expires-in", "1s",
         "--db-path", str(db)])
    import time
    time.sleep(2)
    capsys.readouterr()
    rc = run(["obs-alert-ack-list", "--include-expired", "--json",
              "--db-path", str(db)])
    assert rc == 0
    import json as _json
    out = _json.loads(capsys.readouterr().out)
    keys = {r["dedupe_key"] for r in out}
    assert "short" in keys


# ===== spec I2: probe-credential CLI =====


def test_cli_probe_credential_issue_happy(tmp_path, age_recipient, capsys):
    from mthydra.controller.cli import run
    from mthydra.controller.state.db import connect
    db = tmp_path / "state.sqlite"
    run(["init", "--db-path", str(db),
         "--age-recipient", age_recipient,
         "--provider-credential", "b2=id:secret"])
    # authority-migrate-placeholder: the test bootstrap inserts a real
    # authority, so we don't need to migrate. Seed a box + vantage.
    conn = connect(db)
    conn.execute(
        "INSERT INTO ru_images (image_version, upstream_release, upstream_repo, "
        "binary_url, manifest_url, binary_sha256, binary_size_bytes, state, built_at) "
        "VALUES ('v1', 'r', 'r', 'u', 'm', 'sha', 1, 'candidate', '2026-05-26T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO ru_boxes (box_id, provider, region, sni, state, image_version, created_at) "
        "VALUES ('b1', 'p', 'r', 'sni-b1', 'live', 'v1', '2026-05-26T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO probe_vantages (vantage_id, label, source_kind, state, added_at) "
        "VALUES ('vk', 'kz', 'cloud-cis', 'active', '2026-05-26T00:00:00Z')"
    )
    conn.commit()
    conn.close()
    rc = run(["probe-credential-issue", "--box", "b1", "--vantage", "vk",
              "--evidence", "initial cred", "--db-path", str(db)])
    assert rc == 0
    conn = connect(db)
    n = conn.execute(
        "SELECT COUNT(*) FROM probe_credentials WHERE box_id='b1' AND vantage_id='vk'"
    ).fetchone()[0]
    assert n == 1
    conn.close()


def test_cli_probe_credential_issue_unknown_box(tmp_path, age_recipient, capsys):
    from mthydra.controller.cli import run
    from mthydra.controller.state.db import connect
    db = tmp_path / "state.sqlite"
    run(["init", "--db-path", str(db),
         "--age-recipient", age_recipient,
         "--provider-credential", "b2=id:secret"])
    conn = connect(db)
    conn.execute(
        "INSERT INTO probe_vantages (vantage_id, label, source_kind, state, added_at) "
        "VALUES ('vk', 'kz', 'cloud-cis', 'active', '2026-05-26T00:00:00Z')"
    )
    conn.commit()
    conn.close()
    rc = run(["probe-credential-issue", "--box", "ghost", "--vantage", "vk",
              "--db-path", str(db)])
    assert rc == 2
    assert "unknown box" in capsys.readouterr().err


def test_cli_probe_credential_issue_refuses_non_active_vantage(tmp_path, age_recipient, capsys):
    from mthydra.controller.cli import run
    from mthydra.controller.state.db import connect
    db = tmp_path / "state.sqlite"
    run(["init", "--db-path", str(db),
         "--age-recipient", age_recipient,
         "--provider-credential", "b2=id:secret"])
    conn = connect(db)
    conn.execute(
        "INSERT INTO ru_images (image_version, upstream_release, upstream_repo, "
        "binary_url, manifest_url, binary_sha256, binary_size_bytes, state, built_at) "
        "VALUES ('v1', 'r', 'r', 'u', 'm', 'sha', 1, 'candidate', '2026-05-26T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO ru_boxes (box_id, provider, region, sni, state, image_version, created_at) "
        "VALUES ('b1', 'p', 'r', 'sni-b1', 'live', 'v1', '2026-05-26T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO probe_vantages (vantage_id, label, source_kind, state, added_at) "
        "VALUES ('vk', 'kz', 'cloud-cis', 'candidate', '2026-05-26T00:00:00Z')"
    )
    conn.commit()
    conn.close()
    rc = run(["probe-credential-issue", "--box", "b1", "--vantage", "vk",
              "--db-path", str(db)])
    assert rc == 2
    assert "candidate" in capsys.readouterr().err


def test_cli_probe_credential_list_and_revoke(tmp_path, age_recipient, capsys):
    from mthydra.controller.cli import run
    from mthydra.controller.state.db import connect
    db = tmp_path / "state.sqlite"
    run(["init", "--db-path", str(db),
         "--age-recipient", age_recipient,
         "--provider-credential", "b2=id:secret"])
    conn = connect(db)
    conn.execute(
        "INSERT INTO ru_images (image_version, upstream_release, upstream_repo, "
        "binary_url, manifest_url, binary_sha256, binary_size_bytes, state, built_at) "
        "VALUES ('v1', 'r', 'r', 'u', 'm', 'sha', 1, 'candidate', '2026-05-26T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO ru_boxes (box_id, provider, region, sni, state, image_version, created_at) "
        "VALUES ('b1', 'p', 'r', 'sni-b1', 'live', 'v1', '2026-05-26T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO probe_vantages (vantage_id, label, source_kind, state, added_at) "
        "VALUES ('vk', 'kz', 'cloud-cis', 'active', '2026-05-26T00:00:00Z')"
    )
    conn.commit()
    conn.close()
    run(["probe-credential-issue", "--box", "b1", "--vantage", "vk",
         "--db-path", str(db)])
    capsys.readouterr()
    rc = run(["probe-credential-list", "--json", "--db-path", str(db)])
    assert rc == 0
    import json as _json
    out = _json.loads(capsys.readouterr().out)
    assert len(out) == 1
    cred_id = out[0]["cred_id"]
    # Revoke.
    rc = run(["probe-credential-revoke", cred_id, "--reason", "rotation",
              "--db-path", str(db)])
    assert rc == 0
    # Default list excludes revoked.
    capsys.readouterr()
    run(["probe-credential-list", "--json", "--db-path", str(db)])
    out = _json.loads(capsys.readouterr().out)
    assert out == []
    # --include-revoked shows it.
    capsys.readouterr()
    run(["probe-credential-list", "--include-revoked", "--json",
         "--db-path", str(db)])
    out = _json.loads(capsys.readouterr().out)
    assert len(out) == 1
    assert out[0]["revoked_at"] is not None


def test_cli_probe_credential_revoke_missing(tmp_path, age_recipient, capsys):
    from mthydra.controller.cli import run
    db = tmp_path / "state.sqlite"
    run(["init", "--db-path", str(db),
         "--age-recipient", age_recipient,
         "--provider-credential", "b2=id:secret"])
    rc = run(["probe-credential-revoke", "nope", "--reason", "x",
              "--db-path", str(db)])
    assert rc == 2
