"""Descriptor payload dataclass and canonical JSON encoding (spec B §4, B-D2)."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

SCHEMA = "mthydra.descriptor.v1"

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

_KNOWN_EXIT_FIELDS = frozenset({"fingerprint", "endpoint", "weight"})


@dataclass(frozen=True)
class EUExit:
    fingerprint: str
    endpoint: str
    weight: int


@dataclass(frozen=True)
class DescriptorPayload:
    generation: int
    signing_key_gen: int
    issued_at: str
    valid_until: str
    eu_exit_set: tuple[EUExit, ...]
    previous_generation_hash: str | None
    next_signing_pubkey: str | None

    @classmethod
    def from_canonical_bytes(cls, blob: bytes) -> "DescriptorPayload":
        """Parse without re-serialising. Raises ValueError on any structural error."""
        try:
            obj = json.loads(blob.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise ValueError(f"invalid JSON: {e}") from e

        unknown = set(obj.keys()) - _KNOWN_FIELDS
        if unknown:
            raise ValueError(f"unknown fields in descriptor payload: {sorted(unknown)}")

        schema = obj.get("schema")
        if schema != SCHEMA:
            raise ValueError(f"schema mismatch: expected {SCHEMA!r}, got {schema!r}")

        exits_raw = obj.get("eu_exit_set", [])
        exits = []
        for e in exits_raw:
            unknown_exit = set(e.keys()) - _KNOWN_EXIT_FIELDS
            if unknown_exit:
                raise ValueError(f"unknown fields in eu_exit entry: {sorted(unknown_exit)}")
            exits.append(EUExit(
                fingerprint=str(e["fingerprint"]),
                endpoint=str(e["endpoint"]),
                weight=int(e["weight"]),
            ))

        return cls(
            generation=int(obj["generation"]),
            signing_key_gen=int(obj["signing_key_gen"]),
            issued_at=str(obj["issued_at"]),
            valid_until=str(obj["valid_until"]),
            eu_exit_set=tuple(exits),
            previous_generation_hash=obj.get("previous_generation_hash"),
            next_signing_pubkey=obj.get("next_signing_pubkey"),
        )


def canonical_bytes(payload: DescriptorPayload) -> bytes:
    """Produce deterministic JSON bytes (spec B §4 B-D2).

    Rules: sort_keys=True, no whitespace separators, UTF-8.
    Floats are prohibited — this function will produce incorrect results
    if floats are somehow introduced; see spec B §4 constraint note.
    """
    obj: dict[str, Any] = {
        "schema": SCHEMA,
        "generation": payload.generation,
        "signing_key_gen": payload.signing_key_gen,
        "issued_at": payload.issued_at,
        "valid_until": payload.valid_until,
        "eu_exit_set": [
            {
                "endpoint": e.endpoint,
                "fingerprint": e.fingerprint,
                "weight": e.weight,
            }
            for e in payload.eu_exit_set
        ],
        "previous_generation_hash": payload.previous_generation_hash,
        "next_signing_pubkey": payload.next_signing_pubkey,
    }
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def payload_hash(payload_bytes: bytes) -> str:
    """Hex sha256 of canonical bytes — used for the chain field (spec B §4 B-D4)."""
    return hashlib.sha256(payload_bytes).hexdigest()
