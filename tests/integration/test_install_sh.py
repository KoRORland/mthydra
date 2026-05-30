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


def test_ini_fallback_supplies_git_url(tmp_path):
    """When --config points at an ini with [install] git_url, the shell
    layer picks it up automatically — operator doesn't need to specify on
    both CLI and ini."""
    # Build a real ini with git_url in [install].
    ini = tmp_path / "install.ini"
    ini.write_text(
        "[install]\n"
        "git_url = https://example.invalid/mthydra.git\n"
        "git_ref = v1.2.3\n"
    )
    # Stub git and python3.12 so the script gets far enough to fail in a
    # known way — we want to see it ATTEMPT the clone with the ini-derived
    # URL, not bail with 'git_url required'.
    binroot = tmp_path / "bin"
    binroot.mkdir()
    recorder = tmp_path / "git-argv.txt"
    (binroot / "git").write_text(
        f'#!/bin/sh\nprintf "%s\\n" "$@" > "{recorder}"\nexit 0\n'
    )
    (binroot / "git").chmod(0o755)
    # python3.12 stub — just creates an empty venv-shaped dir tree so the
    # later mthydra-ops --help smoke check can run our stub binary.
    (binroot / "python3.12").write_text("#!/bin/sh\nexit 0\n")
    (binroot / "python3.12").chmod(0o755)
    venv_bin = tmp_path / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "mthydra-ops").write_text("#!/bin/sh\nexit 0\n")
    (venv_bin / "mthydra-ops").chmod(0o755)
    (venv_bin / "pip").write_text("#!/bin/sh\nexit 0\n")
    (venv_bin / "pip").chmod(0o755)
    src_dir = tmp_path / "src"        # absent → triggers the clone path

    env = {
        "PATH": f"{binroot}:" + os.environ.get("PATH", ""),
        "MTHYDRA_SKIP_APT": "1",
        "MTHYDRA_SRC_DIR": str(src_dir),
        "MTHYDRA_VENV_DIR": str(tmp_path / "venv"),
    }
    r = subprocess.run(
        ["sh", str(SCRIPT), "--config", str(ini)],
        env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    # git was invoked with the URL and ref from the ini.
    git_args = recorder.read_text()
    assert "https://example.invalid/mthydra.git" in git_args
    assert "v1.2.3" in git_args
