# EU-side RU automation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the three EU-side automations from spec P — `mthydra-ops image-prepare` (latest-release + auto-promote), `mthydra-ops agent-publish` (controller-side tarball + S3 upload + presign), and the SSH-based probe runner wheel — so the operator's RU touchpoint shrinks to "paste cloud-init, give back the IP."

**Architecture:** Three loosely-coupled subsystems landing in one plan because they jointly remove the same MVP busywork. New modules: `src/mthydra/ops/image_ops.py`, `src/mthydra/ops/agent_ops.py`, `src/mthydra/controller/probe_runner/{__init__.py,probers.py,ssh.py,wheel.py}`. Schema migration v14 → v15 adds SSH fields on `probe_vantages`. New CLI: `vantage-set-ssh`. Wired into `_cmd_serve` only when `cfg.probe.runner_enabled` (default `true` — P-D3). All file/state writes use the spec M17 atomic-write pattern.

**Tech Stack:** Python 3.12 stdlib (`urllib.request`, `tarfile`, `subprocess`, `json`, `sqlite3`, `dataclasses`), `boto3` (already a dep), `apscheduler` (already a dep), `cryptography` (already a dep — hashlib for sha256). No new third-party.

**Spec:** `doc/specs/2026-05-30-P-eu-side-ru-automation.md` (P-D1…P-D9).

**Naming contract (used across all tasks — keep consistent):**
- Image ops module: `src/mthydra/ops/image_ops.py`. Functions: `resolve_latest_tag(*, upstream_repo, github_api_url) -> str`, `default_profile_json(tag, arch) -> dict`, `cmd_image_prepare(args) -> int`.
- Agent ops module: `src/mthydra/ops/agent_ops.py`. Functions: `package_agent(source_dir: Path) -> tuple[bytes, str]` (returns `(tar_bytes, sha256_hex)`), `publish_agent(cfg, tar_bytes, sha, *, ttl_days, bucket=None) -> AgentManifest`, `read_manifest(path) -> AgentManifest | None`, `cmd_agent_publish(args) -> int`. Dataclass: `AgentManifest(url: str, sha256: str, published_at: str, expires_at: str)`.
- Manifest path constant: `AGENT_MANIFEST_PATH = Path("/var/lib/mthydra/agent.json")`.
- Probe runner package: `src/mthydra/controller/probe_runner/`.
  - `ssh.py` — `ssh_cmd(vantage_row, *cmd_parts, timeout_s=30) -> subprocess.CompletedProcess`.
  - `probers.py` — `probe_tls_fall_through(ssh_cmd_fn, box_ip, cover_sni) -> tuple[str, str]`, `probe_cover_consistency(ssh_cmd_fn, box_ip, cover_sni) -> tuple[str, str]`, `probe_surface_scan(ssh_cmd_fn, box_ip) -> tuple[str, str]`. Each returns `(status, evidence)` where status ∈ {`pass`, `soft_fail`, `hard_fail`}.
  - `wheel.py` — `ProbeRunnerWheel(db_path, interval_seconds, max_concurrent, mode='active')` with `.start() / .shutdown(wait=False)` matching the existing wheel surface (see `src/mthydra/controller/probe/audit_wheel.py` for shape).
- Schema column names on `probe_vantages` (v15): `ssh_host TEXT`, `ssh_port INTEGER`, `ssh_user TEXT`, `ssh_key_path TEXT`, `ssh_known_hosts_path TEXT` — all nullable (vantages without SSH config are skipped by the wheel).
- Controller CLI: `vantage-set-ssh <vantage_id> --host --user --key-path [--port 22] [--known-hosts <path>]` → `_cmd_vantage_set_ssh`.
- `controller.toml` `[probe]` section additions: `runner_enabled = true` (default), `runner_interval_seconds = 1800` (default), `runner_max_concurrent = 4` (default).

---

## Task 1: `resolve_latest_tag` — GitHub releases/latest resolver

**Files:**
- Create: `src/mthydra/ops/image_ops.py`
- Test: `tests/unit/ops/test_image_ops.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/ops/test_image_ops.py
from __future__ import annotations

import json
from io import BytesIO

from mthydra.ops import image_ops


class _FakeResp:
    def __init__(self, status, body):
        self.status = status
        self._body = body
    def read(self):
        return self._body
    def getcode(self):
        return self.status


def test_resolve_latest_tag_uses_releases_latest(monkeypatch):
    seen = {}
    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        return _FakeResp(200, json.dumps({"tag_name": "v2.2.8"}).encode())
    monkeypatch.setattr(image_ops.urllib.request, "urlopen", fake_urlopen)
    tag = image_ops.resolve_latest_tag(
        upstream_repo="9seconds/mtg",
        github_api_url="https://api.github.com",
    )
    assert tag == "v2.2.8"
    assert seen["url"] == "https://api.github.com/repos/9seconds/mtg/releases/latest"


def test_resolve_latest_tag_raises_on_non_200(monkeypatch):
    monkeypatch.setattr(
        image_ops.urllib.request, "urlopen",
        lambda req, timeout=None: _FakeResp(404, b'{"message":"Not Found"}'),
    )
    import pytest
    with pytest.raises(image_ops.ImageOpsError, match="404"):
        image_ops.resolve_latest_tag(upstream_repo="x/y",
                                     github_api_url="https://api.github.com")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/ops/test_image_ops.py -k resolve_latest -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mthydra.ops.image_ops'`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/mthydra/ops/image_ops.py
"""mthydra-ops image-prepare — automated image fetch/build/promote (spec P)."""
from __future__ import annotations

import json
import urllib.request


class ImageOpsError(RuntimeError):
    pass


def resolve_latest_tag(*, upstream_repo: str, github_api_url: str) -> str:
    """Query GitHub's `releases/latest` endpoint, return the `tag_name`.

    Excludes drafts + prereleases by GitHub's own semantics."""
    url = f"{github_api_url}/repos/{upstream_repo}/releases/latest"
    req = urllib.request.Request(
        url, headers={"Accept": "application/vnd.github+json"})
    resp = urllib.request.urlopen(req, timeout=30)
    status = resp.getcode()
    if status != 200:
        raise ImageOpsError(
            f"GitHub releases/latest returned {status} for {upstream_repo!r}")
    body = json.loads(resp.read())
    tag = body.get("tag_name")
    if not tag:
        raise ImageOpsError(
            f"GitHub releases/latest for {upstream_repo!r} has no tag_name")
    return str(tag)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/unit/ops/test_image_ops.py -k resolve_latest -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit + push**

```bash
git add src/mthydra/ops/image_ops.py tests/unit/ops/test_image_ops.py
git commit -m "feat(image-ops): resolve_latest_tag via GitHub releases/latest (P-D8)"
git push origin main
```

---

## Task 2: `default_profile_json` — minimal placeholder profile

**Files:**
- Modify: `src/mthydra/ops/image_ops.py`
- Test: `tests/unit/ops/test_image_ops.py`

- [ ] **Step 1: Write the failing test**

```python
# add to tests/unit/ops/test_image_ops.py
def test_default_profile_json_has_required_schema_fields():
    p = image_ops.default_profile_json("v2.2.8", "linux-amd64")
    # Required fields for spec D / D2 image_profiles row.
    assert p["image_version"] == "iv-v2.2.8"
    assert p["transport_build_hash"]      # placeholder is non-empty
    assert "tls_handshake" in p
    assert "expected_surface" in p
    assert p["expected_surface"] == [443]
    assert "baseline_latency_ms" in p
    # MVP marker so the operator (and the audit) can distinguish placeholder
    # from a real captured profile.
    assert p["notes"].startswith("MVP placeholder")
```

- [ ] **Step 2: Run, expect FAIL** — `default_profile_json` undefined.

- [ ] **Step 3: Write minimal implementation**

```python
# add to src/mthydra/ops/image_ops.py
def default_profile_json(tag: str, arch: str) -> dict:
    """Minimal placeholder profile for MVP image-prepare flows. NOT a real
    captured profile — a real one comes from running probes against a soaked
    canary box and recording the observed handshake/timing fingerprints."""
    return {
        "image_version": f"iv-{tag}",
        "transport_build_hash": f"placeholder-{tag}-{arch}",
        "tls_handshake": {
            "expected_cipher_order": [
                "TLS_AES_128_GCM_SHA256",
                "TLS_AES_256_GCM_SHA384",
                "TLS_CHACHA20_POLY1305_SHA256",
            ],
            "expected_extensions": [
                "server_name", "supported_versions",
                "key_share", "supported_groups",
            ],
        },
        "malformed_input_response": {
            "tcp_reset_within_ms": 250,
            "no_application_layer_response": True,
        },
        "expected_surface": [443],
        "baseline_latency_ms": {"p50": 50, "p95": 200},
        "notes": "MVP placeholder — replace with a real profile captured "
                 "from a soaked canary before relying on probe verdicts.",
    }
```

- [ ] **Step 4: Run test** — PASS.

- [ ] **Step 5: Commit + push**

```bash
git add src/mthydra/ops/image_ops.py tests/unit/ops/test_image_ops.py
git commit -m "feat(image-ops): default_profile_json placeholder for MVP image-prepare"
git push origin main
```

---

## Task 3: `cmd_image_prepare` wizard + CLI wiring

**Files:**
- Modify: `src/mthydra/ops/image_ops.py`, `src/mthydra/ops/main.py`
- Test: `tests/unit/ops/test_image_ops.py`, `tests/unit/ops/test_main.py`

- [ ] **Step 1: Write the failing test**

