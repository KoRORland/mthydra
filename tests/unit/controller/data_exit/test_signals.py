def test_sighup_via_systemctl(monkeypatch):
    from mthydra.controller.data_exit import signals
    calls = []
    monkeypatch.setattr(signals.subprocess, "run",
                        lambda *a, **kw: calls.append((a, kw)) or type("R", (), {"returncode": 0})())
    signals.sighup_sing_box_unit("sing-box.service")
    assert calls[0][0][0] == ["systemctl", "kill", "-s", "HUP", "sing-box.service"]


def test_sighup_failure_raises(monkeypatch):
    import pytest
    from mthydra.controller.data_exit import signals
    monkeypatch.setattr(signals.subprocess, "run",
                        lambda *a, **kw: type("R", (), {"returncode": 1, "stderr": b"oops"})())
    with pytest.raises(RuntimeError, match="SIGHUP failed"):
        signals.sighup_sing_box_unit("sing-box.service")
