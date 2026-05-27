"""Install + verify + uninstall iptables/ip6tables TPROXY rules.

Outbound traffic to Telegram MTProto DC subnets gets routed into sing-box's
tproxy inbound on 127.0.0.1:<tproxy_port>. mtg's hardcoded Telegram upstream
is captured before the kernel actually connects out.
"""
from __future__ import annotations

import subprocess


class IptablesError(RuntimeError):
    pass


_CHAIN = "MTHYDRA_DCS"


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
    """Install the mangle-table chain and per-CIDR TPROXY rules."""
    for tool, cidrs in (("iptables", dc_cidrs_v4), ("ip6tables", dc_cidrs_v6)):
        if not cidrs:
            continue
        # Create the chain (or flush if it exists).
        _run([tool, "-t", "mangle", "-N", _CHAIN])
        _run([tool, "-t", "mangle", "-F", _CHAIN])
        for cidr in cidrs:
            _run([
                tool, "-t", "mangle", "-A", _CHAIN,
                "-d", cidr, "-p", "tcp",
                "-j", "TPROXY", "--on-port", str(tproxy_port),
            ])
        # Hook the chain into OUTPUT (locally-originated traffic).
        _run([tool, "-t", "mangle", "-A", "OUTPUT", "-j", _CHAIN])


def _rule_present(out: str, cidr: str, port: int) -> bool:
    """True iff some rule line routes exactly `cidr` to exactly `port`.

    Token-exact, not substring: `-d 10.0.0.0/8` must not satisfy a query for
    `10.0.0.0/16` (or vice-versa), and `--on-port 123456` must not satisfy a
    query for port 12345. The destination CIDR and the on-port must also be on
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
            toks[i] == "--on-port" and i + 1 < len(toks) and toks[i + 1] == port_s
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
            out = _run([tool, "-t", "mangle", "-S", _CHAIN])
        except IptablesError:
            return False
        for cidr in cidrs:
            if not _rule_present(out, cidr, tproxy_port):
                return False
    return True


def uninstall() -> None:
    """Remove the chain. Idempotent."""
    for tool in ("iptables", "ip6tables"):
        try:
            _run([tool, "-t", "mangle", "-D", "OUTPUT", "-j", _CHAIN])
        except IptablesError:
            pass
        try:
            _run([tool, "-t", "mangle", "-F", _CHAIN])
        except IptablesError:
            pass
        try:
            _run([tool, "-t", "mangle", "-X", _CHAIN])
        except IptablesError:
            pass
