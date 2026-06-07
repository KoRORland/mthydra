from mthydra import debuglog
from mthydra.controller.state.db import connect


def test_db_connect_silent_when_disabled(tmp_path):
    debuglog.disable()
    sink = tmp_path / "debug.log"
    # Not enabled -> no sink file is even created by connect().
    connect(tmp_path / "state.sqlite").close()
    assert not sink.exists()
    assert debuglog.is_enabled() is False


def test_db_connect_emits_debug_when_enabled(tmp_path):
    # The debug logger does not propagate (so it can't leak into the app's root
    # logger), so assert against the file sink rather than caplog.
    sink = tmp_path / "debug.log"
    debuglog.enable(sink=sink)
    try:
        connect(tmp_path / "state.sqlite").close()
        content = sink.read_text()
        assert "category=db connect" in content
    finally:
        debuglog.disable()
