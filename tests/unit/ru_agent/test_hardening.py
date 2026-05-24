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


def test_swap_disabled_missing_proc_swaps(monkeypatch, tmp_path):
    from mthydra.ru_agent import hardening
    monkeypatch.setattr(hardening, "_PROC_SWAPS_PATH", str(tmp_path / "nope"))
    assert hardening._swap_disabled() is True


def test_journald_volatile_true_when_run_log_journal(monkeypatch):
    from mthydra.ru_agent import hardening

    def fake_run(*a, **kw):
        return type("R", (), {
            "returncode": 0,
            "stdout": "Path /run/log/journal/abc.journal\nFile: live\n",
            "stderr": "",
        })()
    monkeypatch.setattr(hardening.subprocess, "run", fake_run)
    assert hardening._journald_volatile() is True


def test_journald_volatile_false_when_var_log_journal(monkeypatch):
    from mthydra.ru_agent import hardening

    def fake_run(*a, **kw):
        return type("R", (), {
            "returncode": 0,
            "stdout": "Path /var/log/journal/abc.journal\n",
            "stderr": "",
        })()
    monkeypatch.setattr(hardening.subprocess, "run", fake_run)
    assert hardening._journald_volatile() is False


def test_journald_volatile_false_on_nonzero_rc(monkeypatch):
    from mthydra.ru_agent import hardening

    def fake_run(*a, **kw):
        return type("R", (), {"returncode": 1, "stdout": "", "stderr": "err"})()
    monkeypatch.setattr(hardening.subprocess, "run", fake_run)
    assert hardening._journald_volatile() is False


def test_journald_volatile_false_when_journalctl_missing(monkeypatch):
    from mthydra.ru_agent import hardening

    def fake_run(*a, **kw):
        raise FileNotFoundError("journalctl")
    monkeypatch.setattr(hardening.subprocess, "run", fake_run)
    assert hardening._journald_volatile() is False


def test_journald_volatile_false_on_timeout(monkeypatch):
    import subprocess as sp
    from mthydra.ru_agent import hardening

    def fake_run(*a, **kw):
        raise sp.TimeoutExpired(cmd="journalctl", timeout=5)
    monkeypatch.setattr(hardening.subprocess, "run", fake_run)
    assert hardening._journald_volatile() is False


def test_core_pattern_disabled_when_bin_false(tmp_path, monkeypatch):
    from mthydra.ru_agent import hardening
    fake = tmp_path / "core_pattern"
    fake.write_text("|/bin/false\n")
    monkeypatch.setattr(hardening, "_CORE_PATTERN_PATH", str(fake))
    assert hardening._core_pattern_disabled() is True


def test_core_pattern_disabled_when_active_pattern(tmp_path, monkeypatch):
    from mthydra.ru_agent import hardening
    fake = tmp_path / "core_pattern"
    fake.write_text("|/usr/lib/systemd/systemd-coredump %P\n")
    monkeypatch.setattr(hardening, "_CORE_PATTERN_PATH", str(fake))
    assert hardening._core_pattern_disabled() is False


def test_core_pattern_disabled_when_missing(monkeypatch, tmp_path):
    from mthydra.ru_agent import hardening
    monkeypatch.setattr(hardening, "_CORE_PATTERN_PATH", str(tmp_path / "nope"))
    assert hardening._core_pattern_disabled() is True


def test_path_on_tmpfs_true_when_listed(tmp_path, monkeypatch):
    from mthydra.ru_agent import hardening
    fake_mounts = tmp_path / "mounts"
    fake_mounts.write_text(
        "proc /proc proc rw,nosuid,nodev 0 0\n"
        "tmpfs /var/log tmpfs rw,nosuid 0 0\n"
    )
    real_open = open

    def fake_open(path, *a, **kw):
        if path == "/proc/mounts":
            return real_open(fake_mounts, *a, **kw)
        return real_open(path, *a, **kw)
    monkeypatch.setattr("builtins.open", fake_open)
    assert hardening._path_on_tmpfs("/var/log") is True


def test_path_on_tmpfs_false_when_not_tmpfs(tmp_path, monkeypatch):
    from mthydra.ru_agent import hardening
    fake_mounts = tmp_path / "mounts"
    fake_mounts.write_text("ext4 /var/log ext4 rw 0 0\n")
    real_open = open

    def fake_open(path, *a, **kw):
        if path == "/proc/mounts":
            return real_open(fake_mounts, *a, **kw)
        return real_open(path, *a, **kw)
    monkeypatch.setattr("builtins.open", fake_open)
    assert hardening._path_on_tmpfs("/var/log") is False


def test_path_on_tmpfs_false_when_proc_mounts_missing(monkeypatch):
    from mthydra.ru_agent import hardening
    real_open = open

    def fake_open(path, *a, **kw):
        if path == "/proc/mounts":
            raise FileNotFoundError
        return real_open(path, *a, **kw)
    monkeypatch.setattr("builtins.open", fake_open)
    assert hardening._path_on_tmpfs("/var/log") is False


def test_verify_all_raises_on_run_mthydra_not_tmpfs(monkeypatch):
    from mthydra.ru_agent import hardening
    import pytest
    monkeypatch.setattr(hardening, "_swap_disabled", lambda: True)
    monkeypatch.setattr(hardening, "_journald_volatile", lambda: True)
    monkeypatch.setattr(hardening, "_core_pattern_disabled", lambda: True)
    monkeypatch.setattr(hardening, "_path_on_tmpfs",
                         lambda p: p != "/run/mthydra")
    with pytest.raises(hardening.HardeningError, match="/run/mthydra"):
        hardening.verify_all()
