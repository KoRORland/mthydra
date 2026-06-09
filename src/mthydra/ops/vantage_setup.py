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

import contextlib
import sqlite3
import subprocess
import sys
from pathlib import Path

from mthydra.controller.probe_runner.key import ensure_probe_key
from mthydra.controller.state.db import connect
from mthydra.controller.state.schema import apply_schema


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
    # We may be running as mthydra against a dir owned by root; best-effort.
    with contextlib.suppress(PermissionError):
        ssh_dir.chmod(0o700)


def _entry_ssh_opts(args) -> tuple[str, str, list[str]]:
    """Return (ssh_user, identity, extra_opts) for the privileged bootstrap
    session, per the chosen entry method.

    - --password : user 'root', NO identity, NO BatchMode (ssh prompts on the
                   controlling TTY; the password is never captured — spec T2-D4).
    - --root-key : user 'root', -i <key>, BatchMode=yes.
    - re-run after --print-pubkey (no flag): user <bootstrap_user> (root-capable),
                   identity 'SHARED' (caller substitutes the shared key path),
                   BatchMode=yes.
    identity is '' for password, a real path for --root-key, or the sentinel
    'SHARED' for the bootstrap re-run."""
    if args.password:
        return "root", "", ["-o", "StrictHostKeyChecking=accept-new",
                            "-o", "ConnectTimeout=15"]
    if args.root_key:
        return "root", str(Path(args.root_key).expanduser()), [
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", "BatchMode=yes", "-o", "ConnectTimeout=15"]
    return args.bootstrap_user, "SHARED", [
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "BatchMode=yes", "-o", "ConnectTimeout=15"]


def _ssh_provision_vantage(
    *,
    vantage_host: str,
    vantage_port: int,
    ssh_user: str,
    identity: str,        # key path, or "" for password (no -i)
    extra_opts: list[str],
    probe_pubkey: str,
    timeout: int = 120,
) -> None:
    """SSH to the vantage with a root-capable session and provision the probe
    user + deps in one round trip. Idempotent: adduser is gated on `id probe`
    and the pubkey is only appended if not already present.

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
    argv = ["ssh"]
    if identity:
        argv += ["-i", identity]
    argv += ["-p", str(vantage_port), *extra_opts, f"{ssh_user}@{vantage_host}",
             "bash", "-s"]
    _say(f"provisioning vantage {vantage_host}:{vantage_port} as {ssh_user}")
    # capture_output is safe on the --password path: ssh reads the password from
    # /dev/tty (the inherited controlling terminal), NOT from these piped std
    # streams, so the prompt still reaches the operator. The script is delivered
    # to remote `bash -s` via the stdin pipe; remote stdout ("OK") is captured.
    # No controlling tty (cron/pipe) -> ssh exits non-zero promptly, no hang.
    res = subprocess.run(
        argv, input=remote_script, capture_output=True, text=True, timeout=timeout,
    )
    if res.returncode != 0 or "OK" not in res.stdout:
        raise VantageSetupError(
            f"remote provisioning failed (rc={res.returncode}): "
            f"{res.stderr.strip() or res.stdout.strip()}"
        )


def _probe_login_ok(*, vantage_host: str, vantage_port: int,
                    key_path: Path, timeout: int = 20) -> bool:
    """True iff we can log in as probe with the shared key. Used both for the
    pre-hardening verification and for the idempotent re-run short-circuit
    (a vantage that already accepts the probe key needs no root provisioning —
    and after lockdown root access is gone, so a root re-entry would fail).

    accept-new (TOFU) is deliberate: a one-time setup check; the authoritative
    host-key pinning happens in _ssh_keyscan -> the dedicated known_hosts the
    runner uses. The operator supplied the IP."""
    argv = [
        "ssh", "-i", str(key_path), "-p", str(vantage_port),
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
        f"probe@{vantage_host}", "echo", "VERIFY-OK",
    ]
    res = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    return res.returncode == 0 and "VERIFY-OK" in res.stdout


def _verify_probe_login(*, vantage_host: str, vantage_port: int,
                        key_path: Path, timeout: int = 30) -> None:
    """Confirm probe-key login works BEFORE hardening — a bad key here would
    otherwise lock us out (spec T2-D6)."""
    _say("verifying probe-key login before hardening")
    if not _probe_login_ok(vantage_host=vantage_host, vantage_port=vantage_port,
                           key_path=key_path, timeout=timeout):
        raise VantageSetupError(
            "probe login verification failed; NOT hardening "
            f"(probe@{vantage_host}:{vantage_port} with {key_path})")


def _harden_sshd(*, vantage_host: str, vantage_port: int, ssh_user: str,
                 identity: str, extra_opts: list[str], timeout: int = 60) -> None:
    """Lock the vantage to probe-key-only (spec T2-D7). Writes a sshd_config
    drop-in, validates with `sshd -t`, then RELOADS (existing sessions survive,
    so this same root session stays alive to report success)."""
    remote_script = """
set -e
cat > /etc/ssh/sshd_config.d/60-mthydra-probe.conf <<'EOF'
AllowUsers probe
PasswordAuthentication no
PermitRootLogin no
EOF
sshd -t
systemctl reload ssh 2>/dev/null || systemctl reload sshd
echo HARDENED
"""
    # Reload dispatch: Debian/Ubuntu's unit is `ssh`, RHEL/Fedora's is `sshd`.
    # We try `ssh` first (2>/dev/null hides the expected "unit not found" on
    # RHEL), falling back to `sshd`. Under `set -e`, if BOTH reloads fail the
    # final `||` command's non-zero status aborts the script before `echo
    # HARDENED` — so a genuine reload failure is never reported as success.
    argv = ["ssh"]
    if identity:
        argv += ["-i", identity]
    argv += ["-p", str(vantage_port), *extra_opts, f"{ssh_user}@{vantage_host}",
             "bash", "-s"]
    _say("hardening vantage sshd to probe-key-only (AllowUsers probe)")
    res = subprocess.run(argv, input=remote_script, capture_output=True,
                         text=True, timeout=timeout)
    if res.returncode != 0 or "HARDENED" not in res.stdout:
        raise VantageSetupError(
            f"hardening failed (rc={res.returncode}): "
            f"{res.stderr.strip() or res.stdout.strip()}")


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
    # argparse enforces mutual exclusion on the CLI; this guard also covers
    # programmatic callers (e.g. tests) that build the Namespace directly.
    methods = [bool(args.root_key), bool(args.password), bool(args.print_pubkey)]
    if sum(methods) > 1:
        _err("choose at most one of --root-key / --password / --print-pubkey")
        return 2
    if args.root_key and not Path(args.root_key).expanduser().exists():
        _err(f"--root-key not found: {args.root_key}")
        return 2
    try:
        _ensure_ssh_dir(ssh_dir)
        # apply_schema here because vantage-setup may run before the controller
        # process has ever started (fresh host); the call is idempotent.
        with connect(args.db_path) as conn:
            apply_schema(conn)
            key_path, probe_pubkey = ensure_probe_key(conn, ssh_dir)

        if args.print_pubkey:
            print(probe_pubkey)
            _say("install the line above into the authorized_keys of a "
                 "root-capable user on the vantage, then re-run vantage-setup "
                 "WITHOUT --print-pubkey (uses --bootstrap-user, default root).")
            return 0

        known_hosts = ssh_dir / "known_hosts"
        # Idempotent re-run: if the shared probe key already logs in, the vantage
        # is already provisioned (and likely hardened — after which root access
        # is gone, so a root entry method would fail here). Skip straight to the
        # idempotent keyscan + register; do NOT attempt a root session.
        if _probe_login_ok(vantage_host=args.vantage_host,
                           vantage_port=args.vantage_port, key_path=key_path):
            _say("probe key already authorized on this vantage; skipping root "
                 "provisioning + hardening (idempotent re-run)")
            _ssh_keyscan(args.vantage_host, args.vantage_port, known_hosts)
            _register_with_controller(
                vantage_id=args.vantage_id, vantage_host=args.vantage_host,
                vantage_port=args.vantage_port, key_path=key_path,
                known_hosts=known_hosts, db_path=args.db_path)
            _say(f"vantage {args.vantage_id} ready for probes")
            return 0

        ssh_user, identity, extra_opts = _entry_ssh_opts(args)
        if identity == "SHARED":
            identity = str(key_path)
        _ssh_provision_vantage(
            vantage_host=args.vantage_host, vantage_port=args.vantage_port,
            ssh_user=ssh_user, identity=identity, extra_opts=extra_opts,
            probe_pubkey=probe_pubkey)
        _verify_probe_login(
            vantage_host=args.vantage_host, vantage_port=args.vantage_port,
            key_path=key_path)
        _harden_sshd(
            vantage_host=args.vantage_host, vantage_port=args.vantage_port,
            ssh_user=ssh_user, identity=identity, extra_opts=extra_opts)
        _ssh_keyscan(args.vantage_host, args.vantage_port, known_hosts)
        _register_with_controller(
            vantage_id=args.vantage_id, vantage_host=args.vantage_host,
            vantage_port=args.vantage_port, key_path=key_path,
            known_hosts=known_hosts, db_path=args.db_path)
    except VantageSetupError as e:
        _err(str(e))
        return 3
    except (sqlite3.Error, subprocess.SubprocessError, OSError) as e:
        # DB open/migrate or key generation failed — surface cleanly instead of
        # a raw traceback, same exit code as a provisioning failure.
        _err(f"setup failed: {e}")
        return 3
    _say(f"vantage {args.vantage_id} ready for probes")
    return 0
