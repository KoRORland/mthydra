"""K3: read live VLESS sessions from the local sing-box clash_api.

The pure parse (parse_connections) is separated from I/O (poll_active_sessions)
so it is unit-tested without a network. The EU exit names its VLESS users by
box_id, so each live connection's user field IS the box id with a live tunnel.
"""
from __future__ import annotations

import json
import urllib.request


def parse_connections(body: str) -> set[str]:
    """Extract the set of box_ids from a clash_api /connections JSON body.

    sing-box versions have used both ``inboundUser`` and ``user`` for the
    per-connection inbound user; check both so a version skew does not silently
    yield an empty set (verify against the running sing-box during integration).
    A malformed body is treated as 'no sessions', never an exception.
    """
    try:
        doc = json.loads(body)
    except (ValueError, TypeError):
        return set()
    out: set[str] = set()
    for c in doc.get("connections") or []:
        meta = c.get("metadata") or {}
        user = meta.get("inboundUser") or meta.get("user") or ""
        if user:
            out.add(user)
    return out


def poll_active_sessions(clash_api_url: str, *, timeout: float = 5.0) -> set[str]:
    """GET <clash_api_url>/connections and return the box_ids with live sessions.

    Raises on connection/HTTP error — the caller decides how to treat an
    unreadable API (K3: 'no observations this tick', never 'all boxes broken').
    """
    url = clash_api_url.rstrip("/") + "/connections"
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        body = resp.read().decode("utf-8", errors="replace")
    return parse_connections(body)
