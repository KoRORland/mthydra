"""mthydra-ops upgrade — one-command controller upgrade (spec Q)."""
from __future__ import annotations

import ast
import os
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
    the freshly-checked-out source.

    Forces umask 022 in the subprocess (R-D6): if the calling shell has
    umask 077 (common after writing a credential file), pip creates the
    editable .pth + .dist-info as mode 600 root:root, which makes the
    install unreadable by the mthydra service user → ModuleNotFoundError
    at next service restart.
    """
    pip = str(Path(venv_dir) / "bin" / "pip")
    res = subprocess.run(
        [pip, "install", "-e", str(src_dir)],
        capture_output=True, text=True,
        preexec_fn=lambda: os.umask(0o022),
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


# ---------------------------------------------------------------------------
# orchestrator
# ---------------------------------------------------------------------------


def _say(msg: str) -> None:
    print(f"[mthydra-upgrade] {msg}", flush=True)


def _err(msg: str) -> None:
    print(f"[mthydra-upgrade] ERROR: {msg}", file=sys.stderr, flush=True)


def cmd_upgrade(args) -> int:  # noqa: C901 — orchestrator
    """One-command controller upgrade (spec Q).

    Exit codes:
      0 success
      2 preflight failure
      3 target-resolution failure
      4 record-prior (forced backup) failure
      5 fetch-and-checkout failure
      6 schema-migration refused without --allow-schema-migration
      7 pip install failure
      8 service stop failure
      9 verify failed (auto-rollback may have run)
    """
    src_dir = Path(args.src_dir)
    venv_dir = Path(args.venv_dir)

    # Phase 1: preflight
    _say(f"phase 1/8: preflight (src={src_dir})")
    try:
        head_sha = _current_head_sha(src_dir)
    except UpgradeError as e:
        _err(f"preflight: {e}")
        return 2
    _say(f"  current HEAD: {head_sha}")
    _say(f"  current version: {_pyproject_version(src_dir)}")

    # Phase 2: resolve target ref
    _say("phase 2/8: resolve-target")
    try:
        target_ref = _resolve_target_ref(
            ref=args.ref,
            upstream_repo=args.upstream_repo,
            github_api_url=args.github_api_url,
        )
    except Exception as e:
        _err(f"resolve-target: {e}")
        return 3
    _say(f"  target ref: {target_ref}")

    # No-op short-circuit when already at target (works for SHAs; for tags
    # we still take the safer path through fetch-and-checkout below).
    if target_ref == head_sha:
        _say("already at target ref → nothing to do")
        return 0

    # Phase 3: record-prior (forced backup + SHA snapshot)
    _say("phase 3/8: record-prior (forced backup-now)")
    try:
        prior = _record_prior(src_dir, args.db_path, args.config)
    except UpgradeError as e:
        _err(f"record-prior: {e}")
        return 4
    _say(f"  prior_sha={prior['prior_sha']} version={prior['prior_version']} "
         f"backup_generation={prior['backup_generation']}")

    # Phase 4: fetch-and-checkout
    _say(f"phase 4/8: fetch-and-checkout {target_ref}")
    try:
        _fetch_and_checkout(src_dir, target_ref)
    except UpgradeError as e:
        _err(f"fetch-and-checkout: {e}")
        return 5

    # Schema-migration gate (post-checkout: new schema.py is on disk)
    will_migrate, tgt_schema, cur_schema = _schema_would_migrate(
        src_dir, args.db_path)
    if will_migrate and not args.allow_schema_migration:
        _err(
            f"this upgrade would migrate schema {cur_schema} -> {tgt_schema}; "
            "rollback can NOT undo schema changes. Re-run with "
            "--allow-schema-migration to acknowledge.")
        _say("rolling back checkout to prior SHA (no install was performed)")
        try:
            _fetch_and_checkout(src_dir, prior["prior_sha"])
        except UpgradeError as e:
            _err(f"checkout-rollback also failed: {e}")
        return 6
    if will_migrate:
        _say(f"  schema migration acknowledged: {cur_schema} -> {tgt_schema}")

    # Phase 5: pip install
    _say("phase 5/8: pip install -e .")
    try:
        _pip_install(venv_dir, src_dir)
    except UpgradeError as e:
        _err(f"pip-install: {e}")
        return 7

    # Phase 6: stop service
    _say(f"phase 6/8: stop {args.unit}")
    try:
        _stop_service(args.unit)
    except UpgradeError as e:
        _err(f"stop-service: {e}")
        return 8

    # Phase 7: start + verify (with auto-rollback)
    _say(f"phase 7/8: start + verify {args.unit}")
    try:
        _start_and_verify(
            args.unit, args.db_path, args.config,
            verify_timeout_s=args.verify_timeout,
        )
    except VerifyFailed as e:
        _err(f"verify failed: {e}")
        if args.no_auto_rollback:
            _err("auto-rollback DISABLED (--no-auto-rollback); leaving as-is.")
            return 9
        _say(f"auto-rollback: reverting to prior_sha={prior['prior_sha']}")
        try:
            _stop_service(args.unit)
            _rollback_to(src_dir, venv_dir, prior["prior_sha"])
            _start_and_verify(
                args.unit, args.db_path, args.config,
                verify_timeout_s=args.verify_timeout,
            )
            _say("auto-rollback: prior version is back up and healthy")
        except (UpgradeError, VerifyFailed) as e2:
            _err(f"auto-rollback FAILED: {e2}")
            _err(f"recovery floor: backup generation {prior['backup_generation']}")
        return 9

    # Phase 8: summary
    _say("phase 8/8: upgrade complete")
    _say(f"  {prior['prior_version']} ({prior['prior_sha'][:12]}) "
         f"-> {_pyproject_version(src_dir)} ({target_ref})")
    return 0
