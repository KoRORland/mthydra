"""Verify RU-box hardening: swap off, journald volatile, core dumps disabled,
/var/log + /run/mthydra on tmpfs. Refuses to continue on any failure."""
from __future__ import annotations

import subprocess
from pathlib import Path


class HardeningError(RuntimeError):
    """A hardening invariant is violated."""


_PROC_SWAPS_PATH = "/proc/swaps"
_CORE_PATTERN_PATH = "/proc/sys/kernel/core_pattern"


def _swap_disabled() -> bool:
    """True iff /proc/swaps has only the header line (no active swap area)."""
    try:
        with open(_PROC_SWAPS_PATH) as f:
            lines = [l for l in f.read().splitlines() if l.strip()]
    except FileNotFoundError:
        return True  # No /proc/swaps means no swap subsystem.
    return len(lines) <= 1  # header only


def _journald_volatile() -> bool:
    """True iff systemd-journald is configured with Storage=volatile (or similar)."""
    try:
        result = subprocess.run(
            ["journalctl", "--header"], capture_output=True, text=True, timeout=5,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False
    if result.returncode != 0:
        return False
    # When Storage=volatile, journals are under /run/log/journal (tmpfs).
    return "/run/log/journal" in result.stdout and "/var/log/journal" not in result.stdout


def _core_pattern_disabled() -> bool:
    """True iff kernel.core_pattern routes to /bin/false (or similar nullification)."""
    try:
        content = Path(_CORE_PATTERN_PATH).read_text().strip()
    except FileNotFoundError:
        return True
    # Acceptable patterns: piping to /bin/false, /dev/null, or empty.
    return content in ("|/bin/false", "|/bin/true", "/dev/null", "")


def _path_on_tmpfs(path: str) -> bool:
    """True iff `path` is a mountpoint of type tmpfs."""
    try:
        with open("/proc/mounts") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 3 and parts[1] == path and parts[2] == "tmpfs":
                    return True
    except FileNotFoundError:
        return False
    return False


def verify_all() -> None:
    """Run all hardening checks. Raises HardeningError on first failure."""
    if not _swap_disabled():
        raise HardeningError("swap is enabled (expected swapoff -a)")
    if not _journald_volatile():
        raise HardeningError(
            "journald is not volatile (expected Storage=volatile)"
        )
    if not _core_pattern_disabled():
        raise HardeningError(
            "kernel.core_pattern is not disabled (expected |/bin/false)"
        )
    if not _path_on_tmpfs("/var/log"):
        raise HardeningError("/var/log is not on tmpfs")
    if not _path_on_tmpfs("/run/mthydra"):
        raise HardeningError("/run/mthydra is not on tmpfs")
