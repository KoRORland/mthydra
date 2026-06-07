"""ru_agent.__main__ — desync/nfqws helpers (V2 Tasks 4-5)."""
from __future__ import annotations

from mthydra.ru_agent import __main__ as agent_main


def test_exit_ips_from_descriptor():
    payload = {"eu_exit_set": [{"endpoint": "9.9.9.9:443", "fingerprint": "f1"},
                               {"endpoint": "8.8.8.8:443", "fingerprint": "f2"}]}
    assert agent_main._exit_endpoints(payload) == ["9.9.9.9:443", "8.8.8.8:443"]


def test_desync_disabled_when_no_strategy():
    payload = {"eu_exit_set": [{"endpoint": "9.9.9.9:443", "fingerprint": "f"}]}
    assert agent_main._desync_strategy(payload) is None


def test_desync_strategy_present():
    assert (agent_main._desync_strategy({"desync_strategy": "--dpi-desync=fake"})
            == "--dpi-desync=fake")
    assert agent_main._desync_strategy({"desync_strategy": ""}) is None