```python
# add to tests/unit/ops/test_image_ops.py
import argparse
import subprocess


def _prepare_args(tmp_path, **over):
    base = dict(
        release="latest", arch="linux-amd64", profile_json="auto",
        yes=True, non_interactive=True,
        db_path=str(tmp_path / "x.sqlite"),
        config=str(tmp_path / "c.toml"),
        upstream_repo="9seconds/mtg",
        github_api_url="https://api.github.com",
        verbose=False, quiet=True, dry_run=False,
    )
    base.update(over)
    return argparse.Namespace(**base)


def test_cmd_image_prepare_end_to_end(monkeypatch, tmp_path):
    monkeypatch.setattr(image_ops, "resolve_latest_tag",
                        lambda **kw: "v2.2.8")
    calls = []
    def fake_run(*args, check=True, capture=False, env=None):
        calls.append(list(args))
        return subprocess.CompletedProcess(args, 0, "", "")
    monkeypatch.setattr(image_ops, "_run_controller", fake_run, raising=False)
    rc = image_ops.cmd_image_prepare(_prepare_args(tmp_path))
    assert rc == 0
    subs = [a[0] for a in calls]
    assert "image-build" in subs and "image-promote" in subs
    ib = next(a for a in calls if a[0] == "image-build")
    assert "--release" in ib and "v2.2.8" in ib
    assert "--asset" in ib and "mtg-v2.2.8-linux-amd64.tar.gz" in ib
    assert "--profile-json" in ib   # auto-generated path passed


def test_cmd_image_prepare_skips_promote_without_yes(monkeypatch, tmp_path):
    monkeypatch.setattr(image_ops, "resolve_latest_tag", lambda **kw: "v1.0")
    monkeypatch.setattr("builtins.input", lambda _p: "n")
    calls = []
    monkeypatch.setattr(image_ops, "_run_controller",
        lambda *a, **k: calls.append(list(a))
        or subprocess.CompletedProcess(a, 0, "", ""), raising=False)
    rc = image_ops.cmd_image_prepare(_prepare_args(tmp_path, yes=False,
                                                   non_interactive=False))
    assert rc == 0
    assert [a[0] for a in calls] == ["image-build"]   # no promote
```

```python
# add to tests/unit/ops/test_main.py
def test_image_prepare_subcommand_parses_and_routes(monkeypatch):
    from mthydra.ops import main as m
    from mthydra.ops import image_ops
    called = {}
    monkeypatch.setattr(image_ops, "cmd_image_prepare",
                        lambda args: called.setdefault("v", 0) or 0)
    rc = m.main(["image-prepare"])
    assert rc == 0 and "v" in called
```

- [ ] **Step 2: Run, expect FAIL.**

- [ ] **Step 3: Implement.** Add to `src/mthydra/ops/image_ops.py`:

```python
import os
import subprocess
import sys
from pathlib import Path

from . import main as _main

_run_controller = _main._run_controller
_DEFAULT_DB = _main._DEFAULT_DB
_DEFAULT_CONFIG = _main._DEFAULT_CONFIG


def _say(msg: str) -> None:
    _main._say(f"image-prepare: {msg}")


def cmd_image_prepare(args) -> int:
    """Resolve latest → build → (optionally) promote, in one wizard."""
    tag = args.release
    if tag == "latest":
        _say(f"resolving latest from {args.upstream_repo}")
        try:
            tag = resolve_latest_tag(upstream_repo=args.upstream_repo,
                                     github_api_url=args.github_api_url)
        except ImageOpsError as e:
            _main._err(str(e))
            return 2
        _say(f"latest = {tag}")

    asset = f"mtg-{tag}-{args.arch}.tar.gz"
    _say(f"asset = {asset}")

    # Profile JSON: 'auto' writes a minimal placeholder to a temp path.
    if args.profile_json == "auto":
        import json as _j, tempfile
        profile = default_profile_json(tag, args.arch)
        fd, profile_path = tempfile.mkstemp(prefix="profile-", suffix=".json")
        with os.fdopen(fd, "w") as f:
            _j.dump(profile, f, indent=2, sort_keys=True)
        _say(f"placeholder profile written to {profile_path}")
    else:
        profile_path = args.profile_json

    # image-build.
    try:
        _run_controller(
            "image-build", "--release", tag, "--asset", asset,
            "--profile-json", profile_path,
            "--db-path", args.db_path, "--config", args.config,
            check=True,
        )
    except subprocess.CalledProcessError as e:
        _main._err(f"image-build failed (exit {e.returncode}): see above")
        return e.returncode

    # Promote (gated unless --yes; safety per P-D7).
    if not args.yes:
        if args.non_interactive:
            _say(f"non-interactive without --yes — image iv-{tag} stays candidate")
            return 0
        ans = input(f"Promote iv-{tag}? [y/N] ").strip().lower()
        if ans not in ("y", "yes"):
            _say(f"promotion declined — iv-{tag} stays candidate")
            return 0
    try:
        _run_controller(
            "image-promote", f"iv-{tag}",
            "--evidence", f"mthydra-ops image-prepare auto-promote {tag}",
            "--db-path", args.db_path, "--config", args.config,
            check=True,
        )
    except subprocess.CalledProcessError as e:
        _main._err(f"image-promote failed (exit {e.returncode}): see above")
        return e.returncode
    _say(f"iv-{tag} promoted")
    return 0
```

In `src/mthydra/ops/main.py`, after the existing `_dispatch_ru_image_cycle` lazy wrapper, add:

```python
def _dispatch_image_prepare(args) -> int:
    from . import image_ops
    return image_ops.cmd_image_prepare(args)
```

Add to `_DISPATCH`:
```python
    "image-prepare": _dispatch_image_prepare,
```

Add the subparser in `build_parser()` after the existing image-build-template parser:
```python
    ip = sub.add_parser("image-prepare",
                        help="resolve latest mtg release → build → (optionally) promote")
    ip.add_argument("--release", default="latest")
    ip.add_argument("--arch", default="linux-amd64")
    ip.add_argument("--profile-json", default="auto",
                    help="'auto' = generate a minimal placeholder; otherwise a path")
    ip.add_argument("--yes", action="store_true",
                    help="auto-promote after build (skip the [y/N] prompt)")
    ip.add_argument("--non-interactive", action="store_true")
    ip.add_argument("--db-path", default=_DEFAULT_DB)
    ip.add_argument("--config", default=_DEFAULT_CONFIG)
    ip.add_argument("--upstream-repo", default="9seconds/mtg")
    ip.add_argument("--github-api-url", default="https://api.github.com")
    ip.add_argument("--verbose", action="store_true")
    ip.add_argument("--quiet", action="store_true")
    ip.add_argument("--dry-run", action="store_true")
```

- [ ] **Step 4: Run tests** — PASS (image_ops + test_main).

- [ ] **Step 5: Commit + push**

```bash
git add src/mthydra/ops/image_ops.py src/mthydra/ops/main.py \
        tests/unit/ops/test_image_ops.py tests/unit/ops/test_main.py
git commit -m "feat(image-prepare): wizard subcommand (latest → build → promote gate)"
git push origin main
```

---

## Task 4: `package_agent` — tar `mthydra/ru_agent` + compute sha256

**Files:**
- Create: `src/mthydra/ops/agent_ops.py`
- Test: `tests/unit/ops/test_agent_ops.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/ops/test_agent_ops.py
from __future__ import annotations

import hashlib
import tarfile
from io import BytesIO
from pathlib import Path

from mthydra.ops import agent_ops


def test_package_agent_includes_ru_agent_and_init(tmp_path):
    # Build a minimal source tree shaped like the real one.
    src = tmp_path / "src"
    (src / "mthydra" / "ru_agent").mkdir(parents=True)
    (src / "mthydra" / "__init__.py").write_text("# mthydra root pkg\n")
    (src / "mthydra" / "ru_agent" / "__init__.py").write_text("")
    (src / "mthydra" / "ru_agent" / "__main__.py").write_text("# agent main\n")
    # Junk that MUST be excluded:
    (src / "mthydra" / "ru_agent" / "__pycache__").mkdir()
    (src / "mthydra" / "ru_agent" / "__pycache__" / "x.pyc").write_bytes(b"x")
    (src / "mthydra" / "ru_agent" / "stale.pyc").write_bytes(b"y")

    tar_bytes, sha = agent_ops.package_agent(src)
    assert len(sha) == 64
    assert sha == hashlib.sha256(tar_bytes).hexdigest()
    with tarfile.open(fileobj=BytesIO(tar_bytes), mode="r:gz") as tf:
        names = sorted(m.name for m in tf.getmembers())
    assert "mthydra/__init__.py" in names
    assert "mthydra/ru_agent/__init__.py" in names
    assert "mthydra/ru_agent/__main__.py" in names
    # Excluded:
    assert not any("__pycache__" in n for n in names)
    assert not any(n.endswith(".pyc") for n in names)


def test_package_agent_is_deterministic(tmp_path):
    src = tmp_path / "src"
    (src / "mthydra" / "ru_agent").mkdir(parents=True)
    (src / "mthydra" / "__init__.py").write_text("hi\n")
    (src / "mthydra" / "ru_agent" / "__init__.py").write_text("")
    t1, s1 = agent_ops.package_agent(src)
    t2, s2 = agent_ops.package_agent(src)
    assert s1 == s2  # mtime-independent
```

- [ ] **Step 2: Run, expect FAIL.**

- [ ] **Step 3: Write minimal implementation**

