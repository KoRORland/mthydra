"""Controller-side descriptor signing (spec B §6)."""
from __future__ import annotations

import sqlite3
import struct

from mthydra.controller.state.audit import log_event
from mthydra.controller.state.descriptor import (
    insert_descriptor,
    latest_descriptor_with_signature,
    next_descriptor_generation,
)
from mthydra.controller.state.eu_exit_set import list_active
from mthydra.descriptor.keys import is_placeholder, sign as ed_sign
from mthydra.descriptor.payload import (
    KNOWN_UTLS_FINGERPRINTS,
    DescriptorPayload,
    EUExit,
    canonical_bytes,
    payload_hash,
)


class SignError(RuntimeError):
    pass


def encode_descriptor_blob(payload: bytes, signature: bytes) -> bytes:
    """Encode the descriptor in the wire format the RU agent expects:
    [2-byte BE payload-length][payload bytes][64-byte Ed25519 signature].

    Same encoding used inline at provisioning.seed.provision_box (which
    base64-encodes the result for embedding in cloud-init). Extracted here
    so the descriptor-publish-now path produces an identical blob — the
    RU agent's RefreshLoop._verify is the consumer in both cases.
    """
    if len(signature) != 64:
        raise SignError(
            f"signature must be 64 bytes (got {len(signature)})")
    if len(payload) > 65535:
        raise SignError(
            f"payload too long for 2-byte length prefix ({len(payload)})")
    return struct.pack(">H", len(payload)) + payload + signature


def _active_signing_key(conn: sqlite3.Connection) -> tuple[int, bytes, bytes]:
    """Return (generation, privkey_bytes, pubkey_bytes) for the active descriptor signing key."""
    row = conn.execute(
        "SELECT generation, privkey, pubkey FROM descriptor_signing_key "
        "WHERE retired_at IS NULL ORDER BY generation DESC LIMIT 1"
    ).fetchone()
    if row is None:
        raise SignError("no active descriptor_signing_key in DB; run init first")
    gen, priv, pub = row
    priv_bytes = bytes(priv)
    pub_bytes = bytes(pub)
    if is_placeholder(priv_bytes):
        raise SignError(
            "active descriptor_signing_key is a spec A placeholder; "
            "run: mthydra-controller descriptor-migrate-placeholder"
        )
    return gen, priv_bytes, pub_bytes


def sign_new_descriptor(
    conn: sqlite3.Connection,
    *,
    now_iso: str,
    valid_until_iso: str,
    next_signing_pubkey_hex: str | None = None,
    tls_fingerprints: tuple[tuple[str, int], ...] | None = None,
) -> tuple[int, bytes, bytes]:
    """Assemble payload from current DB state, sign, persist.

    Returns (generation, payload_bytes, signature).
    Raises SignError if no active real signing key is available.
    """
    if tls_fingerprints is not None:
        for fp, _w in tls_fingerprints:
            if fp not in KNOWN_UTLS_FINGERPRINTS:
                raise SignError(f"invariant #33: unknown uTLS fingerprint {fp!r}")

    key_gen, priv, _pub = _active_signing_key(conn)

    prev = latest_descriptor_with_signature(conn)
    prev_hash: str | None = None
    if prev is not None:
        prev_hash = payload_hash(prev[1])

    try:
        from mthydra.controller.state.desync_strategy import live as _live_desync
        _desync = _live_desync(conn)
    except Exception:
        _desync = None

    exits_raw = list_active(conn)
    exits = tuple(
        EUExit(
            fingerprint=e.fingerprint,
            endpoint=e.endpoint,
            weight=e.weight,
            cover_sni=e.cover_sni,
            reality_pubkey=e.reality_pubkey,
        )
        for e in exits_raw
    )
    gen = next_descriptor_generation(conn)

    payload = DescriptorPayload(
        generation=gen,
        signing_key_gen=key_gen,
        issued_at=now_iso,
        valid_until=valid_until_iso,
        eu_exit_set=exits,
        previous_generation_hash=prev_hash,
        next_signing_pubkey=next_signing_pubkey_hex,
        tls_fingerprints=tls_fingerprints,
        desync_strategy=_desync,
    )
    blob = canonical_bytes(payload)
    sig = ed_sign(priv, blob)

    insert_descriptor(
        conn,
        generation=gen,
        payload=blob.decode("utf-8"),
        signed_at=now_iso,
        valid_until=valid_until_iso,
        signing_key_generation=key_gen,
        signature=sig,
    )
    log_event(
        conn,
        ts=now_iso,
        actor="controller",
        action="descriptor_signed",
        target=str(gen),
        details_json=None,
    )
    return gen, blob, sig
