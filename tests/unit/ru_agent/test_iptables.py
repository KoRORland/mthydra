"""Tests for mthydra.ru_agent.iptables — TPROXY rule install/verify/uninstall."""
from __future__ import annotations


def test_install_runs_iptables_with_expected_args(monkeypatch):
    from mthydra.ru_agent import iptables
    calls = []
    monkeypatch.setattr(
        iptables.subprocess, "run",
        lambda cmd, **kw: calls.append(cmd) or type("R", (), {"returncode": 0, "stdout": b"", "stderr": b""})(),
    )
    iptables.install(
        dc_cidrs_v4=["149.154.160.0/20"],
        dc_cidrs_v6=["2001:b28:f23d::/48"],
        tproxy_port=12345,
    )
    # Expect a TPROXY rule for each v4 + v6 CIDR.
    v4_calls = [c for c in calls if c[0] == "iptables"]
    v6_calls = [c for c in calls if c[0] == "ip6tables"]
    assert len(v4_calls) >= 1
    assert len(v6_calls) >= 1
    # tproxy_port appears in the rule.
    assert any("12345" in " ".join(c) for c in v4_calls)


def test_install_raises_on_failure(monkeypatch):
    import pytest
    from mthydra.ru_agent import iptables
    monkeypatch.setattr(
        iptables.subprocess, "run",
        lambda cmd, **kw: type("R", (), {"returncode": 1, "stdout": b"", "stderr": b"err"})(),
    )
    with pytest.raises(iptables.IptablesError):
        iptables.install(
            dc_cidrs_v4=["149.154.160.0/20"], dc_cidrs_v6=[], tproxy_port=12345,
        )


def test_verify_installed_detects_present_rules(monkeypatch):
    from mthydra.ru_agent import iptables
    monkeypatch.setattr(
        iptables.subprocess, "run",
        lambda cmd, **kw: type("R", (), {
            "returncode": 0,
            "stdout": b"-N MTHYDRA_DCS\n-A MTHYDRA_DCS -d 149.154.160.0/20 -p tcp -j TPROXY --on-port 12345\n",
            "stderr": b"",
        })(),
    )
    assert iptables.verify_installed(["149.154.160.0/20"], [], tproxy_port=12345)


def test_verify_installed_detects_missing_rule(monkeypatch):
    from mthydra.ru_agent import iptables
    monkeypatch.setattr(
        iptables.subprocess, "run",
        lambda cmd, **kw: type("R", (), {
            "returncode": 0, "stdout": b"-N MTHYDRA_DCS\n", "stderr": b"",
        })(),
    )
    assert iptables.verify_installed(["149.154.160.0/20"], [], tproxy_port=12345) is False


def test_verify_installed_returns_false_when_chain_missing(monkeypatch):
    """If iptables -S CHAIN fails (chain absent), verify returns False."""
    from mthydra.ru_agent import iptables
    monkeypatch.setattr(
        iptables.subprocess, "run",
        lambda cmd, **kw: type("R", (), {
            "returncode": 1, "stdout": b"", "stderr": b"No chain",
        })(),
    )
    assert iptables.verify_installed(["149.154.160.0/20"], [], tproxy_port=12345) is False


def test_verify_installed_skips_empty_cidr_list(monkeypatch):
    """Empty CIDR list for a family -> not checked. Both empty -> True."""
    from mthydra.ru_agent import iptables
    calls = []
    monkeypatch.setattr(
        iptables.subprocess, "run",
        lambda cmd, **kw: calls.append(cmd) or type("R", (), {
            "returncode": 0, "stdout": b"", "stderr": b"",
        })(),
    )
    assert iptables.verify_installed([], [], tproxy_port=12345) is True
    assert calls == []


def test_verify_installed_v6_only(monkeypatch):
    """Mix: empty v4 list skipped, v6 list checked."""
    from mthydra.ru_agent import iptables

    def fake_run(cmd, **kw):
        assert cmd[0] == "ip6tables"
        return type("R", (), {
            "returncode": 0,
            "stdout": b"-A MTHYDRA_DCS -d 2001:b28:f23d::/48 -p tcp -j TPROXY --on-port 12345\n",
            "stderr": b"",
        })()
    monkeypatch.setattr(iptables.subprocess, "run", fake_run)
    assert iptables.verify_installed([], ["2001:b28:f23d::/48"], tproxy_port=12345) is True


def test_uninstall_idempotent_swallows_errors(monkeypatch):
    """uninstall() invokes -D / -F / -X for both v4 and v6; non-zero rc is swallowed."""
    from mthydra.ru_agent import iptables
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        # Simulate that all calls fail (chain absent etc.), but uninstall
        # must not raise.
        return type("R", (), {
            "returncode": 1, "stdout": b"", "stderr": b"no such chain",
        })()
    monkeypatch.setattr(iptables.subprocess, "run", fake_run)
    iptables.uninstall()  # must not raise
    # 3 ops (-D, -F, -X) x 2 tools (iptables, ip6tables) = 6
    assert len(calls) == 6
    tools = [c[0] for c in calls]
    assert tools.count("iptables") == 3
    assert tools.count("ip6tables") == 3


def test_uninstall_succeeds_when_rules_present(monkeypatch):
    """When all calls succeed, uninstall still completes cleanly."""
    from mthydra.ru_agent import iptables
    monkeypatch.setattr(
        iptables.subprocess, "run",
        lambda cmd, **kw: type("R", (), {
            "returncode": 0, "stdout": b"", "stderr": b"",
        })(),
    )
    iptables.uninstall()  # no exception
