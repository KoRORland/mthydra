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


def test_check_children_once_skips_when_proc_is_none(monkeypatch):
    """If a child Popen is None (e.g., launch_all never called), it's skipped."""
    from mthydra.ru_agent import supervisor
    s = supervisor.Supervisor(
        mtg_cmd=["mtg"],
        sing_box_cmd=["sing-box"],
        clock=lambda: 0.0,
        sleep_fn=lambda d: None,
    )
    # _mtg_proc and _sing_box_proc are both None; nothing should happen.
    s.check_children_once()  # no exception


def test_shutdown_children_terminates_running(monkeypatch):
    """Running children get terminate() called and wait() returns within timeout."""
    from mthydra.ru_agent import supervisor
    terminated = []

    class _Running:
        def __init__(self):
            self._rc = None
        def poll(self):
            return self._rc
        def terminate(self):
            terminated.append(self)
            self._rc = -15
        def wait(self, timeout=None):
            return self._rc
        def kill(self):
            terminated.append(("kill", self))

    s = supervisor.Supervisor(
        mtg_cmd=["mtg"], sing_box_cmd=["sing-box"],
        clock=lambda: 0.0, sleep_fn=lambda d: None,
    )
    s._mtg_proc = _Running()
    s._sing_box_proc = _Running()
    s.shutdown_children()
    assert len(terminated) == 2
    assert all(not isinstance(t, tuple) for t in terminated)  # no kills


def test_shutdown_children_force_kills_on_timeout(monkeypatch):
    """If wait() raises TimeoutExpired, kill() is invoked."""
    import subprocess as sp
    from mthydra.ru_agent import supervisor

    killed = []

    class _Stubborn:
        def __init__(self):
            self._rc = None
        def poll(self):
            return self._rc
        def terminate(self):
            pass
        def wait(self, timeout=None):
            raise sp.TimeoutExpired(cmd="x", timeout=timeout)
        def kill(self):
            killed.append(True)
            self._rc = -9

    s = supervisor.Supervisor(
        mtg_cmd=["mtg"], sing_box_cmd=["sing-box"],
        clock=lambda: 0.0, sleep_fn=lambda d: None,
    )
    s._mtg_proc = _Stubborn()
    s._sing_box_proc = _Stubborn()
    s.shutdown_children()
    assert killed == [True, True]


def test_shutdown_children_skips_already_exited(monkeypatch):
    """Children that have already exited (poll() != None) are not terminated."""
    from mthydra.ru_agent import supervisor
    actions = []

    class _Exited:
        def poll(self):
            return 0
        def terminate(self):
            actions.append("terminate")
        def kill(self):
            actions.append("kill")
        def wait(self, timeout=None):
            return 0

    s = supervisor.Supervisor(
        mtg_cmd=["mtg"], sing_box_cmd=["sing-box"],
        clock=lambda: 0.0, sleep_fn=lambda d: None,
    )
    s._mtg_proc = _Exited()
    s._sing_box_proc = _Exited()
    s.shutdown_children()
    assert actions == []


def test_run_forever_breaks_on_keyboard_interrupt(monkeypatch):
    """KeyboardInterrupt in sleep -> shutdown_children invoked, no re-raise."""
    from mthydra.ru_agent import supervisor

    shutdowns = []
    s = supervisor.Supervisor(
        mtg_cmd=["mtg"], sing_box_cmd=["sing-box"],
        clock=lambda: 0.0,
        sleep_fn=lambda d: (_ for _ in ()).throw(KeyboardInterrupt()),
    )
    monkeypatch.setattr(s, "shutdown_children",
                         lambda: shutdowns.append(True))
    s.run_forever()  # must return cleanly
    assert shutdowns == [True]
