"""MVP probers — all run via an ssh_cmd_fn injected by the wheel.

Each returns (status, evidence). Status ∈ {pass, soft_fail, hard_fail}.

soft_fail = transient/vantage-side failure (SSH timeout, connect refused at
the vantage's NIC). hard_fail = the box itself looks wrong. pass = clean.
"""
from __future__ import annotations

import re
from collections.abc import Callable


def _ssh_or_softfail(ssh_cmd_fn: Callable, *cmd_parts: str
                     ) -> tuple[str, int, str] | tuple[None, int, str]:
    """Run an SSH command. Returns ('ok', rc, output) on success or
    ('softfail', rc, err) if SSH itself failed (rc 255 / nonzero with empty
    stdout). Wheel uses this to short-circuit probe-record with soft_fail."""
    try:
        res = ssh_cmd_fn(*cmd_parts)
    except Exception as e:
        return ("softfail", -1, f"ssh transport error: {e}")
    out = (res.stdout or "") + (res.stderr or "")
    if res.returncode == 255 or (res.returncode != 0 and not res.stdout):
        return ("softfail", res.returncode, out)
    return ("ok", res.returncode, out)


def probe_tls_fall_through(ssh_cmd_fn: Callable, box_ip: str, cover_sni: str
                           ) -> tuple[str, str]:
    """openssl s_client to the box; pass iff TLS verification succeeds against
    the cover's chain (i.e. the box presents the cover-domain cert cleanly)."""
    status, _rc, out = _ssh_or_softfail(
        ssh_cmd_fn,
        "sh", "-c",
        f"openssl s_client -connect {box_ip}:443 -servername {cover_sni}"
        f" </dev/null 2>&1 | head -60",
    )
    if status == "softfail":
        return ("soft_fail", out)
    if "Verify return code: 0" in out:
        return ("pass", out)
    return ("hard_fail", out)


_ISSUER_RE = re.compile(r"^issuer=(.+)$", re.MULTILINE)


def probe_cover_consistency(ssh_cmd_fn: Callable, box_ip: str, cover_sni: str
                            ) -> tuple[str, str]:
    """Fetch issuer from both <box>:443 and <cover>:443 via the vantage.
    Pass iff issuers match."""
    box_status, _rc, box_out = _ssh_or_softfail(
        ssh_cmd_fn, "sh", "-c",
        f"openssl s_client -connect {box_ip}:443 -servername {cover_sni}"
        f" </dev/null 2>&1 | head -60",
    )
    if box_status == "softfail":
        return ("soft_fail", box_out)
    cover_status, _rc, cover_out = _ssh_or_softfail(
        ssh_cmd_fn, "sh", "-c",
        f"openssl s_client -connect {cover_sni}:443 -servername {cover_sni}"
        f" </dev/null 2>&1 | head -60",
    )
    if cover_status == "softfail":
        return ("soft_fail", cover_out)
    box_iss = _ISSUER_RE.search(box_out)
    cov_iss = _ISSUER_RE.search(cover_out)
    if not (box_iss and cov_iss):
        return ("hard_fail",
                f"could not parse issuer; box={box_out[:200]} "
                f"cover={cover_out[:200]}")
    if box_iss.group(1).strip() == cov_iss.group(1).strip():
        return ("pass",
                f"box issuer == cover issuer: {box_iss.group(1).strip()}")
    return ("hard_fail",
            f"issuer mismatch: box={box_iss.group(1).strip()!r} "
            f"cover={cov_iss.group(1).strip()!r}")


_NCAT_OPEN_RE = re.compile(r"(?:Ncat: Connected to )(\d+\.\d+\.\d+\.\d+:)(\d+)")
_BARE_NC_OPEN_RE = re.compile(r"^([^\s]+) (\d+) port .* open\b", re.MULTILINE)
_SCAN_PORTS = ("80", "443", "8080", "22", "53")


def probe_surface_scan(ssh_cmd_fn: Callable, box_ip: str) -> tuple[str, str]:
    """nc -zv against {80, 443, 8080, 22, 53}; pass iff only 443 answered."""
    status, _rc, out = _ssh_or_softfail(
        ssh_cmd_fn, "sh", "-c",
        f"for p in {' '.join(_SCAN_PORTS)}; do nc -zv -w 3 {box_ip} $p"
        f" 2>&1; done",
    )
    if status == "softfail":
        return ("soft_fail", out)
    open_ports: set[str] = set()
    for m in _NCAT_OPEN_RE.finditer(out):
        open_ports.add(m.group(2))
    for m in _BARE_NC_OPEN_RE.finditer(out):
        open_ports.add(m.group(2))
    if open_ports == {"443"}:
        return ("pass", f"only 443 open ({sorted(open_ports)})")
    extras = sorted(open_ports - {"443"})
    return ("hard_fail",
            f"unexpected open ports: {extras} (full: {sorted(open_ports)})")