```python
# src/mthydra/ops/agent_ops.py
"""mthydra-ops agent-publish — package ru_agent + upload to S3 + presign (spec P)."""
from __future__ import annotations

import hashlib
import io
import tarfile
from dataclasses import dataclass
from pathlib import Path


_EXCLUDE_DIRS = {"__pycache__"}
_EXCLUDE_SUFFIXES = (".pyc", ".pyo")


def package_agent(source_dir: Path | str) -> tuple[bytes, str]:
    """Tar mthydra/__init__.py + mthydra/ru_agent/* (excluding caches),
    return (tar_bytes, sha256_hex). Deterministic across runs: file mtimes
    are zeroed, members are added in sorted-name order, no compression
    metadata leaks (gzip mtime fixed at 0)."""
    src = Path(source_dir)
    members: list[Path] = []
    root = src / "mthydra"
    if not root.is_dir():
        raise RuntimeError(f"agent source missing: {root}")
    init = root / "__init__.py"
    if init.is_file():
        members.append(init)
    for path in (root / "ru_agent").rglob("*"):
        if not path.is_file():
            continue
        if any(part in _EXCLUDE_DIRS for part in path.parts):
            continue
        if path.suffix in _EXCLUDE_SUFFIXES:
            continue
        members.append(path)
    members.sort(key=lambda p: p.relative_to(src).as_posix())

    buf = io.BytesIO()
    # mtime=0 in GzipFile to keep output deterministic across runs.
    import gzip
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz, \
            tarfile.open(fileobj=gz, mode="w") as tf:
        for p in members:
            info = tf.gettarinfo(str(p), arcname=p.relative_to(src).as_posix())
            info.mtime = 0
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            with open(p, "rb") as fh:
                tf.addfile(info, fh)
    data = buf.getvalue()
    return data, hashlib.sha256(data).hexdigest()
```

- [ ] **Step 4: Run, expect PASS.**

- [ ] **Step 5: Commit + push**

```bash
git add src/mthydra/ops/agent_ops.py tests/unit/ops/test_agent_ops.py
git commit -m "feat(agent-ops): package_agent — deterministic ru_agent tarball + sha"
git push origin main
```

---

## Task 5: `publish_agent` — S3 upload, presign, atomic manifest write

**Files:**
- Modify: `src/mthydra/ops/agent_ops.py`
- Test: `tests/unit/ops/test_agent_ops.py`

- [ ] **Step 1: Write the failing test**

```python
# add to tests/unit/ops/test_agent_ops.py
import json
from datetime import datetime, timedelta, UTC


class _FakeS3Client:
    def __init__(self):
        self.put_calls = []
        self.presign_calls = []
    def put_object(self, **kw):
        self.put_calls.append(kw)
    def generate_presigned_url(self, op, Params, ExpiresIn):
        self.presign_calls.append((op, Params, ExpiresIn))
        return f"https://fake.example/{Params['Key']}?sig=stub"


class _FakeCfg:
    """Minimal cfg object — only the fields agent_ops touches."""
    class backup:
        endpoint = "https://s3.eu-west-1.amazonaws.com"
        bucket = "mthydra-prod"
        region = "eu-west-1"


def test_publish_agent_uploads_and_writes_manifest(monkeypatch, tmp_path):
    fake = _FakeS3Client()
    monkeypatch.setattr(agent_ops, "_make_s3_client", lambda cfg: fake)
    monkeypatch.setattr(agent_ops, "_get_s3_credentials", lambda cfg: ("AKIA", "SECRET"))
    monkeypatch.setattr(agent_ops, "AGENT_MANIFEST_PATH", tmp_path / "agent.json")

    m = agent_ops.publish_agent(_FakeCfg(), tar_bytes=b"hello",
                                sha="0123456789abcdef" * 4, ttl_days=7)
    assert m.url.startswith("https://fake.example/agent/")
    assert m.sha256 == "0123456789abcdef" * 4
    assert "agent/mthydra-ru-agent-0123456789ab.tar.gz" in fake.put_calls[0]["Key"]
    assert fake.presign_calls[0][2] == 7 * 86400
    on_disk = json.loads((tmp_path / "agent.json").read_text())
    assert on_disk["sha256"] == m.sha256


def test_publish_agent_skips_when_manifest_fresh_and_sha_matches(
        monkeypatch, tmp_path):
    sha = "abc" + "0" * 61
    manifest_path = tmp_path / "agent.json"
    now = datetime.now(UTC)
    manifest_path.write_text(json.dumps({
        "url": "https://existing.example/agent.tar.gz",
        "sha256": sha,
        "published_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "expires_at": (now + timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }))
    monkeypatch.setattr(agent_ops, "AGENT_MANIFEST_PATH", manifest_path)
    fake = _FakeS3Client()
    monkeypatch.setattr(agent_ops, "_make_s3_client",
                        lambda cfg: (_ for _ in ()).throw(
                            AssertionError("should not call S3")))
    m = agent_ops.publish_agent(_FakeCfg(), tar_bytes=b"x", sha=sha, ttl_days=7)
    assert m.url == "https://existing.example/agent.tar.gz"
    assert fake.put_calls == []
```

- [ ] **Step 2: Run, expect FAIL** — `publish_agent` / `AgentManifest` undefined.

- [ ] **Step 3: Write minimal implementation**

```python
# add to src/mthydra/ops/agent_ops.py
import contextlib
import json
import os
import tempfile
from datetime import UTC, datetime, timedelta

import boto3


AGENT_MANIFEST_PATH = Path("/var/lib/mthydra/agent.json")
_REPUBLISH_HEADROOM_HOURS = 24


@dataclass(frozen=True)
class AgentManifest:
    url: str
    sha256: str
    published_at: str
    expires_at: str


def read_manifest(path=None) -> AgentManifest | None:
    path = Path(path or AGENT_MANIFEST_PATH)
    if not path.exists():
        return None
    raw = json.loads(path.read_text())
    return AgentManifest(**raw)


def _get_s3_credentials(cfg) -> tuple[str, str]:
    # Pulled from the controller's existing tokens table. The credential is
    # stored as "KEY_ID:SECRET" — split on the first colon.
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.tokens import get_provider_credential
    with connect(cfg._db_path) as conn:
        cred = get_provider_credential(conn, "b2")
    key_id, _, secret = cred.partition(":")
    if not secret:
        raise RuntimeError("provider credential malformed (expected KEY:SECRET)")
    return key_id, secret


def _make_s3_client(cfg):
    key_id, secret = _get_s3_credentials(cfg)
    return boto3.client(
        "s3",
        endpoint_url=cfg.backup.endpoint,
        aws_access_key_id=key_id,
        aws_secret_access_key=secret,
        region_name=cfg.backup.region,
    )


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp",
                               dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        dir_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        except OSError:
            pass
        finally:
            os.close(dir_fd)
    except Exception:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


def publish_agent(cfg, tar_bytes: bytes, sha: str, *,
                  ttl_days: int = 7, bucket: str | None = None) -> AgentManifest:
    """Upload tar_bytes to s3://<bucket>/agent/mthydra-ru-agent-<sha12>.tar.gz
    (idempotent — content-addressed), presign, write manifest. Returns the
    manifest. If a fresh manifest with matching sha already exists, returns
    that without re-uploading."""
    existing = read_manifest()
    if existing and existing.sha256 == sha:
        expires = datetime.strptime(existing.expires_at,
                                    "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        if expires - datetime.now(UTC) > timedelta(hours=_REPUBLISH_HEADROOM_HOURS):
            return existing

    bucket = bucket or cfg.backup.bucket
    key = f"agent/mthydra-ru-agent-{sha[:12]}.tar.gz"
    client = _make_s3_client(cfg)
    client.put_object(Bucket=bucket, Key=key, Body=tar_bytes)
    url = client.generate_presigned_url(
        "get_object", Params={"Bucket": bucket, "Key": key},
        ExpiresIn=ttl_days * 86400,
    )
    now = datetime.now(UTC)
    manifest = AgentManifest(
        url=url, sha256=sha,
        published_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        expires_at=(now + timedelta(days=ttl_days)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    _atomic_write_json(AGENT_MANIFEST_PATH, {
        "url": manifest.url, "sha256": manifest.sha256,
        "published_at": manifest.published_at, "expires_at": manifest.expires_at,
    })
    return manifest
```

Note: `cfg._db_path` — `_get_s3_credentials` reads from `cfg` so the test can pass a fake. In real production, `cmd_agent_publish` will set `cfg._db_path = args.db_path` before calling. The plan's Task 6 handles that.

- [ ] **Step 4: Run, expect PASS.**

- [ ] **Step 5: Commit + push**

```bash
git add src/mthydra/ops/agent_ops.py tests/unit/ops/test_agent_ops.py
git commit -m "feat(agent-ops): publish_agent — S3 upload + presign + atomic manifest (P-D2, P-D4)"
git push origin main
```

---

## Task 6: `cmd_agent_publish` + CLI wiring

**Files:**
- Modify: `src/mthydra/ops/agent_ops.py`, `src/mthydra/ops/main.py`
- Test: `tests/unit/ops/test_agent_ops.py`, `tests/unit/ops/test_main.py`

- [ ] **Step 1: Write the failing test**

```python
# add to tests/unit/ops/test_agent_ops.py
import argparse


def test_cmd_agent_publish_tars_uploads_and_prints_manifest(monkeypatch, tmp_path):
    # Set up a minimal source tree.
    src = tmp_path / "src"
    (src / "mthydra" / "ru_agent").mkdir(parents=True)
    (src / "mthydra" / "__init__.py").write_text("")
    (src / "mthydra" / "ru_agent" / "__init__.py").write_text("")

    monkeypatch.setattr(agent_ops, "AGENT_MANIFEST_PATH",
                        tmp_path / "agent.json")
    # Stub the cfg loader to avoid touching the controller config layer.
    class _FakeCfg(_FakeCfg):
        pass
    monkeypatch.setattr(agent_ops, "_load_cfg",
                        lambda db, config: _FakeCfg())
    captured = {}
    monkeypatch.setattr(agent_ops, "publish_agent",
        lambda cfg, tar_bytes, sha, *, ttl_days, bucket=None:
            captured.setdefault("sha", sha) or agent_ops.AgentManifest(
                url="https://fake/x", sha256=sha,
                published_at="2026-05-30T00:00:00Z",
                expires_at="2026-06-06T00:00:00Z"))

    args = argparse.Namespace(
        ttl_days=7, source_dir=str(src),
        db_path=str(tmp_path / "x.sqlite"),
        config=str(tmp_path / "c.toml"),
        verbose=False, quiet=True,
    )
    rc = agent_ops.cmd_agent_publish(args)
    assert rc == 0
    assert captured["sha"]   # publish_agent was called
```

