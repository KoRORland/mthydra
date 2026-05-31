"""mthydra-ops upgrade — one-command controller upgrade (spec Q)."""
from __future__ import annotations

import ast
import sqlite3
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


def _schema_would_migrate(src_dir: Path, db_path: str
                          ) -> tuple[bool, int, int]:
    schema_py = (Path(src_dir) / "src" / "mthydra" / "controller"
                 / "state" / "schema.py")
    target = _parse_schema_version_constant(schema_py)
    current = _current_schema_version(db_path)
    return (target > current, target, current)
