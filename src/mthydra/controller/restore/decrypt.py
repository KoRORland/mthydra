"""age decryption for restore — runs on the operator's machine, not on the controller."""
from __future__ import annotations

import subprocess
from pathlib import Path


class DecryptError(RuntimeError):
    pass


def decrypt_blob(blob_path: Path | str, identity_path: Path | str, out: Path | str) -> None:
    """Decrypt an age-encrypted blob to *out* using the operator's identity key.

    Raises DecryptError on any failure (wrong key, missing file, age not on PATH).
    Never modifies the source blob.
    """
    blob_path = Path(blob_path)
    identity_path = Path(identity_path)
    out = Path(out)
    if not blob_path.exists():
        raise DecryptError(f"blob not found: {blob_path}")
    if not identity_path.exists():
        raise DecryptError(f"identity not found: {identity_path}")
    try:
        subprocess.run(
            ["age", "-d", "-i", str(identity_path), "-o", str(out), str(blob_path)],
            check=True,
            capture_output=True,
            timeout=300,    # match age_crypt.encrypt_file (L18, audit 2026-05-30)
        )
    except subprocess.CalledProcessError as e:
        raise DecryptError(
            f"age decrypt failed: {e.stderr.decode(errors='replace')}"
        ) from e
    except FileNotFoundError as e:
        raise DecryptError("age binary not on PATH") from e
    except subprocess.TimeoutExpired as e:
        raise DecryptError(f"age decrypt timed out after {e.timeout}s") from e
