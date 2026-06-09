"""Tests for probe_runner.key.ensure_probe_key — keygen + DB persist + file cache."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from mthydra.controller.probe_runner import key as keymod
from mthydra.controller.state import probe_key as pk
from mthydra.controller.state.db import connect
from mthydra.controller.state.schema import apply_schema


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "state.sqlite")
    apply_schema(c)
    yield c
    c.close()


def _fake_keygen(monkeypatch):
    """ssh-keygen shim: writes a private file + .pub at the -f path."""
    def _run(argv, capture_output=True, text=True, timeout=None, input=None, check=False):
        if argv[0] == "ssh-keygen":
            for i, tok in enumerate(argv):
                if tok == "-f":
                    Path(argv[i + 1]).write_text("PRIVATE-KEY-BODY\n")
                    Path(argv[i + 1] + ".pub").write_text("ssh-ed25519 PUBKEY mthydra\n")
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.CompletedProcess(argv, 0, "", "")
    monkeypatch.setattr(keymod.subprocess, "run", _run)


def test_generates_and_persists_on_empty_db(tmp_path, conn, monkeypatch):
    _fake_keygen(monkeypatch)
    ssh_dir = tmp_path / "ssh"
    key_path, pubkey = keymod.ensure_probe_key(conn, ssh_dir)
    assert key_path == ssh_dir / "probe.key"
    assert key_path.read_text() == "PRIVATE-KEY-BODY\n"
    assert (ssh_dir / "probe.key.pub").read_text().strip() == pubkey
    row = pk.get(conn)
    assert row.private_key == "PRIVATE-KEY-BODY\n"
    assert row.public_key == "ssh-ed25519 PUBKEY mthydra"
    assert oct(key_path.stat().st_mode)[-3:] == "600"


def test_materializes_file_from_existing_db_row_without_keygen(tmp_path, conn, monkeypatch):
    pk.put(conn, private_key="DB-PRIV\n", public_key="ssh-ed25519 DBPUB x",
           comment=None, at="2026-06-05T00:00:00Z")
    calls = []
    def _run(argv, **kw):
        calls.append(argv[0])
        return subprocess.CompletedProcess(argv, 0, "", "")
    monkeypatch.setattr(keymod.subprocess, "run", _run)
    ssh_dir = tmp_path / "ssh"
    key_path, pubkey = keymod.ensure_probe_key(conn, ssh_dir)
    assert "ssh-keygen" not in calls          # no regeneration
    assert key_path.read_text() == "DB-PRIV\n"
    assert pubkey == "ssh-ed25519 DBPUB x"


def test_rewrites_file_when_cache_is_stale(tmp_path, conn, monkeypatch):
    pk.put(conn, private_key="CORRECT\n", public_key="ssh-ed25519 P x",
           comment=None, at="2026-06-05T00:00:00Z")
    monkeypatch.setattr(keymod.subprocess, "run",
                        lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "", ""))
    ssh_dir = tmp_path / "ssh"
    ssh_dir.mkdir()
    (ssh_dir / "probe.key").write_text("STALE\n")     # wrong contents
    key_path, _ = keymod.ensure_probe_key(conn, ssh_dir)
    assert key_path.read_text() == "CORRECT\n"        # rewritten from DB


def test_wheel_start_materializes_key_from_db(tmp_path, monkeypatch):
    """A promoted standby restores the DB; wheel.start() must rematerialize
    the probe.key file before scheduling ticks."""
    from mthydra.controller.probe_runner.wheel import ProbeRunnerWheel
    from mthydra.controller.state import probe_key as pk
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.schema import apply_schema

    db = tmp_path / "state.sqlite"
    c = connect(db)
    apply_schema(c)
    pk.put(c, private_key="RESTORED\n", public_key="ssh-ed25519 R x",
           comment=None, at="2026-06-05T00:00:00Z")
    c.close()

    ssh_dir = tmp_path / "ssh"
    # mode='offline' so start() does materialization but schedules nothing.
    wheel = ProbeRunnerWheel(db_path=str(db), interval_seconds=1800,
                             max_concurrent=2, mode="offline",
                             ssh_dir=str(ssh_dir))
    wheel.start()
    assert (ssh_dir / "probe.key").read_text() == "RESTORED\n"
