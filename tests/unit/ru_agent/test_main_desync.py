"""ru_agent.__main__ — desync/nfqws helpers (V2 Tasks 4-5)."""
from __future__ import annotations

from dataclasses import dataclass

from mthydra.ru_agent import __main__ as agent_main
from mthydra.ru_agent import desync


@dataclass
class _FakeSeed:
    nfqws_url: str | None = None


def _record_desync(monkeypatch):
    """Monkeypatch desync.install/clear to record calls; return (installs, clears)."""
    installs: list[dict] = []
    clears: list[int] = []
    monkeypatch.setattr(
        desync, "install",
        lambda *, exit_ips, qnum: installs.append({"exit_ips": exit_ips, "qnum": qnum}))
    monkeypatch.setattr(desync, "clear", lambda qnum: clears.append(qnum))
    return installs, clears


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


def test_refresh_no_startup_strategy_never_installs_rules(monkeypatch):
    # Box started WITHOUT nfqws; a refresh introduces a strategy. NFQUEUE rules
    # must stay untouched (no nfqws reader -> rules would black-hole egress).
    installs, clears = _record_desync(monkeypatch)
    payload = {
        "desync_strategy": "--dpi-desync=fake",
        "eu_exit_set": [{"endpoint": "9.9.9.9:443", "fingerprint": "f"}],
    }
    eps = agent_main._apply_desync_on_refresh(
        startup_strategy=None, seed=_FakeSeed(nfqws_url=None), payload=payload)
    assert installs == []
    assert clears == []
    assert eps == ["9.9.9.9:443"]
    assert agent_main._current_exit_endpoints == ["9.9.9.9:443"]


def test_refresh_with_strategy_installs_new_endpoints(monkeypatch):
    # Box started WITH nfqws; refresh keeps a strategy -> install new endpoints.
    installs, clears = _record_desync(monkeypatch)
    payload = {
        "desync_strategy": "--dpi-desync=fake",
        "eu_exit_set": [{"endpoint": "8.8.8.8:443", "fingerprint": "f"}],
    }
    agent_main._apply_desync_on_refresh(
        startup_strategy="--dpi-desync=fake",
        seed=_FakeSeed(nfqws_url="https://x/nfqws"),
        payload=payload)
    assert installs == [{"exit_ips": ["8.8.8.8:443"], "qnum": agent_main.DESYNC_QNUM}]
    assert clears == []


def test_refresh_drops_strategy_clears_rules(monkeypatch):
    # Box started WITH nfqws; refresh DROPS the strategy -> clear the rules.
    installs, clears = _record_desync(monkeypatch)
    payload = {"eu_exit_set": [{"endpoint": "8.8.8.8:443", "fingerprint": "f"}]}
    agent_main._apply_desync_on_refresh(
        startup_strategy="--dpi-desync=fake",
        seed=_FakeSeed(nfqws_url="https://x/nfqws"),
        payload=payload)
    assert installs == []
    assert clears == [agent_main.DESYNC_QNUM]
