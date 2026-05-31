from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path

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


def test_resolve_target_ref_explicit_wins(monkeypatch):
    monkeypatch.setattr(
        upgrade, "_call_resolve_latest_tag",
        lambda **kw: (_ for _ in ()).throw(
            AssertionError("should not call GitHub when --ref given")))
    assert upgrade._resolve_target_ref(
        ref="v0.0.5",
        upstream_repo="KoRORland/mthydra",
        github_api_url="https://api.github.com",
    ) == "v0.0.5"


def test_resolve_target_ref_falls_back_to_latest(monkeypatch):
    monkeypatch.setattr(upgrade, "_call_resolve_latest_tag",
                        lambda **kw: "v0.1.0")
    assert upgrade._resolve_target_ref(
        ref=None,
        upstream_repo="KoRORland/mthydra",
        github_api_url="https://api.github.com",
    ) == "v0.1.0"


def _seed_schema_db(path: Path, version: int) -> None:
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE schema_version (version INTEGER, applied_at TEXT)")
    conn.execute("INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                 (version, "2026-05-31T00:00:00Z"))
    conn.commit()
    conn.close()


def _seed_target_src(src: Path, target_version: int) -> None:
    (src / "src" / "mthydra" / "controller" / "state").mkdir(parents=True)
    (src / "src" / "mthydra" / "controller" / "state" / "schema.py").write_text(
        f"SCHEMA_VERSION = {target_version}\n# rest of file irrelevant for AST\n")


def test_schema_would_migrate_true_when_target_higher(tmp_path):
    db = tmp_path / "state.sqlite"
    _seed_schema_db(db, 14)
    src = tmp_path / "src-tree"
    _seed_target_src(src, 16)
    would, target, current = upgrade._schema_would_migrate(src, str(db))
    assert (would, target, current) == (True, 16, 14)


def test_schema_would_migrate_false_when_equal(tmp_path):
    db = tmp_path / "state.sqlite"
    _seed_schema_db(db, 15)
    src = tmp_path / "src-tree"
    _seed_target_src(src, 15)
    would, _t, _c = upgrade._schema_would_migrate(src, str(db))
    assert would is False


def test_schema_would_migrate_raises_when_constant_missing(tmp_path):
    db = tmp_path / "state.sqlite"
    _seed_schema_db(db, 15)
    src = tmp_path / "src-tree"
    (src / "src" / "mthydra" / "controller" / "state").mkdir(parents=True)
    (src / "src" / "mthydra" / "controller" / "state" / "schema.py").write_text(
        "# no SCHEMA_VERSION constant\n")
    with pytest.raises(upgrade.UpgradeError, match="SCHEMA_VERSION"):
        upgrade._schema_would_migrate(src, str(db))
