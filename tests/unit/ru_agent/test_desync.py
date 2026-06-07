from mthydra.ru_agent import desync


def test_nfqws_argv():
    argv = desync.nfqws_argv("/run/mthydra/nfqws",
                             "--dpi-desync=fake,split2 --dpi-desync-ttl=4", qnum=200)
    assert argv[0] == "/run/mthydra/nfqws"
    assert "--qnum=200" in argv
    assert "--dpi-desync=fake,split2" in argv
    assert "--dpi-desync-ttl=4" in argv


def test_exit_ips_split_v4_v6():
    v4, v6 = desync.split_exit_ips(["9.9.9.9:443", "[2001:db8::1]:443", "8.8.8.8:443"])
    assert v4 == ["9.9.9.9", "8.8.8.8"]
    assert v6 == ["2001:db8::1"]


def test_install_builds_per_ip_nfqueue_rules(monkeypatch):
    calls = []
    monkeypatch.setattr(desync, "_run", lambda cmd: calls.append(cmd) or "")
    desync.install(exit_ips=["9.9.9.9:443"], qnum=200)
    flat = [" ".join(c) for c in calls]
    assert any("MTHYDRA_DESYNC" in f for f in flat)
    assert any("-d 9.9.9.9" in f and "--dport 443" in f
               and "NFQUEUE" in f and "--queue-num 200" in f for f in flat)


def test_verify_installed_token_exact(monkeypatch):
    listing = ("-A MTHYDRA_DESYNC -d 9.9.9.9/32 -p tcp -m tcp --dport 443 "
               "-j NFQUEUE --queue-num 200\n")
    monkeypatch.setattr(desync, "_run", lambda cmd: listing)
    assert desync.verify_installed(exit_ips=["9.9.9.9:443"], qnum=200) is True
    assert desync.verify_installed(exit_ips=["1.1.1.1:443"], qnum=200) is False


def test_clear_issues_delete_flush_destroy_per_tool(monkeypatch):
    calls = []
    monkeypatch.setattr(desync, "_run", lambda cmd: calls.append(cmd) or "")
    desync.clear(200)
    flat = [" ".join(c) for c in calls]
    for tool in ("iptables", "ip6tables"):
        assert any(f.startswith(f"{tool} -t mangle -D OUTPUT") for f in flat)
        assert any(f.startswith(f"{tool} -t mangle -F MTHYDRA_DESYNC") for f in flat)
        assert any(f.startswith(f"{tool} -t mangle -X MTHYDRA_DESYNC") for f in flat)


def test_install_empty_exit_ips_installs_nothing(monkeypatch):
    calls = []
    monkeypatch.setattr(desync, "_run", lambda cmd: calls.append(cmd) or "")
    desync.install(exit_ips=[], qnum=200)
    flat = [" ".join(c) for c in calls]
    assert not any("-N" in f or "NFQUEUE" in f for f in flat)
