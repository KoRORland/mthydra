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
        password=False,
        print_pubkey=False,
        bootstrap_user="root",
        ssh_dir=str(tmp_path / "ssh"),
        db_path=str(tmp_path / "db.sqlite"),
    )
    base.update(over)
    return argparse.Namespace(**base)


def _fake_run_factory(history, probe_authorized=False):
    """Stateful ssh fake. The probe-login check (`echo VERIFY-OK`) fails until a
    provision round has run (mirroring a fresh vantage where the probe key is
    not yet authorized). Set probe_authorized=True to model an already-set-up
    vantage (the idempotent re-run short-circuit)."""
    state = {"provisioned": probe_authorized}

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
            # Probe-login check / verify has 'echo VERIFY-OK'; succeeds only once
            # the probe key is authorized (after provisioning).
            if "VERIFY-OK" in argv:
                if state["provisioned"]:
                    return subprocess.CompletedProcess(argv, 0, "VERIFY-OK\n", "")
                return subprocess.CompletedProcess(argv, 255, "", "Permission denied")
            if "bash" in argv and "-s" in argv:
                # Harden script carries 'HARDENED'; everything else is provision.
                if input and "HARDENED" in input:
                    return subprocess.CompletedProcess(argv, 0, "HARDENED\n", "")
                state["provisioned"] = True  # provisioning authorizes the key
                return subprocess.CompletedProcess(argv, 0, "OK\n", "")
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
    assert bins.count("ssh") == 4  # probe-check (fails) + provision + verify + harden
    assert bins.count("ssh-keyscan") == 1
    # Last call must be vantage-set-ssh through the controller binary.
    controller_calls = [h for h in history if "mthydra-controller" in h["argv"][0]]
    assert len(controller_calls) == 1
    assert "vantage-set-ssh" in controller_calls[0]["argv"]


