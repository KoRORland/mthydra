from mthydra.controller.probe_runner.probers import (
    HandshakeProbeResult,
    parse_handshake_probe_output,
)


def test_parse_ok():
    r = parse_handshake_probe_output(
        "mthydra-rh result=ok ja3=771,4865-4866,0-23,29-23,0 ttfb_ms=42\n")
    assert r == HandshakeProbeResult(
        result="ok", ja3="771,4865-4866,0-23,29-23,0", ttfb_ms=42, detail=None)


def test_parse_reset():
    r = parse_handshake_probe_output("mthydra-rh result=reset detail=rst_by_peer")
    assert r.result == "reset" and r.ja3 is None and r.detail == "rst_by_peer"


def test_parse_garbage_is_error_result():
    r = parse_handshake_probe_output("totally unexpected text")
    assert r.result == "error" and r.ja3 is None


def test_parse_empty_is_error():
    assert parse_handshake_probe_output("").result == "error"
