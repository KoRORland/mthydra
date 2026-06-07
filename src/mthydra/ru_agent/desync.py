"""Install + verify + clear an NFQUEUE rule that hands the RU->EU Reality flow
(outbound TCP to the EU-exit IPs on :443) to nfqws for DPI desync.

Targets ONLY the exit IPs on :443 — the local mtg->sing-box redirect inbound
(127.0.0.1) is never matched (spec V V-D5). The rule lives in the mangle table
in its own chain hooked from OUTPUT, mirroring ru_agent.iptables discipline."""
from __future__ import annotations

import contextlib
import shlex
import subprocess

EXIT_PORT = 443
_CHAIN = "MTHYDRA_DESYNC"
_TABLE = "mangle"


class DesyncError(RuntimeError):
    pass


def _run(cmd: list[str]) -> str:
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        raise DesyncError(
            f"command {' '.join(cmd)!r} failed: rc={result.returncode} "
            f"stderr={getattr(result, 'stderr', b'')!r}"
        )
    return (getattr(result, "stdout", b"") or b"").decode("utf-8", errors="replace")


def nfqws_argv(nfqws_path: str, strategy: str, *, qnum: int) -> list[str]:
    """Build the nfqws command. The agent owns --qnum; `strategy` is the signed,
    operator-tuned argument string (everything else)."""
    return [nfqws_path, f"--qnum={qnum}", *shlex.split(strategy)]


def split_exit_ips(endpoints: list[str]) -> tuple[list[str], list[str]]:
    """Split 'host:port' endpoints into (v4_ips, v6_ips). IPv6 endpoints are
    bracketed: '[2001:db8::1]:443'."""
    v4: list[str] = []
    v6: list[str] = []
    for ep in endpoints:
        if ep.startswith("["):
            host = ep[1:ep.index("]")]
            v6.append(host)
        else:
            host = ep.rsplit(":", 1)[0]
            (v6 if ":" in host else v4).append(host)
    return v4, v6


def install(*, exit_ips: list[str], qnum: int) -> None:
    """(Re)install the desync chain + per-exit-IP NFQUEUE rules. Idempotent."""
    clear(qnum)
    v4, v6 = split_exit_ips(exit_ips)
    for tool, ips in (("iptables", v4), ("ip6tables", v6)):
        if not ips:
            continue
        _run([tool, "-t", _TABLE, "-N", _CHAIN])
        for ip in ips:
            _run([
                tool, "-t", _TABLE, "-A", _CHAIN,
                "-d", ip, "-p", "tcp", "--dport", str(EXIT_PORT),
                "-j", "NFQUEUE", "--queue-num", str(qnum),
            ])
        _run([tool, "-t", _TABLE, "-A", "OUTPUT", "-p", "tcp",
              "--dport", str(EXIT_PORT), "-j", _CHAIN])


def _rule_present(out: str, ip: str, qnum: int) -> bool:
    """Token-exact: dest IP (with or without /32,/128 mask) AND --queue-num on
    the same line. Mirrors ru_agent.iptables._rule_present strictness."""
    q = str(qnum)
    ip_forms = {ip, f"{ip}/32", f"{ip}/128"}
    for line in out.splitlines():
        toks = line.split()
        has_dst = any(
            toks[i] == "-d" and i + 1 < len(toks) and toks[i + 1] in ip_forms
            for i in range(len(toks))
        )
        has_q = any(
            toks[i] == "--queue-num" and i + 1 < len(toks) and toks[i + 1] == q
            for i in range(len(toks))
        )
        if has_dst and has_q:
            return True
    return False


def verify_installed(*, exit_ips: list[str], qnum: int) -> bool:
    """True iff every expected exit IP has its NFQUEUE rule (#34/#35)."""
    v4, v6 = split_exit_ips(exit_ips)
    for tool, ips in (("iptables", v4), ("ip6tables", v6)):
        if not ips:
            continue
        try:
            out = _run([tool, "-t", _TABLE, "-S", _CHAIN])
        except DesyncError:
            return False
        for ip in ips:
            if not _rule_present(out, ip, qnum):
                return False
    return True


def clear(qnum: int) -> None:
    """Remove the chain. Idempotent."""
    for tool in ("iptables", "ip6tables"):
        for cmd in (
            [tool, "-t", _TABLE, "-D", "OUTPUT", "-p", "tcp",
             "--dport", str(EXIT_PORT), "-j", _CHAIN],
            [tool, "-t", _TABLE, "-F", _CHAIN],
            [tool, "-t", _TABLE, "-X", _CHAIN],
        ):
            with contextlib.suppress(DesyncError):
                _run(cmd)
