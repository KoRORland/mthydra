"""Per-user subset builder + content hash — spec K §4.

Pure. One read transaction. Returns None for unassigned users.
"""
from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
from dataclasses import dataclass

from mthydra.controller.state import shards as _shards


@dataclass(frozen=True)
class SubsetBox:
    box_id: str
    public_ip: str
    port: int
    sni: str
    credential_b64: str


@dataclass(frozen=True)
class SubsetPayload:
    user_id: str
    shard_id: str
    generated_at: str
    boxes: tuple[SubsetBox, ...]
    subset_hash: str


def hash_subset(boxes: list[SubsetBox]) -> str:
    """Deterministic sha256 over sorted box descriptors."""
    lines = sorted(
        f"{b.box_id}|{b.public_ip}|{b.sni}|{b.credential_b64}" for b in boxes
    )
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def _box_port(_box_id: str) -> int:
    """MTProto Fake-TLS terminator listens on :443 per design §1.

    A future enhancement may store a per-box port on ru_boxes; for spec K
    MVP the constant is the right answer.
    """
    return 443


def build_subset(
    conn: sqlite3.Connection, user_id: str, *, now: str,
) -> SubsetPayload | None:
    """Per-user subset. None for unassigned users.

    Boxes without an active onward_credentials row are skipped (with no
    surfacing — the caller decides whether an empty subset is alertable).
    """
    row = conn.execute(
        "SELECT current_shard_id FROM users WHERE user_id=?", (user_id,)
    ).fetchone()
    if row is None or row[0] is None:
        return None
    shard_id: str = row[0]
    box_ids = _shards.list_shard_boxes(conn, shard_id, include_terminated=False)
    boxes: list[SubsetBox] = []
    for box_id in box_ids:
        meta = conn.execute(
            "SELECT public_ip, sni FROM ru_boxes "
            "WHERE box_id=? AND state IN ('provisioning','live')",
            (box_id,),
        ).fetchone()
        if meta is None or not meta[0]:
            continue
        cred = conn.execute(
            "SELECT credential FROM onward_credentials "
            "WHERE box_id=? AND revoked_at IS NULL "
            "ORDER BY issued_at DESC LIMIT 1",
            (box_id,),
        ).fetchone()
        if cred is None:
            continue
        cred_blob = bytes(cred[0])
        boxes.append(SubsetBox(
            box_id=box_id, public_ip=meta[0], port=_box_port(box_id),
            sni=meta[1], credential_b64=base64.b64encode(cred_blob).decode("ascii"),
        ))
    return SubsetPayload(
        user_id=user_id, shard_id=shard_id, generated_at=now,
        boxes=tuple(boxes), subset_hash=hash_subset(boxes),
    )


def payload_to_json(payload: SubsetPayload) -> str:
    return json.dumps(
        {
            "user_id": payload.user_id,
            "shard_id": payload.shard_id,
            "generated_at": payload.generated_at,
            "subset_hash": payload.subset_hash,
            "boxes": [
                {
                    "box_id": b.box_id,
                    "public_ip": b.public_ip,
                    "port": b.port,
                    "sni": b.sni,
                    "credential_b64": b.credential_b64,
                }
                for b in payload.boxes
            ],
        },
        sort_keys=True,
    )
