import json
import struct

import pytest


def _make_keyed_signer():
    """Return (priv, pub_raw)."""
    from cryptography.hazmat.primitives.asymmetric import ed25519
    priv = ed25519.Ed25519PrivateKey.generate()
    pub_raw = priv.public_key().public_bytes_raw()
    return priv, pub_raw


def _sign_payload(priv, payload_dict):
    payload = json.dumps(payload_dict, sort_keys=True, separators=(",", ":")).encode()
    sig = priv.sign(payload)
    return struct.pack(">H", len(payload)) + payload + sig


def _signed_descriptor(generation=5):
    """Return (blob, trust_anchor_bytes)."""
    priv, pub_raw = _make_keyed_signer()
    blob = _sign_payload(priv, {
        "schema": "mthydra.descriptor.v2",
        "generation": generation,
        "signed_at": "2026-05-23T00:00:00Z",
        "valid_until": "2026-05-24T00:00:00Z",
        "exits": [],
    })
    return blob, pub_raw


def test_refresh_no_change_does_nothing(tmp_path, monkeypatch):
    """Initial descriptor + B2 returns same blob -> no sing-box config rewrite."""
    from mthydra.ru_agent import descriptor_refresh
    blob, anchor = _signed_descriptor()
    rewrites = []

    def fake_fetch(url, if_modified_since):
        return blob, "2026-05-23T00:00:00Z"
    loop = descriptor_refresh.RefreshLoop(
        url="https://b2/descriptors/current",
        trust_anchors=[anchor],
        initial_descriptor=blob,
        rewrite_fn=lambda b: rewrites.append(b),
        fetch_fn=fake_fetch,
        terminate_fn=lambda r: pytest.fail("should not terminate"),
        clock=lambda: 1.0,
    )
    loop.tick()
    assert rewrites == []


def test_refresh_change_triggers_rewrite(tmp_path):
    """When new blob differs but signs with same anchor, rewrite_fn is called."""
    from mthydra.ru_agent import descriptor_refresh
    # Use a single signer so anchor validates both blobs; differ by generation.
    priv, anchor = _make_keyed_signer()
    blob1 = _sign_payload(priv, {
        "schema": "mthydra.descriptor.v2",
        "generation": 5,
        "signed_at": "2026-05-23T00:00:00Z",
        "valid_until": "2026-05-24T00:00:00Z",
        "exits": [],
    })
    blob2 = _sign_payload(priv, {
        "schema": "mthydra.descriptor.v2",
        "generation": 6,
        "signed_at": "2026-05-23T01:00:00Z",
        "valid_until": "2026-05-24T01:00:00Z",
        "exits": [],
    })
    assert blob1 != blob2
    rewrites = []
    loop = descriptor_refresh.RefreshLoop(
        url="https://b2/descriptors/current",
        trust_anchors=[anchor],
        initial_descriptor=blob1,
        rewrite_fn=lambda b: rewrites.append(b),
        fetch_fn=lambda url, ims: (blob2, "2026-05-23T01:00:00Z"),
        terminate_fn=lambda r: pytest.fail(),
        clock=lambda: 1.0,
    )
    loop.tick()
    assert rewrites == [blob2]
    assert loop.failure_count == 0


def test_refresh_drops_bad_signature(tmp_path):
    from mthydra.ru_agent import descriptor_refresh
    blob_good, anchor = _signed_descriptor()
    # Tamper the signature (last byte).
    blob_bad = blob_good[:-1] + bytes([blob_good[-1] ^ 0x01])
    rewrites = []
    loop = descriptor_refresh.RefreshLoop(
        url="https://b2/descriptors/current",
        trust_anchors=[anchor],
        initial_descriptor=blob_good,
        rewrite_fn=lambda b: rewrites.append(b),
        fetch_fn=lambda url, ims: (blob_bad, "2026-05-23T01:00:00Z"),
        terminate_fn=lambda r: pytest.fail(),
        clock=lambda: 1.0,
    )
    loop.tick()
    assert rewrites == []
    assert loop.failure_count >= 1


def test_refresh_terminates_after_6h_of_failures(tmp_path):
    from mthydra.ru_agent import descriptor_refresh
    blob, anchor = _signed_descriptor()
    terminated = []
    loop = descriptor_refresh.RefreshLoop(
        url="https://b2/descriptors/current",
        trust_anchors=[anchor],
        initial_descriptor=blob,
        rewrite_fn=lambda b: None,
        fetch_fn=lambda url, ims: (_ for _ in ()).throw(IOError("boom")),
        terminate_fn=lambda r: terminated.append(r),
        clock=lambda: 1.0,
    )
    # 6h / (15min tick) = 24 failures; trigger threshold.
    for _ in range(loop.MAX_FAILURE_TICKS):
        loop.tick()
    assert terminated  # terminate_fn was called


