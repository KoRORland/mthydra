"""Hardcoded Telegram MTProto DC subnet list — parser + flattener."""
from __future__ import annotations

import ipaddress


def flatten_cidrs(v4: tuple[str, ...], v6: tuple[str, ...]) -> list[str]:
    """Validate and return a flat list of CIDR strings.

    Raises ValueError on any malformed entry.
    """
    out: list[str] = []
    for cidr in list(v4) + list(v6):
        try:
            ipaddress.ip_network(cidr, strict=True)
        except ValueError as e:
            raise ValueError(f"invalid CIDR: {cidr!r}: {e}") from e
        out.append(cidr)
    return out
