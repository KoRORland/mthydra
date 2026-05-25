"""Pinned known-good profile repository — spec I §5, §8 (T3).

One row per image_version (FK to ru_images). Operator-attested via the
CLI `profile-pin`. The §8 design says the profile is built at image-build
time; spec D's image-promote will later be amended to require a pinned
profile, but spec I MVP allows the pin to follow promotion.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from mthydra.controller.state import audit


@dataclass(frozen=True)
class ImageProfile:
    image_version: str
    profile_json: str
    recorded_at: str
    recorded_by: str
    notes: str | None


def pin(
    conn: sqlite3.Connection,
    *,
    image_version: str,
    profile_json: str,
    recorded_by: str,
    at: str,
    notes: str | None = None,
) -> None:
    """Insert or overwrite the image_profiles row for image_version.

    Refuses if image_version is not present in ru_images.
    """
    if not profile_json:
        raise ValueError("profile_json must be non-empty")
    row = conn.execute(
        "SELECT 1 FROM ru_images WHERE image_version=?", (image_version,)
    ).fetchone()
    if row is None:
        raise LookupError(f"no ru_image {image_version!r}")
    existed = conn.execute(
        "SELECT 1 FROM image_profiles WHERE image_version=?", (image_version,)
    ).fetchone()
    if existed:
        conn.execute(
            "UPDATE image_profiles SET profile_json=?, recorded_at=?, recorded_by=?, notes=? "
            "WHERE image_version=?",
            (profile_json, at, recorded_by, notes, image_version),
        )
    else:
        conn.execute(
            "INSERT INTO image_profiles (image_version, profile_json, recorded_at, "
            "recorded_by, notes) VALUES (?, ?, ?, ?, ?)",
            (image_version, profile_json, at, recorded_by, notes),
        )
    audit.log_event(
        conn, ts=at, actor="operator", action="image_profile_pin",
        target=image_version,
        details_json=json.dumps({
            "recorded_by": recorded_by,
            "overwrite": bool(existed),
            "notes": notes,
        }),
    )


def get_profile(
    conn: sqlite3.Connection, image_version: str,
) -> ImageProfile | None:
    r = conn.execute(
        "SELECT image_version, profile_json, recorded_at, recorded_by, notes "
        "FROM image_profiles WHERE image_version=?",
        (image_version,),
    ).fetchone()
    return ImageProfile(*r) if r else None


def list_pinned(conn: sqlite3.Connection) -> list[ImageProfile]:
    rows = conn.execute(
        "SELECT image_version, profile_json, recorded_at, recorded_by, notes "
        "FROM image_profiles ORDER BY image_version"
    ).fetchall()
    return [ImageProfile(*r) for r in rows]
