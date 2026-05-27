"""B2-pull descriptor refresh loop with jitter + signature verification."""
from __future__ import annotations

import hashlib
import json
import random
import struct
import time
from typing import Callable

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric import ed25519


class RefreshError(RuntimeError):
    pass


class _NotModified:
    """Sentinel returned by a fetch_fn when the server answered 304 Not Modified.

    A 304 means our cached descriptor is still current — it is a *success*, not
    a failure. Returning bytes here (e.g. an empty body) would fail signature
    verification and, after MAX_FAILURE_TICKS, needlessly self-terminate the box.
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return "NOT_MODIFIED"


NOT_MODIFIED = _NotModified()


class RefreshLoop:
    TICK_INTERVAL_SECONDS = 15 * 60  # 15 min
    JITTER_SECONDS = 5 * 60  # ±5 min
    MAX_FAILURE_TICKS = 24  # 24 × 15min = 6h before self-termination

    def __init__(
        self,
        *,
        url: str,
        trust_anchors: list[bytes],  # raw 32-byte Ed25519 pubkeys
        initial_descriptor: bytes,
        rewrite_fn: Callable[[bytes], None],
        terminate_fn: Callable[[str], None],
        fetch_fn: Callable[[str, str | None], tuple[bytes, str]] | None = None,
        clock: Callable[[], float] | None = None,
    ):
        self._url = url
        self._anchors = list(trust_anchors)
        self._current_blob = initial_descriptor
        self._current_hash = self._hash(initial_descriptor)
        self._last_modified: str | None = None
        self._rewrite_fn = rewrite_fn
        self._fetch_fn = fetch_fn or _fetch_b2
        self._terminate_fn = terminate_fn
        self._clock = clock or time.monotonic
        self.failure_count = 0

    @staticmethod
    def _hash(blob: bytes) -> str:
        return hashlib.sha256(blob).hexdigest()

    def _verify(self, blob: bytes) -> dict:
        """Returns parsed payload dict on success; raises RefreshError on failure."""
        if len(blob) < 2 + 64:
            raise RefreshError("descriptor blob too short")
        n = struct.unpack(">H", blob[:2])[0]
        if len(blob) != 2 + n + 64:
            raise RefreshError("descriptor length mismatch")
        payload = blob[2:2 + n]
        sig = blob[2 + n:]
        verified = False
        for anchor in self._anchors:
            try:
                ed25519.Ed25519PublicKey.from_public_bytes(anchor).verify(sig, payload)
                verified = True
                break
            except InvalidSignature:
                continue
        if not verified:
            raise RefreshError("signature did not validate against any trust anchor")
        try:
            return json.loads(payload)
        except json.JSONDecodeError as e:
            raise RefreshError(f"payload not JSON: {e}") from e

    def tick(self) -> None:
        """One refresh tick. Idempotent. Never raises — failures increment counter."""
        try:
            blob, last_modified = self._fetch_fn(self._url, self._last_modified)
        except Exception:
            self.failure_count += 1
            if self.failure_count >= self.MAX_FAILURE_TICKS:
                self._terminate_fn("descriptor refresh failed for too long")
            return
        # 304 Not Modified: cached descriptor is still current. Success, not failure.
        if blob is NOT_MODIFIED:
            self.failure_count = 0
            if last_modified is not None:
                self._last_modified = last_modified
            return
        # 304-equivalent: fetch returned the same blob.
        new_hash = self._hash(blob)
        if new_hash == self._current_hash:
            self.failure_count = 0
            self._last_modified = last_modified
            return
        try:
            self._verify(blob)
        except RefreshError:
            self.failure_count += 1
            if self.failure_count >= self.MAX_FAILURE_TICKS:
                self._terminate_fn("descriptor refresh: signature failures")
            return
        self._current_blob = blob
        self._current_hash = new_hash
        self._last_modified = last_modified
        self.failure_count = 0
        self._rewrite_fn(blob)

    def next_sleep_seconds(self) -> float:
        return self.TICK_INTERVAL_SECONDS + random.uniform(
            -self.JITTER_SECONDS, self.JITTER_SECONDS,
        )

    def run_forever(self, sleep_fn: Callable[[float], None] | None = None) -> None:
        sleep_fn = sleep_fn or time.sleep
        while True:
            self.tick()
            sleep_fn(self.next_sleep_seconds())


def _fetch_b2(url: str, if_modified_since: str | None) -> tuple[bytes | _NotModified, str | None]:
    import urllib.error
    import urllib.request
    req = urllib.request.Request(url)
    if if_modified_since:
        req.add_header("If-Modified-Since", if_modified_since)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            last_modified = resp.headers.get("Last-Modified", "")
            return resp.read(), last_modified
    except urllib.error.HTTPError as e:
        if e.code == 304:
            # Unchanged: keep the prior If-Modified-Since token, signal success.
            return NOT_MODIFIED, if_modified_since
        raise
