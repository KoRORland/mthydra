"""Tests for the mthydra-ops automation script.

The script wraps mthydra-controller via subprocess. Tests intercept the
subprocess call by monkeypatching `_run_controller` so they don't need
the real binary installed.
"""
from __future__ import annotations

import io
import json
import subprocess
import sys

import pytest

from mthydra.ops import main as ops_main


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


class _FakeRun:
    """Records every call to _run_controller. Returns a configurable result."""

    def __init__(self):
        self.calls: list[list[str]] = []
        self.responses: list[subprocess.CompletedProcess] = []
        self.raise_on: set[int] = set()

    def queue(self, stdout: str = "", stderr: str = "", returncode: int = 0):
        self.responses.append(
            subprocess.CompletedProcess(
                args=[], returncode=returncode, stdout=stdout, stderr=stderr,
            )
        )

    def __call__(self, *args, check=True, capture=False):
        self.calls.append(list(args))
        if not self.responses:
            self.responses.append(
                subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
            )
        res = self.responses.pop(0)
        if check and res.returncode != 0:
            raise subprocess.CalledProcessError(res.returncode, args)
        return res


@pytest.fixture
def fake_run(monkeypatch):
    fake = _FakeRun()
    monkeypatch.setattr(ops_main, "_run_controller", fake)
    return fake


# ---------------------------------------------------------------------------
# bootstrap
# ---------------------------------------------------------------------------


def _bootstrap_args(tmp_path, **overrides):
    db = tmp_path / "state.sqlite"
    cfg = tmp_path / "controller.toml"
    defaults = {
        "--db-path": str(db),
        "--config": str(cfg),
        "--age-recipient": "age1abc",
        "--hostname": "eu1.example.com",
        "--operator-email": "op@example.org",
        "--b2-key-id": "id",
        "--b2-app-key": "secret",
        "--b2-bucket": "b",
        "--b2-endpoint": "https://b2.example",
        "--obs-tg-bot-token": "tg-op-token",
        "--obs-tg-chat-id": "12345",
        "--obs-smtp-host": "smtp.example.org",
        "--obs-smtp-port": "587",
        "--obs-smtp-from": "ops@example.org",
        "--obs-smtp-to": "me@example.org",
        "--obs-smtp-user": "ops@example.org",
        "--obs-smtp-pass": "obs-pw",
        "--dist-tg-bot-token": "tg-dist-token",
        "--dist-smtp-host": "smtp.example.org",
        "--dist-smtp-port": "587",
        "--dist-smtp-from": "dist@example.org",
        "--dist-smtp-user": "dist@example.org",
        "--dist-smtp-pass": "dist-pw",
    }
    defaults.update(overrides)
    argv = ["bootstrap"]
    for k, v in defaults.items():
        argv.extend([k, v])
    return argv, db, cfg


def test_bootstrap_calls_init_then_migrate_then_writes_toml(tmp_path, fake_run):
    argv, db, cfg = _bootstrap_args(tmp_path)
    rc = ops_main.main(argv)
    assert rc == 0
    # First call = init, second = authority-migrate-placeholder.
    assert fake_run.calls[0][0] == "init"
    assert "--age-recipient" in fake_run.calls[0]
    assert "age1abc" in fake_run.calls[0]
    assert fake_run.calls[1][0] == "authority-migrate-placeholder"
    # controller.toml landed with our values substituted.
    assert cfg.exists()
    body = cfg.read_text()
    assert 'hostname = "eu1.example.com"' in body
    assert 'bot_token = "tg-op-token"' in body
    assert 'bot_token = "tg-dist-token"' in body
    assert "smtp_port = 587" in body
    # File mode 0600.
    import stat
    mode = stat.S_IMODE(cfg.stat().st_mode)
    assert mode == 0o600


def test_bootstrap_refuses_existing_db_without_force(tmp_path, fake_run):
    argv, db, cfg = _bootstrap_args(tmp_path)
    db.write_text("x")
    rc = ops_main.main(argv)
    assert rc == 2
    assert not fake_run.calls  # never reached subprocess


def test_bootstrap_propagates_init_failure(tmp_path, fake_run):
    argv, db, cfg = _bootstrap_args(tmp_path)
    fake_run.queue(returncode=7)  # init fails
    rc = ops_main.main(argv)
    assert rc == 7
    assert not cfg.exists()  # toml not written


