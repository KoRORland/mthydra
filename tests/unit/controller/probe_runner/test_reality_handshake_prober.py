from __future__ import annotations

import subprocess

from mthydra.controller.probe_runner.probers import probe_reality_handshake


def _stub_ssh(returncode, stdout, stderr=""):
    def fn(*cmd_parts, timeout_s=30):
        return subprocess.CompletedProcess(
            ("ssh",) + tuple(cmd_parts), returncode, stdout, stderr)
    return fn


def test_ok_handshake():
    captured = {}

    def fake_ssh(*cmd_parts, timeout_s=30):
        captured["cmd"] = cmd_parts
        return subprocess.CompletedProcess(
            ("ssh",) + cmd_parts, 0,
            "mthydra-rh result=ok ja3=771,4865,0,29,0 ttfb_ms=30\n", "")

    r = probe_reality_handshake(
        fake_ssh, exit_endpoint="9.9.9.9:443",
        cover_sni="cover.example", reality_pubkey="pub==", fingerprint="chrome")

    assert r.result == "ok" and r.ja3 == "771,4865,0,29,0"
    full_cmd = " ".join(captured["cmd"])
    assert "9.9.9.9" in full_cmd
    assert "443" in full_cmd
    assert "cover.example" in full_cmd


def test_ssh_failure_becomes_error():
    r = probe_reality_handshake(
        _stub_ssh(255, "", "ssh: connect timeout"),
        exit_endpoint="9.9.9.9:443",
        cover_sni="c", reality_pubkey="p", fingerprint="chrome")
    assert r.result == "error"
    assert "ssh" in (r.detail or "").lower()


def test_transport_exception_becomes_error():
    def boom(*cmd_parts, timeout_s=30):
        raise RuntimeError("ssh down")

    r = probe_reality_handshake(
        boom, exit_endpoint="9.9.9.9:443",
        cover_sni="c", reality_pubkey="p", fingerprint="chrome")
    assert r.result == "error" and "ssh down" in (r.detail or "")


def test_reset_passes_through():
    def fake_ssh(*cmd_parts, timeout_s=30):
        return subprocess.CompletedProcess(
            ("ssh",) + cmd_parts, 0,
            "mthydra-rh result=reset detail=connection_reset_by_peer\n", "")

    r = probe_reality_handshake(
        fake_ssh, exit_endpoint="9.9.9.9:443",
        cover_sni="c", reality_pubkey="p", fingerprint="chrome")
    assert r.result == "reset" and r.detail == "connection_reset_by_peer"
