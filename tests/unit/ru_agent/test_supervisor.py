"""Tests for mthydra.ru_agent.supervisor — child process lifecycle + crash budget."""
from __future__ import annotations


class _FakeChild:
    def __init__(self, returncode=None):
        self._rc = returncode
    def poll(self): return self._rc
    def terminate(self): self._rc = -15
    def wait(self, timeout=None): return self._rc


def test_supervisor_launches_two_children(monkeypatch):
    from mthydra.ru_agent import supervisor
    launched = []
    def fake_popen(cmd, **kw):
        launched.append(cmd)
        return _FakeChild(returncode=None)
    monkeypatch.setattr(supervisor.subprocess, "Popen", fake_popen)
    s = supervisor.Supervisor(
        mtg_cmd=["mtg", "run", "/run/mtg.toml"],
        sing_box_cmd=["sing-box", "run", "-c", "/run/sb.json"],
        clock=lambda: 0.0,
    )
    s.launch_all()
    assert launched == [
        ["mtg", "run", "/run/mtg.toml"],
        ["sing-box", "run", "-c", "/run/sb.json"],
    ]


def test_supervisor_restarts_crashed_child_within_budget(monkeypatch):
    from mthydra.ru_agent import supervisor
    def fake_popen(cmd, **kw):
        if "mtg" in cmd[0]:
            return _FakeChild(returncode=1)  # crashed
        return _FakeChild(returncode=None)
    monkeypatch.setattr(supervisor.subprocess, "Popen", fake_popen)

    clock = [0.0]
    terminated = []
    s = supervisor.Supervisor(
        mtg_cmd=["mtg", "run"],
        sing_box_cmd=["sing-box", "run"],
        clock=lambda: clock[0],
        sleep_fn=lambda s: None,
        on_persistent_failure=lambda r: terminated.append(r),
    )
    s.launch_all()
    # Simulate 3 crashes within 5min -> still restarting (threshold is >=4).
    for _ in range(3):
        clock[0] += 1.0
        s.check_children_once()
    assert terminated == []


def test_supervisor_terminates_box_after_crash_loop(monkeypatch):
    from mthydra.ru_agent import supervisor
    def fake_popen(cmd, **kw): return _FakeChild(returncode=1)
    monkeypatch.setattr(supervisor.subprocess, "Popen", fake_popen)

    clock = [0.0]
    terminated = []
    s = supervisor.Supervisor(
        mtg_cmd=["mtg", "run"],
        sing_box_cmd=["sing-box", "run"],
        clock=lambda: clock[0],
        sleep_fn=lambda s: None,
        on_persistent_failure=lambda r: terminated.append(r),
    )
    s.launch_all()
    for _ in range(5):
        clock[0] += 1.0
        s.check_children_once()
    assert terminated, "expected on_persistent_failure to fire"