def test_bootstrap_propagates_migrate_failure(tmp_path, fake_run):
    argv, db, cfg = _bootstrap_args(tmp_path)
    fake_run.queue(returncode=0)   # init ok
    fake_run.queue(returncode=4)   # migrate fails
    rc = ops_main.main(argv)
    assert rc == 4
    assert not cfg.exists()


# ---------------------------------------------------------------------------
# preflight
# ---------------------------------------------------------------------------


def test_preflight_runs_three_steps(tmp_path, fake_run):
    rc = ops_main.main([
        "preflight",
        "--db-path", str(tmp_path / "state.sqlite"),
        "--config", str(tmp_path / "controller.toml"),
    ])
    assert rc == 0
    cmds = [c[0] for c in fake_run.calls]
    assert cmds == ["obs-alert-test", "obs-heartbeat-now", "startup-check"]


def test_preflight_fails_fast_if_alert_test_fails(tmp_path, fake_run):
    fake_run.queue(returncode=2)
    rc = ops_main.main([
        "preflight",
        "--db-path", str(tmp_path / "x"),
        "--config", str(tmp_path / "c"),
    ])
    assert rc == 2
    # Only the failing first command should have been attempted.
    assert [c[0] for c in fake_run.calls] == ["obs-alert-test"]


# ---------------------------------------------------------------------------
# daily-check
# ---------------------------------------------------------------------------


def test_daily_check_summarises_status(tmp_path, fake_run, capsys):
    fake_run.queue(stdout=json.dumps({
        "collected_at": "2026-05-26T00:00:00Z",
        "summary_line": "obligations: 5 green, 0 overdue; anti: 0",
        "obligations_overdue": [],
        "anti_obligations": [],
        "eu_nodes": [],
        "counts": {
            "boxes_provisioning": 0, "boxes_live": 3, "boxes_terminated": 1,
            "cover_domains_in_use": 3, "cover_domains_burned": 2,
            "active_vantages": 2, "active_shards": 1,
        },
    }))
    fake_run.queue(stdout="[]")
    rc = ops_main.main(["daily-check", "--db-path", str(tmp_path / "x")])
    assert rc == 0
    out = capsys.readouterr().out
    assert "ALL GREEN" in out
    assert "boxes: provisioning=0 live=3 terminated=1" in out


def test_daily_check_returns_1_on_crit_anti_obligation(tmp_path, fake_run, capsys):
    fake_run.queue(stdout=json.dumps({
        "collected_at": "2026-05-26T00:00:00Z",
        "summary_line": "1 crit",
        "obligations_overdue": [],
        "anti_obligations": [
            {"severity": "crit", "kind": "probe_kill_pending",
             "target": "b1", "obligation_id": "probe_kill_pending::b1"},
        ],
        "eu_nodes": [],
        "counts": {
            "boxes_provisioning": 0, "boxes_live": 1, "boxes_terminated": 0,
            "cover_domains_in_use": 1, "cover_domains_burned": 0,
            "active_vantages": 2, "active_shards": 1,
        },
    }))
    fake_run.queue(stdout="[]")
    rc = ops_main.main(["daily-check", "--db-path", str(tmp_path / "x")])
    assert rc == 1
    out = capsys.readouterr().out
    assert "probe_kill_pending" in out


def test_daily_check_returns_1_on_overdue_obligation(tmp_path, fake_run):
    fake_run.queue(stdout=json.dumps({
        "collected_at": "2026-05-26T00:00:00Z",
        "summary_line": "",
        "obligations_overdue": [
            {"severity": "warn",
             "obligation_id": "probe_audit_sweep_ran",
             "overdue_seconds": 1800},
        ],
        "anti_obligations": [],
        "eu_nodes": [],
        "counts": {
            "boxes_provisioning": 0, "boxes_live": 0, "boxes_terminated": 0,
            "cover_domains_in_use": 0, "cover_domains_burned": 0,
            "active_vantages": 0, "active_shards": 0,
        },
    }))
    fake_run.queue(stdout="[]")
    rc = ops_main.main(["daily-check", "--db-path", str(tmp_path / "x")])
    assert rc == 1


