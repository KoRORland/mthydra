"""Download + verify the mtg binary from a signed B2 URL."""
from __future__ import annotations

import hashlib
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
    out.write_bytes(content)
    out.chmod(0o755)
