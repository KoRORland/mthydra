"""Self-terminate the RU box: audit + `shutdown -h now`."""
from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timezone


def terminate_box(reason: str, *, dry_run: bool = False) -> None:
    """Audit + invoke shutdown.

    Prints a final audit line to stderr (which journald captures), then
    calls `shutdown -h now`. In dry_run mode, the shutdown command is
    not executed (used in tests).
    """
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(
        f"mthydra-agent: TERMINATING at {ts} - reason: {reason}",
        file=sys.stderr,
        flush=True,
    )
    if dry_run:
        return
    subprocess.run(["shutdown", "-h", "now", f"mthydra: {reason}"], check=False)
    # If shutdown returns (e.g., in a container), force exit.
    sys.exit(1)
