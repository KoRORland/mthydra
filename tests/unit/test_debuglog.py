import logging

from mthydra import debuglog


def _reset():
    debuglog.disable()


def test_disabled_by_default_log_is_noop(caplog):
    _reset()
    with caplog.at_level(logging.DEBUG, logger=debuglog.LOGGER_NAME):
        debuglog.log("conn", "should not appear", ip="1.2.3.4")
    assert debuglog.is_enabled() is False
    assert "should not appear" not in caplog.text


def test_enable_emits_banner_and_logs_to_file(tmp_path):
    _reset()
    sink = tmp_path / "logs" / "debug.log"
    debuglog.enable(sink=sink, max_bytes=10 * 1024 * 1024, backups=5)
    try:
        assert debuglog.is_enabled() is True
        debuglog.log("conn", "incoming", ip="5.6.7.8", exit="eu-3")
        content = sink.read_text()
        assert "DEBUG MODE ON" in content
        assert "category=conn incoming" in content
        assert "ip=5.6.7.8" in content and "exit=eu-3" in content
    finally:
        _reset()


def test_disable_stops_logging_and_emits_off_banner(tmp_path):
    _reset()
    sink = tmp_path / "debug.log"
    debuglog.enable(sink=sink, max_bytes=1024, backups=1)
    debuglog.disable()
    after = sink.read_text()
    assert "DEBUG MODE OFF" in after
    debuglog.log("db", "must not appear")
    assert "must not appear" not in sink.read_text()
    assert debuglog.is_enabled() is False
