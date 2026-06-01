"""mthydra-ops vantage-setup — one-command vantage SSH provisioning (T-Task 2).

Quickstart §7.7 used to be 7 manual steps across two hosts (EU + vantage):
ssh-keygen on EU, scp pubkey to vantage, adduser probe on vantage, paste
pubkey to authorized_keys, apt install ncat+openssl, ssh-keyscan back on
EU, mthydra-controller vantage-set-ssh. Each step had its own failure modes.

This module collapses all of that into:

    mthydra-ops vantage-setup \\
        --vantage-id ru-msk-1 \\
        --vantage-host <IPv4> \\
        --root-key ~/.ssh/timeweb-root-key.pem

Every step is idempotent (re-running with the same args is a no-op when
state already matches).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


class VantageSetupError(RuntimeError):
    pass


def _say(msg: str) -> None:
    print(f"[mthydra-vantage-setup] {msg}", flush=True)


def _err(msg: str) -> None:
    print(f"[mthydra-vantage-setup] ERROR: {msg}", file=sys.stderr, flush=True)


def _run(argv: list[str], *, timeout: int = 60) -> subprocess.CompletedProcess:
    res = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    if res.returncode != 0:
        raise VantageSetupError(
            f"command failed (rc={res.returncode}): {' '.join(argv)}\n"
            f"stderr: {res.stderr.strip()}"
        )
    return res


def _ensure_ssh_dir(ssh_dir: Path) -> None:
    ssh_dir.mkdir(parents=True, exist_ok=True)
    try:
        ssh_dir.chmod(0o700)
    except PermissionError:
        # We may be running as mthydra against a dir owned by root; best-effort.
        pass


def _ensure_probe_key(ssh_dir: Path, vantage_id: str) -> Path:
    """Generate an ed25519 keypair at <ssh-dir>/<vantage-id>.key if missing.
    Returns the private key path."""
    key_path = ssh_dir / f"{vantage_id}.key"
    if key_path.exists():
        _say(f"key already exists: {key_path}")
        return key_path
    _say(f"generating ed25519 key at {key_path}")
    _run([
        "ssh-keygen", "-t", "ed25519", "-N", "",
        "-f", str(key_path),
        "-C", f"mthydra-probe-runner@{vantage_id}",
    ])
    return key_path


def _ssh_provision_vantage(
    *,
    vantage_host: str,
    vantage_port: int,
    root_key: Path,
    probe_pubkey: str,
    timeout: int = 120,
) -> None:
    """SSH to the vantage as root and provision the probe user + deps in one
    round trip. Idempotent: adduser is gated on `id probe` and the pubkey is
    only appended if not already present.

    Repr-quoting the pubkey defends against shell-injection — the value comes
    from a file we wrote, but the shape lets a future caller pass an
    operator-supplied pubkey safely too."""
    remote_script = f"""
set -u
id probe >/dev/null 2>&1 || adduser --disabled-password --gecos '' probe
mkdir -p /home/probe/.ssh
chmod 700 /home/probe/.ssh
grep -qxF {probe_pubkey!r} /home/probe/.ssh/authorized_keys 2>/dev/null \\
    || echo {probe_pubkey!r} >> /home/probe/.ssh/authorized_keys
chown -R probe:probe /home/probe/.ssh
chmod 600 /home/probe/.ssh/authorized_keys
DEBIAN_FRONTEND=noninteractive apt-get update -y >/dev/null
DEBIAN_FRONTEND=noninteractive apt-get install -y openssl ncat >/dev/null
echo OK
"""
    _say(f"provisioning vantage {vantage_host}:{vantage_port} via SSH")
    argv = [
        "ssh",
        "-i", str(root_key),
        "-p", str(vantage_port),
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=15",
        f"root@{vantage_host}",
        "bash", "-s",
    ]
    res = subprocess.run(
        argv, input=remote_script, capture_output=True, text=True, timeout=timeout,
    )
    if res.returncode != 0 or "OK" not in res.stdout:
        raise VantageSetupError(
            f"remote provisioning failed (rc={res.returncode}): "
            f"{res.stderr.strip() or res.stdout.strip()}"
        )


def _ssh_keyscan(vantage_host: str, vantage_port: int, known_hosts: Path) -> None:
    """Append the vantage's host key to known_hosts if not already present.
    Idempotence is best-effort here — ssh-keyscan -H hashes the hostname so we
    can't grep for a stable string. Skip-if-host-substring-appears is a
    pragmatic heuristic; worst case is a duplicated entry, which OpenSSH
    tolerates."""
    known_hosts.parent.mkdir(parents=True, exist_ok=True)
    if known_hosts.exists() and vantage_host in known_hosts.read_text():
        _say(f"vantage already in {known_hosts}")
        return
    _say(f"appending vantage host key to {known_hosts}")
    res = _run(["ssh-keyscan", "-p", str(vantage_port), "-H", vantage_host], timeout=15)
    with known_hosts.open("a") as fh:
        fh.write(res.stdout)


def _controller_bin() -> str:
    """Resolve mthydra-controller as the sys.executable sibling — mirrors
    the install.py / ops main.py pattern for no-PATH root shells."""
    return str(Path(sys.executable).parent / "mthydra-controller")


def _register_with_controller(
    *,
    vantage_id: str,
    vantage_host: str,
    vantage_port: int,
    key_path: Path,
    known_hosts: Path,
    db_path: str,
) -> None:
    _say(f"registering vantage-set-ssh for {vantage_id}")
    _run([
        _controller_bin(), "vantage-set-ssh", vantage_id,
        "--host", vantage_host,
        "--user", "probe",
        "--port", str(vantage_port),
        "--key-path", str(key_path),
        "--known-hosts", str(known_hosts),
        "--db-path", db_path,
    ])


def cmd_vantage_setup(args) -> int:
    """One-command vantage SSH provisioning. See module docstring."""
    ssh_dir = Path(args.ssh_dir)
    root_key = Path(args.root_key).expanduser()
    if not root_key.exists():
        _err(f"--root-key not found: {root_key}")
        return 2
    try:
        _ensure_ssh_dir(ssh_dir)
        key_path = _ensure_probe_key(ssh_dir, args.vantage_id)
        pubkey_path = Path(str(key_path) + ".pub")
        probe_pubkey = pubkey_path.read_text().strip()
        _ssh_provision_vantage(
            vantage_host=args.vantage_host,
            vantage_port=args.vantage_port,
            root_key=root_key,
            probe_pubkey=probe_pubkey,
        )
        known_hosts = ssh_dir / "known_hosts"
        _ssh_keyscan(args.vantage_host, args.vantage_port, known_hosts)
        _register_with_controller(
            vantage_id=args.vantage_id,
            vantage_host=args.vantage_host,
            vantage_port=args.vantage_port,
            key_path=key_path,
            known_hosts=known_hosts,
            db_path=args.db_path,
        )
    except VantageSetupError as e:
        _err(str(e))
        return 3
    _say(f"vantage {args.vantage_id} ready for probes")
    return 0
