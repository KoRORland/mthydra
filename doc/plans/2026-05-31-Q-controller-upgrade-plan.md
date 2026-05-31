# `mthydra-ops upgrade` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship `mthydra-ops upgrade` — one operator command that upgrades the EU controller's source + venv + restarts service, with pre-upgrade forced backup, auto-rollback on health-check failure, and forward-only schema-migration acknowledgement.

**Architecture:** One new module `src/mthydra/ops/upgrade.py` with seven internal helpers (`_resolve_target_ref`, `_current_head_sha`, `_pyproject_version`, `_record_prior`, `_fetch_and_checkout`, `_pip_install`, `_stop_service`, `_start_and_verify`, `_rollback_to`, `_schema_would_migrate`) and one orchestrator `cmd_upgrade(args) -> int`. Subprocess to `git`/`pip`/`systemctl`/`mthydra-controller`. Schema-version comparison via AST (don't import the new code into the running process). Lazy dispatch wrapper in `main.py`.

**Tech Stack:** Python 3.12 stdlib (`subprocess`, `tomllib`, `ast`, `sqlite3`, `urllib.request`, `re`, `time`, `dataclasses`), pytest. No new third-party.

**Spec:** `doc/specs/2026-05-31-Q-controller-upgrade.md` (Q-D1…Q-D9).

**Naming contract (used across all tasks — keep consistent):**
- Module: `src/mthydra/ops/upgrade.py`. Tests: `tests/unit/ops/test_upgrade.py`.
- Functions/classes:
  - `class VerifyFailed(RuntimeError)` — raised by `_start_and_verify` when post-restart health checks fail.
  - `class UpgradeError(RuntimeError)` — raised by other helpers for clean error surfacing.
  - `_current_head_sha(src_dir: Path) -> str` — `git rev-parse HEAD`.
  - `_pyproject_version(src_dir: Path) -> str` — read `pyproject.toml`'s `[project] version`.
  - `_resolve_target_ref(*, ref: str | None, upstream_repo: str, github_api_url: str) -> str` — explicit `--ref` wins; else GitHub `releases/latest` tag.
  - `_schema_would_migrate(src_dir: Path, db_path: str) -> tuple[bool, int, int]` — returns `(would_migrate, target_version, current_version)`.
  - `_record_prior(src_dir: Path, db_path: str, config_path: str) -> dict` — returns `{"prior_sha": str, "prior_version": str, "backup_generation": int}`; calls `mthydra-controller backup-now`.
  - `_fetch_and_checkout(src_dir: Path, ref: str) -> None`.
  - `_pip_install(venv_dir: Path, src_dir: Path) -> None`.
  - `_stop_service(unit: str, timeout_s: int = 30) -> None`.
  - `_start_and_verify(unit: str, db_path: str, config_path: str, verify_timeout_s: int = 120) -> None` — raises `VerifyFailed`.
  - `_rollback_to(src_dir: Path, venv_dir: Path, prior_sha: str) -> None`.
  - `cmd_upgrade(args) -> int` — orchestrator.
- main.py wiring: `_dispatch_upgrade` (lazy `from . import upgrade`), `_DISPATCH["upgrade"] = _dispatch_upgrade`, `upgrade` subparser.
- CLI flags (final shape): `--ref`, `--no-auto-rollback`, `--allow-schema-migration`, `--src-dir` (default `/opt/mthydra/src`), `--venv-dir` (default `/opt/mthydra/venv`), `--unit` (default `mthydra-controller`), `--db-path` (default `_DEFAULT_DB`), `--config` (default `_DEFAULT_CONFIG`), `--upstream-repo` (default `KoRORland/mthydra`), `--github-api-url` (default `https://api.github.com`), `--verify-timeout` (int, default 120), `--non-interactive`, `--verbose`, `--quiet`, `--dry-run`.

---

## Task 1: source-dir helpers (`_current_head_sha` + `_pyproject_version`)

**Files:**
- Create: `src/mthydra/ops/upgrade.py`
- Test: `tests/unit/ops/test_upgrade.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/ops/test_upgrade.py
from __future__ import annotations

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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/ops/test_upgrade.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mthydra.ops.upgrade'`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/mthydra/ops/upgrade.py
"""mthydra-ops upgrade — one-command controller upgrade (spec Q)."""
from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path


class UpgradeError(RuntimeError):
    pass


class VerifyFailed(RuntimeError):
    """Raised when post-restart health checks fail; triggers rollback."""


def _current_head_sha(src_dir: Path) -> str:
    """`git rev-parse HEAD` in src_dir. Raises UpgradeError if not a checkout."""
    src_dir = Path(src_dir)
    if not (src_dir / ".git").exists():
        raise UpgradeError(f"not a git checkout: {src_dir}")
    res = subprocess.run(
        ["git", "-C", str(src_dir), "rev-parse", "HEAD"],
        capture_output=True, text=True,
    )
    if res.returncode != 0:
        raise UpgradeError(
            f"git rev-parse HEAD failed in {src_dir}: {res.stderr.strip()}")
    return res.stdout.strip()


def _pyproject_version(src_dir: Path) -> str:
    """Read [project] version from pyproject.toml. Returns 'unknown' if absent."""
    p = Path(src_dir) / "pyproject.toml"
    if not p.exists():
        return "unknown"
    data = tomllib.loads(p.read_text())
    return str(data.get("project", {}).get("version", "unknown"))
```

- [ ] **Step 4: Run, expect PASS** (4 tests).

- [ ] **Step 5: Commit + push**

```bash
git add src/mthydra/ops/upgrade.py tests/unit/ops/test_upgrade.py
git commit -m "feat(upgrade): _current_head_sha + _pyproject_version helpers"
git push origin main
```

---

## Task 2: `_resolve_target_ref` — explicit ref or GitHub latest tag

**Files:**
- Modify: `src/mthydra/ops/upgrade.py`
- Test: `tests/unit/ops/test_upgrade.py`

- [ ] **Step 1: APPEND the failing test**

```python
def test_resolve_target_ref_explicit_wins(monkeypatch):
    # Even with a working resolver, explicit --ref bypasses GitHub entirely.
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
```

- [ ] **Step 2: Run, expect FAIL** — `_resolve_target_ref` undefined.

- [ ] **Step 3: APPEND to `src/mthydra/ops/upgrade.py`**

```python
def _call_resolve_latest_tag(*, upstream_repo: str, github_api_url: str) -> str:
    """Thin wrapper around image_ops.resolve_latest_tag so tests can
    monkeypatch this name without touching image_ops itself."""
    from . import image_ops
    return image_ops.resolve_latest_tag(
        upstream_repo=upstream_repo, github_api_url=github_api_url)


def _resolve_target_ref(*, ref: str | None, upstream_repo: str,
                       github_api_url: str) -> str:
    """Explicit --ref wins; else default to the latest GitHub release tag."""
    if ref:
        return ref
    return _call_resolve_latest_tag(
        upstream_repo=upstream_repo, github_api_url=github_api_url)
```

- [ ] **Step 4: Run, expect PASS** (6 tests total).

- [ ] **Step 5: Commit + push**

```bash
git add src/mthydra/ops/upgrade.py tests/unit/ops/test_upgrade.py
git commit -m "feat(upgrade): _resolve_target_ref — explicit or GitHub latest (Q-D3)"
git push origin main
```

---

## Task 3: `_schema_would_migrate` — AST-parse target SCHEMA_VERSION + DB read

**Files:**
- Modify: `src/mthydra/ops/upgrade.py`
- Test: `tests/unit/ops/test_upgrade.py`

- [ ] **Step 1: APPEND the failing test**

```python
import sqlite3


def _seed_schema_db(path: Path, version: int) -> None:
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE schema_version (version INTEGER, applied_at TEXT)")
    conn.execute("INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                 (version, "2026-05-31T00:00:00Z"))
    conn.commit(); conn.close()


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
```

- [ ] **Step 2: Run, expect FAIL.**

- [ ] **Step 3: APPEND to `src/mthydra/ops/upgrade.py`**

```python
import ast
import sqlite3


def _parse_schema_version_constant(schema_py: Path) -> int:
    """AST-walk schema.py for `SCHEMA_VERSION = <int>`. Avoids importing the
    new code into the running process (which would force a stdlib-cache
    conflict with the running module of the same name)."""
    tree = ast.parse(schema_py.read_text(), filename=str(schema_py))
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name) and target.id == "SCHEMA_VERSION":
                if isinstance(node.value, ast.Constant) and isinstance(
                        node.value.value, int):
                    return node.value.value
    raise UpgradeError(
        f"SCHEMA_VERSION constant not found in {schema_py}")


def _current_schema_version(db_path: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT version FROM schema_version WHERE rowid=1").fetchone()
    finally:
        conn.close()
    if row is None:
        raise UpgradeError(f"schema_version row missing in {db_path}")
    return int(row[0])


def _schema_would_migrate(src_dir: Path, db_path: str
                          ) -> tuple[bool, int, int]:
    schema_py = (Path(src_dir) / "src" / "mthydra" / "controller"
                 / "state" / "schema.py")
    target = _parse_schema_version_constant(schema_py)
    current = _current_schema_version(db_path)
    return (target > current, target, current)
```

- [ ] **Step 4: Run, expect PASS** (9 tests total).

- [ ] **Step 5: Commit + push**

```bash
git add src/mthydra/ops/upgrade.py tests/unit/ops/test_upgrade.py
git commit -m "feat(upgrade): _schema_would_migrate via AST + DB read (Q-D6)"
git push origin main
```

---

## Task 4: `_record_prior` — forced backup + capture prior SHA/version

**Files:**
- Modify: `src/mthydra/ops/upgrade.py`
- Test: `tests/unit/ops/test_upgrade.py`

- [ ] **Step 1: APPEND the failing test**

```python
def test_record_prior_captures_sha_version_and_backup_gen(monkeypatch, tmp_path):
    src = tmp_path / "src"
    subprocess.run(["git", "init", "-q"], cwd=src.parent, check=False)
    src.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=src, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=src, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=src, check=True)
    (src / "a").write_text("a")
    subprocess.run(["git", "add", "a"], cwd=src, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=src, check=True)
    (src / "pyproject.toml").write_text('[project]\nversion = "0.0.1"\n')

    calls = []
    def fake_run(*args, **kw):
        calls.append(list(args[0]) if args else [])
        return subprocess.CompletedProcess(
            args, 0, "backup-now: pushed generation 42\n", "")
    monkeypatch.setattr(upgrade.subprocess, "run", fake_run)

    prior = upgrade._record_prior(src, "/tmp/db.sqlite", "/tmp/c.toml")
    assert len(prior["prior_sha"]) == 40
    assert prior["prior_version"] == "0.0.1"
    assert prior["backup_generation"] == 42
    # backup-now invoked with --db-path + --config + --reason.
    backup_call = next(c for c in calls if c and "backup-now" in c)
    assert "--reason" in backup_call


def test_record_prior_raises_when_backup_gen_unparsable(monkeypatch, tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=src, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=src, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=src, check=True)
    (src / "a").write_text("a"); subprocess.run(["git", "add", "a"], cwd=src, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=src, check=True)
    (src / "pyproject.toml").write_text('[project]\nversion = "0.0.1"\n')

    def fake_run(*args, **kw):
        return subprocess.CompletedProcess(args, 0, "no generation here\n", "")
    monkeypatch.setattr(upgrade.subprocess, "run", fake_run)
    with pytest.raises(upgrade.UpgradeError, match="generation"):
        upgrade._record_prior(src, "/tmp/db.sqlite", "/tmp/c.toml")
```

- [ ] **Step 2: Run, expect FAIL.**

- [ ] **Step 3: APPEND to `src/mthydra/ops/upgrade.py`**

```python
import re
import sys

_BACKUP_GEN_RE = re.compile(r"backup-now: pushed generation (\d+)")


def _controller_bin() -> str:
    """Resolve mthydra-controller as the sys.executable sibling — mirrors
    install.py's fix for the no-PATH root-shell case."""
    return str(Path(sys.executable).parent / "mthydra-controller")


def _record_prior(src_dir: Path, db_path: str, config_path: str) -> dict:
    """Snapshot the upgrade's recovery floor: current commit SHA + pyproject
    version + a freshly-forced backup generation number (parsed from the
    `backup-now: pushed generation <N>` line on the controller's stdout)."""
    sha = _current_head_sha(src_dir)
    version = _pyproject_version(src_dir)
    res = subprocess.run(
        [_controller_bin(), "backup-now",
         "--db-path", db_path, "--config", config_path,
         "--reason", "pre-upgrade snapshot"],
        capture_output=True, text=True,
    )
    if res.returncode != 0:
        raise UpgradeError(
            f"pre-upgrade backup-now failed (exit {res.returncode}): "
            f"{res.stderr.strip()}")
    m = _BACKUP_GEN_RE.search(res.stdout or "")
    if not m:
        raise UpgradeError(
            "could not parse backup generation from backup-now stdout; "
            f"got: {res.stdout!r}")
    return {
        "prior_sha": sha,
        "prior_version": version,
        "backup_generation": int(m.group(1)),
    }
```

- [ ] **Step 4: Run, expect PASS** (11 tests total).

- [ ] **Step 5: Commit + push**

```bash
git add src/mthydra/ops/upgrade.py tests/unit/ops/test_upgrade.py
git commit -m "feat(upgrade): _record_prior — forced backup + SHA/version snapshot (Q-D4)"
git push origin main
```

---

## Task 5: `_fetch_and_checkout` + `_pip_install` + `_rollback_to`

**Files:**
- Modify: `src/mthydra/ops/upgrade.py`
- Test: `tests/unit/ops/test_upgrade.py`

- [ ] **Step 1: APPEND the failing test**

```python
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
    # git reset to the prior SHA, then pip reinstall.
    subs = [a[0] for a in seen]
    assert "git" in subs and "/venv/bin/pip" in subs
    git_call = next(a for a in seen if a[0] == "git")
    assert "reset" in git_call and "--hard" in git_call
    assert ("deadbeef" * 5) in git_call
```

- [ ] **Step 2: Run, expect FAIL.**

- [ ] **Step 3: APPEND to `src/mthydra/ops/upgrade.py`**

```python
def _fetch_and_checkout(src_dir: Path, ref: str) -> None:
    """`git fetch origin <ref>` + `git reset --hard FETCH_HEAD` — same pattern
    as scripts/install.sh. Works for branches, tags, and SHAs."""
    src = str(Path(src_dir))
    res = subprocess.run(
        ["git", "-C", src, "fetch", "origin", ref],
        capture_output=True, text=True,
    )
    if res.returncode != 0:
        raise UpgradeError(
            f"git fetch origin {ref!r} failed: {res.stderr.strip()}")
    res = subprocess.run(
        ["git", "-C", src, "reset", "--hard", "FETCH_HEAD"],
        capture_output=True, text=True,
    )
    if res.returncode != 0:
        raise UpgradeError(
            f"git reset --hard FETCH_HEAD failed: {res.stderr.strip()}")


def _pip_install(venv_dir: Path, src_dir: Path) -> None:
    """`<venv>/bin/pip install -e <src>` — re-installs editable mode against
    the freshly-checked-out source."""
    pip = str(Path(venv_dir) / "bin" / "pip")
    res = subprocess.run(
        [pip, "install", "-e", str(src_dir)],
        capture_output=True, text=True,
    )
    if res.returncode != 0:
        raise UpgradeError(
            f"pip install failed (exit {res.returncode}): "
            f"{res.stderr.strip() or res.stdout.strip()}")


def _rollback_to(src_dir: Path, venv_dir: Path, prior_sha: str) -> None:
    """Hard-reset to prior_sha + reinstall venv. Caller runs verify next."""
    src = str(Path(src_dir))
    res = subprocess.run(
        ["git", "-C", src, "reset", "--hard", prior_sha],
        capture_output=True, text=True,
    )
    if res.returncode != 0:
        raise UpgradeError(
            f"rollback git reset --hard {prior_sha!r} failed: "
            f"{res.stderr.strip()}")
    _pip_install(venv_dir, src_dir)
```

- [ ] **Step 4: Run, expect PASS** (15 tests total).

- [ ] **Step 5: Commit + push**

```bash
git add src/mthydra/ops/upgrade.py tests/unit/ops/test_upgrade.py
git commit -m "feat(upgrade): _fetch_and_checkout + _pip_install + _rollback_to"
git push origin main
```

---

## Task 6: `_stop_service` + `_start_and_verify` (+ `VerifyFailed`)

**Files:**
- Modify: `src/mthydra/ops/upgrade.py`
- Test: `tests/unit/ops/test_upgrade.py`

- [ ] **Step 1: APPEND the failing test**

```python
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
    seq = iter(["active"])
    def fake_run(argv, **kw):
        if argv[:3] == ["systemctl", "is-active", "x"]:
            return subprocess.CompletedProcess(argv, 0, next(seq) + "\n", "")
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
```

- [ ] **Step 2: Run, expect FAIL.**

- [ ] **Step 3: APPEND to `src/mthydra/ops/upgrade.py`**

```python
import time


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["systemctl", *args], capture_output=True, text=True,
    )


