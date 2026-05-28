from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "install.sh"


def test_shellcheck_clean():
    if shutil.which("shellcheck") is None:
        pytest.skip("shellcheck not installed")
    r = subprocess.run(["shellcheck", str(SCRIPT)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


def test_execs_correct_subcommand_with_stub(tmp_path):
    # stub mthydra-ops that records its argv then exits 0
    binroot = tmp_path / "opt" / "mthydra" / "venv" / "bin"
    binroot.mkdir(parents=True)
    recorder = tmp_path / "argv.txt"
    stub = binroot / "mthydra-ops"
    stub.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > "{recorder}"\n')
    stub.chmod(0o755)
    env = {**os.environ, "MTHYDRA_SKIP_APT": "1", "MTHYDRA_SKIP_BUILD": "1",
           "MTHYDRA_VENV_DIR": str(tmp_path / "opt" / "mthydra" / "venv")}
    r = subprocess.run(
        ["sh", str(SCRIPT), "--standby", "--config", "/tmp/s.ini",
         "--promote", "--case", "B"],
        env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    forwarded = recorder.read_text().split("\n")
    assert "install-standby" in forwarded
    assert "--promote" in forwarded and "B" in forwarded
