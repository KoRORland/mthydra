from __future__ import annotations

import argparse
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


def test_resolve_target_ref_defaults_to_main(monkeypatch):
    """No --ref must track main — AGENTS.md: fixes land on main and are picked
    up via `mthydra-ops upgrade`. It must NOT call GitHub for a release tag (the
    project does not tag per fix, so the latest tag is stale)."""
    monkeypatch.setattr(
        upgrade, "_call_resolve_latest_tag",
        lambda **kw: (_ for _ in ()).throw(
            AssertionError("must not resolve a release tag by default")))
    assert upgrade._resolve_target_ref(
        ref=None,
        upstream_repo="KoRORland/mthydra",
        github_api_url="https://api.github.com",
    ) == "main"


def test_resolve_target_ref_latest_keyword_resolves_tag(monkeypatch):
    """`--ref latest` opts in to the newest GitHub release tag."""
    monkeypatch.setattr(upgrade, "_call_resolve_latest_tag",
                        lambda **kw: "v0.1.0")
    assert upgrade._resolve_target_ref(
        ref="latest",
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


def test_fetch_and_checkout_falls_back_to_full_fetch_on_short_sha(monkeypatch):
    """`git fetch origin <short-sha>` fails with 'couldn't find remote ref'
    because git's wire protocol can't expand partial SHAs on the server.
    Fall back to `git fetch origin` (full refs) + `git reset --hard <ref>`
    locally.
    """
    calls = []
    def fake_run(argv, **kw):
        calls.append(argv)
        # Targeted fetch fails the way a short SHA would; the broad fetch
        # and the reset both succeed.
        if argv[:5] == ["git", "-C", "/src", "fetch", "origin"] and len(argv) == 6:
            return subprocess.CompletedProcess(
                argv, 128, "", "fatal: couldn't find remote ref 60934e5")
        return subprocess.CompletedProcess(argv, 0, "", "")
    monkeypatch.setattr(upgrade.subprocess, "run", fake_run)
    upgrade._fetch_and_checkout(Path("/src"), "60934e5")
    # The fallback sequence: targeted fetch attempt → full fetch → reset.
    assert calls[0] == ["git", "-C", "/src", "fetch", "origin", "60934e5"]
    assert calls[1] == ["git", "-C", "/src", "fetch", "origin"]
    assert calls[2] == ["git", "-C", "/src", "reset", "--hard", "60934e5"]


def test_fetch_and_checkout_raises_on_failure(monkeypatch):
    def fake_run(argv, **kw):
        # Both the targeted fetch AND the fallback full fetch fail.
        return subprocess.CompletedProcess(argv, 128, "", "fatal: bad ref")
    monkeypatch.setattr(upgrade.subprocess, "run", fake_run)
    with pytest.raises(upgrade.UpgradeError, match="git fetch"):
        upgrade._fetch_and_checkout(Path("/opt/mthydra/src"), "v9.9.9")


def test_pip_install_invokes_venv_pip(monkeypatch):
    seen = []
    def fake_run(argv, **kw):
        seen.append((argv, kw))
        return subprocess.CompletedProcess(argv, 0, "", "")
    monkeypatch.setattr(upgrade.subprocess, "run", fake_run)
    upgrade._pip_install(Path("/opt/mthydra/venv"), Path("/opt/mthydra/src"))
    assert seen[0][0] == ["/opt/mthydra/venv/bin/pip", "install", "-e",
                          "/opt/mthydra/src"]
    # R-D6: must set umask 022 via preexec_fn so the editable .pth + dist-info
    # are world-readable regardless of the caller's umask. Without this, a
    # root shell with umask 077 (set earlier to write a credential file) makes
    # the install unreadable by the mthydra service user → ModuleNotFoundError.
    preexec = seen[0][1].get("preexec_fn")
    assert preexec is not None, "preexec_fn must be set to enforce umask 022"
    # Calling the preexec sets the process umask; verify it lands at 0o022.
    import os as _os
    prev = _os.umask(0o077)
    try:
        preexec()
        assert _os.umask(0o000) == 0o022
    finally:
        _os.umask(prev)


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


def test_stop_service_invokes_systemctl_and_waits(monkeypatch):
    calls = []
    states = iter(["active", "active", "inactive"])
    def fake_run(argv, **kw):
        calls.append(argv)
        if argv[:3] == ["systemctl", "is-active", "x"]:
            state = next(states)
            return subprocess.CompletedProcess(
                argv, 0 if state == "active" else 3, state + "\n", "")
        return subprocess.CompletedProcess(argv, 0, "", "")
    monkeypatch.setattr(upgrade.subprocess, "run", fake_run)
    monkeypatch.setattr(upgrade.time, "sleep", lambda s: None)
    upgrade._stop_service("x", timeout_s=5)
    assert ["systemctl", "stop", "x"] in calls
    assert calls.count(["systemctl", "is-active", "x"]) >= 1


def test_start_and_verify_success(monkeypatch):
    def fake_run(argv, **kw):
        if argv[:3] == ["systemctl", "is-active", "x"]:
            return subprocess.CompletedProcess(argv, 0, "active\n", "")
        # startup-check + obs-heartbeat-now both succeed.
        return subprocess.CompletedProcess(argv, 0, "ok\n", "")
    monkeypatch.setattr(upgrade.subprocess, "run", fake_run)
    monkeypatch.setattr(upgrade.time, "sleep", lambda s: None)
    upgrade._start_and_verify("x", "/db", "/cfg", verify_timeout_s=5)


def test_start_and_verify_raises_on_startup_check_fail(monkeypatch):
    def fake_run(argv, **kw):
        if argv[:3] == ["systemctl", "is-active", "x"]:
            return subprocess.CompletedProcess(argv, 0, "active\n", "")
        if "startup-check" in argv:
            return subprocess.CompletedProcess(argv, 1, "", "broken")
        return subprocess.CompletedProcess(argv, 0, "", "")
    monkeypatch.setattr(upgrade.subprocess, "run", fake_run)
    monkeypatch.setattr(upgrade.time, "sleep", lambda s: None)
    with pytest.raises(upgrade.VerifyFailed, match="startup-check"):
        upgrade._start_and_verify("x", "/db", "/cfg", verify_timeout_s=5)


def test_start_and_verify_raises_when_service_never_active(monkeypatch):
    def fake_run(argv, **kw):
        if argv[:3] == ["systemctl", "is-active", "x"]:
            return subprocess.CompletedProcess(argv, 3, "activating\n", "")
        return subprocess.CompletedProcess(argv, 0, "", "")
    monkeypatch.setattr(upgrade.subprocess, "run", fake_run)
    monkeypatch.setattr(upgrade.time, "sleep", lambda s: None)
    with pytest.raises(upgrade.VerifyFailed, match="never became active"):
        upgrade._start_and_verify("x", "/db", "/cfg", verify_timeout_s=1)


# ---------------------------------------------------------------------------
# cmd_upgrade orchestrator tests
# ---------------------------------------------------------------------------


def _upgrade_args(tmp_path, **over):
    base = dict(
        ref="v0.0.2", no_auto_rollback=False, allow_schema_migration=False,
        src_dir=str(tmp_path / "src"),
        venv_dir=str(tmp_path / "venv"),
        unit="mthydra-controller",
        db_path=str(tmp_path / "db.sqlite"),
        config=str(tmp_path / "controller.toml"),
        upstream_repo="KoRORland/mthydra",
        github_api_url="https://api.github.com",
        verify_timeout=30,
        non_interactive=True, verbose=False, quiet=True, dry_run=False,
    )
    base.update(over)
    return argparse.Namespace(**base)


def _seed_min_src(src: Path) -> str:
    """Create a minimal src tree that _current_head_sha can read (via the
    sentinel .git dir + a monkeypatched subprocess returning a fake SHA in
    the test). Returns the fake prior_sha string the fake_run will yield."""
    src.mkdir()
    (src / ".git").mkdir()
    (src / "pyproject.toml").write_text('[project]\nversion = "0.0.1"\n')
    return "a" * 40


def test_cmd_upgrade_happy_path(monkeypatch, tmp_path):
    prior_sha = _seed_min_src(tmp_path / "src")
    _seed_schema_db(tmp_path / "db.sqlite", 15)

    monkeypatch.setattr(upgrade, "_call_resolve_latest_tag",
                        lambda **kw: "v0.0.2")
    monkeypatch.setattr(upgrade, "_schema_would_migrate",
                        lambda src, db: (False, 15, 15))
    monkeypatch.setattr(upgrade, "_fetch_and_checkout", lambda src, ref: None)
    monkeypatch.setattr(upgrade, "_pip_install", lambda v, s: None)
    monkeypatch.setattr(upgrade, "_stop_service", lambda u, timeout_s=30: None)
    monkeypatch.setattr(upgrade, "_start_and_verify",
                        lambda *a, **kw: None)

    def fake_run(*args, **kw):
        argv = args[0] if args else []
        if "rev-parse" in argv:
            return subprocess.CompletedProcess(args, 0, prior_sha + "\n", "")
        if "backup-now" in argv:
            return subprocess.CompletedProcess(
                args, 0, "backup-now: pushed generation 7\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")
    monkeypatch.setattr(upgrade.subprocess, "run", fake_run)

    rc = upgrade.cmd_upgrade(_upgrade_args(tmp_path))
    assert rc == 0


def test_cmd_upgrade_auto_rollback_on_verify_fail(monkeypatch, tmp_path):
    prior_sha = _seed_min_src(tmp_path / "src")
    _seed_schema_db(tmp_path / "db.sqlite", 15)
    monkeypatch.setattr(upgrade, "_call_resolve_latest_tag",
                        lambda **kw: "v0.0.2")
    monkeypatch.setattr(upgrade, "_schema_would_migrate",
                        lambda src, db: (False, 15, 15))
    monkeypatch.setattr(upgrade, "_fetch_and_checkout", lambda src, ref: None)
    monkeypatch.setattr(upgrade, "_pip_install", lambda v, s: None)
    monkeypatch.setattr(upgrade, "_stop_service", lambda u, timeout_s=30: None)
    verify_calls = {"n": 0}
    def fake_verify(*a, **kw):
        verify_calls["n"] += 1
        if verify_calls["n"] == 1:
            raise upgrade.VerifyFailed("simulated post-upgrade fail")
    monkeypatch.setattr(upgrade, "_start_and_verify", fake_verify)
    rollback_calls = {"n": 0}
    def fake_rollback(src, venv, sha):
        rollback_calls["n"] += 1
        assert sha == prior_sha
    monkeypatch.setattr(upgrade, "_rollback_to", fake_rollback)

    def fake_run(*args, **kw):
        argv = args[0] if args else []
        if "rev-parse" in argv:
            return subprocess.CompletedProcess(args, 0, prior_sha + "\n", "")
        if "backup-now" in argv:
            return subprocess.CompletedProcess(
                args, 0, "backup-now: pushed generation 7\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")
    monkeypatch.setattr(upgrade.subprocess, "run", fake_run)

    rc = upgrade.cmd_upgrade(_upgrade_args(tmp_path))
    assert verify_calls["n"] == 2
    assert rollback_calls["n"] == 1
    assert rc != 0   # upgrade failed even though rollback succeeded


def test_cmd_upgrade_refuses_schema_migration_without_flag(monkeypatch, tmp_path):
    _seed_min_src(tmp_path / "src")
    _seed_schema_db(tmp_path / "db.sqlite", 15)
    monkeypatch.setattr(upgrade, "_call_resolve_latest_tag",
                        lambda **kw: "v0.0.2")
    monkeypatch.setattr(upgrade, "_schema_would_migrate",
                        lambda src, db: (True, 16, 15))
    monkeypatch.setattr(upgrade, "_fetch_and_checkout", lambda src, ref: None)
    monkeypatch.setattr(upgrade, "_rollback_to", lambda src, venv, sha: None)
    def fake_run(*args, **kw):
        argv = args[0] if args else []
        if "rev-parse" in argv:
            return subprocess.CompletedProcess(args, 0, "b" * 40 + "\n", "")
        if "backup-now" in argv:
            return subprocess.CompletedProcess(
                args, 0, "backup-now: pushed generation 7\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")
    monkeypatch.setattr(upgrade.subprocess, "run", fake_run)
    rc = upgrade.cmd_upgrade(_upgrade_args(tmp_path))
    assert rc != 0


def test_cmd_upgrade_noop_when_already_at_target(monkeypatch, tmp_path):
    prior_sha = _seed_min_src(tmp_path / "src")
    _seed_schema_db(tmp_path / "db.sqlite", 15)
    monkeypatch.setattr(upgrade, "_call_resolve_latest_tag",
                        lambda **kw: prior_sha)
    fetched = {"v": False}
    monkeypatch.setattr(upgrade, "_fetch_and_checkout",
                        lambda s, r: fetched.__setitem__("v", True))
    monkeypatch.setattr(upgrade, "_pip_install",
                        lambda v, s: (_ for _ in ()).throw(
                            AssertionError("should not pip-install on no-op")))
    monkeypatch.setattr(upgrade, "_stop_service",
                        lambda u, timeout_s=30: (_ for _ in ()).throw(
                            AssertionError("should not stop on no-op")))
    def fake_run(*args, **kw):
        argv = args[0] if args else []
        if "rev-parse" in argv:
            return subprocess.CompletedProcess(args, 0, prior_sha + "\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")
    monkeypatch.setattr(upgrade.subprocess, "run", fake_run)
    rc = upgrade.cmd_upgrade(_upgrade_args(tmp_path, ref=prior_sha))
    assert rc == 0
    assert fetched["v"] is False


def test_cmd_upgrade_resumes_when_partial_state_detected(monkeypatch, tmp_path, capsys):
    """S-Task 3: when a previous upgrade got past phase 4 (checkout) but
    failed at phase 5 (pip-install), source HEAD == target_ref but the
    installed venv is still the old version. Re-running must NOT
    short-circuit as 'already at target' — it must redo phases 4-7 so
    the venv catches up.

    Discovered 2026-06-01: user's first prod mthydra-ops upgrade failed
    EACCES at pip-install, leaving the source at v0.0.3 but the venv at
    v0.0.2."""
    prior_sha = _seed_min_src(tmp_path / "src")
    _seed_schema_db(tmp_path / "db.sqlite", 15)
    # Simulate the partial-state condition: source pyproject says 0.0.3,
    # but importlib.metadata thinks 0.0.2 is installed (the failed prior
    # upgrade left the venv un-updated).
    (tmp_path / "src" / "pyproject.toml").write_text('[project]\nversion = "0.0.3"\n')
    monkeypatch.setattr(upgrade, "_installed_version", lambda: "0.0.2")
    monkeypatch.setattr(upgrade, "_call_resolve_latest_tag",
                        lambda **kw: prior_sha)
    monkeypatch.setattr(upgrade, "_schema_would_migrate",
                        lambda src, db: (False, 15, 15))

    fetched = {"v": False}
    monkeypatch.setattr(upgrade, "_fetch_and_checkout",
                        lambda s, r: fetched.__setitem__("v", True))
    pip_called = {"v": 0}
    monkeypatch.setattr(upgrade, "_pip_install",
                        lambda v, s: pip_called.__setitem__("v", pip_called["v"] + 1))
    monkeypatch.setattr(upgrade, "_stop_service", lambda u, timeout_s=30: None)
    monkeypatch.setattr(upgrade, "_start_and_verify", lambda *a, **kw: None)

    def fake_run(*args, **kw):
        argv = args[0] if args else []
        if "rev-parse" in argv:
            return subprocess.CompletedProcess(args, 0, prior_sha + "\n", "")
        if "backup-now" in argv:
            return subprocess.CompletedProcess(
                args, 0, "backup-now: pushed generation 11\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")
    monkeypatch.setattr(upgrade.subprocess, "run", fake_run)

    rc = upgrade.cmd_upgrade(_upgrade_args(tmp_path, ref=prior_sha))
    assert rc == 0
    # The crucial assertion: pip-install ran exactly once (partial state did
    # NOT trigger the no-op short-circuit), so the venv caught up.
    assert pip_called["v"] == 1
    # And the warning message landed in stdout so the operator knows why.
    out = capsys.readouterr().out
    assert "partial state detected" in out
    assert "v0.0.2" in out and "v0.0.3" in out


def test_cmd_upgrade_runs_schema_migrate_when_acknowledged(monkeypatch, tmp_path):
    """R-D7: when --allow-schema-migration is set AND a migration is needed,
    cmd_upgrade must invoke `mthydra-controller schema-migrate` after pip
    install and before stop-service. Spec Q's flag was previously a gate-only
    no-op — the migration never actually ran."""
    prior_sha = _seed_min_src(tmp_path / "src")
    _seed_schema_db(tmp_path / "db.sqlite", 14)
    monkeypatch.setattr(upgrade, "_call_resolve_latest_tag",
                        lambda **kw: "v0.0.3")
    monkeypatch.setattr(upgrade, "_schema_would_migrate",
                        lambda src, db: (True, 15, 14))
    monkeypatch.setattr(upgrade, "_fetch_and_checkout", lambda src, ref: None)
    monkeypatch.setattr(upgrade, "_pip_install", lambda v, s: None)
    monkeypatch.setattr(upgrade, "_stop_service", lambda u, timeout_s=30: None)
    monkeypatch.setattr(upgrade, "_start_and_verify", lambda *a, **kw: None)

    schema_migrate_calls = {"n": 0}
    def fake_run(*args, **kw):
        argv = args[0] if args else []
        if "rev-parse" in argv:
            return subprocess.CompletedProcess(args, 0, prior_sha + "\n", "")
        if "backup-now" in argv:
            return subprocess.CompletedProcess(
                args, 0, "backup-now: pushed generation 9\n", "")
        if "schema-migrate" in argv:
            schema_migrate_calls["n"] += 1
            return subprocess.CompletedProcess(
                args, 0, "schema-migrate: db v14 -> v15 OK\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")
    monkeypatch.setattr(upgrade.subprocess, "run", fake_run)

    rc = upgrade.cmd_upgrade(_upgrade_args(tmp_path, allow_schema_migration=True))
    assert rc == 0
    assert schema_migrate_calls["n"] == 1


def test_cmd_upgrade_aborts_if_schema_migrate_fails(monkeypatch, tmp_path):
    """R-D7: schema-migrate failure must abort BEFORE stop-service, so the
    old code keeps running against the old (un-migrated) schema."""
    prior_sha = _seed_min_src(tmp_path / "src")
    _seed_schema_db(tmp_path / "db.sqlite", 14)
    monkeypatch.setattr(upgrade, "_call_resolve_latest_tag",
                        lambda **kw: "v0.0.3")
    monkeypatch.setattr(upgrade, "_schema_would_migrate",
                        lambda src, db: (True, 15, 14))
    monkeypatch.setattr(upgrade, "_fetch_and_checkout", lambda src, ref: None)
    monkeypatch.setattr(upgrade, "_pip_install", lambda v, s: None)
    monkeypatch.setattr(upgrade, "_stop_service",
                        lambda u, timeout_s=30: (_ for _ in ()).throw(
                            AssertionError("must not stop service on migrate fail")))

    def fake_run(*args, **kw):
        argv = args[0] if args else []
        if "rev-parse" in argv:
            return subprocess.CompletedProcess(args, 0, prior_sha + "\n", "")
        if "backup-now" in argv:
            return subprocess.CompletedProcess(
                args, 0, "backup-now: pushed generation 9\n", "")
        if "schema-migrate" in argv:
            return subprocess.CompletedProcess(args, 3, "", "boom\n")
        return subprocess.CompletedProcess(args, 0, "", "")
    monkeypatch.setattr(upgrade.subprocess, "run", fake_run)

    rc = upgrade.cmd_upgrade(_upgrade_args(tmp_path, allow_schema_migration=True))
    assert rc != 0


# ---------------------------------------------------------------------------
# Pre-flight health check (2026-06-01 fix for user's stale-heartbeat block)
# ---------------------------------------------------------------------------


def test_preflight_health_returns_0_on_success(monkeypatch, tmp_path):
    """Pre-flight runs startup-check + obs-heartbeat-now. Both succeeding
    returns rc=0 and the upgrade proceeds."""
    calls = []
    def fake_run(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "ok", "")
    monkeypatch.setattr(upgrade.subprocess, "run", fake_run)

    rc = upgrade._preflight_health(
        "/var/lib/mthydra/state.sqlite",
        "/etc/mthydra/controller.toml",
        "mthydra-controller",
    )
    assert rc == 0
    # Both checks ran (and in the right order).
    bins = [c[1] for c in calls]
    assert bins == ["startup-check", "obs-heartbeat-now"]


def test_preflight_health_aborts_on_startup_check_fail(monkeypatch, tmp_path, capsys):
    """If startup-check refuses pre-upgrade, return 11 without forcing a
    heartbeat. The whole point is to NOT proceed if the host is broken."""
    def fake_run(argv, **kw):
        if "startup-check" in argv:
            return subprocess.CompletedProcess(
                argv, 10, "", "check 42: stale heartbeat\n")
        # Should never get here.
        raise AssertionError(f"unexpected call: {argv}")
    monkeypatch.setattr(upgrade.subprocess, "run", fake_run)

    rc = upgrade._preflight_health("/db", "/cfg", "u")
    assert rc == 11
    assert "check 42" in capsys.readouterr().err


def test_preflight_health_aborts_on_heartbeat_fail(monkeypatch, tmp_path, capsys):
    """Startup-check passes but heartbeat refuses (SMTP broken). Return 12
    + tell the operator they can't get alerts."""
    def fake_run(argv, **kw):
        if "startup-check" in argv:
            return subprocess.CompletedProcess(argv, 0, "ok", "")
        if "obs-heartbeat-now" in argv:
            return subprocess.CompletedProcess(
                argv, 2, "", "smtp 530 auth failed\n")
        raise AssertionError(f"unexpected call: {argv}")
    monkeypatch.setattr(upgrade.subprocess, "run", fake_run)

    rc = upgrade._preflight_health("/db", "/cfg", "u")
    assert rc == 12
    err = capsys.readouterr().err
    assert "smtp 530" in err
    assert "Fix the email/Telegram sink" in err


def test_cmd_upgrade_aborts_on_preflight_fail_without_backup(monkeypatch, tmp_path):
    """If pre-flight fails, NO backup is taken, NO checkout happens,
    NO pip install runs. The host stays untouched so the operator
    can fix the root cause and retry cleanly."""
    _seed_min_src(tmp_path / "src")
    _seed_schema_db(tmp_path / "db.sqlite", 15)
    monkeypatch.setattr(upgrade, "_call_resolve_latest_tag",
                        lambda **kw: "v0.0.8")
    # Pre-flight fails → no other helper should be called.
    monkeypatch.setattr(upgrade, "_record_prior",
                        lambda *a: (_ for _ in ()).throw(
                            AssertionError("record_prior must not run")))
    monkeypatch.setattr(upgrade, "_fetch_and_checkout",
                        lambda *a: (_ for _ in ()).throw(
                            AssertionError("fetch must not run")))
    monkeypatch.setattr(upgrade, "_pip_install",
                        lambda *a: (_ for _ in ()).throw(
                            AssertionError("pip_install must not run")))

    def fake_run(argv, **kw):
        if "rev-parse" in argv:
            return subprocess.CompletedProcess(
                argv, 0, "a" * 40 + "\n", "")
        if "startup-check" in argv:
            return subprocess.CompletedProcess(
                argv, 10, "", "check 42: stale heartbeat at startup\n")
        raise AssertionError(f"unexpected call: {argv}")
    monkeypatch.setattr(upgrade.subprocess, "run", fake_run)

    rc = upgrade.cmd_upgrade(_upgrade_args(tmp_path))
    assert rc == 11  # pre-flight startup-check failure
