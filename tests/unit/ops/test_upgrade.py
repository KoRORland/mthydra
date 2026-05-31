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


def test_record_prior_captures_sha_version_and_backup_gen(monkeypatch, tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / ".git").mkdir()    # make _current_head_sha's "not a git checkout" check pass
    (src / "pyproject.toml").write_text('[project]\nversion = "0.0.1"\n')

    calls = []
    def fake_run(*args, **kw):
        argv = args[0] if args else []
        calls.append(list(argv))
        if "rev-parse" in argv:
            return subprocess.CompletedProcess(args, 0, "a" * 40 + "\n", "")
        return subprocess.CompletedProcess(
            args, 0, "backup-now: pushed generation 42\n", "")
    monkeypatch.setattr(upgrade.subprocess, "run", fake_run)

    prior = upgrade._record_prior(src, "/tmp/db.sqlite", "/tmp/c.toml")
    assert prior["prior_sha"] == "a" * 40
    assert prior["prior_version"] == "0.0.1"
    assert prior["backup_generation"] == 42
    backup_call = next(c for c in calls if c and "backup-now" in c)
    assert "--reason" in backup_call


def test_record_prior_raises_when_backup_gen_unparsable(monkeypatch, tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / ".git").mkdir()
    (src / "pyproject.toml").write_text('[project]\nversion = "0.0.1"\n')

    def fake_run(*args, **kw):
        argv = args[0] if args else []
        if "rev-parse" in argv:
            return subprocess.CompletedProcess(args, 0, "a" * 40 + "\n", "")
        return subprocess.CompletedProcess(args, 0, "no generation here\n", "")
    monkeypatch.setattr(upgrade.subprocess, "run", fake_run)
    with pytest.raises(upgrade.UpgradeError, match="generation"):
        upgrade._record_prior(src, "/tmp/db.sqlite", "/tmp/c.toml")


def test_fetch_and_checkout_invokes_git_correctly(monkeypatch):
    calls = []
    def fake_run(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")
    monkeypatch.setattr(upgrade.subprocess, "run", fake_run)
    upgrade._fetch_and_checkout(Path("/opt/mthydra/src"), "v0.0.2")
    assert calls[0] == ["git", "-C", "/opt/mthydra/src", "fetch", "origin", "v0.0.2"]
    assert calls[1] == ["git", "-C", "/opt/mthydra/src", "reset", "--hard", "FETCH_HEAD"]


def test_fetch_and_checkout_raises_on_failure(monkeypatch):
    def fake_run(argv, **kw):
        return subprocess.CompletedProcess(argv, 128, "", "fatal: bad ref")
    monkeypatch.setattr(upgrade.subprocess, "run", fake_run)
    with pytest.raises(upgrade.UpgradeError, match="git fetch"):
        upgrade._fetch_and_checkout(Path("/opt/mthydra/src"), "v9.9.9")


def test_pip_install_invokes_venv_pip(monkeypatch):
    seen = []
    def fake_run(argv, **kw):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")
    monkeypatch.setattr(upgrade.subprocess, "run", fake_run)
    upgrade._pip_install(Path("/opt/mthydra/venv"), Path("/opt/mthydra/src"))
    assert seen[0] == ["/opt/mthydra/venv/bin/pip", "install", "-e",
                       "/opt/mthydra/src"]


def test_rollback_to_resets_and_reinstalls(monkeypatch):
    seen = []
    def fake_run(argv, **kw):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")
    monkeypatch.setattr(upgrade.subprocess, "run", fake_run)
    upgrade._rollback_to(Path("/src"), Path("/venv"), "deadbeef" * 5)
    subs = [a[0] for a in seen]
    assert "git" in subs and "/venv/bin/pip" in subs
    git_call = next(a for a in seen if a[0] == "git")
    assert "reset" in git_call and "--hard" in git_call
    assert ("deadbeef" * 5) in git_call
