"""Verify RU-box hardening: swap off, journald volatile, core dumps disabled,
/var/log + /run/mthydra on tmpfs. Refuses to continue on any failure."""
from __future__ import annotations

import contextlib
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
            lines = [ln for ln in f.read().splitlines() if ln.strip()]
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
    """True iff `path` is backed by a tmpfs filesystem — i.e. the mount that
    contains it is tmpfs. Both a dedicated tmpfs mount (e.g. tmpfs on /var/log)
    and a directory under a tmpfs mount (e.g. /run/mthydra under the /run tmpfs)
    qualify; the property being enforced is "in RAM, not on persistent disk".
    Resolves the longest matching mountpoint prefix and checks its fstype."""
    best_mp = ""
    best_fs: str | None = None
    try:
        with open("/proc/mounts") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 3:
                    continue
                mp, fs = parts[1], parts[2]
                contained = path == mp or mp == "/" or path.startswith(
                    mp.rstrip("/") + "/")
                if contained and len(mp) >= len(best_mp):
                    best_mp, best_fs = mp, fs
    except FileNotFoundError:
        return False
    return best_fs == "tmpfs"


def apply_best_effort() -> None:
    """Apply the hardening the agent can enforce itself at runtime (it runs as
    root, after boot). Currently: re-assert kernel.core_pattern=|/bin/false.

    apport (and similar crash handlers) set core_pattern imperatively when their
    service starts at boot — *after* cloud-init's bootcmd — so the value cloud-init
    set is overwritten by the time the agent runs. Re-asserting it here is
    reliable because nothing re-applies it again post-boot. Best-effort: any
    failure is surfaced by the subsequent verify_all()."""
    with contextlib.suppress(OSError):
        Path(_CORE_PATTERN_PATH).write_text("|/bin/false")


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
