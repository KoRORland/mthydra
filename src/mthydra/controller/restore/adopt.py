"""Replace live state with a restored snapshot — spec A §7.2."""
from __future__ import annotations

import os
import secrets
import shutil
from pathlib import Path

from mthydra.controller.state.audit import log_event
from mthydra.controller.state.authority import current_authority, insert_authority, retire_authority
from mthydra.controller.state.db import connect


class AdoptError(RuntimeError):
    pass


def _fsync_path(path: Path) -> None:
    """Best-effort fsync of a file or directory.

    fsyncing the directory is what makes the rename durable; without it a power
    loss right after a restore could lose the just-adopted state the operator
    now relies on. Best-effort: platforms that cannot fsync a directory fd are
    skipped rather than failing the adoption.
    """
    flags = os.O_RDONLY
    if path.is_dir() and hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    try:
        fd = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _fresh_pem() -> tuple[str, str]:
    """Placeholder key generator (spec B will replace with real X25519/Ed25519).

    Until spec B lands, adopt --case=B mints a pseudo-random opaque token so
    the authority table has a demonstrably fresh row.  The spec B implementation
    will swap in real asymmetric key generation without changing adopt()'s
    calling convention.
    """
    nonce = secrets.token_hex(16)
    return (f"PRIV-PLACEHOLDER-{nonce}", f"PUB-PLACEHOLDER-{nonce}")


def adopt_restored_state(
    live_path: Path | str,
    restored_path: Path | str,
    case: str | None,
    rotate_published_subset: bool,
    at: str,
) -> None:
    """Adopt a restored SQLite snapshot as the new live controller state.

    Steps (spec A §7.2):
    1. Validate inputs.
    2. Move live_path → live_path.preadopt.<ts> (forensics; never auto-deleted).
    3. Move restored_path → live_path.
    4. If case==B: retire current authority, insert fresh authority (placeholder).
    5. If rotate_published_subset: append _pending_rotation marker row.
    6. Audit log the adoption.

    Must only be called when the controller daemon is NOT running.
    Raises AdoptError on any pre-flight failure.
    """
    live_path = Path(live_path)
    restored_path = Path(restored_path)

    if not restored_path.exists():
        raise AdoptError(f"restored file not found: {restored_path}")
    if case is not None and case not in {"A", "B"}:
        raise AdoptError(f"invalid case {case!r}; must be 'A', 'B', or None")

    # Step 2: preserve existing live DB for forensics
    if live_path.exists():
        ts_tag = at.replace(":", "").replace("-", "")
        preadopt_path = live_path.parent / f"{live_path.name}.preadopt.{ts_tag}"
        shutil.move(str(live_path), str(preadopt_path))

    # Step 3: install the restored DB as the new live DB
    shutil.move(str(restored_path), str(live_path))

    conn = connect(live_path)
    try:
        # Step 6: audit
        import json
        log_event(
            conn,
            ts=at,
            actor="operator",
            action="adopt_restored_state",
            target=str(live_path),
            details_json=json.dumps({"case": case, "rotate_published_subset": rotate_published_subset}),
        )

        # Step 4: Case B re-key
        if case == "B":
            cur = current_authority(conn)
            retire_authority(conn, cur.generation, at=at)
            priv, pub = _fresh_pem()
            insert_authority(conn, cur.generation + 1, priv, pub, at)
            log_event(
                conn,
                ts=at,
                actor="operator",
                action="case_b_rekey",
                target=None,
                details_json=json.dumps({"new_generation": cur.generation + 1}),
            )

        # Step 5: pending-rotation marker
        if rotate_published_subset:
            conn.execute(
                "INSERT INTO published_subsets (payload_json, published_at, channel) "
                "VALUES (?, ?, ?)",
                ('{"_pending_rotation":true}', at, "telegram"),
            )
            conn.commit()
            log_event(
                conn,
                ts=at,
                actor="operator",
                action="rotate_published_subset_marker",
                target=None,
                details_json=None,
            )
    finally:
        conn.close()

    # L4: make the install durable before reporting success — fsync the new
    # live DB and its parent directory so the rename survives a crash/power loss
    # immediately after a restore.
    _fsync_path(live_path)
    _fsync_path(live_path.parent)
