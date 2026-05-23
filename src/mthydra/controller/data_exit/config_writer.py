"""Render sing-box server JSON from controller state."""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from pathlib import Path

from mthydra.controller.config import DataExitConfig
from mthydra.controller.data_exit.telegram_dcs import flatten_cidrs


def _live_users(conn: sqlite3.Connection) -> list[dict]:
    """Return the (box_id, reality_uuid) list eligible for the allowlist:
    state=live AND non-NULL reality_uuid AND has a non-revoked credential.
    """
    rows = conn.execute(
        """
        SELECT DISTINCT rb.box_id, rb.reality_uuid
        FROM ru_boxes rb
        JOIN onward_credentials oc ON oc.box_id = rb.box_id
        WHERE rb.state = 'live'
          AND rb.reality_uuid IS NOT NULL
          AND oc.revoked_at IS NULL
        ORDER BY rb.box_id
        """
    ).fetchall()
    return [
        {"name": box_id, "uuid": reality_uuid, "flow": "xtls-rprx-vision"}
        for (box_id, reality_uuid) in rows
    ]


def render_sing_box_config(
    conn: sqlite3.Connection,
    cfg: DataExitConfig,
    *,
    node_id: str,
    cover_sni: str,
    reality_private_key: str,
) -> bytes:
    """Render the full sing-box server config as canonical-JSON bytes."""
    users = _live_users(conn)
    dc_cidrs = flatten_cidrs(cfg.telegram_dcs_v4, cfg.telegram_dcs_v6)

    payload = {
        "log": {"level": "warn", "timestamp": True},
        "inbounds": [
            {
                "type": "vless",
                "tag": "vless-in",
                "listen": "0.0.0.0",
                "listen_port": cfg.listen_port,
                "users": users,
                "tls": {
                    "enabled": True,
                    "server_name": cover_sni,
                    "reality": {
                        "enabled": True,
                        "handshake": {"server": cover_sni, "server_port": 443},
                        "private_key": reality_private_key,
                        "short_id": [""],
                        "max_time_difference": "1m",
                    },
                },
            }
        ],
        "outbounds": [
            {"type": "direct", "tag": "telegram-direct"},
        ],
        "route": {
            "rules": (
                [{"ip_cidr": dc_cidrs, "outbound": "telegram-direct"}]
                if dc_cidrs else []
            ),
            "final": "telegram-direct",
        },
    }
    return json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")


def write_atomic(path: Path | str, content: bytes) -> None:
    """Write content to path via tempfile + os.replace in the same directory."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(content)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        raise
