from mthydra import debuglog
from mthydra.ru_agent import supervisor


class _FakeProc:
    def poll(self):
        return None


def test_supervisor_launch_silent_when_disabled(tmp_path, monkeypatch):
    debuglog.disable()
    monkeypatch.setattr(supervisor.subprocess, "Popen",
                        lambda *a, **k: _FakeProc())
    sink = tmp_path / "debug.log"
    sup = supervisor.Supervisor(mtg_cmd=["true"], sing_box_cmd=["true"])
    sup.launch_all()
    assert not sink.exists()
    assert debuglog.is_enabled() is False


def test_supervisor_launch_emits_debug_when_enabled(tmp_path, monkeypatch):
    monkeypatch.setattr(supervisor.subprocess, "Popen",
                        lambda *a, **k: _FakeProc())
    sink = tmp_path / "debug.log"
    debuglog.enable(sink=sink)
    try:
        sup = supervisor.Supervisor(mtg_cmd=["true"], sing_box_cmd=["true"])
        sup.launch_all()
        content = sink.read_text()
        assert "category=child launched" in content
        assert "name=mtg" in content and "name=sing-box" in content
    finally:
        debuglog.disable()
