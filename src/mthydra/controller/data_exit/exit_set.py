"""Maintain eu_exit_set rows tracking the EU exit's data-plane liveness."""
from __future__ import annotations

import hashlib
import sqlite3


def _fingerprint(node_id: str, endpoint: str) -> str:
    return hashlib.sha256(f"{node_id}|{endpoint}".encode()).hexdigest()[:16]


def register_started(
    conn: sqlite3.Connection, *, node_id: str, listen_port: int, at: str,
) -> None:
    """Insert (or update if already present and not-retired) the eu_exit_set
    row for this node's live data-exit endpoint."""
    row = conn.execute(
        "SELECT public_ip, cover_sni, reality_pubkey FROM eu_nodes WHERE node_id=?",
        (node_id,),
    ).fetchone()
    if row is None:
        raise KeyError(f"eu_node {node_id!r} not found")
    public_ip, cover_sni, reality_pubkey = row
    if public_ip is None:
        raise ValueError(f"eu_node {node_id!r} has no public_ip")
    if cover_sni is None or reality_pubkey is None:
        raise ValueError(f"eu_node {node_id!r} missing cover_sni or reality_pubkey")

    endpoint = f"{public_ip}:{listen_port}"
    fp = _fingerprint(node_id, endpoint)
    existing = conn.execute(
        "SELECT retired_at FROM eu_exit_set WHERE fingerprint=?", (fp,),
    ).fetchone()
    if existing is None:
        conn.execute(
            "INSERT INTO eu_exit_set (fingerprint, endpoint, weight, added_at, "
            "cover_sni, reality_pubkey) VALUES (?, ?, 1, ?, ?, ?)",
            (fp, endpoint, at, cover_sni, reality_pubkey),
        )
    else:
        # Idempotent: if already present and not retired, refresh fields.
        if existing[0] is None:
            conn.execute(
                "UPDATE eu_exit_set SET cover_sni=?, reality_pubkey=? "
                "WHERE fingerprint=?",
                (cover_sni, reality_pubkey, fp),
            )
        else:
            # Was retired; un-retire by clearing retired_at and refreshing.
            conn.execute(
                "UPDATE eu_exit_set SET retired_at=NULL, cover_sni=?, "
                "reality_pubkey=?, added_at=? WHERE fingerprint=?",
                (cover_sni, reality_pubkey, at, fp),
            )
    conn.commit()


def clear(conn: sqlite3.Connection, *, node_id: str, at: str) -> None:
    """Retire all eu_exit_set rows whose endpoint matches this node's public_ip prefix."""
    rows = conn.execute(
        "SELECT fingerprint, endpoint FROM eu_exit_set WHERE retired_at IS NULL"
    ).fetchall()
    public_ip = conn.execute(
        "SELECT public_ip FROM eu_nodes WHERE node_id=?", (node_id,),
    ).fetchone()
    if public_ip is None or public_ip[0] is None:
        return
    needle = f"{public_ip[0]}:"
    for fp, endpoint in rows:
        if endpoint.startswith(needle):
            conn.execute(
                "UPDATE eu_exit_set SET retired_at=? WHERE fingerprint=?",
                (at, fp),
            )
    conn.commit()
