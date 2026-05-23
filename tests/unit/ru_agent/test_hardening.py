import pytest


def test_verify_all_passes_when_all_checks_pass(monkeypatch):
    """All five checks (swap, journald, core_pattern, /var/log tmpfs,
    /run/mthydra tmpfs) return True -> verify_all() returns None."""
    from mthydra.ru_agent import hardening
    monkeypatch.setattr(hardening, "_swap_disabled", lambda: True)
    monkeypatch.setattr(hardening, "_journald_volatile", lambda: True)
    monkeypatch.setattr(hardening, "_core_pattern_disabled", lambda: True)
    monkeypatch.setattr(hardening, "_path_on_tmpfs", lambda p: True)
    hardening.verify_all()  # no exception


def test_verify_all_raises_on_swap_enabled(monkeypatch):
    from mthydra.ru_agent import hardening
    monkeypatch.setattr(hardening, "_swap_disabled", lambda: False)
    monkeypatch.setattr(hardening, "_journald_volatile", lambda: True)
    monkeypatch.setattr(hardening, "_core_pattern_disabled", lambda: True)
    monkeypatch.setattr(hardening, "_path_on_tmpfs", lambda p: True)
    with pytest.raises(hardening.HardeningError, match="swap"):
        hardening.verify_all()


def test_verify_all_raises_on_journald_persistent(monkeypatch):
    from mthydra.ru_agent import hardening
    monkeypatch.setattr(hardening, "_swap_disabled", lambda: True)
    monkeypatch.setattr(hardening, "_journald_volatile", lambda: False)
    monkeypatch.setattr(hardening, "_core_pattern_disabled", lambda: True)
    monkeypatch.setattr(hardening, "_path_on_tmpfs", lambda p: True)
    with pytest.raises(hardening.HardeningError, match="journald"):
        hardening.verify_all()


def test_verify_all_raises_on_core_pattern_enabled(monkeypatch):
    from mthydra.ru_agent import hardening
    monkeypatch.setattr(hardening, "_swap_disabled", lambda: True)
    monkeypatch.setattr(hardening, "_journald_volatile", lambda: True)
    monkeypatch.setattr(hardening, "_core_pattern_disabled", lambda: False)
    monkeypatch.setattr(hardening, "_path_on_tmpfs", lambda p: True)
    with pytest.raises(hardening.HardeningError, match="core"):
        hardening.verify_all()


def test_verify_all_raises_on_var_log_not_tmpfs(monkeypatch):
    from mthydra.ru_agent import hardening
    monkeypatch.setattr(hardening, "_swap_disabled", lambda: True)
    monkeypatch.setattr(hardening, "_journald_volatile", lambda: True)
    monkeypatch.setattr(hardening, "_core_pattern_disabled", lambda: True)
    monkeypatch.setattr(hardening, "_path_on_tmpfs",
                         lambda p: p != "/var/log")
    with pytest.raises(hardening.HardeningError, match="/var/log"):
        hardening.verify_all()


def test_swap_disabled_reads_proc_swaps(tmp_path, monkeypatch):
    """Empty /proc/swaps (header only) -> swap disabled."""
    from mthydra.ru_agent import hardening
    fake = tmp_path / "swaps"
    fake.write_text("Filename\t\t\t\tType\t\tSize\t\tUsed\t\tPriority\n")
    monkeypatch.setattr(hardening, "_PROC_SWAPS_PATH", str(fake))
    assert hardening._swap_disabled() is True


def test_swap_disabled_detects_active_swap(tmp_path, monkeypatch):
    from mthydra.ru_agent import hardening
    fake = tmp_path / "swaps"
    fake.write_text(
        "Filename\t\t\t\tType\t\tSize\t\tUsed\t\tPriority\n"
        "/swap.img\t\tfile\t\t1048572\t\t0\t\t-2\n"
    )
    monkeypatch.setattr(hardening, "_PROC_SWAPS_PATH", str(fake))
    assert hardening._swap_disabled() is False
