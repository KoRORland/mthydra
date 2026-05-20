"""First-run bootstrap — spec A §10.1 + spec B Ed25519 key generation."""
from __future__ import annotations

import os
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mthydra.controller.backup.age_crypt import AgeError, validate_recipient
from mthydra.controller.state.authority import insert_authority
from mthydra.controller.state.db import connect
from mthydra.controller.state.descriptor import insert_signing_key
from mthydra.controller.state.obligations import set_obligation
from mthydra.controller.state.schema import apply_schema
from mthydra.controller.state.tokens import set_provider_credential
from mthydra.descriptor.keys import generate_keypair


class BootstrapError(RuntimeError):
    pass


def _placeholder_keypair_pem() -> tuple[str, str]:
    """Placeholder key pair for credential_authority (spec C will replace with real key)."""
    nonce = secrets.token_hex(16)
    return (f"PRIV-BOOTSTRAP-{nonce}", f"PUB-BOOTSTRAP-{nonce}")


def _add_hours(iso: str, hours: int) -> str:
    t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return (t + timedelta(hours=hours)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def init_state(
    db_path: Path | str,
    age_recipient: str,
    provider_credentials: dict[str, str],
    obligation_timer_hours: dict[str, int],
    now: str,
    role: str = "active",
) -> None:
    """Create a fresh state.sqlite at db_path.

    role='active' (default): seeds credential_authority + descriptor_signing_key
        + provider credentials + obligation_clocks + node_state='active'.
    role='standby': seeds only schema + B2 provider credential + node_state='standby'.
        Refuses if 'b2' not in provider_credentials.

    Refuses if db_path already exists (operator must explicitly remove first).
    Validates the age recipient before writing anything.

    Note: the synthetic do_backup step (§10.1 step 7) is the CLI's responsibility,
    not init_state's, so that init_state remains easily testable without a live
    S3 endpoint.
    """
    if role not in ("active", "standby"):
        raise BootstrapError(f"unknown role {role!r}")

    if role == "standby" and "b2" not in provider_credentials:
        raise BootstrapError(
            "standby role requires a 'b2' provider credential (for heartbeat publishing)"
        )

    db_path = Path(db_path)
    if db_path.exists():
        raise BootstrapError(
            f"refusing to bootstrap: {db_path} already exists; move or delete first"
        )
    try:
        validate_recipient(age_recipient)
    except AgeError as e:
        raise BootstrapError(f"invalid age recipient: {e}") from e

    conn = connect(db_path)
    try:
        apply_schema(conn)
        # apply_schema seeds node_state='active' by default; override for standby.
        if role == "standby":
            conn.execute("UPDATE node_state SET role='standby' WHERE rowid=1")
            conn.commit()
            set_provider_credential(
                conn, provider="b2",
                credential=provider_credentials["b2"], at=now,
            )
            # Standby seeds no authority, no keys, no obligations from its own DB.
        else:
            priv, pub = _placeholder_keypair_pem()
            insert_authority(conn, generation=1, privkey_pem=priv, pubkey_pem=pub, created_at=now)

            # Spec B: generate a real Ed25519 keypair for the descriptor signing key.
            dpriv, dpub = generate_keypair()
            insert_signing_key(conn, generation=1, privkey=dpriv, pubkey=dpub, created_at=now)

            for provider, cred in provider_credentials.items():
                set_provider_credential(conn, provider=provider, credential=cred, at=now)

            for obligation_id, hours in obligation_timer_hours.items():
                next_due = _add_hours(now, hours) if hours > 0 else now
                set_obligation(
                    conn,
                    obligation_id=obligation_id,
                    last_proven_at=now,
                    proven_by="bootstrap",
                    next_due_at=next_due,
                    details=None,
                )
    finally:
        conn.close()

    # Spec §3: enforce file-mode discipline after the connection is closed.
    # 0600 on the DB (only owner can read), 0700 on the parent dir.
    # No-op on non-POSIX systems (e.g. Windows CI) — spec is Ubuntu only.
    if hasattr(os, "chmod"):
        os.chmod(db_path, 0o600)
        os.chmod(db_path.parent, 0o700)