def test_daily_check_lists_silent_delivery_failures(tmp_path, fake_run, capsys):
    fake_run.queue(stdout=json.dumps({
        "collected_at": "X", "summary_line": "",
        "obligations_overdue": [], "anti_obligations": [], "eu_nodes": [],
        "counts": {
            "boxes_provisioning": 0, "boxes_live": 0, "boxes_terminated": 0,
            "cover_domains_in_use": 0, "cover_domains_burned": 0,
            "active_vantages": 0, "active_shards": 0,
        },
    }))
    fake_run.queue(stdout=json.dumps([
        {"id": 5, "attempted_at": "T", "sink": "email", "kind": "k",
         "delivered_at": None, "error": "smtp 530"},
        {"id": 6, "attempted_at": "T", "sink": "telegram", "kind": "k",
         "delivered_at": "T2", "error": None},
    ]))
    ops_main.main(["daily-check", "--db-path", str(tmp_path / "x")])
    out = capsys.readouterr().out
    assert "SILENT-DELIVERY" in out
    assert "smtp 530" in out


# ---------------------------------------------------------------------------
# monthly-compact
# ---------------------------------------------------------------------------


def test_monthly_compact_dry_run_default(tmp_path, fake_run):
    rc = ops_main.main([
        "monthly-compact",
        "--db-path", str(tmp_path / "x"),
        "--days", "30",
    ])
    assert rc == 0
    # Only one call (dry-run), no --no-dry-run flag.
    assert len(fake_run.calls) == 1
    assert "--no-dry-run" not in fake_run.calls[0]


def test_monthly_compact_real_run(tmp_path, fake_run):
    rc = ops_main.main([
        "monthly-compact",
        "--db-path", str(tmp_path / "x"),
        "--days", "30",
        "--no-dry-run",
        "--evidence", "test",
    ])
    assert rc == 0
    # Two calls: dry-run first, then real.
    assert len(fake_run.calls) == 2
    assert "--no-dry-run" in fake_run.calls[1]
    assert "--evidence" in fake_run.calls[1]


def test_monthly_compact_table_passthrough(tmp_path, fake_run):
    rc = ops_main.main([
        "monthly-compact",
        "--db-path", str(tmp_path / "x"),
        "--table", "alert_log",
    ])
    assert rc == 0
    # --table arg appears immediately after the subcommand name.
    assert "alert_log" in fake_run.calls[0]


# ---------------------------------------------------------------------------
# image-build-template
# ---------------------------------------------------------------------------


def test_image_build_template_emits_valid_json(capsys):
    rc = ops_main.main(["image-build-template"])
    assert rc == 0
    out = capsys.readouterr().out
    obj = json.loads(out)
    assert "tls_handshake" in obj
    assert obj["expected_surface"] == [443]
    assert obj["image_version"].startswith("REPLACE_WITH")


# ---------------------------------------------------------------------------
# user-onboard
# ---------------------------------------------------------------------------


def test_user_onboard_three_step_flow(tmp_path, fake_run):
    rc = ops_main.main([
        "user-onboard", "alice",
        "--out-of-band", "signal:+1555",
        "--display-name", "Alice",
        "--chat-id", "12345",
        "--email", "alice@example.org",
        "--db-path", str(tmp_path / "x"),
        "--config", str(tmp_path / "c"),
    ])
    assert rc == 0
    cmds = [c[0] for c in fake_run.calls]
    assert cmds == ["user-add", "user-channels-set", "dist-test"]
    # user-channels-set received both --telegram and --email.
    cs_call = fake_run.calls[1]
    assert "--telegram" in cs_call
    assert "--email" in cs_call


def test_user_onboard_chat_id_only(tmp_path, fake_run):
    """When only Telegram is supplied, --email is omitted from channels-set."""
    rc = ops_main.main([
        "user-onboard", "alice",
        "--out-of-band", "signal:+1",
        "--chat-id", "12345",
        "--db-path", str(tmp_path / "x"),
        "--config", str(tmp_path / "c"),
    ])
    assert rc == 0
    cs_call = fake_run.calls[1]
    assert "--telegram" in cs_call
    assert "--email" not in cs_call


def test_user_onboard_propagates_failure(tmp_path, fake_run):
    fake_run.queue(returncode=2)  # user-add fails
    rc = ops_main.main([
        "user-onboard", "alice",
        "--out-of-band", "signal:+1",
        "--chat-id", "12345",
        "--db-path", str(tmp_path / "x"),
        "--config", str(tmp_path / "c"),
    ])
    assert rc == 2
    assert [c[0] for c in fake_run.calls] == ["user-add"]


