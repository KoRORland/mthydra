"""SSH transport for the probe runner wheel (spec P-D5).

Stdlib subprocess to /usr/bin/ssh. No shell. No paramiko. Keys only.
"""
from __future__ import annotations

import subprocess
from collections.abc import Mapping


class SshNotConfigured(RuntimeError):
    """Raised when a probe_vantages row is missing required SSH fields."""


def ssh_cmd(vantage: Mapping, *cmd_parts: str, timeout_s: int = 30
            ) -> subprocess.CompletedProcess:
    """Run `cmd_parts` on the vantage via SSH; return the CompletedProcess.

    `vantage` is a dict-like with keys ssh_host / ssh_port / ssh_user /
    ssh_key_path / ssh_known_hosts_path. Capture both streams. No shell
    interpretation — cmd_parts is forwarded as separate argv tokens to
    OpenSSH, which preserves quoting end-to-end."""
    if not vantage.get("ssh_host") or not vantage.get("ssh_user") \
            or not vantage.get("ssh_key_path"):
        raise SshNotConfigured(
            "vantage missing ssh_host / ssh_user / ssh_key_path")
    target = f"{vantage['ssh_user']}@{vantage['ssh_host']}"
    argv = [
        "/usr/bin/ssh",
        "-i", vantage["ssh_key_path"],
        "-p", str(vantage.get("ssh_port") or 22),
        "-o", "StrictHostKeyChecking=yes",
        "-o", f"UserKnownHostsFile={vantage.get('ssh_known_hosts_path') or '/dev/null'}",
        "-o", "ConnectTimeout=10",
        "-o", "BatchMode=yes",
        target, "--", *cmd_parts,
    ]
    return subprocess.run(
        argv, capture_output=True, text=True, timeout=timeout_s,
    )
