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
) -> None:
    """Create a fresh state.sqlite at db_path with all required seed rows.

    Refuses if db_path already exists (operator must explicitly remove first).
    Validates the age recipient before writing anything.

    Steps (spec A §10.1):
    1. Refuse if db_path exists.
    2. Validate age_recipient.
    3. Create schema.
    4. Insert credential_authority generation=1 (placeholder keypair).
    5. Insert descriptor_signing_key generation=1 (placeholder keypair).
    6. Insert provider_api_credentials rows.
    7. Initialize obligation_clocks rows.

    Note: the synthetic do_backup step (§10.1 step 7) is the CLI's responsibility,
    not init_state's, so that init_state remains easily testable without a live
    S3 endpoint.
    """
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
