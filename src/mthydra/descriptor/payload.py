"""Descriptor payload dataclass and canonical JSON encoding (spec B §4, B-D2; spec E Task 5).

Schema label evolution:
- v1 (spec B): per-exit dict is {endpoint, fingerprint, weight}.
- v2 (spec E): per-exit dict adds {cover_sni, reality_pubkey} (both optional/nullable).

Both schemas round-trip through this module. New signs emit v2; verifiers
accept both for rolling-deployment compatibility.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

SCHEMA_V1 = "mthydra.descriptor.v1"
SCHEMA_V2 = "mthydra.descriptor.v2"
SCHEMA = SCHEMA_V2  # default schema for new payloads
_ACCEPTED_SCHEMAS = frozenset({SCHEMA_V1, SCHEMA_V2})

_KNOWN_FIELDS = frozenset({
    "schema",
    "generation",
    "signing_key_gen",
    "issued_at",
    "valid_until",
    "eu_exit_set",
    "previous_generation_hash",
    "next_signing_pubkey",
})

_KNOWN_EXIT_FIELDS_V1 = frozenset({"fingerprint", "endpoint", "weight"})
_KNOWN_EXIT_FIELDS_V2 = _KNOWN_EXIT_FIELDS_V1 | {"cover_sni", "reality_pubkey"}


@dataclass(frozen=True)
class EUExit:
    fingerprint: str
    endpoint: str
    weight: int
    cover_sni: str | None = None
    reality_pubkey: str | None = None


@dataclass(frozen=True)
class DescriptorPayload:
    generation: int
    signing_key_gen: int
    issued_at: str
    valid_until: str
    eu_exit_set: tuple[EUExit, ...]
    previous_generation_hash: str | None
    next_signing_pubkey: str | None
    schema: str = SCHEMA_V2

    @classmethod
    def from_canonical_bytes(cls, blob: bytes) -> "DescriptorPayload":
        """Parse without re-serialising. Raises ValueError on any structural error.

        Accepts both v1 and v2 schema labels.
        """
        try:
            obj = json.loads(blob.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise ValueError(f"invalid JSON: {e}") from e

        unknown = set(obj.keys()) - _KNOWN_FIELDS
        if unknown:
            raise ValueError(f"unknown fields in descriptor payload: {sorted(unknown)}")

        schema = obj.get("schema")
        if schema not in _ACCEPTED_SCHEMAS:
            raise ValueError(
                f"schema mismatch: expected one of {sorted(_ACCEPTED_SCHEMAS)!r}, "
                f"got {schema!r}"
            )

        allowed_exit_fields = (
            _KNOWN_EXIT_FIELDS_V2 if schema == SCHEMA_V2 else _KNOWN_EXIT_FIELDS_V1
        )

        exits_raw = obj.get("eu_exit_set", [])
        exits: list[EUExit] = []
        for e in exits_raw:
            unknown_exit = set(e.keys()) - allowed_exit_fields
            if unknown_exit:
                raise ValueError(f"unknown fields in eu_exit entry: {sorted(unknown_exit)}")
            cover_sni = e.get("cover_sni") if schema == SCHEMA_V2 else None
            reality_pubkey = e.get("reality_pubkey") if schema == SCHEMA_V2 else None
            exits.append(EUExit(
                fingerprint=str(e["fingerprint"]),
                endpoint=str(e["endpoint"]),
                weight=int(e["weight"]),
                cover_sni=None if cover_sni is None else str(cover_sni),
                reality_pubkey=None if reality_pubkey is None else str(reality_pubkey),
            ))

        return cls(
            generation=int(obj["generation"]),
            signing_key_gen=int(obj["signing_key_gen"]),
            issued_at=str(obj["issued_at"]),
            valid_until=str(obj["valid_until"]),
            eu_exit_set=tuple(exits),
            previous_generation_hash=obj.get("previous_generation_hash"),
            next_signing_pubkey=obj.get("next_signing_pubkey"),
            schema=schema,
        )


def canonical_bytes(payload: DescriptorPayload) -> bytes:
    """Produce deterministic JSON bytes (spec B §4 B-D2).

    Rules: sort_keys=True, no whitespace separators, UTF-8.
    Floats are prohibited — this function will produce incorrect results
    if floats are somehow introduced; see spec B §4 constraint note.

    Per-exit fields depend on payload.schema: v1 omits cover_sni/reality_pubkey;
    v2 always emits them (nullable when unset).
    """
    if payload.schema not in _ACCEPTED_SCHEMAS:
        raise ValueError(f"unknown payload.schema: {payload.schema!r}")

    if payload.schema == SCHEMA_V2:
        exits = [
            {
                "cover_sni": e.cover_sni,
                "endpoint": e.endpoint,
                "fingerprint": e.fingerprint,
                "reality_pubkey": e.reality_pubkey,
                "weight": e.weight,
            }
            for e in payload.eu_exit_set
        ]
    else:  # v1 legacy
        exits = [
            {
                "endpoint": e.endpoint,
                "fingerprint": e.fingerprint,
                "weight": e.weight,
            }
            for e in payload.eu_exit_set
        ]

    obj: dict[str, Any] = {
        "schema": payload.schema,
        "generation": payload.generation,
        "signing_key_gen": payload.signing_key_gen,
        "issued_at": payload.issued_at,
        "valid_until": payload.valid_until,
        "eu_exit_set": exits,
        "previous_generation_hash": payload.previous_generation_hash,
        "next_signing_pubkey": payload.next_signing_pubkey,
    }
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def payload_hash(payload_bytes: bytes) -> str:
    """Hex sha256 of canonical bytes — used for the chain field (spec B §4 B-D4)."""
    return hashlib.sha256(payload_bytes).hexdigest()
