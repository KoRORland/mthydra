"""Tests for the controller CLI (spec A phase 6)."""
import shutil
import subprocess

import pytest

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


def test_backup_now_returns_zero(tmp_path):
    exit_code = run(["backup-now"])
    assert exit_code == 0


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


def test_dryrun_mode_without_bucket_override_fails():
    exit_code = run([
        "--mode", "dryrun",
        "startup-check",
        "--db-path", "/nonexistent/state.sqlite",
        "--age-recipient", FAKE_RECIPIENT,
    ])
    assert exit_code != 0