def test_idempotent_rerun_skips_root_when_probe_works(tmp_path, monkeypatch):
    """If the shared probe key already logs in (e.g. re-run after the vantage
    was already set up + hardened), skip ALL root operations — no provision, no
    harden — and just re-keyscan + re-register. This is the regression behind
    'root@...: Permission denied' on a re-run of an already-hardened vantage."""
    (tmp_path / "root.pem").write_text("-----BEGIN PRIVATE KEY-----\nfake\n")
    history: list[dict] = []
    monkeypatch.setattr(vantage_setup.subprocess, "run",
                        _fake_run_factory(history, probe_authorized=True))

    rc = vantage_setup.cmd_vantage_setup(_args(tmp_path, password=True, root_key=None))
    assert rc == 0
    # Exactly one ssh: the probe-login check. No provision/harden 'bash -s'.
    ssh_calls = [h for h in history if h["argv"][0] == "ssh"]
    assert len(ssh_calls) == 1
    assert "VERIFY-OK" in ssh_calls[0]["argv"]
    assert not any("bash" in h["argv"] and "-s" in h["argv"] for h in ssh_calls)
    # Still keyscans + registers (idempotent).
    assert any(h["argv"][0] == "ssh-keyscan" for h in history)
    controller_calls = [h for h in history if "mthydra-controller" in h["argv"][0]]
    assert len(controller_calls) == 1 and "vantage-set-ssh" in controller_calls[0]["argv"]


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
    def _fake(argv, capture_output=True, text=True, timeout=None, input=None, **kw):
        captured["argv"] = argv
        captured["input"] = input
        return subprocess.CompletedProcess(argv, 0, "OK\n", "")
    monkeypatch.setattr(vantage_setup.subprocess, "run", _fake)

    vantage_setup._ssh_provision_vantage(
        vantage_host="x.example",
        vantage_port=2222,
        ssh_user="root",
        identity=str(Path("/tmp/nope")),
        extra_opts=["-o", "StrictHostKeyChecking=accept-new",
                    "-o", "BatchMode=yes", "-o", "ConnectTimeout=15"],
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
    def _fake(argv, capture_output=True, text=True, timeout=None, input=None, **kw):
        return subprocess.CompletedProcess(argv, 0, "weird success\n", "")
    monkeypatch.setattr(vantage_setup.subprocess, "run", _fake)
    with pytest.raises(vantage_setup.VantageSetupError, match="remote provisioning failed"):
        vantage_setup._ssh_provision_vantage(
            vantage_host="x.example", vantage_port=22,
            ssh_user="root", identity=str(Path("/tmp/nope")),
            extra_opts=["-o", "BatchMode=yes"], probe_pubkey="k")


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


def test_print_pubkey_emits_key_and_exits_without_ssh(tmp_path, monkeypatch, capsys):
    history: list[dict] = []
    monkeypatch.setattr(vantage_setup.subprocess, "run",
                        _fake_run_factory(history))
    args = _args(tmp_path, root_key=None, password=False, print_pubkey=True,
                 bootstrap_user="root")
    rc = vantage_setup.cmd_vantage_setup(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "ssh-ed25519" in out                       # pubkey printed
    assert not any(h["argv"][0] == "ssh" for h in history)  # no connection made


def test_password_method_omits_batchmode(tmp_path, monkeypatch):
    """Password entry must NOT set BatchMode=yes and must NOT use sshpass."""
    captured = {}
    def _fake(argv, capture_output=True, text=True, timeout=None, input=None, **kw):
        if argv[0] == "ssh-keygen":
            for i, tok in enumerate(argv):
                if tok == "-f" and i + 1 < len(argv):
                    Path(argv[i + 1]).write_text("PRIV\n")
                    Path(argv[i + 1] + ".pub").write_text(
                        "ssh-ed25519 AAAAFAKEKEY mthydra-probe-runner\n")
                    break
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[0] == "ssh":
            captured.setdefault("ssh_argvs", []).append(argv)
            return subprocess.CompletedProcess(argv, 0, "OK\n", "")
        if argv[0] == "ssh-keyscan":
            return subprocess.CompletedProcess(argv, 0, "h x\n", "")
        return subprocess.CompletedProcess(argv, 0, "", "")
    monkeypatch.setattr(vantage_setup.subprocess, "run", _fake)
    # Force the fresh-vantage path (probe key not yet authorized) so the first
    # ssh call is the provision session, not the idempotent-rerun short-circuit.
    monkeypatch.setattr(vantage_setup, "_probe_login_ok", lambda **kw: False)
    monkeypatch.setattr(vantage_setup, "_verify_probe_login", lambda **kw: None)
    monkeypatch.setattr(vantage_setup, "_harden_sshd", lambda **kw: None)
    args = _args(tmp_path, root_key=None, password=True, print_pubkey=False,
                 bootstrap_user="root")
    rc = vantage_setup.cmd_vantage_setup(args)
    assert rc == 0
    prov = captured["ssh_argvs"][0]
    assert "sshpass" not in " ".join(prov)
    assert "BatchMode=yes" not in prov


def test_root_key_method_uses_batchmode(tmp_path, monkeypatch):
    (tmp_path / "root.pem").write_text("k")
    captured = {}
    def _fake(argv, capture_output=True, text=True, timeout=None, input=None, **kw):
        if argv[0] == "ssh-keygen":
            for i, tok in enumerate(argv):
                if tok == "-f" and i + 1 < len(argv):
                    Path(argv[i + 1]).write_text("PRIV\n")
                    Path(argv[i + 1] + ".pub").write_text(
                        "ssh-ed25519 AAAAFAKEKEY mthydra-probe-runner\n")
                    break
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[0] == "ssh":
            captured.setdefault("ssh_argvs", []).append(argv)
            return subprocess.CompletedProcess(argv, 0, "OK\n", "")
        return subprocess.CompletedProcess(argv, 0, "h x\n", "")
    monkeypatch.setattr(vantage_setup.subprocess, "run", _fake)
    monkeypatch.setattr(vantage_setup, "_probe_login_ok", lambda **kw: False)
    monkeypatch.setattr(vantage_setup, "_verify_probe_login", lambda **kw: None)
    monkeypatch.setattr(vantage_setup, "_harden_sshd", lambda **kw: None)
    args = _args(tmp_path, password=False, print_pubkey=False, bootstrap_user="root")
    rc = vantage_setup.cmd_vantage_setup(args)
    assert rc == 0
    prov = captured["ssh_argvs"][0]
    assert "BatchMode=yes" in prov
    assert f"root@{args.vantage_host}" in prov


def test_verify_probe_login_uses_probe_user_and_key(tmp_path, monkeypatch):
    captured = {}
    def _fake(argv, capture_output=True, text=True, timeout=None, input=None, **kw):
        captured["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, "VERIFY-OK\n", "")
    monkeypatch.setattr(vantage_setup.subprocess, "run", _fake)
    vantage_setup._verify_probe_login(
        vantage_host="h", vantage_port=22, key_path=Path("/k/probe.key"))
    argv = captured["argv"]
    assert "probe@h" in argv
    assert "BatchMode=yes" in argv
    assert argv[argv.index("-i") + 1] == "/k/probe.key"


def test_verify_probe_login_raises_on_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(vantage_setup.subprocess, "run",
                        lambda argv, **kw: subprocess.CompletedProcess(argv, 255, "", "denied"))
    with pytest.raises(vantage_setup.VantageSetupError, match="probe login verification failed"):
        vantage_setup._verify_probe_login(
            vantage_host="h", vantage_port=22, key_path=Path("/k/probe.key"))


def test_harden_writes_lockdown_dropin_and_validates(tmp_path, monkeypatch):
    captured = {}
    def _fake(argv, capture_output=True, text=True, timeout=None, input=None, **kw):
        captured["argv"] = argv
        captured["input"] = input
        return subprocess.CompletedProcess(argv, 0, "HARDENED\n", "")
    monkeypatch.setattr(vantage_setup.subprocess, "run", _fake)
    vantage_setup._harden_sshd(
        vantage_host="h", vantage_port=22, ssh_user="root",
        identity="/k/root.pem", extra_opts=["-o", "BatchMode=yes"])
    script = captured["input"]
    assert "AllowUsers probe" in script
    assert "PasswordAuthentication no" in script
    assert "PermitRootLogin no" in script
    assert "sshd -t" in script                 # validate before reload
    assert "reload" in script                  # reload, not a hard restart


def test_harden_raises_when_remote_omits_marker(tmp_path, monkeypatch):
    monkeypatch.setattr(vantage_setup.subprocess, "run",
                        lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "config test failed\n", ""))
    with pytest.raises(vantage_setup.VantageSetupError, match="hardening failed"):
        vantage_setup._harden_sshd(
            vantage_host="h", vantage_port=22, ssh_user="root",
            identity="/k/root.pem", extra_opts=[])
