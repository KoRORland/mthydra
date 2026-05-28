# RU bring-up + image-cycle wizards Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship two `mthydra-ops` subcommands — `ru-bringup` (per-box: mint provision-seed → wait for `:443` TLS reachability → mark-live) and `ru-image-cycle` (release-wide: `image-build` → N × `ru-bringup --canary` → operator-paced soak wait → operator-confirmed `image-promote`, with Ctrl-C-safe resume) — to remove the runbook §3 hand-orchestration burden.

**Architecture:** One new module `src/mthydra/ops/ru_bringup.py` with small testable helpers (`mint_seed`, `mark_live`, `box_info`, `wait_for_reachable`, `wait_for_soak`, `CycleState`, `parse_cohort`) and two wizard entry functions (`cmd_ru_bringup`, `cmd_ru_image_cycle`). Sequential wizard style (not the install.py Phase/Runner — these flows are linear and operator-interactive). Subprocess to `mthydra-controller` via the existing `main._run_controller` (secrets via child env, never argv). Resume state file at `/var/lib/mthydra/ru-cycle/<release>.json`. Lazy dispatch wrappers in `main.py` to avoid the circular import (`ru_bringup` imports `main`).

**Tech Stack:** Python 3.12 stdlib (`socket`, `ssl`, `json`, `argparse`, `dataclasses`, `pathlib`, `time`), pytest with monkeypatching, no new third-party deps.

**Spec:** `doc/specs/2026-05-28-O-ru-bringup-and-image-cycle.md` (decisions O-D1…O-D9).

**Naming contract (used across all tasks — keep consistent):**
- Module: `src/mthydra/ops/ru_bringup.py`. Tests: `tests/unit/ops/test_ru_bringup.py`.
- Public entries: `cmd_ru_bringup(args) -> int`, `cmd_ru_image_cycle(args) -> int`.
- Helpers: `mint_seed(provider, region, *, canary, agent_source_url, agent_source_sha256, descriptor_refresh_url, cloud_init_out) -> str` (returns `box_id`), `mark_live(box_id, public_ip) -> None`, `box_info(box_id) -> dict | None`, `wait_for_reachable(host, port, sni, *, timeout_s, poll_s=10, on_progress=None) -> bool`, `wait_for_soak(image_version, *, poll_interval_s, on_progress, state_writer) -> SoakResult`, `compose_evidence(state, vantages_used, started_at, ended_at) -> str`.
- Dataclasses: `SoakResult(passed: bool, reasons: list[str], duration_s: int)`, `CanaryTarget(provider: str, region: str)`, `CycleState(release, image_version, profile_path, image_built, canaries: list[dict], started_at)` with `save(path)` / `load(path)` (classmethod).
- Constants: `CYCLE_STATE_DIR = Path("/var/lib/mthydra/ru-cycle")`.
- main.py wiring: `_dispatch_ru_bringup`, `_dispatch_ru_image_cycle` (lazy `from . import ru_bringup`), subparsers `ru-bringup` + `ru-image-cycle`.

---

## Task 1: `wait_for_reachable` — TLS handshake liveness check

**Files:**
- Create: `src/mthydra/ops/ru_bringup.py`
- Test: `tests/unit/ops/test_ru_bringup.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/ops/test_ru_bringup.py
from __future__ import annotations

import socket
import ssl
import pytest

from mthydra.ops import ru_bringup


def test_wait_for_reachable_returns_true_on_handshake(monkeypatch):
    # Mock socket.create_connection → fake socket; mock ssl context → handshake OK.
    class _FakeTLS:
        def do_handshake(self): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
    class _FakeCtx:
        check_hostname = True
        verify_mode = ssl.CERT_REQUIRED
        def wrap_socket(self, sock, server_hostname=None): return _FakeTLS()
    class _FakeSock:
        def __enter__(self): return self
        def __exit__(self, *a): pass
    monkeypatch.setattr(ru_bringup.socket, "create_connection",
                        lambda addr, timeout=None: _FakeSock())
    monkeypatch.setattr(ru_bringup.ssl, "create_default_context", lambda: _FakeCtx())
    assert ru_bringup.wait_for_reachable("1.2.3.4", 443, "sni.example",
                                         timeout_s=1, poll_s=0) is True


def test_wait_for_reachable_returns_false_on_timeout(monkeypatch):
    def boom(addr, timeout=None):
        raise OSError("refused")
    monkeypatch.setattr(ru_bringup.socket, "create_connection", boom)
    # Advance fake clock past the deadline immediately.
    times = iter([0.0, 0.5, 2.0])  # start, after first attempt, past deadline=1.0
    monkeypatch.setattr(ru_bringup.time, "monotonic", lambda: next(times))
    monkeypatch.setattr(ru_bringup.time, "sleep", lambda s: None)
    progress = []
    assert ru_bringup.wait_for_reachable("1.2.3.4", 443, "sni",
                                         timeout_s=1, poll_s=0,
                                         on_progress=progress.append) is False
    assert progress  # called at least once with the error
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/ops/test_ru_bringup.py -k reachable -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mthydra.ops.ru_bringup'`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/mthydra/ops/ru_bringup.py
"""mthydra-ops ru-bringup / ru-image-cycle — RU node automation wizards.

See doc/specs/2026-05-28-O-ru-bringup-and-image-cycle.md.
"""
from __future__ import annotations

import socket
import ssl
import time
from typing import Callable