def test_verify_rejects_blob_too_short():
    from mthydra.ru_agent import descriptor_refresh
    loop = descriptor_refresh.RefreshLoop(
        url="x", trust_anchors=[b"\0" * 32],
        initial_descriptor=b"\0" * 66,
        rewrite_fn=lambda b: None,
        fetch_fn=lambda u, ims: (b"", ""),
        terminate_fn=lambda r: None,
    )
    with pytest.raises(descriptor_refresh.RefreshError, match="too short"):
        loop._verify(b"AB")  # < 2+64


def test_verify_rejects_length_mismatch():
    from mthydra.ru_agent import descriptor_refresh
    # blob declares n=10 payload, but actual bytes don't match expected length.
    blob = struct.pack(">H", 10) + b"x" * 5 + b"\0" * 64  # short payload
    loop = descriptor_refresh.RefreshLoop(
        url="x", trust_anchors=[b"\0" * 32],
        initial_descriptor=b"\0" * 100,
        rewrite_fn=lambda b: None,
        fetch_fn=lambda u, ims: (b"", ""),
        terminate_fn=lambda r: None,
    )
    with pytest.raises(descriptor_refresh.RefreshError, match="length mismatch"):
        loop._verify(blob)


def test_verify_rejects_payload_not_json():
    """Signed-but-non-JSON payload raises RefreshError('payload not JSON')."""
    from mthydra.ru_agent import descriptor_refresh
    priv, pub = _make_keyed_signer()
    payload = b"this is not json"
    sig = priv.sign(payload)
    blob = struct.pack(">H", len(payload)) + payload + sig
    loop = descriptor_refresh.RefreshLoop(
        url="x", trust_anchors=[pub],
        initial_descriptor=blob,
        rewrite_fn=lambda b: None,
        fetch_fn=lambda u, ims: (b"", ""),
        terminate_fn=lambda r: None,
    )
    with pytest.raises(descriptor_refresh.RefreshError, match="not JSON"):
        loop._verify(blob)


def test_next_sleep_seconds_within_jitter_range():
    from mthydra.ru_agent import descriptor_refresh
    blob, anchor = _signed_descriptor()
    loop = descriptor_refresh.RefreshLoop(
        url="x", trust_anchors=[anchor],
        initial_descriptor=blob,
        rewrite_fn=lambda b: None,
        fetch_fn=lambda u, ims: (blob, ""),
        terminate_fn=lambda r: None,
    )
    lo = loop.TICK_INTERVAL_SECONDS - loop.JITTER_SECONDS
    hi = loop.TICK_INTERVAL_SECONDS + loop.JITTER_SECONDS
    for _ in range(50):
        v = loop.next_sleep_seconds()
        assert lo <= v <= hi


def test_run_forever_calls_tick_then_sleeps(monkeypatch):
    """run_forever loops indefinitely; we break out by raising in sleep_fn."""
    from mthydra.ru_agent import descriptor_refresh
    blob, anchor = _signed_descriptor()
    tick_calls = []

    loop = descriptor_refresh.RefreshLoop(
        url="x", trust_anchors=[anchor],
        initial_descriptor=blob,
        rewrite_fn=lambda b: None,
        fetch_fn=lambda u, ims: (blob, ""),
        terminate_fn=lambda r: None,
    )
    monkeypatch.setattr(loop, "tick", lambda: tick_calls.append(True))

    class _Stop(Exception):
        pass

    sleeps = []

    def fake_sleep(d):
        sleeps.append(d)
        if len(sleeps) >= 3:
            raise _Stop
    with pytest.raises(_Stop):
        loop.run_forever(sleep_fn=fake_sleep)
    assert len(tick_calls) == 3
    assert len(sleeps) == 3


def test_fetch_b2_smoke(monkeypatch):
    """_fetch_b2 builds a Request, optionally adds If-Modified-Since, and reads."""
    from mthydra.ru_agent import descriptor_refresh
    import urllib.request

    class _R:
        headers = {"Last-Modified": "Sun, 23 May 2026 00:00:00 GMT"}
        def read(self): return b"payload"
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: _R())
    body, lm = descriptor_refresh._fetch_b2("https://x", "Sat, 22 May 2026 00:00:00 GMT")
    assert body == b"payload"
    assert lm.startswith("Sun")
    # And without if-modified-since:
    body2, _ = descriptor_refresh._fetch_b2("https://x", None)
    assert body2 == b"payload"
