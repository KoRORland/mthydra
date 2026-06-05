"""Tests for mthydra-ops vantage-setup (T-Task 2)."""
from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

import pytest

from mthydra.ops import vantage_setup


def _args(tmp_path, **over):
    base = dict(
        vantage_id="ru-msk-1",
        vantage_host="203.0.113.5",
        vantage_port=22,
        root_key=str(tmp_path / "root.pem"),
        ssh_dir=str(tmp_path / "ssh"),
        db_path=str(tmp_path / "db.sqlite"),
    )
    base.update(over)
    return argparse.Namespace(**base)


def _fake_run_factory(history):
    def _fake(argv, capture_output=True, text=True, timeout=None, input=None, **kwargs):
        history.append({"argv": argv, "input": input})
        if argv[0] == "ssh-keygen":
            # Side-effect: write both private and .pub files so ensure_probe_key
            # can read them back (it reads private to compare against DB row).
            for i, tok in enumerate(argv):
                if tok == "-f" and i + 1 < len(argv):
                    Path(argv[i + 1]).write_text("PRIV\n")
                    Path(argv[i + 1] + ".pub").write_text(
                        "ssh-ed25519 AAAAFAKEKEY mthydra-probe-runner\n")
                    break
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[0] == "ssh-keyscan":
            return subprocess.CompletedProcess(
                argv, 0, "|1|hashed|x= ssh-ed25519 AAAA...\n", "")
        if argv[0] == "ssh":
            # Provision script — must read 'OK' from stdout
            return subprocess.CompletedProcess(argv, 0, "OK\n", "")
        if "mthydra-controller" in argv[0]:
            return subprocess.CompletedProcess(argv, 0, "vantage-set-ssh: ru-msk-1 updated\n", "")
        return subprocess.CompletedProcess(argv, 0, "", "")
    return _fake


def test_cmd_vantage_setup_happy_path(tmp_path, monkeypatch):
    """Smoke: keygen → ssh provision → keyscan → vantage-set-ssh. Each step
    appears exactly once in the subprocess history."""
    (tmp_path / "root.pem").write_text("-----BEGIN PRIVATE KEY-----\nfake\n")

    history: list[dict] = []
    monkeypatch.setattr(vantage_setup.subprocess, "run",
                        _fake_run_factory(history))

    rc = vantage_setup.cmd_vantage_setup(_args(tmp_path))
    assert rc == 0

    bins = [h["argv"][0] for h in history]
    assert bins.count("ssh-keygen") == 1
    assert bins.count("ssh") == 1
    assert bins.count("ssh-keyscan") == 1
    # Last call must be vantage-set-ssh through the controller binary.
    controller_calls = [h for h in history if "mthydra-controller" in h["argv"][0]]
    assert len(controller_calls) == 1
    assert "vantage-set-ssh" in controller_calls[0]["argv"]


def test_cmd_vantage_setup_refuses_missing_root_key(tmp_path):
    """If --root-key doesn't exist, exit cleanly without touching ssh."""
    rc = vantage_setup.cmd_vantage_setup(_args(tmp_path))
    assert rc == 2


def test_cmd_vantage_setup_registers_shared_key(tmp_path, monkeypatch):
    """vantage-setup resolves the shared probe key from the DB and registers
    that path (not a per-vantage <id>.key)."""
    (tmp_path / "root.pem").write_text("-----BEGIN PRIVATE KEY-----\nfake\n")
    history: list[dict] = []
    monkeypatch.setattr(vantage_setup.subprocess, "run",
                        _fake_run_factory(history))

    rc = vantage_setup.cmd_vantage_setup(_args(tmp_path))
    assert rc == 0
    controller_calls = [h for h in history if "mthydra-controller" in h["argv"][0]]
    assert len(controller_calls) == 1
    argv = controller_calls[0]["argv"]
    assert "vantage-set-ssh" in argv
    kp = argv[argv.index("--key-path") + 1]
    assert kp.endswith("probe.key")


def test_ssh_provision_carries_pubkey_in_script(tmp_path, monkeypatch):
    """The pubkey is repr-quoted into the remote script so a malicious
    pubkey value can't break out of shell context."""
    captured = {}
    def _fake(argv, capture_output=True, text=True, timeout=None, input=None):
        captured["argv"] = argv
        captured["input"] = input
        return subprocess.CompletedProcess(argv, 0, "OK\n", "")
    monkeypatch.setattr(vantage_setup.subprocess, "run", _fake)

    vantage_setup._ssh_provision_vantage(
        vantage_host="x.example",
        vantage_port=2222,
        root_key=Path("/tmp/nope"),
        probe_pubkey="ssh-ed25519 AAAA REAL_KEY",
    )
    assert "-p" in captured["argv"]
    assert captured["argv"][captured["argv"].index("-p") + 1] == "2222"
    assert "REAL_KEY" in captured["input"]
    assert "ssh-ed25519" in captured["input"]


def test_ssh_provision_raises_when_remote_does_not_print_ok(tmp_path, monkeypatch):
    """Defensive: if remote script returns 0 but doesn't print OK (e.g.
    apt-get rewrote stdout), surface as VantageSetupError so the caller
    knows something is off, not silent success."""
    def _fake(argv, capture_output=True, text=True, timeout=None, input=None):
        return subprocess.CompletedProcess(argv, 0, "weird success\n", "")
    monkeypatch.setattr(vantage_setup.subprocess, "run", _fake)
    with pytest.raises(vantage_setup.VantageSetupError, match="remote provisioning failed"):
        vantage_setup._ssh_provision_vantage(
            vantage_host="x.example", vantage_port=22,
            root_key=Path("/tmp/nope"), probe_pubkey="k")


def test_main_routes_vantage_setup(monkeypatch, tmp_path):
    """mthydra-ops main dispatch table picks up vantage-setup."""
    from mthydra.ops import main as m
    from mthydra.ops import vantage_setup as vs
    called = {}
    def _fake(args):
        called["v"] = args
        return 0
    monkeypatch.setattr(vs, "cmd_vantage_setup", _fake)
    rc = m.main([
        "vantage-setup",
        "--vantage-id", "ru-msk-1",
        "--vantage-host", "203.0.113.5",
        "--root-key", "/tmp/k.pem",
    ])
    assert rc == 0 and "v" in called
    assert called["v"].vantage_id == "ru-msk-1"
    assert called["v"].vantage_port == 22  # default
