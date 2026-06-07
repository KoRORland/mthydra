from mthydra.controller import debug_flag


def test_write_then_read_roundtrip(tmp_path):
    p = tmp_path / "debug.flag"
    f = debug_flag.write_flag(p, ttl_hours=24, now=1000.0)
    assert f.enabled_at == 1000.0
    assert f.expires_at == 1000.0 + 24 * 3600
    back = debug_flag.read_flag(p)
    assert back is not None
    assert back.expires_at == f.expires_at
    assert back.ttl_hours == 24


def test_is_expired_boundary(tmp_path):
    f = debug_flag.write_flag(tmp_path / "f", ttl_hours=1, now=0.0)
    assert f.is_expired(3599.0) is False
    assert f.is_expired(3600.0) is True  # now >= expires_at -> expired


def test_read_missing_returns_none(tmp_path):
    assert debug_flag.read_flag(tmp_path / "nope") is None


def test_read_corrupt_returns_none(tmp_path):
    p = tmp_path / "bad.flag"
    p.write_text("{not json")
    assert debug_flag.read_flag(p) is None


def test_clear_is_idempotent(tmp_path):
    p = tmp_path / "f"
    debug_flag.write_flag(p, now=0.0)
    debug_flag.clear_flag(p)
    assert not p.exists()
    debug_flag.clear_flag(p)  # second call must not raise
