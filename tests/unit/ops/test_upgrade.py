from __future__ import annotations

import subprocess

import pytest

from mthydra.ops import upgrade


def test_current_head_sha_reads_git(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    (tmp_path / "a").write_text("a")
    subprocess.run(["git", "add", "a"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)
    sha = upgrade._current_head_sha(tmp_path)
    assert len(sha) == 40 and all(c in "0123456789abcdef" for c in sha)


def test_current_head_sha_raises_when_not_a_repo(tmp_path):
    with pytest.raises(upgrade.UpgradeError, match="not a git checkout"):
        upgrade._current_head_sha(tmp_path)


def test_pyproject_version_reads_project_section(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "mthydra"\nversion = "0.0.7"\n'
    )
    assert upgrade._pyproject_version(tmp_path) == "0.0.7"


def test_pyproject_version_returns_unknown_when_missing(tmp_path):
    assert upgrade._pyproject_version(tmp_path) == "unknown"
