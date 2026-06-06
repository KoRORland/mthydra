from __future__ import annotations

import json

from mthydra.ru_agent import tunnel_check as tc


class FakeSock:
    """Models a redirected socket. held=True -> recv raises timeout (peer is
    holding the connection -> upstream alive). held=False -> recv returns b''
    (EOF: sing-box closed it -> upstream dead)."""
    def __init__(self, *, held: bool):
        self._held = held
        self.closed = False

    def sendall(self, data):
        pass

    def recv(self, n):
        if self._held:
            raise TimeoutError("peer holding connection open")
        return b""

    def close(self):
        self.closed = True


def test_held_connection_is_ok():
    v = tc.check_eu_tunnel(
        dc_ips=["149.154.167.51"],
        connect_fn=lambda ip, port, timeout: FakeSock(held=True),
        clock=lambda: "2026-06-06T10:00:00Z",
    )
    assert v.verdict == "ok"
    assert v.telegram_dc_tried == "149.154.167.51"


def test_eof_connection_is_fail():
    v = tc.check_eu_tunnel(
        dc_ips=["149.154.167.51"],
        connect_fn=lambda ip, port, timeout: FakeSock(held=False),
        clock=lambda: "2026-06-06T10:00:00Z",
    )
    assert v.verdict == "fail"
    assert "eof" in v.detail.lower()


def test_connect_error_is_fail_not_crash():
    def boom(ip, port, timeout):
        raise OSError("no route to host")

    v = tc.check_eu_tunnel(
        dc_ips=["149.154.167.51"], connect_fn=boom,
        clock=lambda: "2026-06-06T10:00:00Z",
    )
    assert v.verdict == "fail"
    assert "no route" in v.detail.lower()


def test_second_dc_tried_when_first_fails():
    seen = []

    def connect_fn(ip, port, timeout):
        seen.append(ip)
        if ip == "1.1.1.1":
            raise OSError("down")
        return FakeSock(held=True)

    v = tc.check_eu_tunnel(
        dc_ips=["1.1.1.1", "149.154.167.51"], connect_fn=connect_fn,
        clock=lambda: "2026-06-06T10:00:00Z",
    )
    assert v.verdict == "ok"
    assert v.telegram_dc_tried == "149.154.167.51"
    assert seen == ["1.1.1.1", "149.154.167.51"]


def test_no_dc_ips_is_fail():
    v = tc.check_eu_tunnel(
        dc_ips=[], connect_fn=lambda *a, **k: FakeSock(held=True),
        clock=lambda: "2026-06-06T10:00:00Z",
    )
    assert v.verdict == "fail"


def test_write_health_writes_json(tmp_path):
    v = tc.Verdict(checked_at="2026-06-06T10:00:00Z", verdict="ok",
                   detail="held", telegram_dc_tried="149.154.167.51")
    p = tmp_path / "health.json"
    tc.write_health(str(p), v)
    doc = json.loads(p.read_text())
    assert doc["verdict"] == "ok"
    assert doc["telegram_dc_tried"] == "149.154.167.51"
    assert doc["checked_at"] == "2026-06-06T10:00:00Z"
