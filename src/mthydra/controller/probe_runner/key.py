"""Probe key materialization (spec T2-D2 / §5).

DB row (controller_probe_key) is the source of truth; the file at
<ssh_dir>/probe.key is a 0600 regenerable cache. ensure_probe_key is called by
vantage-setup and at probe-wheel startup, so a promoted standby that restores
the DB rematerializes the identical key file with no manual step.
"""
from __future__ import annotations

import sqlite3
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from mthydra.controller.state import probe_key as pk

_COMMENT = "mthydra-probe-runner"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _generate_keypair() -> tuple[str, str]:
    """ssh-keygen an ed25519 keypair in a temp dir; return (private, public)."""
    with tempfile.TemporaryDirectory() as td:
        kp = Path(td) / "k"
        subprocess.run(
            ["ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(kp), "-C", _COMMENT],
            capture_output=True, text=True, timeout=30, check=True,
        )
        return kp.read_text(), (Path(str(kp) + ".pub")).read_text().strip()


def ensure_probe_key(conn: sqlite3.Connection, ssh_dir: Path | str) -> tuple[Path, str]:
    """Resolve the shared probe key. Generate+persist on first call; always
    materialize the 0600 file cache from the DB row. Returns (key_path, pubkey)."""
    ssh_dir = Path(ssh_dir)
    ssh_dir.mkdir(parents=True, exist_ok=True)
    try:
        ssh_dir.chmod(0o700)
    except PermissionError:
        pass

    row = pk.get(conn)
    if row is None:
        priv, pub = _generate_keypair()
        pk.put(conn, private_key=priv, public_key=pub, comment=_COMMENT, at=_now())
        row = pk.get(conn)
    assert row is not None  # just inserted above, or already present

    key_path = ssh_dir / "probe.key"
    pub_path = ssh_dir / "probe.key.pub"
    if not key_path.exists() or key_path.read_text() != row.private_key:
        key_path.write_text(row.private_key)
        key_path.chmod(0o600)
    if not pub_path.exists() or pub_path.read_text().strip() != row.public_key:
        pub_path.write_text(row.public_key + "\n")
        pub_path.chmod(0o644)
    return key_path, row.public_key