```python
# add to tests/unit/ops/test_main.py
def test_agent_publish_subcommand_parses_and_routes(monkeypatch):
    from mthydra.ops import main as m
    from mthydra.ops import agent_ops
    called = {}
    monkeypatch.setattr(agent_ops, "cmd_agent_publish",
                        lambda args: called.setdefault("v", 0) or 0)
    rc = m.main(["agent-publish"])
    assert rc == 0 and "v" in called
```

- [ ] **Step 2: Run, expect FAIL.**

- [ ] **Step 3: Implement.** Add to `src/mthydra/ops/agent_ops.py`:

```python
def _load_cfg(db_path: str, config: str):
    """Load the controller config + stash db_path for _get_s3_credentials."""
    from mthydra.controller.config import load_config
    cfg = load_config(Path(config))
    cfg._db_path = db_path
    return cfg


def cmd_agent_publish(args) -> int:
    cfg = _load_cfg(args.db_path, args.config)
    tar_bytes, sha = package_agent(args.source_dir)
    manifest = publish_agent(cfg, tar_bytes, sha, ttl_days=args.ttl_days)
    print(json.dumps({
        "url": manifest.url, "sha256": manifest.sha256,
        "published_at": manifest.published_at,
        "expires_at": manifest.expires_at,
    }, indent=2, sort_keys=True))
    return 0
```

Add to `src/mthydra/ops/main.py`:

```python
def _dispatch_agent_publish(args) -> int:
    from . import agent_ops
    return agent_ops.cmd_agent_publish(args)
```

`_DISPATCH` += `"agent-publish": _dispatch_agent_publish,`.

In `build_parser()`:
```python
    ap = sub.add_parser("agent-publish",
                        help="tar mthydra/ru_agent + upload to S3 + presign + write agent.json")
    ap.add_argument("--ttl-days", type=int, default=7)
    ap.add_argument("--source-dir", default="/opt/mthydra/src/src",
                    help="root containing mthydra/ru_agent/ and mthydra/__init__.py")
    ap.add_argument("--db-path", default=_DEFAULT_DB)
    ap.add_argument("--config", default=_DEFAULT_CONFIG)
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--quiet", action="store_true")
```

- [ ] **Step 4: Run, expect PASS.**

- [ ] **Step 5: Commit + push**

```bash
git add src/mthydra/ops/agent_ops.py src/mthydra/ops/main.py \
        tests/unit/ops/test_agent_ops.py tests/unit/ops/test_main.py
git commit -m "feat(agent-publish): wizard subcommand wiring"
git push origin main
```

---

## Task 7: `ru-bringup` auto-fetches agent URL from manifest

**Files:**
- Modify: `src/mthydra/ops/ru_bringup.py`, `src/mthydra/ops/main.py` (subparser: make `--agent-source-url` / `--agent-source-sha256` optional)
- Test: `tests/unit/ops/test_ru_bringup.py`

- [ ] **Step 1: Write the failing test**

```python
# add to tests/unit/ops/test_ru_bringup.py
def test_cmd_ru_bringup_auto_fetches_agent_from_manifest(monkeypatch, tmp_path):
    """When --agent-source-url not provided, ru-bringup reads agent.json."""
    from mthydra.ops import agent_ops
    manifest = agent_ops.AgentManifest(
        url="https://auto/agent.tar.gz", sha256="deadbeef" * 8,
        published_at="2026-05-30T00:00:00Z",
        expires_at="2026-06-30T00:00:00Z",
    )
    monkeypatch.setattr(ru_bringup, "_resolve_agent",
                        lambda args, cfg=None: (manifest.url, manifest.sha256))
    # Stub everything else as in test_cmd_ru_bringup_happy_path:
    def fake_run(*args, check=True, capture=False, env=None):
        sub = args[0]
        if sub == "provision-seed":
            return subprocess.CompletedProcess(args, 0, "",
                "provision-seed: created box_id=b-1\n")
        if sub == "ru-box-list":
            return subprocess.CompletedProcess(args, 0,
                json.dumps([{"box_id": "b-1", "state": "provisioning",
                             "sni": "x"}]), "")
        return subprocess.CompletedProcess(args, 0, "", "")
    monkeypatch.setattr(ru_bringup, "_run_controller", fake_run, raising=False)
    monkeypatch.setattr(ru_bringup, "_run_controller_capture_both",
                        fake_run, raising=False)
    monkeypatch.setattr(ru_bringup, "wait_for_reachable", lambda *a, **kw: True)

    args = argparse.Namespace(
        provider="timeweb", region="ru-msk-1", canary=False,
        agent_source_url=None, agent_source_sha256=None,
        descriptor_refresh_url="https://desc",
        cloud_init_out=str(tmp_path / "ci.yaml"),
        public_ip="1.2.3.4", box_id=None, reach_timeout=1,
        non_interactive=True, verbose=False, quiet=True, dry_run=False,
        config=None, db_path=str(tmp_path / "x.sqlite"),
    )
    rc = ru_bringup.cmd_ru_bringup(args)
    assert rc == 0
```

- [ ] **Step 2: Run, expect FAIL** — `_resolve_agent` undefined; current code requires `--agent-source-url`.

- [ ] **Step 3: Implement.** Add to `src/mthydra/ops/ru_bringup.py`:

```python
def _resolve_agent(args, cfg=None) -> tuple[str, str]:
    """Return (url, sha256). If args.agent_source_url+sha given, use them.
    Otherwise read /var/lib/mthydra/agent.json; auto-publish if missing or
    expiry within 24h. Caller passes cfg (or None to load on demand)."""
    if args.agent_source_url and args.agent_source_sha256:
        return args.agent_source_url, args.agent_source_sha256

    from . import agent_ops
    from datetime import UTC, datetime, timedelta
    manifest = agent_ops.read_manifest()
    need_publish = manifest is None
    if manifest is not None:
        try:
            exp = datetime.strptime(manifest.expires_at, "%Y-%m-%dT%H:%M:%SZ"
                                    ).replace(tzinfo=UTC)
        except ValueError:
            need_publish = True
        else:
            if exp - datetime.now(UTC) < timedelta(hours=24):
                need_publish = True
    if need_publish:
        if cfg is None:
            cfg = agent_ops._load_cfg(args.db_path, args.config or
                                      "/etc/mthydra/controller.toml")
        tar_bytes, sha = agent_ops.package_agent("/opt/mthydra/src/src")
        manifest = agent_ops.publish_agent(cfg, tar_bytes, sha, ttl_days=7)
    return manifest.url, manifest.sha256
```

Modify `cmd_ru_bringup` (the mint phase) to call `_resolve_agent` instead of using `args.agent_source_url` directly:

```python
    if args.box_id:
        ...
    else:
        agent_url, agent_sha = _resolve_agent(args)
        _say(f"agent: {agent_url[:60]}…  sha={agent_sha[:12]}…")
        box_id = mint_seed(
            args.provider, args.region, canary=args.canary,
            agent_source_url=agent_url,
            agent_source_sha256=agent_sha,
            descriptor_refresh_url=args.descriptor_refresh_url,
            cloud_init_out=args.cloud_init_out,
        )
```

In `src/mthydra/ops/main.py`, change the ru-bringup subparser to make `--agent-source-url` + `--agent-source-sha256` OPTIONAL (drop `required=True`).

- [ ] **Step 4: Run, expect PASS.** Also run the full ru_bringup test file to confirm existing tests (which pass explicit `agent_source_url`) still pass.

- [ ] **Step 5: Commit + push**

```bash
git add src/mthydra/ops/ru_bringup.py src/mthydra/ops/main.py \
        tests/unit/ops/test_ru_bringup.py
git commit -m "feat(ru-bringup): auto-resolve agent URL from agent.json (P-D4)"
git push origin main
```

---

## Task 8: Schema migration v14 → v15 (SSH columns on probe_vantages)

**Files:**
- Modify: `src/mthydra/controller/state/schema.py` (`SCHEMA_VERSION`, add `migrate_v14_to_v15`, register in the migration dispatcher)
- Test: `tests/unit/controller/state/test_schema_migration_v15.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/controller/state/test_schema_migration_v15.py
import sqlite3

from mthydra.controller.state import schema


def test_v14_to_v15_adds_ssh_columns():
    conn = sqlite3.connect(":memory:")
    schema.initialise(conn)   # creates everything at SCHEMA_VERSION
    cols = {r[1] for r in conn.execute("PRAGMA table_info(probe_vantages)")}
    for c in ("ssh_host", "ssh_port", "ssh_user", "ssh_key_path",
              "ssh_known_hosts_path"):
        assert c in cols


def test_v15_migration_is_idempotent():
    conn = sqlite3.connect(":memory:")
    schema.initialise(conn)
    schema.migrate_v14_to_v15(conn)   # second run, columns already present
    schema.migrate_v14_to_v15(conn)
    row = conn.execute(
        "SELECT version FROM schema_version WHERE rowid=1"
    ).fetchone()
    assert row[0] == schema.SCHEMA_VERSION
```

- [ ] **Step 2: Run, expect FAIL** (columns missing or migration absent).

- [ ] **Step 3: Implement.** In `src/mthydra/controller/state/schema.py`:

- Bump `SCHEMA_VERSION = 15`.
- Add the columns to the inline `CREATE TABLE probe_vantages` so fresh installs get them.
- Add `migrate_v14_to_v15`:

