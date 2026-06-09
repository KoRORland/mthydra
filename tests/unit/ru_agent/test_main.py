"""ru_agent.__main__ orchestration — startup must not power off the box.

The agent is fail-closed for *runtime* tamper (periodic hardening regression →
shutdown). But a *startup* failure — most commonly the mtg download failing
because the VM clock is still at epoch or the network/S3 isn't ready — must NOT
`shutdown -h now`: that bricks the box (cloud-init is once-per-instance and the
seed lives on tmpfs, so the restart comes up bare and unrecoverable). Startup
failures retry briefly, then leave the box up so the operator can read the
journal. Discovered 2026-06-02 bringing up the first RU box: it imported fine
(descriptor fix worked) but powered itself off on a startup step.
"""
from __future__ import annotations

from mthydra.ru_agent import __main__ as agent_main
from mthydra.ru_agent import binary, hardening


class _FakeSeed:
    image = {"url": "https://s3/mtg?sig=x", "sha256": "a" * 64}
    initial_descriptor = b"\x00\x02{}"
    telegram_dcs = {"v4": [], "v6": []}
    descriptor_refresh_url = "https://s3/descriptor"
    descriptor_trust_anchors = ()


def _spy_no_shutdown(monkeypatch):
    calls = []
    monkeypatch.setattr(agent_main.shutdown_mod, "terminate_box",
                        lambda reason, **kw: calls.append(reason))
    monkeypatch.setattr(agent_main.time, "sleep", lambda s: None)
    return calls


def test_startup_binary_failure_does_not_power_off(monkeypatch):
    shutdown_calls = _spy_no_shutdown(monkeypatch)
    monkeypatch.setattr(hardening, "verify_all", lambda: None)
    monkeypatch.setattr(agent_main.seed_mod, "load", lambda p: _FakeSeed())
    monkeypatch.setattr(agent_main.seed_mod, "verify_credential", lambda s: None)
    # mtg download fails every time (e.g. clock at epoch → TLS/presign reject).
    monkeypatch.setattr(
        binary, "download_and_verify",
        lambda **kw: (_ for _ in ()).throw(binary.BinaryError("clock/TLS")))
    monkeypatch.setattr(agent_main, "STARTUP_MAX_ATTEMPTS", 3)

    rc = agent_main.main()

    assert rc == 2
    assert shutdown_calls == [], "startup failure must NOT power off the box"


def test_startup_hardening_failure_does_not_power_off(monkeypatch):
    shutdown_calls = _spy_no_shutdown(monkeypatch)
    monkeypatch.setattr(
        hardening, "verify_all",
        lambda: (_ for _ in ()).throw(hardening.HardeningError("tmpfs not ready")))
    monkeypatch.setattr(agent_main, "STARTUP_MAX_ATTEMPTS", 2)

    rc = agent_main.main()

    assert rc == 2
    assert shutdown_calls == []


def test_startup_retries_then_succeeds(monkeypatch):
    """A transient startup failure (clock not synced yet) is retried; once it
    clears, the agent proceeds to launch instead of dying."""
    shutdown_calls = _spy_no_shutdown(monkeypatch)
    monkeypatch.setattr(hardening, "verify_all", lambda: None)
    monkeypatch.setattr(agent_main.seed_mod, "load", lambda p: _FakeSeed())
    monkeypatch.setattr(agent_main.seed_mod, "verify_credential", lambda s: None)

    attempts = {"n": 0}
    def flaky(**kw):
        attempts["n"] += 1
        if attempts["n"] < 2:
            raise binary.BinaryError("not ready")
    monkeypatch.setattr(binary, "download_and_verify", flaky)
    monkeypatch.setattr(agent_main, "_atomic_write_bytes", lambda p, d: None)
    monkeypatch.setattr(agent_main.config_gen, "render_mtg_config", lambda *a, **k: b"")
    monkeypatch.setattr(agent_main.config_gen, "render_sing_box_config",
                        lambda *a, **k: b"")
    monkeypatch.setattr(agent_main.iptables, "install", lambda **kw: None)

    launched = {"ok": False}
    class _Sup:
        def __init__(self, **kw): pass
        def launch_all(self): launched["ok"] = True
        def run_forever(self): return None
    monkeypatch.setattr(agent_main.supervisor, "Supervisor", _Sup)
    # Don't spin real background threads.
    monkeypatch.setattr(agent_main.threading, "Thread",
                        lambda *a, **k: type("T", (), {"start": lambda self: None})())
    monkeypatch.setattr(agent_main, "STARTUP_MAX_ATTEMPTS", 5)

    rc = agent_main.main()

    assert attempts["n"] == 2          # failed once, retried, succeeded
    assert launched["ok"] is True
    assert shutdown_calls == []
    assert rc == 0
