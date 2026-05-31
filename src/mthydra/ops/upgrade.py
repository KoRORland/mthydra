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