```python
def migrate_v14_to_v15(conn: sqlite3.Connection) -> None:
    """Idempotent v14 → v15: add SSH config columns to probe_vantages."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(probe_vantages)")}
    for col, decl in (
        ("ssh_host", "TEXT"),
        ("ssh_port", "INTEGER"),
        ("ssh_user", "TEXT"),
        ("ssh_key_path", "TEXT"),
        ("ssh_known_hosts_path", "TEXT"),
    ):
        if col not in cols:
            conn.execute(f"ALTER TABLE probe_vantages ADD COLUMN {col} {decl}")
    conn.execute(
        "UPDATE schema_version SET version=?, applied_at=? WHERE rowid=1",
        (15, _now()),
    )
    conn.commit()
```

- Register it in the migration dispatcher. Find the chain that's already there (search for `migrate_v13_to_v14` or whatever the latest is in the file) and add a `migrate_v14_to_v15` call after it.

- [ ] **Step 4: Run, expect PASS.** Also run the full controller test suite to ensure other migration / invariant tests still pass: `.venv/bin/pytest tests/unit/controller -q`.

- [ ] **Step 5: Commit + push**

```bash
git add src/mthydra/controller/state/schema.py \
        tests/unit/controller/state/test_schema_migration_v15.py
git commit -m "feat(schema v15): SSH config columns on probe_vantages"
git push origin main
```

---

## Task 9: `vantage-set-ssh` controller subcommand

**Files:**
- Modify: `src/mthydra/controller/state/probe_vantages.py` (add `set_ssh(conn, vantage_id, host, port, user, key_path, known_hosts_path)`), `src/mthydra/controller/cli.py` (subparser + dispatch)
- Test: `tests/unit/controller/test_cli.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/unit/controller/test_cli.py (in the area where other
# vantage tests live; if there's a separate file like test_vantage.py, use
# that — match the existing test layout)
def test_vantage_set_ssh_persists_columns(tmp_db_path):
    from mthydra.controller import cli
    cli.run(["init", "--db-path", str(tmp_db_path),
             "--age-recipient", "age1qqp0000000000000000000000000000000000000000000000000q",
             "--role", "active"])
    cli.run(["vantage-add", "ru-msk-1",
             "--label", "ru-msk-1", "--source-kind", "cloud-cis",
             "--db-path", str(tmp_db_path)])
    rc = cli.run([
        "vantage-set-ssh", "ru-msk-1",
        "--host", "203.0.113.5", "--user", "probe", "--port", "2222",
        "--key-path", "/var/lib/mthydra/ssh/ru-msk-1.key",
        "--known-hosts", "/var/lib/mthydra/ssh/known_hosts",
        "--db-path", str(tmp_db_path),
    ])
    assert rc == 0
    import sqlite3
    conn = sqlite3.connect(str(tmp_db_path))
    row = conn.execute(
        "SELECT ssh_host, ssh_port, ssh_user, ssh_key_path, ssh_known_hosts_path"
        " FROM probe_vantages WHERE vantage_id=?", ("ru-msk-1",)
    ).fetchone()
    assert row == ("203.0.113.5", 2222, "probe",
                   "/var/lib/mthydra/ssh/ru-msk-1.key",
                   "/var/lib/mthydra/ssh/known_hosts")
```

- [ ] **Step 2: Run, expect FAIL.**

- [ ] **Step 3: Implement.**

In `src/mthydra/controller/state/probe_vantages.py`:

```python
def set_ssh(conn, vantage_id: str, *, host: str, port: int, user: str,
            key_path: str, known_hosts_path: str) -> None:
    cur = conn.execute(
        "UPDATE probe_vantages SET ssh_host=?, ssh_port=?, ssh_user=?,"
        " ssh_key_path=?, ssh_known_hosts_path=? WHERE vantage_id=?",
        (host, port, user, key_path, known_hosts_path, vantage_id),
    )
    if cur.rowcount == 0:
        raise ValueError(f"no probe_vantages row for {vantage_id!r}")
    conn.commit()
```

In `src/mthydra/controller/cli.py`, add the subparser (search for the existing `vantage-add` subparser to match style):

```python
    vss = sub.add_parser("vantage-set-ssh",
                          help="configure SSH access so the probe runner can reach this vantage")
    vss.add_argument("vantage_id")
    vss.add_argument("--host", required=True)
    vss.add_argument("--user", required=True)
    vss.add_argument("--key-path", required=True, dest="key_path")
    vss.add_argument("--port", type=int, default=22)
    vss.add_argument("--known-hosts", default="/var/lib/mthydra/ssh/known_hosts",
                      dest="known_hosts")
    vss.add_argument("--db-path", default=DEFAULT_DB)
```

And the dispatch handler:

```python
def _cmd_vantage_set_ssh(args) -> int:
    from mthydra.controller.state.db import connect
    from mthydra.controller.state import probe_vantages as _pv
    with connect(args.db_path) as conn:
        try:
            _pv.set_ssh(conn, args.vantage_id,
                        host=args.host, port=args.port, user=args.user,
                        key_path=args.key_path,
                        known_hosts_path=args.known_hosts)
        except ValueError as e:
            print(f"vantage-set-ssh: {e}", file=sys.stderr)
            return 2
    print(f"vantage-set-ssh: {args.vantage_id} updated")
    return 0
```

Wire into the `if args.cmd == "vantage-set-ssh": return _cmd_vantage_set_ssh(args)` block.

- [ ] **Step 4: Run, expect PASS.**

- [ ] **Step 5: Commit + push**

```bash
git add src/mthydra/controller/state/probe_vantages.py \
        src/mthydra/controller/cli.py tests/unit/controller/test_cli.py
git commit -m "feat(vantage): vantage-set-ssh CLI + state.set_ssh()"
git push origin main
```

---

## Task 10: `ssh.ssh_cmd` helper

**Files:**
- Create: `src/mthydra/controller/probe_runner/__init__.py` (empty)
- Create: `src/mthydra/controller/probe_runner/ssh.py`
- Test: `tests/unit/controller/probe_runner/test_ssh.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/controller/probe_runner/__init__.py — empty
# tests/unit/controller/probe_runner/test_ssh.py
from __future__ import annotations

import subprocess

from mthydra.controller.probe_runner import ssh as ssh_mod


def test_ssh_cmd_builds_correct_argv(monkeypatch):
    """Confirm the ssh argv shape — no shell, no string-concat, all options
    explicit and safe."""
    seen = {}
    def fake_run(argv, **kw):
        seen["argv"] = argv
        seen["kw"] = kw
        return subprocess.CompletedProcess(argv, 0, "ok", "")
    monkeypatch.setattr(ssh_mod.subprocess, "run", fake_run)
    vantage = {
        "ssh_host": "203.0.113.5", "ssh_port": 2222, "ssh_user": "probe",
        "ssh_key_path": "/etc/mthydra/ssh/k", "ssh_known_hosts_path": "/etc/mthydra/ssh/kh",
    }
    res = ssh_mod.ssh_cmd(vantage, "openssl", "s_client",
                          "-connect", "1.2.3.4:443")
    assert res.returncode == 0
    argv = seen["argv"]
    assert argv[0] == "/usr/bin/ssh"
    assert "-i" in argv and "/etc/mthydra/ssh/k" in argv
    assert "-p" in argv and "2222" in argv
    assert "StrictHostKeyChecking=yes" in " ".join(argv)
    assert "UserKnownHostsFile=/etc/mthydra/ssh/kh" in " ".join(argv)
    assert "BatchMode=yes" in " ".join(argv)
    # Target is "probe@203.0.113.5".
    assert "probe@203.0.113.5" in argv
    # Command parts are passed AFTER the host, as separate argv entries:
    assert argv[-4:] == ["openssl", "s_client", "-connect", "1.2.3.4:443"]


def test_ssh_cmd_raises_if_ssh_not_configured():
    import pytest
    with pytest.raises(ssh_mod.SshNotConfigured):
        ssh_mod.ssh_cmd({"ssh_host": None, "ssh_port": 22,
                         "ssh_user": "x", "ssh_key_path": "/k",
                         "ssh_known_hosts_path": "/kh"}, "true")
```

- [ ] **Step 2: Run, expect FAIL.**

- [ ] **Step 3: Implement.**

```python
# src/mthydra/controller/probe_runner/ssh.py
"""SSH transport for the probe runner wheel (spec P-D5).

Stdlib subprocess to /usr/bin/ssh. No shell. No paramiko. Keys only.
"""
from __future__ import annotations

import subprocess
from typing import Mapping


class SshNotConfigured(RuntimeError):
    """Raised when a probe_vantages row is missing required SSH fields."""


def ssh_cmd(vantage: Mapping, *cmd_parts: str, timeout_s: int = 30
            ) -> subprocess.CompletedProcess:
    """Run `cmd_parts` on the vantage via SSH; return the CompletedProcess.

    `vantage` is a dict-like with keys ssh_host / ssh_port / ssh_user /
    ssh_key_path / ssh_known_hosts_path. Capture both streams. No shell
    interpretation — cmd_parts is forwarded as separate argv tokens to
    OpenSSH, which preserves quoting end-to-end."""
    if not vantage.get("ssh_host") or not vantage.get("ssh_user") \
            or not vantage.get("ssh_key_path"):
        raise SshNotConfigured(
            "vantage missing ssh_host / ssh_user / ssh_key_path")
    target = f"{vantage['ssh_user']}@{vantage['ssh_host']}"
    argv = [
        "/usr/bin/ssh",
        "-i", vantage["ssh_key_path"],
        "-p", str(vantage.get("ssh_port") or 22),
        "-o", "StrictHostKeyChecking=yes",
        "-o", f"UserKnownHostsFile={vantage.get('ssh_known_hosts_path') or '/dev/null'}",
        "-o", "ConnectTimeout=10",
        "-o", "BatchMode=yes",
        target, "--", *cmd_parts,
    ]
    return subprocess.run(
        argv, capture_output=True, text=True, timeout=timeout_s,
    )
```

- [ ] **Step 4: Run, expect PASS.**

- [ ] **Step 5: Commit + push**