# ---------------------------------------------------------------------------
# rotate-vantage
# ---------------------------------------------------------------------------


def test_rotate_vantage_three_step_flow(tmp_path, fake_run):
    rc = ops_main.main([
        "rotate-vantage",
        "--old", "kz1-old",
        "--new", "kz2",
        "--new-label", "kz2",
        "--source-kind", "cloud-cis",
        "--region-hint", "KZ-almaty",
        "--burn-reason", "rotation 30d",
        "--attest-evidence", "fresh probes pass",
        "--db-path", str(tmp_path / "x"),
    ])
    assert rc == 0
    cmds = [c[0] for c in fake_run.calls]
    assert cmds == ["vantage-burn", "vantage-add", "vantage-attest-active"]


def test_rotate_vantage_stops_on_burn_failure(tmp_path, fake_run):
    fake_run.queue(returncode=2)
    rc = ops_main.main([
        "rotate-vantage",
        "--old", "kz1", "--new", "kz2",
        "--new-label", "kz2",
        "--burn-reason", "x", "--attest-evidence", "x",
        "--db-path", str(tmp_path / "x"),
    ])
    assert rc == 2
    assert [c[0] for c in fake_run.calls] == ["vantage-burn"]


# ---------------------------------------------------------------------------
# alert-summary
# ---------------------------------------------------------------------------


