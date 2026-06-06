from __future__ import annotations

import json

from mthydra.ru_agent import __main__ as agent_main


class _Held:
    def sendall(self, d):
        pass

    def recv(self, n):
        raise TimeoutError()

    def close(self):
        pass


def test_run_tunnel_check_writes_health(tmp_path, monkeypatch):
    health = tmp_path / "health.json"
    monkeypatch.setattr(agent_main, "HEALTH_PATH", str(health))
    logged = []
    agent_main._run_tunnel_check(
        dc_ips=["149.154.167.51"],
        connect_fn=lambda ip, port, timeout: _Held(),
        log=logged.append,
        clock=lambda: "2026-06-06T10:00:00Z",
    )
    assert json.loads(health.read_text())["verdict"] == "ok"
    assert any("ok" in m for m in logged)


def test_run_tunnel_check_logs_loudly_on_fail(tmp_path, monkeypatch):
    health = tmp_path / "health.json"
    monkeypatch.setattr(agent_main, "HEALTH_PATH", str(health))
    logged = []

    def boom(ip, port, timeout):
        raise OSError("dead")

    agent_main._run_tunnel_check(
        dc_ips=["149.154.167.51"], connect_fn=boom, log=logged.append,
        clock=lambda: "2026-06-06T10:00:00Z",
    )
    assert json.loads(health.read_text())["verdict"] == "fail"
    assert any("EU tunnel check FAILED" in m for m in logged)


def test_run_tunnel_check_never_raises(tmp_path, monkeypatch):
    # Even if writing health throws (bad dir), the loop must not crash.
    monkeypatch.setattr(agent_main, "HEALTH_PATH", "/nonexistent-dir/health.json")
    agent_main._run_tunnel_check(
        dc_ips=["149.154.167.51"],
        connect_fn=lambda ip, port, timeout: _Held(),
        log=lambda m: None, clock=lambda: "2026-06-06T10:00:00Z",
    )  # must return without raising
