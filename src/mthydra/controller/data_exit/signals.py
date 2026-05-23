"""SIGHUP / restart helpers for the sing-box systemd unit."""
from __future__ import annotations

import subprocess


def sighup_sing_box_unit(unit_name: str) -> None:
    """Send SIGHUP to the sing-box systemd unit. Raises RuntimeError on failure."""
    result = subprocess.run(
        ["systemctl", "kill", "-s", "HUP", unit_name],
        capture_output=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"SIGHUP failed for {unit_name!r}: rc={result.returncode} "
            f"stderr={result.stderr!r}"
        )


def restart_sing_box_unit(unit_name: str) -> None:
    """systemctl restart the sing-box unit. Raises on failure."""
    result = subprocess.run(
        ["systemctl", "restart", unit_name], capture_output=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"restart failed for {unit_name!r}: rc={result.returncode} "
            f"stderr={result.stderr!r}"
        )
