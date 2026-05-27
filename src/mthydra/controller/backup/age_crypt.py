"""Thin wrapper around the `age` CLI for encryption only.

Decryption is intentionally NOT exposed from the controller — the operator's
private key never touches the controller (spec D1).
"""
from __future__ import annotations

import subprocess
from pathlib import Path


class AgeError(RuntimeError):
    """Raised when age invocation fails or recipient format is bad."""


# Bech32 (BIP-173) — age v1 recipients are bech32 with HRP "age".
_BECH32_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def _bech32_polymod(values: list[int]) -> int:
    gen = [0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3]
    chk = 1
    for v in values:
        top = chk >> 25
        chk = (chk & 0x1FFFFFF) << 5 ^ v
        for i in range(5):
            chk ^= gen[i] if ((top >> i) & 1) else 0
    return chk


def _bech32_hrp_expand(hrp: str) -> list[int]:
    return [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]


def validate_recipient(recipient: str) -> None:
    """Accept only well-formed age v1 recipients.

    A bare prefix/length check let a typo'd or truncated recipient through;
    age would then encrypt to a key nobody holds, silently making the backup
    undecryptable. Verify the full bech32 checksum so corruption fails loudly
    at config time instead of at restore time.
    """
    if not isinstance(recipient, str) or not recipient.startswith("age1"):
        raise AgeError(f"invalid age recipient: {recipient!r}")
    # bech32 forbids mixed case and non-printable / out-of-range chars.
    if recipient.lower() != recipient and recipient.upper() != recipient:
        raise AgeError(f"invalid age recipient (mixed case): {recipient!r}")
    s = recipient.lower()
    pos = s.rfind("1")
    if pos < 1 or pos + 7 > len(s) or len(s) > 90:
        raise AgeError(f"invalid age recipient (malformed): {recipient!r}")
    hrp = s[:pos]
    if hrp != "age":
        raise AgeError(f"invalid age recipient (HRP {hrp!r} != 'age'): {recipient!r}")
    try:
        data = [_BECH32_CHARSET.index(c) for c in s[pos + 1:]]
    except ValueError as e:
        raise AgeError(f"invalid age recipient (bad bech32 char): {recipient!r}") from e
    if _bech32_polymod(_bech32_hrp_expand(hrp) + data) != 1:
        raise AgeError(f"invalid age recipient (bad bech32 checksum): {recipient!r}")


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