```bash
git add src/mthydra/controller/probe_runner/__init__.py \
        src/mthydra/controller/probe_runner/ssh.py \
        tests/unit/controller/probe_runner/__init__.py \
        tests/unit/controller/probe_runner/test_ssh.py
git commit -m "feat(probe-runner): ssh_cmd helper — stdlib ssh, no shell (P-D5)"
git push origin main
```

---

## Task 11: Three MVP probers (`tls_fall_through`, `cover_consistency`, `surface_scan`)

**Files:**
- Create: `src/mthydra/controller/probe_runner/probers.py`
- Test: `tests/unit/controller/probe_runner/test_probers.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/controller/probe_runner/test_probers.py
from __future__ import annotations

import subprocess

from mthydra.controller.probe_runner import probers


def _stub_ssh(returncode, stdout, stderr=""):
    def fn(*cmd_parts, timeout_s=30):
        return subprocess.CompletedProcess(
            ("ssh",) + tuple(cmd_parts), returncode, stdout, stderr)
    return fn


# --- tls_fall_through ---

_OPENSSL_OK = """\
CONNECTED(00000003)
depth=2 C = US, O = ...
verify return:1
---
Certificate chain
 0 s:CN = www.cloudflare.com
   i:C = US, O = Cloudflare, Inc., CN = Cloudflare Inc ECC CA-3
---
Server certificate
-----BEGIN CERTIFICATE-----
...
-----END CERTIFICATE-----
subject=/CN=www.cloudflare.com
issuer=/C=US/O=Cloudflare, Inc./CN=Cloudflare Inc ECC CA-3
---
SSL handshake has read 3201 bytes and written 388 bytes
Verification: OK
Verify return code: 0 (ok)
---
"""

_OPENSSL_BAD = "CONNECTED\nverify error:num=20:unable to get local issuer cert\nVerify return code: 20\n"


def test_tls_fall_through_pass_on_verified_handshake():
    status, evidence = probers.probe_tls_fall_through(
        _stub_ssh(0, _OPENSSL_OK), "1.2.3.4", "www.cloudflare.com")
    assert status == "pass"
    assert "Verify return code: 0" in evidence


def test_tls_fall_through_hard_fail_on_verify_error():
    status, evidence = probers.probe_tls_fall_through(
        _stub_ssh(0, _OPENSSL_BAD), "1.2.3.4", "www.cloudflare.com")
    assert status == "hard_fail"


def test_tls_fall_through_soft_fail_on_ssh_failure():
    status, _ = probers.probe_tls_fall_through(
        _stub_ssh(255, "", "ssh: connect timeout"),
        "1.2.3.4", "www.cloudflare.com")
    assert status == "soft_fail"     # transient — vantage problem, not box


# --- surface_scan ---

_NC_443_ONLY = """\
nc: connect to 1.2.3.4 80 (tcp) failed: Connection refused
Ncat: Connected to 1.2.3.4:443.
Ncat: 0 bytes sent, 0 bytes received in 0.01 seconds.
nc: connect to 1.2.3.4 8080 (tcp) failed: Connection refused
nc: connect to 1.2.3.4 22 (tcp) failed: Connection refused
nc: connect to 1.2.3.4 53 (tcp) failed: Connection refused
"""

_NC_EXTRA_PORT = _NC_443_ONLY.replace(
    "nc: connect to 1.2.3.4 22 (tcp) failed: Connection refused",
    "Ncat: Connected to 1.2.3.4:22.",
)


def test_surface_scan_pass_on_443_only():
    status, _ = probers.probe_surface_scan(
        _stub_ssh(0, _NC_443_ONLY), "1.2.3.4")
    assert status == "pass"


def test_surface_scan_hard_fail_on_extra_open_port():
    status, evidence = probers.probe_surface_scan(
        _stub_ssh(0, _NC_EXTRA_PORT), "1.2.3.4")
    assert status == "hard_fail"
    assert "22" in evidence


# --- cover_consistency ---
# Compares two openssl runs: against the box, and against the cover. Both must
# produce the same issuer.

def test_cover_consistency_pass_when_issuers_match():
    box_out = "issuer=/C=US/O=Cloudflare, Inc./CN=Cloudflare Inc ECC CA-3\n"
    cover_out = box_out
    calls = []
    def stub(*cmd_parts, timeout_s=30):
        calls.append(list(cmd_parts))
        out = box_out if "1.2.3.4:443" in " ".join(cmd_parts) else cover_out
        return subprocess.CompletedProcess(cmd_parts, 0, out, "")
    status, _ = probers.probe_cover_consistency(stub, "1.2.3.4",
                                                "www.cloudflare.com")
    assert status == "pass"


def test_cover_consistency_hard_fail_on_mismatch():
    def stub(*cmd_parts, timeout_s=30):
        if "1.2.3.4:443" in " ".join(cmd_parts):
            return subprocess.CompletedProcess(cmd_parts, 0,
                "issuer=/CN=SuspiciousCA\n", "")
        return subprocess.CompletedProcess(cmd_parts, 0,
            "issuer=/C=US/O=Cloudflare, Inc./CN=Cloudflare Inc ECC CA-3\n", "")
    status, _ = probers.probe_cover_consistency(stub, "1.2.3.4",
                                                "www.cloudflare.com")
    assert status == "hard_fail"
```

- [ ] **Step 2: Run, expect FAIL.**

- [ ] **Step 3: Implement.**

```python
# src/mthydra/controller/probe_runner/probers.py
"""MVP probers — all run via an ssh_cmd_fn injected by the wheel.

Each returns (status, evidence). Status ∈ {pass, soft_fail, hard_fail}.

soft_fail = transient/vantage-side failure (SSH timeout, connect refused at
the vantage's NIC). hard_fail = the box itself looks wrong. pass = clean.
"""
from __future__ import annotations

import re
from collections.abc import Callable


def _ssh_or_softfail(ssh_cmd_fn: Callable, *cmd_parts: str
                     ) -> tuple[str, int, str] | tuple[None, int, str]:
    """Run an SSH command. Returns ('ok', rc, output) on success or
    ('softfail', rc, err) if SSH itself failed (rc 255 / nonzero with empty
    stdout). Wheel uses this to short-circuit probe-record with soft_fail."""
    try:
        res = ssh_cmd_fn(*cmd_parts)
    except Exception as e:    # transport-layer failure
        return ("softfail", -1, f"ssh transport error: {e}")
    out = (res.stdout or "") + (res.stderr or "")
    if res.returncode == 255 or (res.returncode != 0 and not res.stdout):
        return ("softfail", res.returncode, out)
    return ("ok", res.returncode, out)


def probe_tls_fall_through(ssh_cmd_fn: Callable, box_ip: str, cover_sni: str
                           ) -> tuple[str, str]:
    """openssl s_client to the box; pass iff TLS verification succeeds against
    the cover's chain (i.e. the box presents the cover-domain cert cleanly)."""
    status, _rc, out = _ssh_or_softfail(
        ssh_cmd_fn,
        "sh", "-c",
        f"openssl s_client -connect {box_ip}:443 -servername {cover_sni}"
        f" </dev/null 2>&1 | head -60",
    )
    if status == "softfail":
        return ("soft_fail", out)
    if "Verify return code: 0" in out:
        return ("pass", out)
    return ("hard_fail", out)


_ISSUER_RE = re.compile(r"^issuer=(.+)$", re.MULTILINE)


def probe_cover_consistency(ssh_cmd_fn: Callable, box_ip: str, cover_sni: str
                            ) -> tuple[str, str]:
    """Fetch issuer from both <box>:443 and <cover>:443 via the vantage.
    Pass iff issuers match."""
    box_status, _rc, box_out = _ssh_or_softfail(
        ssh_cmd_fn, "sh", "-c",
        f"openssl s_client -connect {box_ip}:443 -servername {cover_sni}"
        f" </dev/null 2>&1 | head -60",
    )
    if box_status == "softfail":
        return ("soft_fail", box_out)
    cover_status, _rc, cover_out = _ssh_or_softfail(
        ssh_cmd_fn, "sh", "-c",
        f"openssl s_client -connect {cover_sni}:443 -servername {cover_sni}"
        f" </dev/null 2>&1 | head -60",
    )
    if cover_status == "softfail":
        return ("soft_fail", cover_out)
    box_iss = _ISSUER_RE.search(box_out)
    cov_iss = _ISSUER_RE.search(cover_out)
    if not (box_iss and cov_iss):
        return ("hard_fail",
                f"could not parse issuer; box={box_out[:200]} "
                f"cover={cover_out[:200]}")
    if box_iss.group(1).strip() == cov_iss.group(1).strip():
        return ("pass",
                f"box issuer == cover issuer: {box_iss.group(1).strip()}")
    return ("hard_fail",
            f"issuer mismatch: box={box_iss.group(1).strip()!r} "
            f"cover={cov_iss.group(1).strip()!r}")


_NCAT_OPEN_RE = re.compile(r"(?:Ncat: Connected to )(\d+\.\d+\.\d+\.\d+:)(\d+)")
_BARE_NC_OPEN_RE = re.compile(r"^([^\s]+) (\d+) port .* open\b", re.MULTILINE)
_SCAN_PORTS = ("80", "443", "8080", "22", "53")


def probe_surface_scan(ssh_cmd_fn: Callable, box_ip: str) -> tuple[str, str]:
    """nc -zv against {80, 443, 8080, 22, 53}; pass iff only 443 answered."""
    status, _rc, out = _ssh_or_softfail(
        ssh_cmd_fn, "sh", "-c",
        f"for p in {' '.join(_SCAN_PORTS)}; do nc -zv -w 3 {box_ip} $p"
        f" 2>&1; done",
    )
    if status == "softfail":
        return ("soft_fail", out)
    open_ports: set[str] = set()
    for m in _NCAT_OPEN_RE.finditer(out):
        open_ports.add(m.group(2))
    for m in _BARE_NC_OPEN_RE.finditer(out):
        open_ports.add(m.group(2))
    if open_ports == {"443"}:
        return ("pass", f"only 443 open ({sorted(open_ports)})")
    extras = sorted(open_ports - {"443"})
    return ("hard_fail",
            f"unexpected open ports: {extras} (full: {sorted(open_ports)})")
```

