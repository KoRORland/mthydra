from __future__ import annotations

import subprocess

import pytest

from mthydra.controller.probe_runner import ssh as ssh_mod


def test_ssh_cmd_builds_correct_argv(monkeypatch):
    seen = {}
    def fake_run(argv, **kw):
        seen["argv"] = argv
        seen["kw"] = kw
        return subprocess.CompletedProcess(argv, 0, "ok", "")
    monkeypatch.setattr(ssh_mod.subprocess, "run", fake_run)
    vantage = {
        "ssh_host": "203.0.113.5", "ssh_port": 2222, "ssh_user": "probe",
        "ssh_key_path": "/etc/mthydra/ssh/k", "ssh_known_hosts_path": "/etc/mthydra/ssh/kh",
    }
    res = ssh_mod.ssh_cmd(vantage, "openssl", "s_client",
                          "-connect", "1.2.3.4:443")
    assert res.returncode == 0
    argv = seen["argv"]
    assert argv[0] == "/usr/bin/ssh"
    assert "-i" in argv and "/etc/mthydra/ssh/k" in argv
    assert "-p" in argv and "2222" in argv
    assert "StrictHostKeyChecking=yes" in " ".join(argv)
    assert "UserKnownHostsFile=/etc/mthydra/ssh/kh" in " ".join(argv)
    assert "BatchMode=yes" in " ".join(argv)
    assert "probe@203.0.113.5" in argv
    assert argv[-4:] == ["openssl", "s_client", "-connect", "1.2.3.4:443"]


def test_ssh_cmd_raises_if_ssh_not_configured():
    with pytest.raises(ssh_mod.SshNotConfigured):
        ssh_mod.ssh_cmd({"ssh_host": None, "ssh_port": 22,
                         "ssh_user": "x", "ssh_key_path": "/k",
                         "ssh_known_hosts_path": "/kh"}, "true")
