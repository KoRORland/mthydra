"""Pure-Python Ed25519 descriptor verifier — RU-callable (spec B §7, B-D6).

ZERO imports from mthydra.controller.  Spec E copies this module into RU images.
Import isolation is enforced by tests/unit/descriptor/test_verify_import_isolation.py.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Sequence

from mthydra.descriptor.keys import verify as ed_verify
from mthydra.descriptor.payload import DescriptorPayload, canonical_bytes, payload_hash


class VerifyError(ValueError):
    pass


@dataclass(frozen=True)
class TrustedKey:
    generation: int
    pubkey: bytes  # 32 raw bytes


def verify_descriptor(
    blob: bytes,
    signature: bytes,
    trusted_keys: Sequence[TrustedKey],
    now_iso: str,
    *,
    previous_descriptor_hash: str | None = None,
    grace_hours: int = 24,
) -> DescriptorPayload:
    """Verify a signed descriptor. Returns parsed payload on success.

    Raises VerifyError if:
    - blob is not valid canonical JSON of expected schema.
    - Any unknown fields present.
    - signing_key_gen not in trusted_keys.
    - Signature does not verify under the named trusted key.
    - now_iso is past valid_until + grace_hours.
    - previous_descriptor_hash provided and does not match payload.previous_generation_hash.
    - Generation 1 with non-null previous_generation_hash.
    - Generation > 1 with null previous_generation_hash (missing chain link).
    - Generation > 1 with non-null previous_generation_hash but no prior given (TOFU defence).
    """
    # 1. Parse + structural validation (schema, unknown fields)
    try:
        payload = DescriptorPayload.from_canonical_bytes(blob)
    except (ValueError, KeyError, TypeError) as e:
        raise VerifyError(f"payload parse error: {e}") from e

    # 2. Verify blob IS canonical (prevents non-canonical injection attacks)
    if canonical_bytes(payload) != blob:
        raise VerifyError("payload bytes are not in canonical form")

    # 3. Find the trusted key for this descriptor's signing_key_gen
    key_map = {tk.generation: tk.pubkey for tk in trusted_keys}
    pub = key_map.get(payload.signing_key_gen)
    if pub is None:
        raise VerifyError(
            f"signing_key_gen={payload.signing_key_gen} not in trusted key set "
            f"(trusted generations: {sorted(key_map)})"
        )

    # 4. Signature verification
    if not ed_verify(pub, blob, signature):
        raise VerifyError("Ed25519 signature verification failed")

    # 5. Expiry check
    now_dt = datetime.fromisoformat(now_iso.replace("Z", "+00:00"))
    valid_until_dt = datetime.fromisoformat(payload.valid_until.replace("Z", "+00:00"))
    if now_dt > valid_until_dt + timedelta(hours=grace_hours):
        raise VerifyError(
            f"descriptor expired: valid_until={payload.valid_until}, "
            f"now={now_iso}, grace={grace_hours}h"
        )

    # 6. Chain verification
    if payload.generation == 1:
        if payload.previous_generation_hash is not None:
            raise VerifyError("generation 1 must have null previous_generation_hash")
    else:
        # generation > 1
        if payload.previous_generation_hash is None:
            raise VerifyError(
                f"generation {payload.generation} has null previous_generation_hash "
                "(missing chain link)"
            )
        if previous_descriptor_hash is not None:
            # Caller provided the previous hash — verify chain continuity
            if payload.previous_generation_hash != previous_descriptor_hash:
                raise VerifyError(
                    f"chain break: payload.previous_generation_hash="
                    f"{payload.previous_generation_hash!r} does not match "
                    f"expected={previous_descriptor_hash!r}"
                )
        else:
            # No prior hash provided — TOFU rollback defence:
            # We cannot verify a non-genesis descriptor without knowing the previous hash.
            raise VerifyError(
                "cannot verify chain: descriptor has generation>1 and a previous hash, "
                "but caller provided no previous_descriptor_hash (TOFU-rollback defence)"
            )

    return payload


def verify_chain(
    descriptors: Sequence[tuple[bytes, bytes]],
    trusted_keys: Sequence[TrustedKey],
    now_iso: str,
) -> list[DescriptorPayload]:
    """Verify a sequence of descriptors in generation order.

    Each descriptor's previous_generation_hash must match sha256 of the previous blob.
    Returns list of parsed payloads.  Raises VerifyError on first failure.
    """
    results: list[DescriptorPayload] = []
    prev_hash: str | None = None
    for blob, sig in descriptors:
        p = verify_descriptor(
            blob,
            sig,
            trusted_keys,
            now_iso,
            previous_descriptor_hash=prev_hash,
        )
        prev_hash = payload_hash(blob)
        results.append(p)
    return results
