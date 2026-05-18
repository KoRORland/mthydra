"""Thin wrapper around the `age` CLI for encryption only.

Decryption is intentionally NOT exposed from the controller — the operator's
private key never touches the controller (spec D1).
"""
from __future__ import annotations

import subprocess
from pathlib import Path


class AgeError(RuntimeError):
    """Raised when age invocation fails or recipient format is bad."""


def validate_recipient(recipient: str) -> None:
    """Accept only age v1 recipients."""
    if not isinstance(recipient, str) or not recipient.startswith("age1") or len(recipient) < 32:
        raise AgeError(f"invalid age recipient: {recipient!r}")


def encrypt_file(input_path: Path | str, recipient: str, out: Path | str) -> None:
    input_path = Path(input_path)
    out = Path(out)
    if not input_path.exists():
        raise AgeError(f"input file not found: {input_path}")
    validate_recipient(recipient)
    try:
        subprocess.run(
            ["age", "-r", recipient, "-o", str(out), str(input_path)],
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError as e:
        raise AgeError(f"age failed: {e.stderr.decode(errors='replace')}") from e
    except FileNotFoundError as e:
        raise AgeError("age binary not on PATH") from e
