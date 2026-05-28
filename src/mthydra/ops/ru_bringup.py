"""mthydra-ops ru-bringup / ru-image-cycle — RU node automation wizards.

See doc/specs/2026-05-28-O-ru-bringup-and-image-cycle.md.
"""
from __future__ import annotations

import socket
import ssl
import time
from collections.abc import Callable


def wait_for_reachable(host: str, port: int, sni: str, *,
                       timeout_s: int, poll_s: int = 10,
                       on_progress: Callable[[Exception], None] | None = None,
                       ) -> bool:
    """TCP+TLS handshake liveness check (no cert validation — Fake-TLS box).

    Returns True on the first successful handshake; False after timeout_s.
    `on_progress` is called with the exception on each failed attempt.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=5) as sock:
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE   # Fake-TLS — liveness only (O-D4)
                with ctx.wrap_socket(sock, server_hostname=sni) as tls:
                    tls.do_handshake()
                    return True
        except (OSError, ssl.SSLError) as e:
            if on_progress is not None:
                on_progress(e)
            time.sleep(poll_s)
    return False
