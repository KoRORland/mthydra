"""Parse + verify the RU-side seed bundle (mthydra.ru_seed.v2)."""
from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from pathlib import Path

from mthydra.descriptor.authority import (
    OnwardCredentialPayload, VerifyError, verify_onward_credential,
)


class SeedError(RuntimeError):
    """Seed parsing or verification failure."""


_REQUIRED_FIELDS = (
    "schema", "box_id", "sni", "transport_role", "reality_uuid",
    "onward_credential", "authority_pubkey_pem", "descriptor_trust_anchors",
    "initial_descriptor", "image", "descriptor_refresh_url",
    "agent_source_url", "agent_source_sha256", "telegram_dcs",
    "issued_at", "issued_by_authority_generation",
)

_SUPPORTED_SCHEMAS = ("mthydra.ru_seed.v2",)


@dataclass(frozen=True)
class Seed:
    box_id: str
    sni: str
    transport_role: str
    reality_uuid: str
    onward_credential: bytes
    authority_pubkey_pem: str
    descriptor_trust_anchors: tuple[bytes, ...]
    initial_descriptor: bytes
    image: dict
    descriptor_refresh_url: str
    agent_source_url: str
    agent_source_sha256: str
    telegram_dcs: dict
    issued_at: str
    issued_by_authority_generation: int


def load(path: Path | str) -> Seed:
    """Read seed.json from disk and parse it. Raises SeedError on any failure."""
    p = Path(path)
    if not p.exists():
        raise SeedError(f"seed.json not found at {p}")
    try:
        raw = json.loads(p.read_text())
    except json.JSONDecodeError as e:
        raise SeedError(f"seed.json is not valid JSON: {e}") from e
    schema = raw.get("schema")
    if schema not in _SUPPORTED_SCHEMAS:
        raise SeedError(
            f"unsupported seed schema: {schema!r} (expected one of {_SUPPORTED_SCHEMAS})"
        )
    for field in _REQUIRED_FIELDS:
        if field not in raw:
            raise SeedError(f"missing required field: {field!r}")
    return Seed(
        box_id=raw["box_id"],
        sni=raw["sni"],
        transport_role=raw["transport_role"],
        reality_uuid=raw["reality_uuid"],
        onward_credential=base64.b64decode(raw["onward_credential"]),
        authority_pubkey_pem=raw["authority_pubkey_pem"],
        descriptor_trust_anchors=tuple(
            base64.b64decode(t) for t in raw["descriptor_trust_anchors"]
        ),
        initial_descriptor=base64.b64decode(raw["initial_descriptor"]),
        image=raw["image"],
        descriptor_refresh_url=raw["descriptor_refresh_url"],
        agent_source_url=raw["agent_source_url"],
        agent_source_sha256=raw["agent_source_sha256"],
        telegram_dcs=raw["telegram_dcs"],
        issued_at=raw["issued_at"],
        issued_by_authority_generation=raw["issued_by_authority_generation"],
    )


def verify_credential(seed: Seed) -> OnwardCredentialPayload:
    """Sanity-check the embedded onward credential against the authority pubkey.

    Confirms:
      - signature is valid against authority_pubkey_pem
      - payload's box_id matches seed.box_id

    Raises SeedError on any failure.
    """
    try:
        payload = verify_onward_credential(
            seed.onward_credential, seed.authority_pubkey_pem,
        )
    except VerifyError as e:
        raise SeedError(f"onward credential verification failed: {e}") from e
    if payload.box_id != seed.box_id:
        raise SeedError(
            f"onward credential box_id mismatch: "
            f"seed has {seed.box_id!r}, credential has {payload.box_id!r}"
        )
    return payload
