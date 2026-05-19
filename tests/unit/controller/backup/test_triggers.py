"""Tests for backup triggers — all-synchronous per plan §16.3."""
import threading
import time

import pytest

from mthydra.controller.backup.triggers import BackupOrchestrator
from mthydra.controller.state.backup_log import BackupTrigger


@pytest.fixture(autouse=True)
def _ensure_scheduler_stopped():
    """Guarantee any BackupOrchestrator started by a test is disarmed afterwards.

    Prevents background APScheduler threads from leaking between tests and
    causing flaky failures (item 1.1 in post-implementation gap review).
    """
    orchestrators: list[BackupOrchestrator] = []
    _orig_arm = BackupOrchestrator.arm

    def _tracking_arm(self, *a, **kw):
        orchestrators.append(self)
        return _orig_arm(self, *a, **kw)

    BackupOrchestrator.arm = _tracking_arm
    yield
    BackupOrchestrator.arm = _orig_arm
    for orch in orchestrators:
        try:
            orch.disarm()
        except Exception:
            pass


class FakePipeline:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self._lock = threading.Lock()

    def do_backup(self, trigger: BackupTrigger) -> int:
        with self._lock:
            val = trigger.value if isinstance(trigger, BackupTrigger) else str(trigger)
            self.calls.append(val)
            return len(self.calls)


# ---------------------------------------------------------------------------
# Fake timer factory for deterministic debounce tests (no real sleep needed)
# ---------------------------------------------------------------------------

class FakeTimerFactory:
    """Collects timers so tests can fire them manually."""

    def __init__(self) -> None:
        self.timers: list[threading.Timer] = []

    def __call__(self, delay: float, fn) -> threading.Timer:
        t = threading.Timer(delay, fn)
        self.timers.append(t)
        return t

    def fire_all(self) -> None:
        for t in list(self.timers):
            if t.is_alive():
                t.cancel()
                t.function()
        self.timers.clear()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_burned_change_debounces_multiple_signals():
    """Multiple notify_burned_change calls within debounce window → one backup."""
    factory = FakeTimerFactory()
    pipeline = FakePipeline()
    orch = BackupOrchestrator(
        pipeline=pipeline,
        debounce_seconds=60,
        floor_interval_seconds=3600,
        timer_factory=factory,
    )
    orch.notify_burned_change()
    orch.notify_burned_change()
    orch.notify_burned_change()
    # Three signals → two timers were cancelled, one remains alive
    alive = [t for t in factory.timers if t.is_alive()]
    assert len(alive) == 1, f"expected 1 live timer, got {len(alive)}"
    factory.fire_all()
    assert pipeline.calls == ["burned_domains_change"]


def test_manual_trigger_runs_immediately():
    pipeline = FakePipeline()
    orch = BackupOrchestrator(pipeline=pipeline, debounce_seconds=60, floor_interval_seconds=3600)
    gen = orch.run_manual()
    assert gen == 1
    assert pipeline.calls == ["manual"]


def test_offline_mode_blocks_notify():
    pipeline = FakePipeline()
    orch = BackupOrchestrator(
        pipeline=pipeline, debounce_seconds=60, floor_interval_seconds=3600, mode="offline"
    )
    orch.notify_burned_change()  # should be no-op
    assert pipeline.calls == []


def test_offline_mode_blocks_manual():
    pipeline = FakePipeline()
    orch = BackupOrchestrator(
        pipeline=pipeline, debounce_seconds=60, floor_interval_seconds=3600, mode="offline"
    )
    with pytest.raises(RuntimeError, match="offline mode"):
        orch.run_manual()


def test_offline_mode_arm_is_noop():
    pipeline = FakePipeline()
    orch = BackupOrchestrator(
        pipeline=pipeline, debounce_seconds=60, floor_interval_seconds=3600, mode="offline"
    )
    orch.arm()
    assert orch._scheduler is None
    orch.disarm()  # should not raise


def test_arm_and_disarm_lifecycle():
    """arm() starts a scheduler; disarm() shuts it down without error."""
    pipeline = FakePipeline()
    orch = BackupOrchestrator(
        pipeline=pipeline, debounce_seconds=60, floor_interval_seconds=3600
    )
    orch.arm()
    assert orch._scheduler is not None
    orch.disarm()
    assert orch._scheduler is None


def test_floor_timer_fires_via_real_scheduler():
    """Integration-level: a very short floor interval actually triggers do_backup."""
    fired = threading.Event()
    original_calls = []

    class EventPipeline:
        def do_backup(self, trigger):
            original_calls.append(trigger.value)
            fired.set()
            return 1

    orch = BackupOrchestrator(
        pipeline=EventPipeline(),
        debounce_seconds=60,
        floor_interval_seconds=0.1,  # 100ms for test speed
    )
    orch.arm()
    fired.wait(timeout=2.0)
    orch.disarm()
    assert fired.is_set(), "floor timer never fired"
    assert "floor_timer" in original_calls
