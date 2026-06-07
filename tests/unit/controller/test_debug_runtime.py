from mthydra import debuglog
from mthydra.controller import debug_flag, debug_runtime


def _reset():
    debuglog.disable()


def test_arm_no_flag_returns_false(tmp_path):
    _reset()
    assert debug_runtime.arm_from_flag(tmp_path / "absent", spawn_watcher=False) is False
    assert debuglog.is_enabled() is False


def test_arm_valid_flag_enables(tmp_path):
    _reset()
    p = tmp_path / "debug.flag"
    debug_flag.write_flag(p, ttl_hours=24, now=1000.0)
    armed = debug_runtime.arm_from_flag(
        p, log_path=tmp_path / "debug.log", now=1000.0, spawn_watcher=False)
    try:
        assert armed is True
        assert debuglog.is_enabled() is True
    finally:
        _reset()


def test_arm_expired_flag_removes_and_stays_off(tmp_path):
    _reset()
    p = tmp_path / "debug.flag"
    debug_flag.write_flag(p, ttl_hours=1, now=0.0)
    armed = debug_runtime.arm_from_flag(p, now=10_000.0, spawn_watcher=False)
    assert armed is False
    assert debuglog.is_enabled() is False
    assert not p.exists()


def test_expiry_tick_turns_off_when_expired(tmp_path):
    _reset()
    p = tmp_path / "debug.flag"
    debug_flag.write_flag(p, ttl_hours=1, now=0.0)
    debuglog.enable(sink=tmp_path / "debug.log")
    turned_off = debug_runtime.expiry_tick(p, now=3600.0)
    assert turned_off is True
    assert debuglog.is_enabled() is False
    assert not p.exists()


def test_expiry_tick_keeps_on_when_valid(tmp_path):
    _reset()
    p = tmp_path / "debug.flag"
    debug_flag.write_flag(p, ttl_hours=24, now=0.0)
    debuglog.enable(sink=tmp_path / "debug.log")
    try:
        assert debug_runtime.expiry_tick(p, now=10.0) is False
        assert debuglog.is_enabled() is True
        assert p.exists()
    finally:
        _reset()