def wait_for_reachable(host: str, port: int, sni: str, *,
                       timeout_s: int, poll_s: int = 10,
                       on_progress: Callable[[Exception], None] | None = None,
                       ) -> bool:
    """TCP+TLS handshake liveness check (no cert validation — Fake-TLS box).

    Returns True on the first successful handshake; False after timeout_s.
    `on_progress` is called with the exception on each failed attempt.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=5) as sock:
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE   # Fake-TLS — liveness only (O-D4)
                with ctx.wrap_socket(sock, server_hostname=sni) as tls:
                    tls.do_handshake()
                    return True
        except (OSError, ssl.SSLError) as e:
            if on_progress is not None:
                on_progress(e)
            time.sleep(poll_s)
    return False
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/unit/ops/test_ru_bringup.py -k reachable -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add src/mthydra/ops/ru_bringup.py tests/unit/ops/test_ru_bringup.py
git commit -m "feat(ru-bringup): wait_for_reachable TLS liveness check (O-D4)"
```

---

## Task 2: `mint_seed`, `mark_live`, `box_info` — controller wrappers

**Files:**
- Modify: `src/mthydra/ops/ru_bringup.py`
- Test: `tests/unit/ops/test_ru_bringup.py`

- [ ] **Step 1: Write the failing test**

```python
# add to tests/unit/ops/test_ru_bringup.py
import json
import subprocess


def _fake_run_factory(stdout_map=None, stderr_map=None, default_rc=0):
    """stdout_map / stderr_map: dict keyed by the first controller subcommand."""
    stdout_map = stdout_map or {}
    stderr_map = stderr_map or {}
    calls = []
    def fake_run(*args, check=True, capture=False, env=None):
        calls.append(list(args))
        sub = args[0] if args else ""
        return subprocess.CompletedProcess(
            args, default_rc,
            stdout_map.get(sub, ""), stderr_map.get(sub, ""),
        )
    return fake_run, calls


def test_mint_seed_returns_box_id_from_stderr(monkeypatch):
    fake_run, calls = _fake_run_factory(stderr_map={
        "provision-seed": "provision-seed: created box_id=b-abc123\n",
    })
    monkeypatch.setattr(ru_bringup, "_run_controller", fake_run, raising=False)
    box_id = ru_bringup.mint_seed(
        "selectel", "ru-msk-1",
        canary=True,
        agent_source_url="https://b2/agent.tar.gz",
        agent_source_sha256="deadbeef",
        descriptor_refresh_url="https://b2/desc",
        cloud_init_out="/tmp/x.yaml",
    )
    assert box_id == "b-abc123"
    argv = calls[0]
    assert argv[0] == "provision-seed"
    assert "--canary" in argv
    assert "selectel" in argv and "ru-msk-1" in argv
    assert "--cloud-init-out" in argv and "/tmp/x.yaml" in argv


def test_mark_live_invokes_controller(monkeypatch):
    fake_run, calls = _fake_run_factory()
    monkeypatch.setattr(ru_bringup, "_run_controller", fake_run, raising=False)
    ru_bringup.mark_live("b-abc", "1.2.3.4")
    assert calls[0][0] == "ru-box-mark-live"
    assert "b-abc" in calls[0] and "--public-ip" in calls[0] and "1.2.3.4" in calls[0]


def test_box_info_parses_ru_box_list_json(monkeypatch):
    rows = [{"box_id": "b-abc", "state": "provisioning", "sni": "cover.example"}]
    fake_run, _ = _fake_run_factory(stdout_map={"ru-box-list": json.dumps(rows)})
    monkeypatch.setattr(ru_bringup, "_run_controller", fake_run, raising=False)
    info = ru_bringup.box_info("b-abc")
    assert info["state"] == "provisioning"
    assert info["sni"] == "cover.example"


def test_box_info_returns_none_when_missing(monkeypatch):
    fake_run, _ = _fake_run_factory(stdout_map={"ru-box-list": "[]"})
    monkeypatch.setattr(ru_bringup, "_run_controller", fake_run, raising=False)
    assert ru_bringup.box_info("b-missing") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/ops/test_ru_bringup.py -k "mint_seed or mark_live or box_info" -v`
Expected: FAIL — functions undefined.

- [ ] **Step 3: Write minimal implementation**

```python
# add to src/mthydra/ops/ru_bringup.py
import json
import re

# Import _run_controller + _extract_box_id from main lazily-friendly: top-level
# import is fine here because main does NOT import ru_bringup at top (it uses
# lazy dispatch wrappers, set up in Task 8).
from . import main as _main

_run_controller = _main._run_controller          # subprocess wrapper (env-safe)
_extract_box_id = _main._extract_box_id           # 'provision-seed: created box_id=...'


def mint_seed(provider: str, region: str, *, canary: bool,
              agent_source_url: str, agent_source_sha256: str,
              descriptor_refresh_url: str, cloud_init_out: str) -> str:
    """Run provision-seed; return the minted box_id parsed from stderr."""
    argv = [
        "provision-seed",
        "--provider", provider, "--region", region,
        "--agent-source-url", agent_source_url,
        "--agent-source-sha256", agent_source_sha256,
        "--descriptor-refresh-url", descriptor_refresh_url,
        "--cloud-init-out", cloud_init_out,
    ]
    if canary:
        argv.append("--canary")
    res = _run_controller(*argv, capture=True)
    box_id = _extract_box_id(res.stderr or "")
    if not box_id:
        raise RuntimeError(
            "provision-seed succeeded but emitted no 'box_id=' line "
            "(controller version mismatch?)")
    return box_id


def mark_live(box_id: str, public_ip: str) -> None:
    """Flip a provisioning box to live."""
    _run_controller("ru-box-mark-live", box_id, "--public-ip", public_ip)


def box_info(box_id: str) -> dict | None:
    """Return the ru_boxes row for `box_id` from ru-box-list, or None."""
    res = _run_controller("ru-box-list", "--json", capture=True)
    try:
        rows = json.loads(res.stdout or "[]")
    except json.JSONDecodeError:
        return None
    for row in rows:
        if row.get("box_id") == box_id:
            return row
    return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/unit/ops/test_ru_bringup.py -k "mint_seed or mark_live or box_info" -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add src/mthydra/ops/ru_bringup.py tests/unit/ops/test_ru_bringup.py
git commit -m "feat(ru-bringup): mint_seed + mark_live + box_info controller wrappers"
```

---

## Task 3: `wait_for_soak` — polling loop around `image-promote-status`

**Files:**
- Modify: `src/mthydra/ops/ru_bringup.py`
- Test: `tests/unit/ops/test_ru_bringup.py`

- [ ] **Step 1: Write the failing test**

```python
# add to tests/unit/ops/test_ru_bringup.py
def test_wait_for_soak_exits_when_passed(monkeypatch):
    payloads = [
        json.dumps({"passed": False, "reasons": ["canary B below threshold"]}),
        json.dumps({"passed": True, "reasons": []}),
    ]
    def fake_run(*args, check=True, capture=False, env=None):
        return subprocess.CompletedProcess(args, 0, payloads.pop(0), "")
    monkeypatch.setattr(ru_bringup, "_run_controller", fake_run, raising=False)
    monkeypatch.setattr(ru_bringup.time, "sleep", lambda s: None)

    progress, writes = [], []
    result = ru_bringup.wait_for_soak(
        "iv-v1", poll_interval_s=0,
        on_progress=lambda reasons: progress.append(list(reasons)),
        state_writer=lambda: writes.append(1),
    )
    assert result.passed is True
    assert result.duration_s >= 0
    assert progress[0] == ["canary B below threshold"]
    assert len(writes) >= 1  # state_writer called at least once during the loop


def test_wait_for_soak_propagates_keyboard_interrupt(monkeypatch):
    def fake_run(*args, **kw):
        return subprocess.CompletedProcess(args, 0,
            json.dumps({"passed": False, "reasons": ["pending"]}), "")
    monkeypatch.setattr(ru_bringup, "_run_controller", fake_run, raising=False)
    def kc(_s):
        raise KeyboardInterrupt
    monkeypatch.setattr(ru_bringup.time, "sleep", kc)

    writes = []
    with pytest.raises(KeyboardInterrupt):
        ru_bringup.wait_for_soak(
            "iv-v1", poll_interval_s=0,
            on_progress=lambda r: None,
            state_writer=lambda: writes.append(1),
        )
    assert writes  # state was saved before the interrupt propagated
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/ops/test_ru_bringup.py -k soak -v`
Expected: FAIL — `wait_for_soak`/`SoakResult` undefined.

- [ ] **Step 3: Write minimal implementation**

```python
# add to src/mthydra/ops/ru_bringup.py
from dataclasses import dataclass


@dataclass(frozen=True)
class SoakResult:
    passed: bool
    reasons: list[str]
    duration_s: int


def wait_for_soak(image_version: str, *, poll_interval_s: int,
                  on_progress: Callable[[list[str]], None],
                  state_writer: Callable[[], None]) -> SoakResult:
    """Poll image-promote-status until passed=True. KeyboardInterrupt is
    propagated (caller catches and prints a resume hint); state_writer is
    called each tick so a Ctrl-C lands the latest progress on disk."""
    started = time.monotonic()
    while True:
        res = _run_controller(
            "image-promote-status", image_version, "--json", capture=True)
        try:
            payload = json.loads(res.stdout or "{}")
        except json.JSONDecodeError:
            payload = {"passed": False, "reasons": ["malformed status JSON"]}
        reasons = list(payload.get("reasons") or [])
        on_progress(reasons)
        state_writer()
        if payload.get("passed"):
            return SoakResult(True, reasons,
                              int(time.monotonic() - started))
        time.sleep(poll_interval_s)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/unit/ops/test_ru_bringup.py -k soak -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add src/mthydra/ops/ru_bringup.py tests/unit/ops/test_ru_bringup.py
git commit -m "feat(ru-bringup): wait_for_soak polling loop with progress+state_writer"
```

---

## Task 4: `CycleState` dataclass + JSON save/load

**Files:**
- Modify: `src/mthydra/ops/ru_bringup.py`
- Test: `tests/unit/ops/test_ru_bringup.py`

- [ ] **Step 1: Write the failing test**

```python
# add to tests/unit/ops/test_ru_bringup.py
def test_cycle_state_round_trip(tmp_path):
    state = ru_bringup.CycleState(
        release="v1.0.0", image_version="iv-v1.0.0",
        profile_path="/tmp/p.json", image_built=True,
        canaries=[{"box_id": "b-1", "provider": "selectel",
                   "region": "ru-msk-1", "public_ip": "1.2.3.4",
                   "marked_live_at": "2026-05-28T12:00:00Z"}],
        started_at="2026-05-28T11:00:00Z",
    )
    p = tmp_path / "v1.0.0.json"
    state.save(p)
    loaded = ru_bringup.CycleState.load(p)
    assert loaded == state


def test_cycle_state_load_missing_returns_none(tmp_path):
    assert ru_bringup.CycleState.load(tmp_path / "absent.json") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/ops/test_ru_bringup.py -k cycle_state -v`
Expected: FAIL — `CycleState` undefined.

- [ ] **Step 3: Write minimal implementation**

```python
# add to src/mthydra/ops/ru_bringup.py
from dataclasses import asdict, dataclass, field
from pathlib import Path

CYCLE_STATE_DIR = Path("/var/lib/mthydra/ru-cycle")


@dataclass
class CycleState:
    release: str
    image_version: str
    profile_path: str
    image_built: bool
    canaries: list[dict] = field(default_factory=list)
    started_at: str = ""

    def save(self, path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2, sort_keys=True))
        tmp.replace(p)   # atomic rename

    @classmethod
    def load(cls, path) -> "CycleState | None":
        p = Path(path)
        if not p.exists():
            return None
        return cls(**json.loads(p.read_text()))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/unit/ops/test_ru_bringup.py -k cycle_state -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add src/mthydra/ops/ru_bringup.py tests/unit/ops/test_ru_bringup.py
git commit -m "feat(ru-bringup): CycleState dataclass with atomic JSON save/load (O-D6)"
```

---

## Task 5: `parse_cohort` — flags form + YAML file form

**Files:**
- Modify: `src/mthydra/ops/ru_bringup.py`
- Test: `tests/unit/ops/test_ru_bringup.py`

- [ ] **Step 1: Write the failing test**

```python
# add to tests/unit/ops/test_ru_bringup.py
def test_parse_cohort_from_flags():
    targets = ru_bringup.parse_cohort(
        flags=["provider=selectel,region=ru-msk-1",
               "provider=timeweb,region=ru-spb-1"],
        file_path=None, expected_count=2,
    )
    assert [(t.provider, t.region) for t in targets] == [
        ("selectel", "ru-msk-1"), ("timeweb", "ru-spb-1"),
    ]


def test_parse_cohort_count_mismatch_raises():
    with pytest.raises(ValueError, match="canaries=3"):
        ru_bringup.parse_cohort(
            flags=["provider=selectel,region=ru-msk-1"],
            file_path=None, expected_count=3,
        )


def test_parse_cohort_from_yaml_like_file(tmp_path):
    # File format: simple "key=value" lines per target, one target per line,
    # to avoid a YAML dep. (Spec O-D9: YAML alternative, but stdlib is enough.)
    f = tmp_path / "cohort.txt"
    f.write_text("provider=selectel,region=ru-msk-1\n"
                 "provider=firstvds,region=ru-spb-1\n")
    targets = ru_bringup.parse_cohort(flags=None, file_path=f, expected_count=2)
    assert len(targets) == 2 and targets[0].provider == "selectel"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/ops/test_ru_bringup.py -k cohort -v`
Expected: FAIL — `parse_cohort`/`CanaryTarget` undefined.

- [ ] **Step 3: Write minimal implementation**

```python
# add to src/mthydra/ops/ru_bringup.py
@dataclass(frozen=True)
class CanaryTarget:
    provider: str
    region: str


def parse_cohort(*, flags: list[str] | None, file_path,
                 expected_count: int) -> list[CanaryTarget]:
    """Accept repeated `provider=X,region=Y` flags OR a file with one such
    line per target. Validates count matches --canaries N.

    Spec O-D9 mentions YAML; we use a stdlib-only "key=value,key=value\\n"
    line format to avoid pulling in PyYAML. The file format is line-oriented
    and human-editable; a future task can add real YAML if needed."""
    raw: list[str] = []
    if flags:
        raw.extend(flags)
    if file_path is not None:
        raw.extend(line.strip() for line in Path(file_path).read_text().splitlines()
                   if line.strip() and not line.lstrip().startswith("#"))
    if len(raw) != expected_count:
        raise ValueError(
            f"cohort size {len(raw)} != canaries={expected_count}")
    targets = []
    for spec in raw:
        kv = dict(part.split("=", 1) for part in spec.split(","))
        targets.append(CanaryTarget(provider=kv["provider"].strip(),
                                    region=kv["region"].strip()))
    return targets
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/unit/ops/test_ru_bringup.py -k cohort -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add src/mthydra/ops/ru_bringup.py tests/unit/ops/test_ru_bringup.py
git commit -m "feat(ru-bringup): parse_cohort flags + line-file forms (O-D9)"
```

---

## Task 6: `cmd_ru_bringup` — per-box wizard wiring

**Files:**
- Modify: `src/mthydra/ops/ru_bringup.py`
- Test: `tests/unit/ops/test_ru_bringup.py`

- [ ] **Step 1: Write the failing test**

```python
# add to tests/unit/ops/test_ru_bringup.py
import argparse


def _bringup_args(tmp_path, **over):
    base = dict(
        provider="selectel", region="ru-msk-1", canary=True,
        agent_source_url="https://b2/a.tar.gz",
        agent_source_sha256="deadbeef",
        descriptor_refresh_url="https://b2/desc",
        cloud_init_out=str(tmp_path / "ci.yaml"),
        public_ip="1.2.3.4",     # skip the input() prompt
        box_id=None,
        reach_timeout=1,
        non_interactive=True,
        verbose=False, quiet=True, dry_run=False,
        config=None,
    )
    base.update(over)
    return argparse.Namespace(**base)


def test_cmd_ru_bringup_happy_path(monkeypatch, tmp_path):
    box_state = {"v": "provisioning"}
    def fake_run(*args, check=True, capture=False, env=None):
        sub = args[0]
        if sub == "provision-seed":
            return subprocess.CompletedProcess(args, 0, "",
                "provision-seed: created box_id=b-c1\n")
        if sub == "ru-box-list":
            return subprocess.CompletedProcess(args, 0,
                json.dumps([{"box_id": "b-c1", "state": box_state["v"],
                             "sni": "cover.example"}]), "")
        if sub == "ru-box-mark-live":
            box_state["v"] = "live"
            return subprocess.CompletedProcess(args, 0, "", "")
        return subprocess.CompletedProcess(args, 0, "", "")
    monkeypatch.setattr(ru_bringup, "_run_controller", fake_run, raising=False)
    monkeypatch.setattr(ru_bringup, "wait_for_reachable",
                        lambda *a, **kw: True)

    rc = ru_bringup.cmd_ru_bringup(_bringup_args(tmp_path))
    assert rc == 0
    assert box_state["v"] == "live"


def test_cmd_ru_bringup_resume_skips_mint(monkeypatch, tmp_path):
    calls = []
    def fake_run(*args, **kw):
        calls.append(args[0])
        if args[0] == "ru-box-list":
            return subprocess.CompletedProcess(args, 0,
                json.dumps([{"box_id": "b-existing", "state": "provisioning",
                             "sni": "cover.example"}]), "")
        return subprocess.CompletedProcess(args, 0, "", "")
    monkeypatch.setattr(ru_bringup, "_run_controller", fake_run, raising=False)
    monkeypatch.setattr(ru_bringup, "wait_for_reachable", lambda *a, **kw: True)

    rc = ru_bringup.cmd_ru_bringup(_bringup_args(tmp_path, box_id="b-existing"))
    assert rc == 0
    assert "provision-seed" not in calls   # mint skipped on resume


def test_cmd_ru_bringup_aborts_on_unreachable(monkeypatch, tmp_path):
    def fake_run(*args, **kw):
        if args[0] == "provision-seed":
            return subprocess.CompletedProcess(args, 0, "",
                "provision-seed: created box_id=b-c1\n")
        if args[0] == "ru-box-list":
            return subprocess.CompletedProcess(args, 0,
                json.dumps([{"box_id": "b-c1", "state": "provisioning",
                             "sni": "cover.example"}]), "")
        return subprocess.CompletedProcess(args, 0, "", "")
    monkeypatch.setattr(ru_bringup, "_run_controller", fake_run, raising=False)
    monkeypatch.setattr(ru_bringup, "wait_for_reachable",
                        lambda *a, **kw: False)

    rc = ru_bringup.cmd_ru_bringup(_bringup_args(tmp_path))
    assert rc != 0   # unreachable → non-zero exit
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/ops/test_ru_bringup.py -k bringup -v`
Expected: FAIL — `cmd_ru_bringup` undefined.

- [ ] **Step 3: Write minimal implementation**

```python
# add to src/mthydra/ops/ru_bringup.py
_say = _main._say
_err = _main._err


def _prompt_public_ip() -> str | None:
    try:
        ans = input("Public IP when VM is up (Ctrl-C to defer): ").strip()
    except (KeyboardInterrupt, EOFError):
        return None
    return ans or None


def cmd_ru_bringup(args) -> int:
    # Phase 1: mint (skipped on --box-id resume).
    if args.box_id:
        box_id = args.box_id
        _say(f"resume: using existing box {box_id}; skipping mint")
    else:
        _say(f"mint: provision-seed for {args.provider}/{args.region}"
             + (" (canary)" if args.canary else ""))
        box_id = mint_seed(
            args.provider, args.region, canary=args.canary,
            agent_source_url=args.agent_source_url,
            agent_source_sha256=args.agent_source_sha256,
            descriptor_refresh_url=args.descriptor_refresh_url,
            cloud_init_out=args.cloud_init_out,
        )
        _say(f"minted box_id={box_id}; cloud-init at {args.cloud_init_out}")

    # Phase 2: boot-handoff. Get public IP.
    public_ip = args.public_ip
    if not public_ip:
        if args.non_interactive:
            _err("--public-ip required in non-interactive mode")
            return 2
        _say("Paste the cloud-init file as user-data in your provider's "
             "console, boot the VM, then come back with the public IP.")
        public_ip = _prompt_public_ip()
        if not public_ip:
            _say(f"deferred. Resume with: mthydra-ops ru-bringup "
                 f"--box-id {box_id} --public-ip <ip>")
            return 0

    # Phase 3: reachability (skip if box already live).
    info = box_info(box_id)
    if info is None:
        _err(f"box {box_id} not found in ru-box-list")
        return 3
    if info.get("state") == "live":
        _say(f"box already live — skipping reachability + mark-live")
    else:
        sni = info.get("sni") or ""
        _say(f"reachability: waiting for TLS handshake on {public_ip}:443 "
             f"(sni={sni!r}, timeout={args.reach_timeout}s)")
        ok = wait_for_reachable(public_ip, 443, sni,
                                timeout_s=args.reach_timeout,
                                on_progress=lambda e: None)
        if not ok:
            _err(f"box {public_ip}:443 not reachable within "
                 f"{args.reach_timeout}s — check provider firewall + "
                 f"cloud-init logs on the VM")
            return 4
        # Phase 4: mark-live.
        _say(f"mark-live: {box_id} @ {public_ip}")
        mark_live(box_id, public_ip)

    # Phase 5: summary.
    canary_note = ("CANARY — next: §3.4 soak (submit probe-record from "
                   "each registered vantage)" if args.canary
                   else "in rotation")
    _say(f"done: box {box_id} live @ {public_ip}; {canary_note}")
    return 0
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/unit/ops/test_ru_bringup.py -k bringup -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add src/mthydra/ops/ru_bringup.py tests/unit/ops/test_ru_bringup.py
git commit -m "feat(ru-bringup): cmd_ru_bringup wizard (mint → boot-handoff → reach → mark-live)"
```

---

## Task 7: `cmd_ru_image_cycle` — release-wide wizard

**Files:**
- Modify: `src/mthydra/ops/ru_bringup.py`
- Test: `tests/unit/ops/test_ru_bringup.py`

- [ ] **Step 1: Write the failing test**

```python
# add to tests/unit/ops/test_ru_bringup.py
def _cycle_args(tmp_path, **over):
    base = dict(
        release="v1.0.0",
        profile_json=str(tmp_path / "p.json"),
        canaries=2,
        canary_target=["provider=selectel,region=ru-msk-1",
                       "provider=firstvds,region=ru-spb-1"],
        cohort=None,
        agent_source_url="https://b2/a.tar.gz",
        agent_source_sha256="deadbeef",
        descriptor_refresh_url="https://b2/desc",
        soak_poll=0, soak_timeout=0,
        evidence=None, resume=False,
        non_interactive=True, verbose=False, quiet=True, dry_run=False,
        config=None,
        state_dir=str(tmp_path / "state"),     # tests use a tmp state dir
        promote_yes=True,                       # bypass interactive confirm in tests
    )
    base.update(over)
    return argparse.Namespace(**base)


def test_cmd_ru_image_cycle_end_to_end(monkeypatch, tmp_path):
    (tmp_path / "p.json").write_text("{}")
    soak_payloads = [
        json.dumps({"passed": False, "reasons": ["short"]}),
        json.dumps({"passed": True, "reasons": []}),
    ]
    state_ipv4 = iter(["1.1.1.1", "2.2.2.2"])
    minted = iter(["b-c1", "b-c2"])
    promoted = {"v": False}

    def fake_run(*args, check=True, capture=False, env=None):
        sub = args[0]
        if sub == "image-build":
            return subprocess.CompletedProcess(args, 0, "", "")
        if sub == "provision-seed":
            return subprocess.CompletedProcess(args, 0, "",
                f"provision-seed: created box_id={next(minted)}\n")
        if sub == "ru-box-list":
            return subprocess.CompletedProcess(args, 0,
                json.dumps([{"box_id": "b-c1", "state": "live", "sni": "x"},
                            {"box_id": "b-c2", "state": "live", "sni": "y"}]),
                "")
        if sub == "image-promote-status":
            return subprocess.CompletedProcess(args, 0, soak_payloads.pop(0), "")
        if sub == "image-promote":
            promoted["v"] = True
            return subprocess.CompletedProcess(args, 0, "", "")
        if sub == "image-current":
            return subprocess.CompletedProcess(args, 0,
                json.dumps({"image_version": "iv-vprev"}), "")
        return subprocess.CompletedProcess(args, 0, "", "")
    monkeypatch.setattr(ru_bringup, "_run_controller", fake_run, raising=False)
    monkeypatch.setattr(ru_bringup, "wait_for_reachable", lambda *a, **kw: True)
    # bringup will call mint→mark-live; ip per box comes from args.public_ip,
    # which the cycle supplies internally — supply it via monkeypatch:
    monkeypatch.setattr(ru_bringup, "_prompt_public_ip",
                        lambda: next(state_ipv4))

    rc = ru_bringup.cmd_ru_image_cycle(_cycle_args(tmp_path))
    assert rc == 0
    assert promoted["v"] is True
    # state file removed on success
    assert not (Path(_cycle_args(tmp_path).state_dir) / "v1.0.0.json").exists()


def test_cmd_ru_image_cycle_resume_skips_built_and_done_canaries(monkeypatch, tmp_path):
    (tmp_path / "p.json").write_text("{}")
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    pre = ru_bringup.CycleState(
        release="v1.0.0", image_version="iv-v1.0.0",
        profile_path=str(tmp_path / "p.json"), image_built=True,
        canaries=[{"box_id": "b-c1", "provider": "selectel",
                   "region": "ru-msk-1", "public_ip": "1.1.1.1",
                   "marked_live_at": "2026-05-28T12:00:00Z"}],
        started_at="2026-05-28T11:00:00Z",
    )
    pre.save(state_dir / "v1.0.0.json")

    seen_subs = []
    def fake_run(*args, **kw):
        seen_subs.append(args[0])
        if args[0] == "provision-seed":
            return subprocess.CompletedProcess(args, 0, "",
                "provision-seed: created box_id=b-c2\n")
        if args[0] == "ru-box-list":
            return subprocess.CompletedProcess(args, 0,
                json.dumps([{"box_id": "b-c2", "state": "live", "sni": "y"}]), "")
        if args[0] == "image-promote-status":
            return subprocess.CompletedProcess(args, 0,
                json.dumps({"passed": True, "reasons": []}), "")
        return subprocess.CompletedProcess(args, 0, "", "")
    monkeypatch.setattr(ru_bringup, "_run_controller", fake_run, raising=False)
    monkeypatch.setattr(ru_bringup, "wait_for_reachable", lambda *a, **kw: True)
    monkeypatch.setattr(ru_bringup, "_prompt_public_ip", lambda: "2.2.2.2")

    rc = ru_bringup.cmd_ru_image_cycle(
        _cycle_args(tmp_path, state_dir=str(state_dir), resume=True))
    assert rc == 0
    # image-build skipped (already built), and only one provision-seed call
    # (b-c1 already in state with marked_live_at)
    assert seen_subs.count("image-build") == 0
    assert seen_subs.count("provision-seed") == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/ops/test_ru_bringup.py -k image_cycle -v`
Expected: FAIL — `cmd_ru_image_cycle` undefined.

- [ ] **Step 3: Write minimal implementation**

```python
# add to src/mthydra/ops/ru_bringup.py
from datetime import UTC, datetime


def compose_evidence(state: "CycleState", soak_started: str, soak_ended: str) -> str:
    boxes = ", ".join(c["box_id"] for c in state.canaries)
    return (f"soak from {soak_started} to {soak_ended}; canaries: {boxes}; "
            f"cover-site behaviour: stable per probe_results; "
            f"latency baseline within profile bounds")


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def cmd_ru_image_cycle(args) -> int:
    state_dir = Path(args.state_dir) if getattr(args, "state_dir", None) \
                else CYCLE_STATE_DIR
    state_dir.mkdir(parents=True, exist_ok=True)
    state_path = state_dir / f"{args.release}.json"

    state = CycleState.load(state_path)  # always pick up prior state if present
    if state is None:
        state = CycleState(
            release=args.release,
            image_version=f"iv-{args.release}",
            profile_path=args.profile_json or "",
            image_built=False,
            canaries=[],
            started_at=_now_iso(),
        )
        state.save(state_path)

    # Phase 1: image-build (skip if already built).
    if not state.image_built:
        _say(f"[1/4] image-build: --release {args.release} "
             f"--profile-json {state.profile_path}")
        _run_controller("image-build", "--release", args.release,
                        "--profile-json", state.profile_path)
        state.image_built = True
        state.save(state_path)
    else:
        _say("[1/4] image-build: already built → skip")

    # Phase 2: canary cohort.
    targets = parse_cohort(flags=args.canary_target, file_path=args.cohort,
                           expected_count=args.canaries)
    done_count = sum(1 for c in state.canaries if c.get("marked_live_at"))
    _say(f"[2/4] canaries: {done_count}/{args.canaries} already live")
    for idx, target in enumerate(targets):
        if idx < len(state.canaries) and state.canaries[idx].get("marked_live_at"):
            continue
        # Mint + bring up this canary.
        cloud_init = str(state_dir / f"{args.release}-c{idx + 1}.yaml")
        box_id = mint_seed(target.provider, target.region, canary=True,
                           agent_source_url=args.agent_source_url,
                           agent_source_sha256=args.agent_source_sha256,
                           descriptor_refresh_url=args.descriptor_refresh_url,
                           cloud_init_out=cloud_init)
        # Cycle always prompts for canary IPs as VMs come up — there is no
        # CLI shape for pre-staging N IPs at invocation. `--non-interactive`
        # on the cycle is a no-op for IP collection (documented O-D-style).
        public_ip = _prompt_public_ip()
        if public_ip is None:
            _say(f"deferred at canary {box_id}. Resume with: "
                 f"mthydra-ops ru-image-cycle --release {args.release} --resume")
            return 0
        info = box_info(box_id) or {}
        if info.get("state") != "live":
            if not wait_for_reachable(public_ip, 443, info.get("sni") or "",
                                      timeout_s=600):
                _err(f"canary {box_id} unreachable; resume later")
                return 4
            mark_live(box_id, public_ip)
        entry = {"box_id": box_id, "provider": target.provider,
                 "region": target.region, "public_ip": public_ip,
                 "marked_live_at": _now_iso()}
        if idx < len(state.canaries):
            state.canaries[idx] = entry
        else:
            state.canaries.append(entry)
        state.save(state_path)

    # Phase 3: soak wait.
    soak_started = _now_iso()
    _say(f"[3/4] soak: polling image-promote-status every {args.soak_poll}s "
         f"(Ctrl-C to defer)")
    def _progress(reasons):
        for r in reasons:
            _say(f"  pending: {r}")
    try:
        result = wait_for_soak(state.image_version,
                               poll_interval_s=args.soak_poll,
                               on_progress=_progress,
                               state_writer=lambda: state.save(state_path))
    except KeyboardInterrupt:
        _say("deferred. Resume with: mthydra-ops ru-image-cycle "
             f"--release {args.release} --resume")
        return 0

    # Phase 4: promote (operator-confirmed unless --promote-yes in tests).
    soak_ended = _now_iso()
    evidence = args.evidence or compose_evidence(state, soak_started, soak_ended)
    if not getattr(args, "promote_yes", False) and not args.non_interactive:
        ans = input(f"soak passed in {result.duration_s}s. "
                    f"Promote {state.image_version}? [y/N] ").strip().lower()
        if ans not in ("y", "yes"):
            _say("promote declined. State preserved; rerun with --resume to retry")
            return 0
    _say(f"[4/4] promote: {state.image_version}")
    _run_controller("image-promote", state.image_version,
                    "--evidence", evidence)

    # Phase 5: summary + remove state file.
    _say(f"done: {state.image_version} promoted. "
         f"Existing boxes age out via §3.7 replace-on-burn; "
         f"use `mthydra-ops ru-bringup` for replacements.")
    state_path.unlink(missing_ok=True)
    return 0
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/unit/ops/test_ru_bringup.py -k image_cycle -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add src/mthydra/ops/ru_bringup.py tests/unit/ops/test_ru_bringup.py
git commit -m "feat(ru-bringup): cmd_ru_image_cycle release-wide wizard (build → canaries → soak → promote)"
```

---

## Task 8: subparsers + lazy dispatch in `main.py`

**Files:**
- Modify: `src/mthydra/ops/main.py`
- Test: `tests/unit/ops/test_main.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/unit/ops/test_main.py
def test_ru_bringup_subcommands_parse():
    from mthydra.ops import main as m
    p = m.build_parser()
    a = p.parse_args([
        "ru-bringup", "--provider", "selectel", "--region", "ru-msk-1",
        "--canary", "--agent-source-url", "u",
        "--agent-source-sha256", "s", "--descriptor-refresh-url", "d",
    ])
    assert a.cmd == "ru-bringup" and a.provider == "selectel" and a.canary is True

    b = p.parse_args([
        "ru-image-cycle", "--release", "v1.0.0",
        "--profile-json", "/tmp/p.json", "--canaries", "2",
        "--canary-target", "provider=selectel,region=ru-msk-1",
        "--canary-target", "provider=firstvds,region=ru-spb-1",
        "--agent-source-url", "u", "--agent-source-sha256", "s",
        "--descriptor-refresh-url", "d",
    ])
    assert b.cmd == "ru-image-cycle" and b.canaries == 2
    assert b.canary_target == [
        "provider=selectel,region=ru-msk-1",
        "provider=firstvds,region=ru-spb-1",
    ]


def test_main_routes_ru_bringup_to_cmd_ru_bringup(monkeypatch):
    from mthydra.ops import main as m
    from mthydra.ops import ru_bringup
    called = {}
    monkeypatch.setattr(ru_bringup, "cmd_ru_bringup",
                        lambda args: called.setdefault("v", 0) or 0)
    rc = m.main([
        "ru-bringup", "--provider", "selectel", "--region", "ru-msk-1",
        "--agent-source-url", "u", "--agent-source-sha256", "s",
        "--descriptor-refresh-url", "d",
    ])
    assert rc == 0 and "v" in called
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/ops/test_main.py -k "ru_bringup_subcommands or routes_ru_bringup" -v`
Expected: FAIL — subcommands not registered.

- [ ] **Step 3: Write minimal implementation**

In `src/mthydra/ops/main.py`, add the dispatch wrappers and subparser registration. After the existing `_dispatch_install_standby` function, add:

```python
def _dispatch_ru_bringup(args) -> int:
    from . import ru_bringup
    return ru_bringup.cmd_ru_bringup(args)


def _dispatch_ru_image_cycle(args) -> int:
    from . import ru_bringup
    return ru_bringup.cmd_ru_image_cycle(args)
```

In `_DISPATCH` (at the bottom of the existing dict), add:
```python
    "ru-bringup": _dispatch_ru_bringup,
    "ru-image-cycle": _dispatch_ru_image_cycle,
```

In `build_parser()`, after the install/install-standby subparsers, add:

```python
    # ru-bringup
    rb = sub.add_parser("ru-bringup",
                        help="per-box wizard: mint provision-seed → reach → mark-live")
    rb.add_argument("--provider", required=True)
    rb.add_argument("--region", required=True)
    rb.add_argument("--canary", action="store_true",
                    help="mark as canary (spec D2 soak cohort)")
    rb.add_argument("--agent-source-url", required=True)
    rb.add_argument("--agent-source-sha256", required=True)
    rb.add_argument("--descriptor-refresh-url", required=True)
    rb.add_argument("--cloud-init-out", default=None,
                    help="cloud-init bundle path (default /tmp/ru-cloud-init-<box>.yaml)")
    rb.add_argument("--public-ip", default=None,
                    help="skip the interactive prompt; supply IP up front")
    rb.add_argument("--box-id", default=None,
                    help="resume an in-flight bring-up with an existing box_id")
    rb.add_argument("--reach-timeout", type=int, default=600,
                    help="seconds to wait for :443 TLS handshake")
    rb.add_argument("--config", default=None,
                    help="optional config file with [ru] defaults")
    rb.add_argument("--non-interactive", action="store_true")
    rb.add_argument("--verbose", action="store_true")
    rb.add_argument("--quiet", action="store_true")
    rb.add_argument("--dry-run", action="store_true")

    # ru-image-cycle
    rc = sub.add_parser("ru-image-cycle",
                        help="release wizard: image-build → canaries → soak → promote")
    rc.add_argument("--release", required=True,
                    help="upstream mtg release tag, e.g. v2.1.7")
    rc.add_argument("--profile-json", default=None,
                    help="path to known-good profile JSON (required for phase 1)")
    rc.add_argument("--canaries", type=int, default=2,
                    help="number of canary boxes to provision")
    rc.add_argument("--canary-target", action="append", default=None,
                    metavar="provider=X,region=Y",
                    help="one cohort target; repeat for N canaries")
    rc.add_argument("--cohort", default=None,
                    help="cohort file (one 'provider=X,region=Y' per line)")
    rc.add_argument("--agent-source-url", required=True)
    rc.add_argument("--agent-source-sha256", required=True)
    rc.add_argument("--descriptor-refresh-url", required=True)
    rc.add_argument("--soak-poll", type=int, default=60,
                    help="seconds between image-promote-status polls")
    rc.add_argument("--soak-timeout", type=int, default=0,
                    help="0 = unlimited (operator-paced per O-D5)")
    rc.add_argument("--evidence", default=None,
                    help="override the auto-composed image-promote evidence")
    rc.add_argument("--resume", action="store_true",
                    help="resume from the saved state file for this release")
    rc.add_argument("--state-dir", default=None,
                    help="resume state dir (default /var/lib/mthydra/ru-cycle)")
    rc.add_argument("--config", default=None)
    rc.add_argument("--non-interactive", action="store_true")
    rc.add_argument("--verbose", action="store_true")
    rc.add_argument("--quiet", action="store_true")
    rc.add_argument("--dry-run", action="store_true")
    # `promote_yes` is set only by tests via Namespace; no CLI flag (O-D7).
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/unit/ops/test_main.py -k "ru_bringup_subcommands or routes_ru_bringup" -v` and `.venv/bin/pytest tests/unit/ops/test_main.py tests/unit/ops/test_ru_bringup.py -q`.
Expected: PASS (new + all prior).

- [ ] **Step 5: Commit**

```bash
git add src/mthydra/ops/main.py tests/unit/ops/test_main.py
git commit -m "feat(ru-bringup): wire ru-bringup / ru-image-cycle subcommands + dispatch"
```

---

## Task 9: Makefile smoke target + full-suite gate

**Files:**
- Modify: `Makefile`
- Verification only

- [ ] **Step 1: Add `smoke-ru-cycle` target to `Makefile`**

Append to `Makefile`, mirroring the existing `smoke-install` style:

```makefile

smoke-ru-cycle:
	@echo "--- mthydra ru-image-cycle smoke procedure (spec O) ---"
	@echo "1. On the EU controller host:"
	@echo "     mthydra-controller upstream-check          # confirm a release is available"
	@echo "     mthydra-ops image-build-template > /tmp/profile-v2.1.7.json"
	@echo "     # edit profile JSON per runbook §3.2"
	@echo "2. Have 2 (provider, region) targets ready for canaries."
	@echo "3. Run the cycle:"
	@echo "     mthydra-ops ru-image-cycle \\"
	@echo "         --release v2.1.7 --profile-json /tmp/profile-v2.1.7.json \\"
	@echo "         --canaries 2 \\"
	@echo "         --canary-target provider=selectel,region=ru-msk-1 \\"
	@echo "         --canary-target provider=firstvds,region=ru-spb-1 \\"
	@echo "         --agent-source-url <b2 url> --agent-source-sha256 <sha> \\"
	@echo "         --descriptor-refresh-url <b2 url>"
	@echo "4. Paste each cloud-init bundle in the corresponding provider console,"
	@echo "   feed each public IP back to the prompt as the VMs boot."
	@echo "5. Submit probe-record from each registered vantage during the soak."
	@echo "6. Confirm the promote prompt → iv-v2.1.7 in image-list."
```

Add `smoke-ru-cycle` to the `.PHONY` line at the top of the Makefile.

- [ ] **Step 2: Run the verification gate**

```bash
.venv/bin/pytest tests/ -q
```

Expected: full suite green (1079 + the new ru_bringup tests + test_main routing tests).

- [ ] **Step 3: Touched-files lint delta**

```bash
for f in src/mthydra/ops/ru_bringup.py src/mthydra/ops/main.py \
         tests/unit/ops/test_ru_bringup.py tests/unit/ops/test_main.py; do
  n=$(.venv/bin/ruff check $f 2>&1 | grep -oE "Found [0-9]+" | head -1); echo "$f  $n"
done
```

Expected: `ru_bringup.py` and `test_ru_bringup.py` are new and must be **0 errors**; `main.py` and `test_main.py` must be **no worse than their pre-task counts** (parent = the commit before this task series started; the repo has pre-existing lint debt elsewhere that's not in scope).

- [ ] **Step 4: Coverage sanity on `ru_bringup.py`**

```bash
.venv/bin/pytest tests/unit/ops/test_ru_bringup.py --cov=mthydra.ops.ru_bringup --cov-report=term-missing
```

Eyeball the missing-lines list; if a non-trivial branch is uncovered (not just the `if __name__ == "__main__"` style boilerplate), add a focused test for it.

- [ ] **Step 5: Commit + push**

```bash
git add Makefile
git commit -m "docs(ru-bringup): smoke-ru-cycle Makefile target"
git push origin main
```

---

## Self-review notes (for the implementer)

- **Spec coverage:** O-D1 → Tasks 6 + 7 (two layered subcommands); O-D2 → Task 7 (soak polls + operator submits probe-record externally); O-D3 → Task 7 phase 1 (image-build folded in); O-D4 → Task 1 (TLS handshake, no cert validation); O-D5 → Task 7 KeyboardInterrupt path + Task 3 `state_writer`; O-D6 → Task 4 `CycleState` + Task 7 state_path; O-D7 → Task 7 confirm prompt (only `promote_yes` test-only escape); O-D8 → all (no Phase/Runner import); O-D9 → Task 5 `parse_cohort`.

- **Watch item — `image-build` arg shape:** Task 7 calls `mthydra-controller image-build --release <ver> --profile-json <path>` per runbook §3.2. If the actual CLI requires additional flags (e.g., `--db-path`, `--config`), add them; verify with `.venv/bin/mthydra-controller image-build --help` before implementing Task 7. The same applies to `image-promote-status` (Task 3) and `image-promote` (Task 7): confirm the JSON shape (we expect `passed: bool, reasons: list[str]`) by running `image-promote-status --help` and a one-off invocation against a test DB if available; if the shape differs, adapt the parser in `wait_for_soak` while keeping the test's mocked payload aligned.

- **Watch item — `_extract_box_id` regex:** Task 2 imports `_extract_box_id` from `main`. Confirm the exact stderr format the controller emits (regex pattern `provision-seed: created box_id=(\S+)`); if the controller's actual format differs, fix the regex in `main.py` (one place, used by both `cmd_ru_provision` and the new `mint_seed`).

- **Watch item — circular import:** `ru_bringup.py` imports `main` at top; `main.py` MUST NOT add `from . import ru_bringup` at top. Task 8 keeps the lazy-dispatch pattern that Task 10 of the spec N plan installed.

- **Watch item — `--state-dir` in the cycle test:** the test passes a tmp `--state-dir` via Namespace so it can assert on the state file; in production the default is `/var/lib/mthydra/ru-cycle`. The CLI flag exists for the same reason.
