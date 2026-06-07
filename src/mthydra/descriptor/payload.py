"""Descriptor payload dataclass and canonical JSON encoding (spec B §4, B-D2; spec E Task 5).

Schema label evolution:
- v1 (spec B): per-exit dict is {endpoint, fingerprint, weight}.
- v2 (spec E): per-exit dict adds {cover_sni, reality_pubkey} (both optional/nullable).
- v3 (V1 plan): same per-exit fields as v2; adds optional top-level tls_fingerprints
  weighted list so RU boxes can self-pick a uTLS fingerprint, and an optional
  top-level desync_strategy string (V2 plan) carrying nfqws CLI args for the
  RU-side desync layer.

All schemas round-trip through this module. New signs emit v3; verifiers
accept v1/v2/v3 for rolling-deployment compatibility.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

SCHEMA_V1 = "mthydra.descriptor.v1"
SCHEMA_V2 = "mthydra.descriptor.v2"
SCHEMA_V3 = "mthydra.descriptor.v3"
SCHEMA = SCHEMA_V3  # default schema for new payloads
_ACCEPTED_SCHEMAS = frozenset({SCHEMA_V1, SCHEMA_V2, SCHEMA_V3})

KNOWN_UTLS_FINGERPRINTS = frozenset({
    "chrome", "firefox", "safari", "ios", "android",
    "edge", "360", "qq", "random", "randomized",
})

_KNOWN_FIELDS = frozenset({
    "schema",
    "generation",
    "signing_key_gen",
    "issued_at",
    "valid_until",
    "eu_exit_set",
    "previous_generation_hash",
    "next_signing_pubkey",
    "tls_fingerprints",
    "desync_strategy",
})

_KNOWN_EXIT_FIELDS_V1 = frozenset({"fingerprint", "endpoint", "weight"})
_KNOWN_EXIT_FIELDS_V2 = _KNOWN_EXIT_FIELDS_V1 | {"cover_sni", "reality_pubkey"}
_KNOWN_EXIT_FIELDS_V3 = _KNOWN_EXIT_FIELDS_V2


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
    schema: str = SCHEMA_V3
    tls_fingerprints: tuple[tuple[str, int], ...] | None = None
    desync_strategy: str | None = None

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

        if "tls_fingerprints" in obj and schema != SCHEMA_V3:
            raise ValueError("tls_fingerprints only valid in v3")

        if "desync_strategy" in obj and schema != SCHEMA_V3:
            raise ValueError("desync_strategy only valid in v3")

        _v2_or_v3 = schema in (SCHEMA_V2, SCHEMA_V3)
        allowed_exit_fields = (
            _KNOWN_EXIT_FIELDS_V3 if schema == SCHEMA_V3
            else _KNOWN_EXIT_FIELDS_V2 if schema == SCHEMA_V2
            else _KNOWN_EXIT_FIELDS_V1
        )

        exits_raw = obj.get("eu_exit_set", [])
        exits: list[EUExit] = []
        for e in exits_raw:
            unknown_exit = set(e.keys()) - allowed_exit_fields
            if unknown_exit:
                raise ValueError(f"unknown fields in eu_exit entry: {sorted(unknown_exit)}")
            cover_sni = e.get("cover_sni") if _v2_or_v3 else None
            reality_pubkey = e.get("reality_pubkey") if _v2_or_v3 else None
            exits.append(EUExit(
                fingerprint=str(e["fingerprint"]),
                endpoint=str(e["endpoint"]),
                weight=int(e["weight"]),
                cover_sni=None if cover_sni is None else str(cover_sni),
                reality_pubkey=None if reality_pubkey is None else str(reality_pubkey),
            ))

        fps_raw = obj.get("tls_fingerprints")
        tls_fingerprints: tuple[tuple[str, int], ...] | None
        if not fps_raw:
            tls_fingerprints = None
        else:
            tls_fingerprints = tuple(
                (str(item["fp"]), int(item["weight"])) for item in fps_raw
            )

        desync_strategy_raw = obj.get("desync_strategy")
        desync_strategy = (
            None if desync_strategy_raw is None else str(desync_strategy_raw)
        )

        return cls(
            generation=int(obj["generation"]),
            signing_key_gen=int(obj["signing_key_gen"]),
            issued_at=str(obj["issued_at"]),
            valid_until=str(obj["valid_until"]),
            eu_exit_set=tuple(exits),
            previous_generation_hash=obj.get("previous_generation_hash"),
            next_signing_pubkey=obj.get("next_signing_pubkey"),
            schema=schema,
            tls_fingerprints=tls_fingerprints,
            desync_strategy=desync_strategy,
        )


def canonical_bytes(payload: DescriptorPayload) -> bytes:
    """Produce deterministic JSON bytes (spec B §4 B-D2).

    Rules: sort_keys=True, no whitespace separators, UTF-8.
    Floats are prohibited — this function will produce incorrect results
    if floats are somehow introduced; see spec B §4 constraint note.

    Per-exit fields depend on payload.schema: v1 omits cover_sni/reality_pubkey;
    v2 and v3 always emit them (nullable when unset). The top-level
    tls_fingerprints key is emitted only for v3, and only when non-empty;
    an empty/None list is omitted (round-trips back to None). The top-level
    desync_strategy key is emitted only for v3, and only when truthy; None
    and the empty string are both omitted (round-trip back to None).
    """
    if payload.schema not in _ACCEPTED_SCHEMAS:
        raise ValueError(f"unknown payload.schema: {payload.schema!r}")

    _v2_or_v3 = payload.schema in (SCHEMA_V2, SCHEMA_V3)
    if _v2_or_v3:
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

    if payload.schema == SCHEMA_V3 and payload.tls_fingerprints:
        obj["tls_fingerprints"] = [
            {"fp": fp, "weight": w}
            for fp, w in sorted(payload.tls_fingerprints)
        ]

    if payload.schema == SCHEMA_V3 and payload.desync_strategy:
        obj["desync_strategy"] = payload.desync_strategy

    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def payload_hash(payload_bytes: bytes) -> str:
    """Hex sha256 of canonical bytes — used for the chain field (spec B §4 B-D4)."""
    return hashlib.sha256(payload_bytes).hexdigest()
