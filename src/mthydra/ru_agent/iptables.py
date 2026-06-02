"""Install + verify + uninstall iptables/ip6tables REDIRECT rules.

mtg's outbound connections to the Telegram MTProto DC subnets are locally
generated (they traverse the OUTPUT chain), so they cannot be captured with
TPROXY — the kernel's xt_TPROXY target is only valid in mangle/PREROUTING and
the kernel rejects a TPROXY-bearing chain hooked from OUTPUT with EINVAL. We use
nat/REDIRECT instead, which is valid in OUTPUT and rewrites the destination to
sing-box's local redirect inbound (127.0.0.1:<port>). sing-box recovers the
original destination via SO_ORIGINAL_DST and tunnels it to the EU exit.

Loop-safe by construction: only Telegram-DC destinations are redirected;
sing-box's own tunnel to the EU exit is not a Telegram DC, so it is never
recaptured.
"""
from __future__ import annotations

import contextlib
import subprocess


class IptablesError(RuntimeError):
    pass


_CHAIN = "MTHYDRA_DCS"
_TABLE = "nat"


def _run(cmd: list[str]) -> str:
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        raise IptablesError(
            f"command {' '.join(cmd)!r} failed: rc={result.returncode} "
            f"stderr={getattr(result, 'stderr', b'')!r}"
        )
    return (getattr(result, "stdout", b"") or b"").decode("utf-8", errors="replace")


def install(
    *, dc_cidrs_v4: list[str], dc_cidrs_v6: list[str], tproxy_port: int,
) -> None:
    """Install the nat-table chain and per-CIDR REDIRECT rules.

    Idempotent: any prior install is torn down first, so a retry never trips
    over a half-created chain ("Chain already exists")."""
    uninstall()
    for tool, cidrs in (("iptables", dc_cidrs_v4), ("ip6tables", dc_cidrs_v6)):
        if not cidrs:
            continue
        _run([tool, "-t", _TABLE, "-N", _CHAIN])
        for cidr in cidrs:
            _run([
                tool, "-t", _TABLE, "-A", _CHAIN,
                "-d", cidr, "-p", "tcp",
                "-j", "REDIRECT", "--to-ports", str(tproxy_port),
            ])
        # Hook the chain into OUTPUT (locally-originated traffic, e.g. mtg).
        _run([tool, "-t", _TABLE, "-A", "OUTPUT", "-p", "tcp", "-j", _CHAIN])


def _rule_present(out: str, cidr: str, port: int) -> bool:
    """True iff some rule line routes exactly `cidr` to exactly `port`.

    Token-exact, not substring: `-d 10.0.0.0/8` must not satisfy a query for
    `10.0.0.0/16` (or vice-versa), and `--to-ports 123456` must not satisfy a
    query for port 12345. The destination CIDR and the to-ports must also be on
    the *same* rule line.
    """
    port_s = str(port)
    for line in out.splitlines():
        toks = line.split()
        has_dst = any(
            toks[i] == "-d" and i + 1 < len(toks) and toks[i + 1] == cidr
            for i in range(len(toks))
        )
        has_port = any(
            toks[i] == "--to-ports" and i + 1 < len(toks) and toks[i + 1] == port_s
            for i in range(len(toks))
        )
        if has_dst and has_port:
            return True
    return False


def verify_installed(
    dc_cidrs_v4: list[str], dc_cidrs_v6: list[str], *, tproxy_port: int,
) -> bool:
    """Return True iff every expected CIDR rule is present in the chain."""
    for tool, cidrs in (("iptables", dc_cidrs_v4), ("ip6tables", dc_cidrs_v6)):
        if not cidrs:
            continue
        try:
            out = _run([tool, "-t", _TABLE, "-S", _CHAIN])
        except IptablesError:
            return False
        for cidr in cidrs:
            if not _rule_present(out, cidr, tproxy_port):
                return False
    return True


def uninstall() -> None:
    """Remove the chain. Idempotent."""
    for tool in ("iptables", "ip6tables"):
        for cmd in (
            [tool, "-t", _TABLE, "-D", "OUTPUT", "-p", "tcp", "-j", _CHAIN],
            [tool, "-t", _TABLE, "-F", _CHAIN],
            [tool, "-t", _TABLE, "-X", _CHAIN],
        ):
            with contextlib.suppress(IptablesError):
                _run(cmd)
