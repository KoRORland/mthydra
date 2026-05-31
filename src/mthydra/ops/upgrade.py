"""mthydra-ops upgrade — one-command controller upgrade (spec Q)."""
from __future__ import annotations

import ast
import re
import sqlite3
import subprocess
import sys
import time
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


def _parse_schema_version_constant(schema_py: Path) -> int:
    """AST-walk schema.py for `SCHEMA_VERSION = <int>`. Avoids importing the
    new code into the running process (which would conflict with the running
    module of the same name in sys.modules)."""
    tree = ast.parse(schema_py.read_text(), filename=str(schema_py))
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if (
                isinstance(target, ast.Name)
                and target.id == "SCHEMA_VERSION"
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, int)
            ):
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


def _schema_would_migrate(src_dir: Path, db_path: str
                          ) -> tuple[bool, int, int]:
    schema_py = (Path(src_dir) / "src" / "mthydra" / "controller"
                 / "state" / "schema.py")
    target = _parse_schema_version_constant(schema_py)
    current = _current_schema_version(db_path)
    return (target > current, target, current)


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


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["systemctl", *args], capture_output=True, text=True,
    )


def _wait_for(predicate, *, timeout_s: int, poll_s: float = 1.0) -> bool:
    """Block until predicate() returns True or timeout. Returns ok."""
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
    res = subprocess.run(
        [_controller_bin(), "startup-check", "--db-path", db_path],
        capture_output=True, text=True,
    )
    if res.returncode != 0:
        raise VerifyFailed(
            f"startup-check failed (exit {res.returncode}): "
            f"{res.stderr.strip() or res.stdout.strip()}")
    res = subprocess.run(
        [_controller_bin(), "obs-heartbeat-now",
         "--db-path", db_path, "--config", config_path],
        capture_output=True, text=True,
    )
    if res.returncode != 0:
        raise VerifyFailed(
            f"obs-heartbeat-now failed (exit {res.returncode}): "
            f"{res.stderr.strip() or res.stdout.strip()}")
