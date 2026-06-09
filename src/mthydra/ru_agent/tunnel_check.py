"""K3: box-side end-to-end RU->EU tunnel self-check.

Opens a TCP connection to a Telegram-DC IP:443. The box's own MTHYDRA_DCS
iptables REDIRECT pushes that connection into sing-box -> Reality tunnel -> EU
exit -> Telegram, so this exercises the exact real path. The success predicate
proves the UPSTREAM established (not just sing-box's local accept): a broken
tunnel makes sing-box close the local socket promptly (recv -> EOF), a healthy
tunnel reaches Telegram which holds the connection (recv -> timeout) or answers.

Verdict is written to /run/mthydra/health.json and logged by the caller. This
module is stdlib-only by design (no controller imports — enforced by the
ru_agent AST guard test).
"""
from __future__ import annotations

import contextlib
import json
import os
import socket
import tempfile
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

DC_PORT = 443
PROBE_BYTES = b"\x00" * 8          # minimal nudge so the upstream must engage
READ_TIMEOUT_SECONDS = 3.0
CONNECT_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True)
class Verdict:
    checked_at: str
    verdict: str              # "ok" | "fail"
    detail: str
    telegram_dc_tried: str | None


def _default_clock() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _real_connect(ip: str, port: int, timeout: float) -> socket.socket:
    s = socket.create_connection((ip, port), timeout=timeout)
    s.settimeout(READ_TIMEOUT_SECONDS)
    return s


def check_eu_tunnel(
    *,
    dc_ips: list[str],
    connect_fn: Callable[[str, int, float], socket.socket] | None = None,
    clock: Callable[[], str] | None = None,
) -> Verdict:
    """Probe Telegram DCs through the tunnel until one succeeds.

    OK if the upstream holds the connection (timeout on read) or returns data;
    FAIL on EOF (sing-box closed it -> upstream dead) or any connect error on
    every DC tried."""
    now = (clock or _default_clock)()
    connect_fn = connect_fn or _real_connect
    if not dc_ips:
        return Verdict(now, "fail", "no telegram DC IPs in seed", None)

    last_ip = dc_ips[0]
    last_detail = "no DC attempted"
    for ip in dc_ips:
        last_ip = ip
        sock = None
        try:
            sock = connect_fn(ip, DC_PORT, CONNECT_TIMEOUT_SECONDS)
            sock.sendall(PROBE_BYTES)
            try:
                data = sock.recv(64)
            except TimeoutError:
                # Peer is holding the connection open -> upstream is alive.
                return Verdict(now, "ok", "upstream held connection", ip)
            if data:
                return Verdict(now, "ok", "upstream returned data", ip)
            last_detail = f"{ip}: EOF on read (sing-box closed; upstream dead)"
        except OSError as e:
            last_detail = f"{ip}: {e}"
        finally:
            if sock is not None:
                with contextlib.suppress(OSError):
                    sock.close()
    return Verdict(now, "fail", last_detail, last_ip)


def write_health(path: str, verdict: Verdict) -> None:
    """Atomically write the verdict JSON to `path` (/run is tmpfs)."""
    data = json.dumps(asdict(verdict), sort_keys=True).encode("utf-8")
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=d)
    try:
        os.write(fd, data)
        os.close(fd)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
