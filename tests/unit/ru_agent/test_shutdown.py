"""Tests for mthydra.ru_agent.shutdown — audit + invoke `shutdown -h now`."""
from __future__ import annotations

import pytest


def test_terminate_box_logs_and_invokes_shutdown(monkeypatch):
    from mthydra.ru_agent import shutdown
    calls = []
    monkeypatch.setattr(
        shutdown.subprocess, "run",
        lambda cmd, **kw: calls.append(cmd) or type("R", (), {"returncode": 0})(),
    )
    # terminate_box calls sys.exit(1) after invoking shutdown; catch that.
    with pytest.raises(SystemExit):
        shutdown.terminate_box("test reason", dry_run=False)
    assert any(c[:2] == ["shutdown", "-h"] for c in calls)


def test_terminate_box_dry_run_does_not_invoke_shutdown(monkeypatch):
    from mthydra.ru_agent import shutdown
    calls = []
    monkeypatch.setattr(
        shutdown.subprocess, "run",
        lambda cmd, **kw: calls.append(cmd) or type("R", (), {"returncode": 0})(),
    )
    shutdown.terminate_box("test reason", dry_run=True)
    assert not any(c[:2] == ["shutdown", "-h"] for c in calls)