def test_alert_summary_aggregates_three_sources(tmp_path, fake_run, capsys):
    fake_run.queue(stdout=json.dumps({"k": "obs"}))
    fake_run.queue(stdout=json.dumps({"k": "probe-due"}))
    fake_run.queue(stdout=json.dumps({"k": "shard-stats"}))
    rc = ops_main.main([
        "alert-summary",
        "--db-path", str(tmp_path / "x"),
        "--config", str(tmp_path / "c"),
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "=== obs-status ===" in out
    assert "=== probe-due ===" in out
    assert "=== shard-stats ===" in out
    assert '"k"' in out


# ---------------------------------------------------------------------------
# setup-host (dry-run path only — root is required for real run)
# ---------------------------------------------------------------------------


def test_setup_host_dry_run_lists_steps(capsys):
    rc = ops_main.main(["setup-host", "--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "DRY-RUN" in out
    assert "apt update" in out
    assert "adduser" in out
    assert "chmod" in out


def test_setup_host_refuses_without_root_or_dry_run(monkeypatch, capsys):
    monkeypatch.setattr("os.geteuid", lambda: 1000)
    rc = ops_main.main(["setup-host"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "must run as root" in err


# ---------------------------------------------------------------------------
# gen-age-key
# ---------------------------------------------------------------------------


def test_gen_age_key_writes_and_prints_pubkey(tmp_path, monkeypatch, capsys):
    """Mock age-keygen to produce a deterministic file."""
    out = tmp_path / "operator.age"

    def _fake_call(cmd):
        # cmd = ['age-keygen', '-o', str(out)]
        if cmd[0] == "age-keygen":
            target = cmd[2]
            from pathlib import Path
            Path(target).write_text(
                "# created: 2026-05-26\n"
                "# public key: age1pubpubpub\n"
                "AGE-SECRET-KEY-XXX\n"
            )
            return 0
        return 1

    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/age-keygen")
    monkeypatch.setattr("subprocess.call", _fake_call)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)  # skips warning

    rc = ops_main.main([
        "gen-age-key",
        "--out", str(out),
        "--yes",
    ])
    assert rc == 0
    msg = capsys.readouterr().out
    assert "age1pubpubpub" in msg
    # mode 0600
    import stat
    assert stat.S_IMODE(out.stat().st_mode) == 0o600


def test_gen_age_key_refuses_existing_without_force(tmp_path, capsys):
    out = tmp_path / "operator.age"
    out.write_text("existing")
    rc = ops_main.main([
        "gen-age-key", "--out", str(out), "--yes",
    ])
    assert rc == 2
    err = capsys.readouterr().err
    assert "refusing to overwrite" in err


def test_gen_age_key_refuses_if_age_keygen_missing(monkeypatch, capsys):
    monkeypatch.setattr("shutil.which", lambda _: None)
    rc = ops_main.main(["gen-age-key", "--out", "/tmp/k.age"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "age-keygen not on PATH" in err


# ---------------------------------------------------------------------------
# ru-provision
# ---------------------------------------------------------------------------


def _ru_provision_argv(tmp_path, **overrides) -> list[str]:
    defaults = {
        "--provider": "selectel",
        "--region": "ru-moscow-1",
        "--db-path": str(tmp_path / "state.sqlite"),
        "--config": str(tmp_path / "controller.toml"),
        "--agent-source-url": "https://files/agent.bin",
        "--agent-source-sha256": "deadbeef",
        "--descriptor-refresh-url": "https://controller/refresh",
    }
    defaults.update(overrides)
    argv = ["ru-provision"]
    for k, v in defaults.items():
        if v is True:
            argv.append(k)
        else:
            argv.extend([k, v])
    return argv


@pytest.fixture
def fake_run_both(monkeypatch):
    """Monkeypatches _run_controller_capture_both alongside _run_controller."""
    fake_both = _FakeRun()
    monkeypatch.setattr(ops_main, "_run_controller_capture_both", fake_both)
    return fake_both


def test_extract_box_id():
    s = "some preamble\nprovision-seed: created box_id=b-12345abc\nmore noise\n"
    assert ops_main._extract_box_id(s) == "b-12345abc"


def test_extract_box_id_missing():
    assert ops_main._extract_box_id("nothing here") is None


def test_ru_provision_emits_cloud_init_and_recipe(
    tmp_path, fake_run, fake_run_both, capsys,
):
    fake_run_both.queue(
        stdout="#cloud-config\nfoo: bar\n",
        stderr="provision-seed: created box_id=b-abc\n",
    )
    rc = ops_main.main(_ru_provision_argv(tmp_path))
    assert rc == 0
    # provision-seed was called once with the provider tag we passed
    assert len(fake_run_both.calls) == 1
    call = fake_run_both.calls[0]
    assert call[0] == "provision-seed"
    assert "selectel" in call
    assert "ru-moscow-1" in call
    # No mark-live: the operator runs it after booting the VM
    assert fake_run.calls == []
    out = capsys.readouterr().out
    assert "#cloud-config" in out
    # The mark-live recipe references the minted box_id
    # (it's on stderr — _say uses stderr)


def test_ru_provision_prints_mark_live_recipe(
    tmp_path, fake_run, fake_run_both, capsys,
):
    fake_run_both.queue(
        stdout="#cloud-config\n",
        stderr="provision-seed: created box_id=b-abc\n",
    )
    ops_main.main(_ru_provision_argv(tmp_path))
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "ru-box-mark-live b-abc" in combined
    # Cordoned-env reminder is present so the operator doesn't reach for Hetzner
    assert "Hetzner" in combined or "Selectel" in combined


def test_ru_provision_writes_cloud_init_to_file_when_requested(
    tmp_path, fake_run, fake_run_both,
):
    fake_run_both.queue(
        stdout="#cloud-config\nfoo: bar\n",
        stderr="provision-seed: created box_id=b-abc\n",
    )
    out_file = tmp_path / "seed.cloud-init"
    rc = ops_main.main(
        _ru_provision_argv(tmp_path, **{"--cloud-init-out": str(out_file)})
    )
    assert rc == 0
    assert out_file.exists()
    assert "#cloud-config" in out_file.read_text()
    import stat
    assert stat.S_IMODE(out_file.stat().st_mode) == 0o600


def test_ru_provision_provision_seed_failure_propagates(
    tmp_path, fake_run, fake_run_both, capsys,
):
    fake_run_both.queue(stderr="boom\n", returncode=7)
    rc = ops_main.main(_ru_provision_argv(tmp_path))
    assert rc == 7
    assert "provision-seed failed" in capsys.readouterr().err


def test_ru_provision_missing_box_id_in_stderr(
    tmp_path, fake_run, fake_run_both, capsys,
):
    fake_run_both.queue(stdout="#cloud-config\n", stderr="some other noise\n")
    rc = ops_main.main(_ru_provision_argv(tmp_path))
    assert rc == 4
    assert "older controller" in capsys.readouterr().err


def test_ru_provision_canary_flag_threaded_through(
    tmp_path, fake_run, fake_run_both,
):
    fake_run_both.queue(
        stdout="#cloud-config\n",
        stderr="provision-seed: created box_id=b-canary\n",
    )
    rc = ops_main.main(_ru_provision_argv(tmp_path) + ["--canary"])
    assert rc == 0
    assert "--canary" in fake_run_both.calls[0]
