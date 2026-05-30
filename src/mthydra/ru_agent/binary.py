"""Download + verify the mtg binary from a signed B2 URL."""
from __future__ import annotations

import hashlib
import os
import tempfile
import urllib.error
import urllib.request
from pathlib import Path


class BinaryError(RuntimeError):
    """Download / verify failure."""


def download_and_verify(
    *,
    url: str,
    expected_sha256: str,
    out_path: Path | str,
    timeout: int = 30,
) -> None:
    """Download `url` to `out_path`; verify sha256; chmod +x.

    Raises BinaryError on any failure.
    """
    out = Path(out_path)
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if getattr(resp, "status", 200) >= 400:
                raise BinaryError(f"HTTP {resp.status} for {url}")
            content = resp.read()
    except urllib.error.HTTPError as e:
        raise BinaryError(f"HTTP {e.code} for {url}: {e.reason}") from e
    except urllib.error.URLError as e:
        raise BinaryError(f"URLError for {url}: {e.reason}") from e
    except OSError as e:
        raise BinaryError(f"network error for {url}: {e}") from e

    actual = hashlib.sha256(content).hexdigest()
    if actual != expected_sha256:
        raise BinaryError(
            f"sha256 mismatch for {url}: "
            f"expected {expected_sha256[:16]}..., got {actual[:16]}..."
        )

    out.parent.mkdir(parents=True, exist_ok=True)
    # Atomic write: tempfile in the same directory → fsync → chmod → rename →
    # fsync directory. Without this, a power cut mid-write leaves a partial
    # binary on disk and the supervisor would try to exec it next boot.
    fd, tmp_path = tempfile.mkstemp(
        prefix=out.name + ".", suffix=".tmp", dir=str(out.parent),
    )
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp_path, 0o755)
        os.replace(tmp_path, out)
        dir_fd = os.open(str(out.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        except OSError:
            pass    # some filesystems can't fsync a directory; best-effort
        finally:
            os.close(dir_fd)
    except Exception:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        raise
