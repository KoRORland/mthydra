from __future__ import annotations

import json
import ssl
import subprocess

from mthydra.ops import ru_bringup


def test_wait_for_reachable_returns_true_on_handshake(monkeypatch):
    # Mock socket.create_connection → fake socket; mock ssl context → handshake OK.
    class _FakeTLS:
        def do_handshake(self): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
    class _FakeCtx:
        check_hostname = True
        verify_mode = ssl.CERT_REQUIRED
        def wrap_socket(self, sock, server_hostname=None): return _FakeTLS()
    class _FakeSock:
        def __enter__(self): return self
        def __exit__(self, *a): pass
    monkeypatch.setattr(ru_bringup.socket, "create_connection",
                        lambda addr, timeout=None: _FakeSock())
    monkeypatch.setattr(ru_bringup.ssl, "create_default_context", lambda: _FakeCtx())
    assert ru_bringup.wait_for_reachable("1.2.3.4", 443, "sni.example",
                                         timeout_s=1, poll_s=0) is True


def test_wait_for_reachable_returns_false_on_timeout(monkeypatch):
    def boom(addr, timeout=None):
        raise OSError("refused")
    monkeypatch.setattr(ru_bringup.socket, "create_connection", boom)
    # Advance fake clock past the deadline immediately.
    times = iter([0.0, 0.5, 2.0])  # start, after first attempt, past deadline=1.0
    monkeypatch.setattr(ru_bringup.time, "monotonic", lambda: next(times))
    monkeypatch.setattr(ru_bringup.time, "sleep", lambda s: None)
    progress = []
    assert ru_bringup.wait_for_reachable("1.2.3.4", 443, "sni",
                                         timeout_s=1, poll_s=0,
                                         on_progress=progress.append) is False
    assert progress  # called at least once with the error


def _fake_run_factory(stdout_map=None, stderr_map=None, default_rc=0):
    """stdout_map / stderr_map: dict keyed by the first controller subcommand."""
    stdout_map = stdout_map or {}
    stderr_map = stderr_map or {}
    calls = []
    def fake_run(*args, check=True, capture=False, env=None):
        calls.append(list(args))
        sub = args[0] if args else ""
        return subprocess.CompletedProcess(
            args, default_rc,
            stdout_map.get(sub, ""), stderr_map.get(sub, ""),
        )
    return fake_run, calls


def test_mint_seed_writes_cloud_init_and_returns_box_id(monkeypatch, tmp_path):
    # provision-seed prints cloud-init to STDOUT and the box_id line to STDERR.
    # mint_seed must capture both via _run_controller_capture_both, write the
    # stdout to --cloud-init-out, parse box_id from stderr.
    fake_run, calls = _fake_run_factory(
        stdout_map={"provision-seed": "#cloud-config\n# fake bundle\n"},
        stderr_map={"provision-seed":
                    "provision-seed: created box_id=b-abc123\n"})
    monkeypatch.setattr(ru_bringup, "_run_controller_capture_both",
                        fake_run, raising=False)
    out = tmp_path / "x.yaml"
    box_id = ru_bringup.mint_seed(
        "selectel", "ru-msk-1",
        canary=True,
        agent_source_url="https://b2/agent.tar.gz",
        agent_source_sha256="deadbeef",
        descriptor_refresh_url="https://b2/desc",
        cloud_init_out=str(out),
    )
    assert box_id == "b-abc123"
    assert out.read_text().startswith("#cloud-config")
    assert (out.stat().st_mode & 0o777) == 0o600
    argv = calls[0]
    assert argv[0] == "provision-seed"
    assert "--canary" in argv
    assert "selectel" in argv and "ru-msk-1" in argv
    assert "--db-path" in argv     # threaded through from _DEFAULT_DB
    assert "--config" in argv      # provision-seed needs --config


def test_mark_live_invokes_controller(monkeypatch):
    fake_run, calls = _fake_run_factory()
    monkeypatch.setattr(ru_bringup, "_run_controller", fake_run, raising=False)
    ru_bringup.mark_live("b-abc", "1.2.3.4")
    assert calls[0][0] == "ru-box-mark-live"
    assert "b-abc" in calls[0] and "--public-ip" in calls[0] and "1.2.3.4" in calls[0]
    assert "--db-path" in calls[0]


def test_box_info_parses_ru_box_list_json(monkeypatch):
    rows = [{"box_id": "b-abc", "state": "provisioning", "sni": "cover.example"}]
    fake_run, _ = _fake_run_factory(stdout_map={"ru-box-list": json.dumps(rows)})
    monkeypatch.setattr(ru_bringup, "_run_controller", fake_run, raising=False)
    info = ru_bringup.box_info("b-abc")
    assert info["state"] == "provisioning"
    assert info["sni"] == "cover.example"


def test_box_info_returns_none_when_missing(monkeypatch):
    fake_run, _ = _fake_run_factory(stdout_map={"ru-box-list": "[]"})
    monkeypatch.setattr(ru_bringup, "_run_controller", fake_run, raising=False)
    assert ru_bringup.box_info("b-missing") is None