def _wait_for(predicate, *, timeout_s: int, poll_s: float = 1.0) -> bool:
    """Block until predicate() returns True or timeout. Returns whether ok."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(poll_s)
    return predicate()


def _stop_service(unit: str, timeout_s: int = 30) -> None:
    _systemctl("stop", unit)
    def _inactive() -> bool:
        return _systemctl("is-active", unit).returncode != 0
    if not _wait_for(_inactive, timeout_s=timeout_s):
        raise UpgradeError(
            f"service {unit!r} did not stop within {timeout_s}s")


def _start_and_verify(unit: str, db_path: str, config_path: str,
                      verify_timeout_s: int = 120) -> None:
    """Start the unit, wait for is-active, then run startup-check + heartbeat.
    Raises VerifyFailed on any check failure."""
    _systemctl("start", unit)
    def _active() -> bool:
        return _systemctl("is-active", unit).returncode == 0
    if not _wait_for(_active, timeout_s=verify_timeout_s):
        raise VerifyFailed(
            f"service {unit!r} never became active within {verify_timeout_s}s")
    # startup-check (runs schema migration + invariants).
    res = subprocess.run(
        [_controller_bin(), "startup-check", "--db-path", db_path],
        capture_output=True, text=True,
    )
    if res.returncode != 0:
        raise VerifyFailed(
            f"startup-check failed (exit {res.returncode}): "
            f"{res.stderr.strip() or res.stdout.strip()}")
    # obs-heartbeat-now (verifies sinks).
    res = subprocess.run(
        [_controller_bin(), "obs-heartbeat-now",
         "--db-path", db_path, "--config", config_path],
        capture_output=True, text=True,
    )
    if res.returncode != 0:
        raise VerifyFailed(
            f"obs-heartbeat-now failed (exit {res.returncode}): "
            f"{res.stderr.strip() or res.stdout.strip()}")
```

- [ ] **Step 4: Run, expect PASS** (19 tests total).

- [ ] **Step 5: Commit + push**

```bash
git add src/mthydra/ops/upgrade.py tests/unit/ops/test_upgrade.py
git commit -m "feat(upgrade): _stop_service + _start_and_verify with VerifyFailed"
git push origin main
```

---

## Task 7: `cmd_upgrade` orchestrator + CLI wiring

**Files:**
- Modify: `src/mthydra/ops/upgrade.py` (the orchestrator)
- Modify: `src/mthydra/ops/main.py` (subparser + lazy dispatch)
- Test: `tests/unit/ops/test_upgrade.py`, `tests/unit/ops/test_main.py`

- [ ] **Step 1a: APPEND the failing tests to `tests/unit/ops/test_upgrade.py`**

```python
import argparse


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


def _seed_repo(src: Path) -> str:
    src.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=src, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=src, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=src, check=True)
    (src / "pyproject.toml").write_text('[project]\nversion = "0.0.1"\n')
    subprocess.run(["git", "add", "pyproject.toml"], cwd=src, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=src, check=True)
    return subprocess.run(
        ["git", "-C", str(src), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def test_cmd_upgrade_happy_path(monkeypatch, tmp_path):
    prior_sha = _seed_repo(tmp_path / "src")
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

    calls = []
    def fake_run(argv, **kw):
        calls.append(argv)
        # backup-now → emits the parseable generation line.
        if "backup-now" in argv:
            return subprocess.CompletedProcess(
                argv, 0, "backup-now: pushed generation 7\n", "")
        return subprocess.CompletedProcess(argv, 0, "", "")
    monkeypatch.setattr(upgrade.subprocess, "run", fake_run)

    rc = upgrade.cmd_upgrade(_upgrade_args(tmp_path))
    assert rc == 0


def test_cmd_upgrade_auto_rollback_on_verify_fail(monkeypatch, tmp_path):
    prior_sha = _seed_repo(tmp_path / "src")
    _seed_schema_db(tmp_path / "db.sqlite", 15)
    monkeypatch.setattr(upgrade, "_call_resolve_latest_tag",
                        lambda **kw: "v0.0.2")
    monkeypatch.setattr(upgrade, "_schema_would_migrate",
                        lambda src, db: (False, 15, 15))
    monkeypatch.setattr(upgrade, "_fetch_and_checkout", lambda src, ref: None)
    monkeypatch.setattr(upgrade, "_pip_install", lambda v, s: None)
    monkeypatch.setattr(upgrade, "_stop_service", lambda u, timeout_s=30: None)
    # First verify (post-upgrade) fails; second verify (post-rollback) succeeds.
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

    def fake_run(argv, **kw):
        if "backup-now" in argv:
            return subprocess.CompletedProcess(
                argv, 0, "backup-now: pushed generation 7\n", "")
        return subprocess.CompletedProcess(argv, 0, "", "")
    monkeypatch.setattr(upgrade.subprocess, "run", fake_run)

    rc = upgrade.cmd_upgrade(_upgrade_args(tmp_path))
    # Verify ran twice (once post-upgrade, once post-rollback); rollback ran once.
    assert verify_calls["n"] == 2
    assert rollback_calls["n"] == 1
    # Exit non-zero — upgrade failed and rolled back; operator must know.
    assert rc != 0


def test_cmd_upgrade_refuses_schema_migration_without_flag(monkeypatch, tmp_path):
    _seed_repo(tmp_path / "src")
    _seed_schema_db(tmp_path / "db.sqlite", 15)
    monkeypatch.setattr(upgrade, "_call_resolve_latest_tag",
                        lambda **kw: "v0.0.2")
    monkeypatch.setattr(upgrade, "_schema_would_migrate",
                        lambda src, db: (True, 16, 15))
    rc = upgrade.cmd_upgrade(_upgrade_args(tmp_path))
    assert rc != 0


def test_cmd_upgrade_noop_when_already_at_target(monkeypatch, tmp_path):
    prior_sha = _seed_repo(tmp_path / "src")
    _seed_schema_db(tmp_path / "db.sqlite", 15)
    monkeypatch.setattr(upgrade, "_call_resolve_latest_tag",
                        lambda **kw: prior_sha)   # target == current
    fetched = {"v": False}
    monkeypatch.setattr(upgrade, "_fetch_and_checkout",
                        lambda s, r: fetched.__setitem__("v", True))
    monkeypatch.setattr(upgrade, "_pip_install",
                        lambda v, s: (_ for _ in ()).throw(
                            AssertionError("should not pip-install on no-op")))
    monkeypatch.setattr(upgrade, "_stop_service",
                        lambda u, timeout_s=30: (_ for _ in ()).throw(
                            AssertionError("should not stop on no-op")))
    rc = upgrade.cmd_upgrade(_upgrade_args(tmp_path, ref=prior_sha))
    assert rc == 0
    assert fetched["v"] is False     # didn't even fetch
```

- [ ] **Step 1b: APPEND the failing test to `tests/unit/ops/test_main.py`**

```python
def test_upgrade_subcommand_parses_and_routes(monkeypatch):
    from mthydra.ops import main as m
    from mthydra.ops import upgrade
    called = {}
    monkeypatch.setattr(upgrade, "cmd_upgrade",
                        lambda args: called.setdefault("v", 0) or 0)
    rc = m.main(["upgrade", "--ref", "v0.0.2"])
    assert rc == 0 and "v" in called
```

- [ ] **Step 2: Run, expect FAIL** on all.

- [ ] **Step 3: APPEND to `src/mthydra/ops/upgrade.py`** (the orchestrator):

```python
def _say(msg: str) -> None:
    print(f"[mthydra-upgrade] {msg}", flush=True)


def _err(msg: str) -> None:
    import sys as _sys
    print(f"[mthydra-upgrade] ERROR: {msg}", file=_sys.stderr, flush=True)


def cmd_upgrade(args) -> int:
    src_dir = Path(args.src_dir)
    venv_dir = Path(args.venv_dir)

    # ----- Phase 1: preflight -----
    try:
        current_sha = _current_head_sha(src_dir)
    except UpgradeError as e:
        _err(str(e)); return 2

    # ----- Phase 2: resolve-target -----
    try:
        target_ref = _resolve_target_ref(
            ref=args.ref,
            upstream_repo=args.upstream_repo,
            github_api_url=args.github_api_url,
        )
    except Exception as e:
        _err(f"resolve target: {e}"); return 2
    _say(f"current SHA: {current_sha[:12]}; target ref: {target_ref}")

    if target_ref == current_sha:
        _say("already at target — nothing to do")
        return 0

    # Schema-migration gate: only meaningful when we have a new src on disk,
    # but we need to know in advance. Since the new src isn't here yet, we
    # check AFTER fetching but BEFORE pip-install, so the fetched code's
    # SCHEMA_VERSION is on disk to AST-parse.

    if args.dry_run:
        _say(f"DRY-RUN: would upgrade from {current_sha[:12]} → {target_ref}")
        return 0

    # ----- Phase 3: record-prior (forced backup) -----
    try:
        prior = _record_prior(src_dir, args.db_path, args.config)
    except UpgradeError as e:
        _err(str(e)); return 3
    _say(f"prior recorded: sha={prior['prior_sha'][:12]} "
         f"version={prior['prior_version']} "
         f"backup_generation={prior['backup_generation']}")

    # ----- Phase 4: fetch-and-checkout -----
    try:
        _fetch_and_checkout(src_dir, target_ref)
    except UpgradeError as e:
        _err(f"fetch failed: {e}")
        _err(f"recovery floor: backup generation {prior['backup_generation']}")
        return 4

    # ---- Schema-migration gate (after fetch, before pip-install) ----
    try:
        would_migrate, target_v, current_v = _schema_would_migrate(
            src_dir, args.db_path)
    except UpgradeError as e:
        _err(f"schema-version check: {e}")
        _err("rolling back source to prior SHA")
        try:
            _rollback_to(src_dir, venv_dir, prior["prior_sha"])
        except UpgradeError as e2:
            _err(f"rollback also failed: {e2}")
        return 5
    if would_migrate and not args.allow_schema_migration:
        _err(f"target SCHEMA_VERSION ({target_v}) > DB version ({current_v}); "
             f"refusing without --allow-schema-migration")
        _err("rolling back source to prior SHA")
        try:
            _rollback_to(src_dir, venv_dir, prior["prior_sha"])
        except UpgradeError as e:
            _err(f"rollback also failed: {e}")
            return 6
        return 5

    # ----- Phase 5: pip-install -----
    try:
        _pip_install(venv_dir, src_dir)
    except UpgradeError as e:
        _err(f"pip install failed: {e}")
        _err(f"recovery floor: backup generation {prior['backup_generation']}")
        return 5

    # ----- Phases 6-7: stop + start + verify (with optional rollback) -----
    try:
        _stop_service(args.unit)
    except UpgradeError as e:
        _err(f"stop service failed: {e}"); return 6

    try:
        _start_and_verify(args.unit, args.db_path, args.config,
                          verify_timeout_s=args.verify_timeout)
    except VerifyFailed as e:
        _err(f"post-upgrade verify failed: {e}")
        if args.no_auto_rollback:
            _err("--no-auto-rollback set — leaving the failed upgrade in place")
            return 7
        _say(f"auto-rollback: resetting to {prior['prior_sha'][:12]}")
        try:
            _stop_service(args.unit)   # may be already stopped, _systemctl is_active will say so
        except UpgradeError:
            pass
        try:
            _rollback_to(src_dir, venv_dir, prior["prior_sha"])
        except UpgradeError as e2:
            _err(f"rollback failed: {e2}")
            _err(f"investigate by hand; backup generation {prior['backup_generation']}")
            return 8
        try:
            _start_and_verify(args.unit, args.db_path, args.config,
                              verify_timeout_s=args.verify_timeout)
        except VerifyFailed as e2:
            _err(f"post-rollback verify ALSO failed: {e2}")
            _err(f"investigate by hand; backup generation {prior['backup_generation']}")
            return 9
        _say(f"rolled back to {prior['prior_sha'][:12]} — verify ok")
        return 7

    # ----- Phase 8: summary -----
    new_sha = _current_head_sha(src_dir)
    new_version = _pyproject_version(src_dir)
    _say(f"done: {prior['prior_sha'][:12]} ({prior['prior_version']}) "
         f"→ {new_sha[:12]} ({new_version}); "
         f"pre-upgrade backup gen={prior['backup_generation']}")
    return 0
```

Then in `src/mthydra/ops/main.py`, add the lazy dispatch wrapper (after the existing `_dispatch_agent_publish`):

```python
def _dispatch_upgrade(args) -> int:
    from . import upgrade
    return upgrade.cmd_upgrade(args)
```

`_DISPATCH` += `"upgrade": _dispatch_upgrade,`.

In `build_parser()` (after the existing `agent-publish` subparser), add:

```python
    upg = sub.add_parser("upgrade",
                          help="one-command controller upgrade: fetch → pip → restart → verify (auto-rollback)")
    upg.add_argument("--ref", default=None,
                      help="git ref to upgrade to (default: latest GitHub release tag)")
    upg.add_argument("--no-auto-rollback", action="store_true",
                      help="leave broken state in place for forensics on verify failure")
    upg.add_argument("--allow-schema-migration", action="store_true",
                      help="acknowledge new SCHEMA_VERSION (rollback won't recover the schema)")
    upg.add_argument("--src-dir", default="/opt/mthydra/src")
    upg.add_argument("--venv-dir", default="/opt/mthydra/venv")
    upg.add_argument("--unit", default="mthydra-controller")
    upg.add_argument("--db-path", default=_DEFAULT_DB)
    upg.add_argument("--config", default=_DEFAULT_CONFIG)
    upg.add_argument("--upstream-repo", default="KoRORland/mthydra")
    upg.add_argument("--github-api-url", default="https://api.github.com")
    upg.add_argument("--verify-timeout", type=int, default=120)
    upg.add_argument("--non-interactive", action="store_true")
    upg.add_argument("--verbose", action="store_true")
    upg.add_argument("--quiet", action="store_true")
    upg.add_argument("--dry-run", action="store_true")
```

- [ ] **Step 4: Run, expect PASS** — `.venv/bin/pytest tests/unit/ops/test_upgrade.py tests/unit/ops/test_main.py -v`. Also full suite: `.venv/bin/pytest tests/ -q`.

- [ ] **Step 5: Commit + push**

```bash
git add src/mthydra/ops/upgrade.py src/mthydra/ops/main.py \
        tests/unit/ops/test_upgrade.py tests/unit/ops/test_main.py
git commit -m "feat(upgrade): cmd_upgrade orchestrator + CLI wiring (Q-D1..Q-D9)"
git push origin main
```

---

## Task 8: Bump `pyproject.toml` to 0.0.2 + tag

**Files:**
- Modify: `pyproject.toml`
- Tag: `v0.0.2` on the head commit

- [ ] **Step 1: Bump version**

Edit `pyproject.toml`:

```toml
[project]
name = "mthydra"
version = "0.0.2"
```

(Find the existing `version = "0.0.1"` line and change it.)

- [ ] **Step 2: Run the full suite + lint**

```bash
.venv/bin/pytest tests/ -q
.venv/bin/ruff check src/mthydra/ops/upgrade.py tests/unit/ops/test_upgrade.py
```

Expected: full suite green; new files ruff-clean.

- [ ] **Step 3: Commit + tag + push**

```bash
git add pyproject.toml
git commit -m "chore: bump version to 0.0.2 (spec Q: mthydra-ops upgrade)"
git tag -a v0.0.2 -m "v0.0.2 — mthydra-ops upgrade ships (spec Q)"
git push origin main
git push origin v0.0.2
```

- [ ] **Step 4: Verify the tag landed**

```bash
git ls-remote --tags origin | grep v0.0.2
```

Expected: one matching line.

- [ ] **Step 5: (Optional smoke) `mthydra-ops upgrade --dry-run`**

On a host that already has v0.0.1 installed:
```bash
sudo -u mthydra /opt/mthydra/venv/bin/mthydra-ops upgrade --dry-run
```

Expected: prints `current SHA: <sha12>; target ref: v0.0.2` then `DRY-RUN: would upgrade from <sha12> → v0.0.2` and exits 0. Real upgrade is the operator's call.

---

## Self-review notes (for the implementer)

- **Spec coverage:** Q-D1 → Task 7 (cmd_upgrade is the one command); Q-D2 → Task 7 (phases enumerated); Q-D3 → Task 2 (`_resolve_target_ref`); Q-D4 → Task 4 (`_record_prior` runs backup-now + captures generation); Q-D5 → Task 7 (auto-rollback branch on `VerifyFailed`); Q-D6 → Task 3 + Task 7 schema gate; Q-D7 (no multi-host) → out of scope, nothing to implement; Q-D8 → Task 7 (rollback-failed path exits with backup gen in error); Q-D9 → Task 1 (`_pyproject_version` is informational only).

- **Watch item — `mthydra-controller` PATH at runtime:** `_controller_bin()` (Task 4) uses `Path(sys.executable).parent / "mthydra-controller"`, matching the install.py fix from spec N. If `mthydra-ops upgrade` is run via the venv binary, this resolves correctly. If a packager later changes the layout, this is the one place to update.

- **Watch item — audit-log writes:** spec §5 listed `action=upgrade_started/completed/rolled_back` writes. Those are NOT in the plan above to keep Task 7 tractable; if you want them, add a `_audit(action, details_json)` helper after `cmd_upgrade` lands (Task 9 in a follow-up commit). The forced `backup-now` already produces its own audit row, which is the load-bearing piece.

- **Watch item — `_seed_repo` fixture:** the Task 7 test uses `git init` + commit. If the test environment forbids running `git` subprocess (sandboxed CI), monkeypatch `_current_head_sha` + `_pyproject_version` directly instead of seeding a real repo.

- **Watch item — verifying the v0.0.2 tag:** Task 8 pushes the tag to origin. The actual rollout (running `mthydra-ops upgrade` on the EC2 host) is operator-driven and not part of this plan — that's the dogfood test of the new command.