- [ ] **Step 4: Run, expect PASS** (7 tests).

- [ ] **Step 5: Commit + push**

```bash
git add src/mthydra/controller/probe_runner/probers.py \
        tests/unit/controller/probe_runner/test_probers.py
git commit -m "feat(probe-runner): three MVP probers (tls/cover/surface) — P-D6"
git push origin main
```

---

## Task 12: `ProbeRunnerWheel` — APScheduler tick over (box × vantage)

**Files:**
- Create: `src/mthydra/controller/probe_runner/wheel.py`
- Test: `tests/unit/controller/probe_runner/test_wheel.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/controller/probe_runner/test_wheel.py
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import pytest

from mthydra.controller.probe_runner import wheel as wheel_mod
from mthydra.controller.state import schema


@pytest.fixture
def seeded_db(tmp_path):
    """A DB with one live box + one active vantage with SSH configured."""
    db = tmp_path / "state.sqlite"
    conn = sqlite3.connect(str(db))
    schema.initialise(conn)
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    # box (minimal — adapt to your ru_boxes schema if columns are required):
    conn.execute(
        "INSERT INTO ru_boxes (box_id, provider, region, public_ip, sni,"
        " state, image_version, created_at) VALUES (?, ?, ?, ?, ?, 'live', ?, ?)",
        ("b-1", "timeweb", "ru-msk-1", "203.0.113.10",
         "www.cloudflare.com", "iv-v2.2.8", now))
    # vantage:
    conn.execute(
        "INSERT INTO probe_vantages (vantage_id, label, source_kind, state,"
        " added_at, attested_at, ssh_host, ssh_port, ssh_user, ssh_key_path,"
        " ssh_known_hosts_path) VALUES (?, ?, 'cloud-cis', 'active', ?, ?,"
        " ?, ?, ?, ?, ?)",
        ("ru-msk-1", "ru-msk-1", now, now,
         "203.0.113.5", 22, "probe", "/k", "/kh"))
    conn.commit(); conn.close()
    return db


def test_wheel_tick_dispatches_probers_and_ingests(monkeypatch, seeded_db):
    """One (box, vantage) pair → three probers run → three probe-record
    subprocess calls happen with the right (box, vantage, check_type, status)."""
    # Fake the SSH + probers to deterministic results.
    monkeypatch.setattr(wheel_mod, "ssh_cmd",
                        lambda v, *c, **kw: None)  # unused — probers stubbed
    monkeypatch.setattr(wheel_mod.probers, "probe_tls_fall_through",
                        lambda fn, ip, sni: ("pass", "tls evidence"))
    monkeypatch.setattr(wheel_mod.probers, "probe_cover_consistency",
                        lambda fn, ip, sni: ("pass", "cover evidence"))
    monkeypatch.setattr(wheel_mod.probers, "probe_surface_scan",
                        lambda fn, ip: ("pass", "surface evidence"))
    recorded = []
    monkeypatch.setattr(wheel_mod, "_record_probe",
        lambda **kw: recorded.append(kw))

    w = wheel_mod.ProbeRunnerWheel(
        db_path=str(seeded_db), interval_seconds=1800, max_concurrent=2,
        mode="offline",   # do not start the scheduler thread
    )
    w.tick()
    assert len(recorded) == 3
    checks = sorted(r["check_type"] for r in recorded)
    assert checks == ["cover_domain_consistency", "surface_scan",
                      "tls_fall_through"]
    assert all(r["box_id"] == "b-1" for r in recorded)
    assert all(r["vantage_id"] == "ru-msk-1" for r in recorded)
    assert all(r["status"] == "pass" for r in recorded)


def test_wheel_tick_skips_vantage_without_ssh(monkeypatch, seeded_db):
    """A vantage row with NULL ssh_host must not be probed against."""
    conn = sqlite3.connect(str(seeded_db))
    conn.execute("UPDATE probe_vantages SET ssh_host=NULL WHERE vantage_id=?",
                 ("ru-msk-1",))
    conn.commit(); conn.close()
    recorded = []
    monkeypatch.setattr(wheel_mod, "_record_probe",
                        lambda **kw: recorded.append(kw))
    w = wheel_mod.ProbeRunnerWheel(db_path=str(seeded_db),
                                   interval_seconds=1800,
                                   max_concurrent=2, mode="offline")
    w.tick()
    assert recorded == []
```

- [ ] **Step 2: Run, expect FAIL.**

- [ ] **Step 3: Implement.**

```python
# src/mthydra/controller/probe_runner/wheel.py
"""Probe runner wheel — periodically run the three MVP probers (P-D6) for
every (live box × active vantage with SSH configured) pair, ingesting via
probe-record."""
from __future__ import annotations

import logging
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

from apscheduler.executors.pool import ThreadPoolExecutor as APSPoolExec
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from mthydra.controller.probe_runner import probers
from mthydra.controller.probe_runner.ssh import SshNotConfigured, ssh_cmd
from mthydra.controller.state.db import connect

_log = logging.getLogger(__name__)
# Resolve relative to sys.executable so root shells without /opt/mthydra/venv/bin
# on PATH still find the binary (mirrors the spec-N install.py fix).
_PROBE_BIN = str(Path(sys.executable).parent / "mthydra-controller")


def _record_probe(*, db_path: str, box_id: str, vantage_id: str,
                  check_type: str, status: str, evidence: str,
                  cycle_at: str) -> None:
    """Wraps `mthydra-controller probe-record` subprocess call. Separate
    function so tests can monkeypatch."""
    subprocess.run([
        _PROBE_BIN, "probe-record",
        "--box-id", box_id, "--vantage", vantage_id,
        "--check", check_type, "--status", status,
        "--cycle-at", cycle_at,
        "--evidence", evidence[:4096],   # cap to avoid pathological evidence
        "--db-path", db_path,
    ], check=False, capture_output=True, text=True)


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _list_pairs(conn: sqlite3.Connection) -> list[dict]:
    """Return list of dicts: one per (live box × active SSH-configured
    vantage) pair, with the box's public_ip + sni and the vantage's SSH cfg."""
    conn.row_factory = sqlite3.Row
    boxes = conn.execute(
        "SELECT box_id, public_ip, sni FROM ru_boxes"
        " WHERE state='live' AND public_ip IS NOT NULL"
    ).fetchall()
    vantages = conn.execute(
        "SELECT vantage_id, ssh_host, ssh_port, ssh_user, ssh_key_path,"
        " ssh_known_hosts_path FROM probe_vantages"
        " WHERE state='active' AND ssh_host IS NOT NULL"
    ).fetchall()
    out = []
    for b in boxes:
        for v in vantages:
            out.append({
                "box_id": b["box_id"], "box_ip": b["public_ip"],
                "cover_sni": b["sni"],
                "vantage_id": v["vantage_id"],
                "vantage_ssh": dict(v),
            })
    return out


def _probe_one(pair: dict, db_path: str) -> None:
    cycle_at = _now_iso()
    v = pair["vantage_ssh"]
    def _ssh(*cmd_parts, timeout_s=30):
        return ssh_cmd(v, *cmd_parts, timeout_s=timeout_s)
    try:
        for check_type, fn in (
            ("tls_fall_through",
             lambda: probers.probe_tls_fall_through(_ssh, pair["box_ip"],
                                                    pair["cover_sni"])),
            ("cover_domain_consistency",
             lambda: probers.probe_cover_consistency(_ssh, pair["box_ip"],
                                                     pair["cover_sni"])),
            ("surface_scan",
             lambda: probers.probe_surface_scan(_ssh, pair["box_ip"])),
        ):
            try:
                status, evidence = fn()
            except SshNotConfigured:
                return    # vantage SSH was unconfigured between list + probe
            except Exception as e:    # last-resort safety net
                status, evidence = "soft_fail", f"prober raised: {e}"
            _record_probe(
                db_path=db_path, box_id=pair["box_id"],
                vantage_id=pair["vantage_id"],
                check_type=check_type, status=status, evidence=evidence,
                cycle_at=cycle_at)
    except Exception:
        _log.exception("probe runner: pair %r threw uncaught", pair)


class ProbeRunnerWheel:
    """Spec P probe runner. Pattern matches src/mthydra/controller/probe/audit_wheel.py
    — a small class with start() / shutdown() / tick(), serve attaches it."""

    def __init__(self, db_path: str, interval_seconds: int,
                 max_concurrent: int, mode: str = "active") -> None:
        self.db_path = db_path
        self.interval_seconds = interval_seconds
        self.max_concurrent = max_concurrent
        self.mode = mode
        self._scheduler: BackgroundScheduler | None = None

    def tick(self) -> None:
        with connect(self.db_path) as conn:
            pairs = _list_pairs(conn)
        if not pairs:
            return
        with ThreadPoolExecutor(max_workers=self.max_concurrent) as pool:
            for p in pairs:
                pool.submit(_probe_one, p, self.db_path)

    def start(self) -> None:
        if self.mode == "offline":
            return
        self._scheduler = BackgroundScheduler(
            executors={"default": APSPoolExec(max_workers=1)})
        self._scheduler.add_job(
            self.tick, IntervalTrigger(seconds=self.interval_seconds),
            id="probe-runner", coalesce=True, max_instances=1)
        self._scheduler.start()

    def shutdown(self, wait: bool = False) -> None:
        if self._scheduler is not None:
            self._scheduler.shutdown(wait=wait)
            self._scheduler = None
```

- [ ] **Step 4: Run, expect PASS.** Run also `pytest tests/unit/controller -q` to confirm nothing else regressed.

- [ ] **Step 5: Commit + push**

```bash
git add src/mthydra/controller/probe_runner/wheel.py \
        tests/unit/controller/probe_runner/test_wheel.py
git commit -m "feat(probe-runner): ProbeRunnerWheel — tick over (box×vantage) pairs (P-D9)"
git push origin main
```

---

## Task 13: `[probe]` config additions + wire wheel into `serve`

**Files:**
- Modify: `src/mthydra/controller/config.py` (add `runner_enabled`, `runner_interval_seconds`, `runner_max_concurrent` to the `ProbeConfig` dataclass + parser)
- Modify: `src/mthydra/controller/cli.py::_cmd_serve` (instantiate + arm the wheel when `runner_enabled`)
- Test: `tests/unit/controller/test_config.py` (or wherever the probe-config tests live) + a small `_cmd_serve` wiring test

- [ ] **Step 1: Write the failing test**

```python
# add to tests/unit/controller/test_config.py
def test_probe_config_defaults_runner_enabled_true(tmp_path):
    from mthydra.controller.config import load_config
    cfg_path = tmp_path / "controller.toml"
    # Minimal config — relies on defaults for [probe].
    cfg_path.write_text(_MINIMAL_CONFIG)   # use whatever this file's existing
                                           # fixture is for a minimal config
    cfg = load_config(cfg_path)
    assert cfg.probe.runner_enabled is True
    assert cfg.probe.runner_interval_seconds == 1800
    assert cfg.probe.runner_max_concurrent == 4
```

(If `test_config.py` doesn't have a `_MINIMAL_CONFIG` fixture, define one inline in the test from the existing valid controller.toml shape — copy from another passing test in the same file.)

- [ ] **Step 2: Run, expect FAIL.**

- [ ] **Step 3: Implement.**

In `src/mthydra/controller/config.py`, find the `ProbeConfig` dataclass and add fields:

```python
@dataclass
class ProbeConfig:
    # … existing fields …
    runner_enabled: bool = True
    runner_interval_seconds: int = 1800
    runner_max_concurrent: int = 4
```

In its parser (where existing [probe] fields are read), add:

```python
        runner_enabled=bool(sec.get("runner_enabled", True)),
        runner_interval_seconds=_require_positive(
            "probe.runner_interval_seconds",
            sec.get("runner_interval_seconds", 1800), positive=True),
        runner_max_concurrent=_require_positive(
            "probe.runner_max_concurrent",
            sec.get("runner_max_concurrent", 4), positive=True),
```

In `src/mthydra/controller/cli.py::_cmd_serve`, after the existing wheel instantiations and before `scheduler.start()` (find the existing `probe_wheel = ProbeAuditWheel(...)` block for placement), add:

```python
    if cfg.probe.runner_enabled and mode != "offline":
        from mthydra.controller.probe_runner.wheel import ProbeRunnerWheel
        probe_runner = ProbeRunnerWheel(
            db_path=args.db_path,
            interval_seconds=cfg.probe.runner_interval_seconds,
            max_concurrent=cfg.probe.runner_max_concurrent,
            mode=mode,
        )
        probe_runner.start()
        # Register for graceful shutdown alongside the other wheels — find the
        # existing shutdown registration list and append:
        _shutdown_list.append(probe_runner)
```

(If the existing serve uses a different pattern for shutdown — e.g. a `try/finally` block with explicit `wheel.shutdown(wait=False)` calls — add `probe_runner.shutdown(wait=False)` there to match.)

- [ ] **Step 4: Run, expect PASS.** Also `pytest tests/ -q` for the full suite.

- [ ] **Step 5: Commit + push**

```bash
git add src/mthydra/controller/config.py src/mthydra/controller/cli.py \
        tests/unit/controller/test_config.py
git commit -m "feat(probe-runner): wire into serve via cfg.probe.runner_enabled (P-D3)"
git push origin main
```

---

## Task 14: Smoke target + full-suite gate + quickstart §7 rewrite

**Files:**
- Modify: `Makefile` (new `smoke-eu-automation` target)
- Modify: `doc/quickstart-mvp.md` §7 (replace the broken manual flow with the new one-command flow)
- Verification only

- [ ] **Step 1: Add `smoke-eu-automation` to `Makefile`.**

Append:

```makefile

smoke-eu-automation:
	@echo "--- mthydra EU-side RU automation smoke procedure (spec P) ---"
	@echo "1. On the EU controller host as the mthydra user:"
	@echo "     mthydra-ops image-prepare --yes        # latest mtg → built → promoted"
	@echo "     mthydra-ops agent-publish              # tar + S3 upload + presign → /var/lib/mthydra/agent.json"
	@echo "2. For each vantage:"
	@echo "     mthydra-controller vantage-set-ssh <id> --host <ip> --user probe --key-path /var/lib/mthydra/ssh/<id>.key"
	@echo "3. Confirm the probe runner is ticking (within 30 min):"
	@echo "     mthydra-controller obs-status --json | jq '.obligations_healthy[] | select(.obligation_id==\"probe_coverage_proven\")'"
	@echo "4. Bring up a box with no extra flags:"
	@echo "     mthydra-ops ru-bringup --provider timeweb --region ru-msk-1 \\"
	@echo "         --descriptor-refresh-url <b2>"
	@echo "5. Probe coverage should stay green automatically going forward."
```

Add `smoke-eu-automation` to the `.PHONY` line at the top.

- [ ] **Step 2: Quickstart §7 rewrite.**

In `doc/quickstart-mvp.md`, replace the entire `# Part 7 — First RU box (20 min)` section with the new one-command flow. Use the existing prose style. New text:

```markdown
# Part 7 — First RU box (10 min)

Spec P removed the previous manual binary-hosting + agent-packaging steps —
the EU controller does all of that itself now.

### 7.1 Register and promote the mtg image (one command)

On the EU host as the mthydra user:

```bash
mthydra-ops image-prepare --yes
```

This resolves the latest `9seconds/mtg` release on GitHub, picks the
`linux-amd64` asset, builds + uploads to your S3 bucket, generates a minimal
placeholder profile, and promotes the image. After this, `image-current`
shows a promoted image.

### 7.2 Publish the ru-agent tarball (one command)

```bash
mthydra-ops agent-publish
```

This packages `mthydra/ru_agent` from your installed source, uploads it to
your S3 bucket (under `agent/`), presigns the URL, and writes
`/var/lib/mthydra/agent.json`. `ru-bringup` reads that automatically; you
don't need to copy the URL anywhere.

### 7.3 Bring up the RU box (one command)

```bash
mthydra-ops ru-bringup \
    --provider timeweb --region ru-msk-1 \
    --descriptor-refresh-url <DESCRIPTOR_REFRESH_URL>
```

(See the earlier guide for how to presign the descriptor-refresh URL — it
stays a fixed S3 object that `serve` overwrites whenever a new descriptor
gets signed.)

Wizard mints the seed, writes the cloud-init bundle, prints the path, prompts
for the public IP after you paste the bundle into the TimeWeb console.
After you give it the IP it verifies `:443` reachability and marks the box
live.
```

- [ ] **Step 3: Run the full-suite gate.**

```bash
.venv/bin/pytest tests/ -q
```

Expected: full suite green; touched-files lint at parent baseline; coverage on the new modules ≥ 75% (orchestrator-style).

- [ ] **Step 4: Per-touched-file lint delta.**

For each of: `src/mthydra/ops/image_ops.py`, `src/mthydra/ops/agent_ops.py`,
`src/mthydra/ops/ru_bringup.py`, `src/mthydra/ops/main.py`,
`src/mthydra/controller/probe_runner/{ssh,probers,wheel}.py`,
`src/mthydra/controller/cli.py`, `src/mthydra/controller/config.py`,
`src/mthydra/controller/state/schema.py`,
`src/mthydra/controller/state/probe_vantages.py`,
and matching test files — confirm `.venv/bin/ruff check <file>` count
matches the parent commit's count (no NEW errors).

- [ ] **Step 5: Commit + push**

```bash
git add Makefile doc/quickstart-mvp.md
git commit -m "docs(quickstart): rewrite §7 around spec P automation; add smoke-eu-automation"
git push origin main
```

---

## Self-review notes (for the implementer)

- **Spec coverage:** P-D1 → Tasks 1-13 jointly; P-D2 → Tasks 5/6; P-D3 → Task 13; P-D4 → Tasks 5/7; P-D5 → Task 10; P-D6 → Task 11; P-D7 → Task 3 (`--yes` gate); P-D8 → Task 1; P-D9 → Tasks 3 (sync wizard) / 12-13 (wheel).
- **Watch item — image-build asset filename convention:** Task 3 constructs `mtg-{tag}-{arch}.tar.gz`. Verify against the assets you saw at https://github.com/9seconds/mtg/releases — the linux-amd64 asset is exactly `mtg-2.2.8-linux-amd64.tar.gz`. If a future release changes the naming, `--arch` is operator-overridable.
- **Watch item — `_load_cfg`:** Task 6's `_load_cfg` stashes `cfg._db_path` as an attribute on the cfg object. If `load_config` returns a frozen dataclass that rejects `__setattr__`, switch to a small wrapper class or thread `db_path` separately through `_get_s3_credentials`. Verify before implementing Task 6.
- **Watch item — `_cmd_serve` shutdown registration:** Task 13 assumes there's an existing list-like structure where wheels register themselves for shutdown. If the existing code uses ad-hoc `try/finally` with explicit `wheel.shutdown()` calls, add `probe_runner.shutdown(wait=False)` to that block instead.
- **Watch item — `mthydra-controller` PATH at probe-record time:** `_record_probe` calls `mthydra-controller` by bare name (relying on PATH). The probe runner wheel runs inside `serve`, which IS invoked via the venv binary, so `/opt/mthydra/venv/bin` is the parent of `sys.executable`. Mirror the install.py fix from spec N: `_PROBE_BIN = str(Path(sys.executable).parent / "mthydra-controller")`.
