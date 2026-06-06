# Unit V2 — nfqws Desync Layer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add zapret's `nfqws` as a fully cattle-integrated, descriptor-tunable packet-desync layer on the RU→EU Reality flow (outbound TCP to EU-exit IPs:443 only), with a controller-enforced canary gate so a bad signed strategy can't hit the whole fleet at once.

**Architecture:** `nfqws` is distributed via B2 like the mtg binary (sha256-verified into tmpfs). A new `ru_agent/desync.py` installs an NFQUEUE iptables rule matching outbound TCP to the current EU-exit IPs:443 and exposes the `nfqws` argv. The agent supervises `nfqws` as a third child, re-verifies the rule each refresh tick, and re-applies it when the exit set changes. The desync *strategy* string travels in the signed descriptor (schema v3, optional). The controller only emits a fleet-wide strategy change once a `v_desync_strategy_canary_proven` marker matches it (invariant #36).

**Tech Stack:** Python 3.14, stdlib (`subprocess`, `shlex`, `hashlib`), sqlite3, pytest; `nfqws` from [zapret](https://github.com/bol-van/zapret) (external binary, NFQUEUE/`CAP_NET_ADMIN`). **Depends on Unit V1** (descriptor schema v3) and benefits from **Unit V5** (handshake-health probe validates strategies). Build it last.

**Spec:** `doc/specs/2026-06-06-V-ru-egress-obfuscation.md` §4, §6.1–§6.3, §7 (#34–#36), §8.

---

## File Structure

- `src/mthydra/ru_agent/seed.py` — MODIFY: optional `nfqws_url` / `nfqws_sha256`; accept seed schema v2 **and** v3.
- `src/mthydra/descriptor/payload.py` — MODIFY: optional v3 `desync_strategy` field.
- `src/mthydra/ru_agent/desync.py` — CREATE: NFQUEUE rule install/verify/clear + `nfqws` argv builder.
- `src/mthydra/ru_agent/supervisor.py` — MODIFY: supervise an optional third child (`nfqws`).
- `src/mthydra/ru_agent/__main__.py` — MODIFY: fetch nfqws, install/verify/re-apply desync rules, launch the child.
- `src/mthydra/descriptor/sign.py` + `scheduler.py` — MODIFY: emit `desync_strategy`, gated by #36.
- `src/mthydra/controller/state/desync_strategy.py` — CREATE: staged/promoted strategy + canary-proven marker.
- `src/mthydra/controller/cli.py` — MODIFY: `desync-strategy-show` / `-stage` / `-promote`.
- `packaging/etc/mthydra/controller.toml.example`, `doc/runbook.md`, `CHANGELOG.md`.
- Tests under `tests/unit/ru_agent/`, `tests/unit/descriptor/`, `tests/unit/controller/`, `tests/integration/`.

**Invariants:** #34 (nfqws running + rule installed when strategy non-empty), #35 (rule set == current exit IPs:443, no broader/narrower), #36 (no fleet-wide strategy without canary marker).

---

## Task 1: Seed schema v3 — optional nfqws binary fields

**Files:**
- Modify: `src/mthydra/ru_agent/seed.py`
- Test: `tests/unit/ru_agent/test_seed_nfqws.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/ru_agent/test_seed_nfqws.py
import base64, json
from mthydra.ru_agent.seed import load


def _min_seed(extra: dict) -> dict:
    # Start from a known-good v2 seed body used by the existing seed tests, then
    # override schema + add fields. Reuse the helper/fixture from
    # tests/unit/ru_agent/test_seed.py rather than re-deriving every field.
    base = json.loads(EXISTING_VALID_SEED_JSON)  # from the seed test module
    base.update(extra)
    return base


def test_v3_seed_carries_nfqws(tmp_path):
    p = tmp_path / "seed.json"
    p.write_text(json.dumps(_min_seed({
        "schema": "mthydra.ru_seed.v3",
        "nfqws_url": "https://b2.example/nfqws",
        "nfqws_sha256": "ab" * 32,
    })))
    s = load(p)
    assert s.nfqws_url == "https://b2.example/nfqws"
    assert s.nfqws_sha256 == "ab" * 32


def test_v2_seed_still_loads_with_none_nfqws(tmp_path):
    p = tmp_path / "seed.json"
    p.write_text(json.dumps(_min_seed({"schema": "mthydra.ru_seed.v2"})))
    s = load(p)
    assert s.nfqws_url is None
    assert s.nfqws_sha256 is None


def test_v3_seed_without_nfqws_is_inert(tmp_path):
    p = tmp_path / "seed.json"
    p.write_text(json.dumps(_min_seed({"schema": "mthydra.ru_seed.v3"})))
    s = load(p)
    assert s.nfqws_url is None
```

> Pull `EXISTING_VALID_SEED_JSON` (or the dict-builder) from the current `tests/unit/ru_agent/test_seed.py` so every existing required field is present.

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/ru_agent/test_seed_nfqws.py -v`
Expected: FAIL — `Seed` has no `nfqws_url`; schema v3 rejected.

- [ ] **Step 3: Implement in `seed.py`**

Accept both schemas; add optional fields (NOT in `_REQUIRED_FIELDS`):

```python
_SUPPORTED_SCHEMAS = ("mthydra.ru_seed.v2", "mthydra.ru_seed.v3")
```

Add to the `Seed` dataclass (after the required fields, with defaults so v2 seeds construct cleanly):

```python
    nfqws_url: str | None = None
    nfqws_sha256: str | None = None
```

In `load(...)`, populate them tolerantly:

```python
        nfqws_url=raw.get("nfqws_url"),
        nfqws_sha256=raw.get("nfqws_sha256"),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/ru_agent/test_seed_nfqws.py tests/unit/ru_agent/test_seed.py -v`
Expected: PASS (new + existing seed tests).

- [ ] **Step 5: Commit**

```bash
git add src/mthydra/ru_agent/seed.py tests/unit/ru_agent/test_seed_nfqws.py
git commit -m "feat(V2): seed schema v3 — optional nfqws_url/nfqws_sha256 (inert when absent)"
```

---

## Task 2: Descriptor `desync_strategy` field (v3, optional)

**Files:**
- Modify: `src/mthydra/descriptor/payload.py`
- Test: `tests/unit/descriptor/test_payload_desync.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/descriptor/test_payload_desync.py
from mthydra.descriptor.payload import (
    DescriptorPayload, SCHEMA_V3, canonical_bytes,
)


def _p(strategy):
    return DescriptorPayload(
        generation=1, signing_key_gen=1,
        issued_at="2026-06-06T00:00:00Z", valid_until="2026-06-07T00:00:00Z",
        eu_exit_set=(), previous_generation_hash=None, next_signing_pubkey=None,
        schema=SCHEMA_V3, desync_strategy=strategy,
    )


def test_desync_strategy_roundtrips():
    blob = canonical_bytes(_p("--dpi-desync=fake,split2 --dpi-desync-ttl=4"))
    p = DescriptorPayload.from_canonical_bytes(blob)
    assert p.desync_strategy == "--dpi-desync=fake,split2 --dpi-desync-ttl=4"
    assert canonical_bytes(p) == blob


def test_desync_strategy_omitted_when_none():
    assert b"desync_strategy" not in canonical_bytes(_p(None))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/descriptor/test_payload_desync.py -v`
Expected: FAIL — no `desync_strategy`.

- [ ] **Step 3: Implement in `payload.py`**

Add `"desync_strategy"` to `_KNOWN_FIELDS`; add the dataclass field:

```python
    desync_strategy: str | None = None
```

In `from_canonical_bytes`, parse it:

```python
        desync_strategy = obj.get("desync_strategy")
        ...
        return cls(..., desync_strategy=(None if desync_strategy is None else str(desync_strategy)))
```

In `canonical_bytes`, emit it for v3 when set (alongside the V1 `tls_fingerprints` block):

```python
    if payload.schema == SCHEMA_V3 and payload.desync_strategy is not None:
        obj["desync_strategy"] = payload.desync_strategy
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/descriptor/test_payload_desync.py tests/unit/descriptor/ -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/mthydra/descriptor/payload.py tests/unit/descriptor/test_payload_desync.py
git commit -m "feat(V2): descriptor v3 optional desync_strategy field"
```

---

## Task 3: `ru_agent/desync.py` — NFQUEUE rules + nfqws argv

Mirrors `ru_agent/iptables.py` discipline (own chain, idempotent install, token-exact verify). Targets ONLY outbound TCP to the EU-exit IPs on port 443.

**Files:**
- Create: `src/mthydra/ru_agent/desync.py`
- Test: `tests/unit/ru_agent/test_desync.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/ru_agent/test_desync.py
import shlex
from mthydra.ru_agent import desync


def test_nfqws_argv():
    argv = desync.nfqws_argv(
        "/run/mthydra/nfqws", "--dpi-desync=fake,split2 --dpi-desync-ttl=4", qnum=200,
    )
    assert argv[0] == "/run/mthydra/nfqws"
    assert "--qnum=200" in argv
    assert "--dpi-desync=fake,split2" in argv
    assert "--dpi-desync-ttl=4" in argv


def test_exit_ips_split_v4_v6():
    v4, v6 = desync.split_exit_ips(["9.9.9.9:443", "[2001:db8::1]:443", "8.8.8.8:443"])
    assert v4 == ["9.9.9.9", "8.8.8.8"]
    assert v6 == ["2001:db8::1"]


def test_install_builds_per_ip_nfqueue_rules(monkeypatch):
    calls = []
    monkeypatch.setattr(desync, "_run", lambda cmd: calls.append(cmd) or "")
    desync.install(exit_ips=["9.9.9.9:443"], qnum=200)
    flat = [" ".join(c) for c in calls]
    assert any("-N MTHYDRA_DESYNC" in f or "MTHYDRA_DESYNC" in f for f in flat)
    assert any("-d 9.9.9.9" in f and "--dport 443" in f
               and "NFQUEUE" in f and "--queue-num 200" in f for f in flat)


def test_verify_installed_token_exact(monkeypatch):
    listing = (
        "-A MTHYDRA_DESYNC -d 9.9.9.9/32 -p tcp -m tcp --dport 443 "
        "-j NFQUEUE --queue-num 200\n"
    )
    monkeypatch.setattr(desync, "_run", lambda cmd: listing)
    assert desync.verify_installed(exit_ips=["9.9.9.9:443"], qnum=200) is True
    assert desync.verify_installed(exit_ips=["1.1.1.1:443"], qnum=200) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/ru_agent/test_desync.py -v`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement `desync.py`**

```python
"""Install + verify + clear an NFQUEUE rule that hands the RU->EU Reality flow
(outbound TCP to the EU-exit IPs on :443) to nfqws for DPI desync.

Targets ONLY the exit IPs on :443 — the local mtg->sing-box redirect inbound
(127.0.0.1) is never matched (spec V V-D5). The rule lives in the mangle table
in its own chain hooked from OUTPUT, mirroring ru_agent.iptables discipline."""
from __future__ import annotations

import contextlib
import shlex
import subprocess

EXIT_PORT = 443
_CHAIN = "MTHYDRA_DESYNC"
_TABLE = "mangle"


class DesyncError(RuntimeError):
    pass


def _run(cmd: list[str]) -> str:
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        raise DesyncError(
            f"command {' '.join(cmd)!r} failed: rc={result.returncode} "
            f"stderr={getattr(result, 'stderr', b'')!r}"
        )
    return (getattr(result, "stdout", b"") or b"").decode("utf-8", errors="replace")


def nfqws_argv(nfqws_path: str, strategy: str, *, qnum: int) -> list[str]:
    """Build the nfqws command. The agent owns --qnum; `strategy` is the signed,
    operator-tuned argument string (everything else)."""
    return [nfqws_path, f"--qnum={qnum}", *shlex.split(strategy)]


def split_exit_ips(endpoints: list[str]) -> tuple[list[str], list[str]]:
    """Split 'host:port' endpoints into (v4_ips, v6_ips). IPv6 endpoints are
    bracketed: '[2001:db8::1]:443'."""
    v4: list[str] = []
    v6: list[str] = []
    for ep in endpoints:
        if ep.startswith("["):
            host = ep[1:ep.index("]")]
            v6.append(host)
        else:
            host = ep.rsplit(":", 1)[0]
            (v6 if ":" in host else v4).append(host)
    return v4, v6


def install(*, exit_ips: list[str], qnum: int) -> None:
    """(Re)install the desync chain + per-exit-IP NFQUEUE rules. Idempotent."""
    clear(qnum)
    v4, v6 = split_exit_ips(exit_ips)
    for tool, ips in (("iptables", v4), ("ip6tables", v6)):
        if not ips:
            continue
        _run([tool, "-t", _TABLE, "-N", _CHAIN])
        for ip in ips:
            _run([
                tool, "-t", _TABLE, "-A", _CHAIN,
                "-d", ip, "-p", "tcp", "--dport", str(EXIT_PORT),
                "-j", "NFQUEUE", "--queue-num", str(qnum),
            ])
        _run([tool, "-t", _TABLE, "-A", "OUTPUT", "-p", "tcp",
              "--dport", str(EXIT_PORT), "-j", _CHAIN])


def _rule_present(out: str, ip: str, qnum: int) -> bool:
    """Token-exact: dest IP (with or without /32,/128 mask) AND --queue-num on
    the same line. Mirrors ru_agent.iptables._rule_present strictness."""
    q = str(qnum)
    ip_forms = {ip, f"{ip}/32", f"{ip}/128"}
    for line in out.splitlines():
        toks = line.split()
        has_dst = any(
            toks[i] == "-d" and i + 1 < len(toks) and toks[i + 1] in ip_forms
            for i in range(len(toks))
        )
        has_q = any(
            toks[i] == "--queue-num" and i + 1 < len(toks) and toks[i + 1] == q
            for i in range(len(toks))
        )
        if has_dst and has_q:
            return True
    return False


def verify_installed(*, exit_ips: list[str], qnum: int) -> bool:
    """True iff every expected exit IP has its NFQUEUE rule (#34/#35)."""
    v4, v6 = split_exit_ips(exit_ips)
    for tool, ips in (("iptables", v4), ("ip6tables", v6)):
        if not ips:
            continue
        try:
            out = _run([tool, "-t", _TABLE, "-S", _CHAIN])
        except DesyncError:
            return False
        for ip in ips:
            if not _rule_present(out, ip, qnum):
                return False
    return True


def clear(qnum: int) -> None:
    """Remove the chain. Idempotent."""
    for tool in ("iptables", "ip6tables"):
        for cmd in (
            [tool, "-t", _TABLE, "-D", "OUTPUT", "-p", "tcp",
             "--dport", str(EXIT_PORT), "-j", _CHAIN],
            [tool, "-t", _TABLE, "-F", _CHAIN],
            [tool, "-t", _TABLE, "-X", _CHAIN],
        ):
            with contextlib.suppress(DesyncError):
                _run(cmd)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/ru_agent/test_desync.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Extend the AST import-isolation test**

`tests/unit/ru_agent/test_ast_no_controller_imports.py` walks every `ru_agent` module; `desync.py` is picked up automatically. Run it:

Run: `python -m pytest tests/unit/ru_agent/test_ast_no_controller_imports.py -v`
Expected: PASS (desync.py imports only stdlib).

- [ ] **Step 6: Commit**

```bash
git add src/mthydra/ru_agent/desync.py tests/unit/ru_agent/test_desync.py
git commit -m "feat(V2): ru_agent.desync — NFQUEUE rules for RU->EU flow + nfqws argv"
```

---

## Task 4: Supervisor — optional third child (`nfqws`)

**Files:**
- Modify: `src/mthydra/ru_agent/supervisor.py`
- Test: `tests/unit/ru_agent/test_supervisor.py` (extend)

- [ ] **Step 1: Write the failing test**

```python
# add to tests/unit/ru_agent/test_supervisor.py
def test_nfqws_child_supervised_and_crashloops(monkeypatch):
    # Build a Supervisor with nfqws_cmd set; make the fake nfqws proc "crash"
    # >=4 times in the window and assert on_persistent_failure fires for it.
    # Mirror the existing mtg/sing-box crash-loop test structure in this file.
    ...
def test_nfqws_absent_is_not_supervised():
    sup = supervisor.Supervisor(
        mtg_cmd=["mtg"], sing_box_cmd=["sb"], nfqws_cmd=None,
    )
    # launch_all must not attempt to Popen a None command (no third child).
    ...
```

Fill these in following the existing crash-loop test in the file (fake `subprocess.Popen` returning a stub whose `.poll()` you control).

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/ru_agent/test_supervisor.py -k nfqws -v`
Expected: FAIL — `Supervisor.__init__` has no `nfqws_cmd`.

- [ ] **Step 3: Implement**

Add `nfqws_cmd: list[str] | None = None` to `__init__`, store it, add `self._nfqws_proc = None` and `self._nfqws_crashes: list[float] = []`. Make the child iteration dynamic so an unset child is skipped:

```python
    def _children(self):
        rows = [
            ("mtg", "_mtg_proc", self._mtg_cmd, self._mtg_crashes),
            ("sing-box", "_sing_box_proc", self._sing_box_cmd, self._sing_box_crashes),
        ]
        if self._nfqws_cmd is not None:
            rows.append(("nfqws", "_nfqws_proc", self._nfqws_cmd, self._nfqws_crashes))
        return rows

    def launch_all(self) -> None:
        self._mtg_proc = subprocess.Popen(self._mtg_cmd)
        self._sing_box_proc = subprocess.Popen(self._sing_box_cmd)
        if self._nfqws_cmd is not None:
            self._nfqws_proc = subprocess.Popen(self._nfqws_cmd)
```

Replace the hardcoded tuple in `check_children_once` with `for name, proc_attr, cmd, crashes in self._children():` (logic otherwise unchanged), and include `self._nfqws_proc` in `shutdown_children`'s loop.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/ru_agent/test_supervisor.py -v`
Expected: PASS (new + existing).

- [ ] **Step 5: Commit**

```bash
git add src/mthydra/ru_agent/supervisor.py tests/unit/ru_agent/test_supervisor.py
git commit -m "feat(V2): supervisor manages optional nfqws child with crash-loop guard"
```

---

## Task 5: Agent `__main__` wiring (fetch + install + supervise + refresh + verify)

**Files:**
- Modify: `src/mthydra/ru_agent/__main__.py`
- Test: `tests/unit/ru_agent/test_main_desync.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/ru_agent/test_main_desync.py
# Unit-level: assert the desync helpers and exit-IP extraction wire correctly.
from mthydra.ru_agent import __main__ as agent_main


def test_exit_ips_from_descriptor():
    payload = {"eu_exit_set": [
        {"endpoint": "9.9.9.9:443", "fingerprint": "f1"},
        {"endpoint": "8.8.8.8:443", "fingerprint": "f2"},
    ]}
    assert agent_main._exit_endpoints(payload) == ["9.9.9.9:443", "8.8.8.8:443"]


def test_desync_disabled_when_no_strategy():
    payload = {"eu_exit_set": [{"endpoint": "9.9.9.9:443", "fingerprint": "f"}]}
    assert agent_main._desync_strategy(payload) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/ru_agent/test_main_desync.py -v`
Expected: FAIL — helpers not defined.

- [ ] **Step 3: Implement helpers + wiring in `__main__.py`**

Add the constant and small pure helpers (testable without root/network):

```python
NFQWS_PATH = "/run/mthydra/nfqws"
DESYNC_QNUM = 200


def _exit_endpoints(descriptor_payload: dict) -> list[str]:
    return [e["endpoint"] for e in descriptor_payload.get("eu_exit_set", [])]


def _desync_strategy(descriptor_payload: dict) -> str | None:
    s = descriptor_payload.get("desync_strategy")
    return s or None
```

Import the module: `from mthydra.ru_agent import desync`.

In `_startup()` after the iptables install (step 5), fetch nfqws and install the desync chain only when both the binary and a strategy are present:

```python
    strategy = _desync_strategy(descriptor_payload)
    if strategy and s.nfqws_url and s.nfqws_sha256:
        try:
            binary.download_and_verify(
                url=s.nfqws_url, expected_sha256=s.nfqws_sha256, out_path=NFQWS_PATH,
            )
        except binary.BinaryError as e:
            raise _StartupError(f"nfqws binary: {e}") from e
        try:
            desync.install(exit_ips=_exit_endpoints(descriptor_payload), qnum=DESYNC_QNUM)
        except desync.DesyncError as e:
            raise _StartupError(f"desync rules: {e}") from e
```

Return the strategy alongside the seed so `main()` can decide whether to launch the nfqws child. Simplest: have `_startup()` return `(s, strategy)` and update the caller, or stash `strategy` on a small module-level holder read in `main()`. Prefer returning a tuple:

```python
    return s, strategy   # _startup
...
    res = _startup()      # in main()
    s, strategy = res
```

In `main()` step 6, pass the nfqws child when a strategy is active:

```python
    sup = supervisor.Supervisor(
        mtg_cmd=[MTG_PATH, "run", MTG_CONFIG_PATH],
        sing_box_cmd=["sing-box", "run", "-c", SING_BOX_CONFIG_PATH],
        nfqws_cmd=(desync.nfqws_argv(NFQWS_PATH, strategy, qnum=DESYNC_QNUM)
                   if strategy and s.nfqws_url else None),
        on_persistent_failure=lambda r: _terminate(f"supervisor: {r}"),
    )
```

In the refresh `_rewrite(blob)` (step 7), after rendering sing-box.json, re-apply the desync rule set to the *new* exit IPs when a strategy is active (#35):

```python
        new_strategy = _desync_strategy(payload)
        if new_strategy and s.nfqws_url:
            try:
                desync.install(exit_ips=_exit_endpoints(payload), qnum=DESYNC_QNUM)
            except desync.DesyncError:
                pass  # next periodic-recheck verify will catch a persistent miss
        else:
            desync.clear(DESYNC_QNUM)
```

In `_periodic_recheck` (step 8), after the iptables verify block, add the #34/#35 verify (re-install once, terminate on a second consecutive miss) — but only when a strategy is active:

```python
            if strategy and s.nfqws_url:
                if not desync.verify_installed(
                    exit_ips=_exit_endpoints(descriptor_payload_current), qnum=DESYNC_QNUM,
                ):
                    try:
                        desync.install(
                            exit_ips=_exit_endpoints(descriptor_payload_current),
                            qnum=DESYNC_QNUM,
                        )
                    except desync.DesyncError as e:
                        _terminate(f"desync: {e}")
                        return
```

> `descriptor_payload_current` must reflect the latest refreshed descriptor (the recheck loop should read the same current exit set the rewrite uses). If the recheck loop currently only sees the startup descriptor, thread the current exit set the same way iptables uses `s.telegram_dcs` — store the latest exit IPs on a thread-safe holder updated by `_rewrite`. Keep it minimal: a module-level `list` guarded by the GIL for read/replace is sufficient here (single writer in the refresh thread, single reader in the recheck thread).

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/ru_agent/test_main_desync.py tests/unit/ru_agent/test_main.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/mthydra/ru_agent/__main__.py tests/unit/ru_agent/test_main_desync.py
git commit -m "feat(V2): agent fetches/supervises nfqws + installs/verifies desync rules"
```

---

## Task 6: Controller — staged/promoted strategy + canary marker (#36)

The canary gate is **controller-enforced**: a fleet-wide `desync_strategy` is only signed once a `v_desync_strategy_canary_proven` marker matches its hash. Operationally the operator stages the candidate on a canary shard and watches the V5 handshake-health signal before promoting.

**Files:**
- Create: `src/mthydra/controller/state/desync_strategy.py`
- Test: `tests/unit/controller/state/test_desync_strategy.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/controller/state/test_desync_strategy.py
from mthydra.controller.state import desync_strategy as ds


def test_stage_then_promote_requires_proof(controller_conn):
    ds.stage(controller_conn, "--dpi-desync=fake,split2", at="2026-06-06T00:00:00Z")
    assert ds.staged(controller_conn) == "--dpi-desync=fake,split2"
    # promote refused without a matching canary-proven marker (#36)
    import pytest
    with pytest.raises(ds.CanaryGateError):
        ds.promote(controller_conn, at="2026-06-06T01:00:00Z")
    # prove the staged candidate, then promote succeeds
    ds.mark_canary_proven(controller_conn, "--dpi-desync=fake,split2",
                          at="2026-06-06T00:30:00Z")
    ds.promote(controller_conn, at="2026-06-06T01:00:00Z")
    assert ds.live(controller_conn) == "--dpi-desync=fake,split2"


def test_promote_refused_when_proof_is_for_other_strategy(controller_conn):
    ds.stage(controller_conn, "--strategy-A", at="2026-06-06T00:00:00Z")
    ds.mark_canary_proven(controller_conn, "--strategy-B", at="2026-06-06T00:30:00Z")
    import pytest
    with pytest.raises(ds.CanaryGateError):
        ds.promote(controller_conn, at="2026-06-06T01:00:00Z")
```

> Use the project's controller-DB fixture (the one that runs `schema.apply`/`db.connect`). If the schema needs a new table, add it in Step 3 via the schema migration the project uses (grep how `eu_exit_set` / `obligation_clocks` tables are declared in `state/schema.py`).

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/controller/state/test_desync_strategy.py -v`
Expected: FAIL — module/table absent.

- [ ] **Step 3: Implement**

Add a `desync_strategy` table to `state/schema.py` (single-row staged/live + a proven-hash marker), following the project's migration style:

```sql
CREATE TABLE IF NOT EXISTS desync_strategy (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    staged TEXT,
    live TEXT,
    canary_proven_hash TEXT,
    updated_at TEXT
);
INSERT OR IGNORE INTO desync_strategy (id) VALUES (1);
```

Module:

```python
# src/mthydra/controller/state/desync_strategy.py
"""Spec V V-D6 / invariant #36 — staged vs live desync strategy with a
canary-proven gate. A fleet-wide (live) strategy can only be set from a staged
candidate whose hash has been marked canary-proven."""
from __future__ import annotations

import hashlib
import sqlite3


class CanaryGateError(RuntimeError):
    pass


def _h(strategy: str) -> str:
    return hashlib.sha256(strategy.encode("utf-8")).hexdigest()


def stage(conn: sqlite3.Connection, strategy: str, *, at: str) -> None:
    conn.execute("UPDATE desync_strategy SET staged=?, updated_at=? WHERE id=1",
                 (strategy, at))
    conn.commit()


def staged(conn: sqlite3.Connection) -> str | None:
    return conn.execute("SELECT staged FROM desync_strategy WHERE id=1").fetchone()[0]


def live(conn: sqlite3.Connection) -> str | None:
    return conn.execute("SELECT live FROM desync_strategy WHERE id=1").fetchone()[0]


def mark_canary_proven(conn: sqlite3.Connection, strategy: str, *, at: str) -> None:
    conn.execute("UPDATE desync_strategy SET canary_proven_hash=?, updated_at=? WHERE id=1",
                 (_h(strategy), at))
    conn.commit()


def promote(conn: sqlite3.Connection, *, at: str) -> None:
    row = conn.execute(
        "SELECT staged, canary_proven_hash FROM desync_strategy WHERE id=1"
    ).fetchone()
    cand, proven = row
    if cand is None:
        raise CanaryGateError("no staged strategy to promote")
    if proven != _h(cand):
        raise CanaryGateError(
            "invariant #36: staged strategy is not canary-proven "
            "(stage it on a canary shard, confirm V5 handshake-health holds, "
            "then mark it proven before promoting)"
        )
    conn.execute("UPDATE desync_strategy SET live=?, updated_at=? WHERE id=1", (cand, at))
    conn.commit()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/controller/state/test_desync_strategy.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/mthydra/controller/state/schema.py src/mthydra/controller/state/desync_strategy.py tests/unit/controller/state/test_desync_strategy.py
git commit -m "feat(V2): controller desync-strategy state with canary-proven gate (#36)"
```

---

## Task 7: Signer emits the *live* desync strategy

**Files:**
- Modify: `src/mthydra/descriptor/sign.py` (read `desync_strategy.live` from the conn it already has) and `scheduler.py`
- Test: `tests/unit/descriptor/test_sign_desync.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/descriptor/test_sign_desync.py
from mthydra.descriptor.payload import DescriptorPayload
from mthydra.descriptor.sign import sign_new_descriptor
from mthydra.controller.state import desync_strategy as ds


def test_sign_emits_live_strategy(controller_conn):
    ds.stage(controller_conn, "--dpi-desync=fake", at="t0")
    ds.mark_canary_proven(controller_conn, "--dpi-desync=fake", at="t0")
    ds.promote(controller_conn, at="t1")
    _gen, blob, _sig = sign_new_descriptor(
        controller_conn, now_iso="2026-06-06T00:00:00Z",
        valid_until_iso="2026-06-07T00:00:00Z",
    )
    assert DescriptorPayload.from_canonical_bytes(blob).desync_strategy == "--dpi-desync=fake"


def test_sign_omits_strategy_when_no_live(controller_conn):
    _gen, blob, _sig = sign_new_descriptor(
        controller_conn, now_iso="2026-06-06T00:00:00Z",
        valid_until_iso="2026-06-07T00:00:00Z",
    )
    assert DescriptorPayload.from_canonical_bytes(blob).desync_strategy is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/descriptor/test_sign_desync.py -v`
Expected: FAIL — signer doesn't read the live strategy.

- [ ] **Step 3: Implement**

`sign.py` already holds `conn`. Read the live strategy and set it on the payload:

```python
    # spec V V2 — emit the canary-proven live desync strategy, if any.
    try:
        from mthydra.controller.state.desync_strategy import live as _live_desync
        desync_strategy = _live_desync(conn)
    except Exception:
        desync_strategy = None
    ...
    payload = DescriptorPayload(
        ...,
        tls_fingerprints=tls_fingerprints,
        desync_strategy=desync_strategy,
    )
```

> `sign.py` lives in `mthydra.descriptor` (RU-embeddable). Importing `mthydra.controller.state.desync_strategy` here couples descriptor → controller, which is FORBIDDEN for RU-side modules. **Resolve by threading instead:** add a `desync_strategy: str | None = None` parameter to `sign_new_descriptor` (like V1's `tls_fingerprints`), and have the controller-side caller (`DescriptorRotator._rotate`) read `desync_strategy.live(conn)` and pass it. This keeps `descriptor` free of `controller` imports. Update the test to pass the conn's live strategy through the rotator path, or call `sign_new_descriptor(..., desync_strategy=ds.live(conn))` directly.

Implement the threaded form: parameter on `sign_new_descriptor`, read+pass in `DescriptorRotator._rotate`, wire `live()` there.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/descriptor/test_sign_desync.py -v`
Expected: PASS (adjust the test to the threaded signature).

- [ ] **Step 5: Confirm import isolation still holds**

Run: `python -m pytest tests/unit/descriptor/test_verify_import_isolation.py -v` (and the ru_agent AST test).
Expected: PASS — `descriptor` imports no `controller`.

- [ ] **Step 6: Commit**

```bash
git add src/mthydra/descriptor/sign.py src/mthydra/descriptor/scheduler.py tests/unit/descriptor/test_sign_desync.py
git commit -m "feat(V2): rotator threads live desync_strategy into descriptor signing"
```

---

## Task 8: CLI — `desync-strategy-show / -stage / -promote`

**Files:**
- Modify: `src/mthydra/controller/cli.py`
- Test: `tests/unit/controller/test_cli_desync_strategy.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/controller/test_cli_desync_strategy.py
from mthydra.controller import cli


def test_stage_show_promote_flow(capsys, controller_db_path):
    assert cli.main(["desync-strategy-stage", "--db-path", controller_db_path,
                     "--strategy", "--dpi-desync=fake"]) == 0
    assert cli.main(["desync-strategy-show", "--db-path", controller_db_path]) == 0
    # promote without proof -> non-zero + a clear message
    rc = cli.main(["desync-strategy-promote", "--db-path", controller_db_path])
    assert rc != 0
    assert "canary" in capsys.readouterr().err.lower()
```

> Match the real CLI db-path/option conventions (grep an existing `data-exit-*` subcommand). `desync-strategy-promote` should surface `CanaryGateError` as a non-zero exit + stderr message, not a traceback. Marking canary-proven is an operator-attested action — add it as `desync-strategy-mark-proven --strategy <s>` (attested), mirroring how `vantage-attest-active` is operator-attested.

- [ ] **Step 2–4: fail → implement subcommands → pass**

Register the four subcommands delegating to `state.desync_strategy`. `-show` prints staged / live / proven-hash-matches-staged. Run the test to green.

- [ ] **Step 5: Commit**

```bash
git add src/mthydra/controller/cli.py tests/unit/controller/test_cli_desync_strategy.py
git commit -m "feat(V2): desync-strategy stage/show/promote/mark-proven CLI"
```

---

## Task 9: nfqws build+publish (ops) + controller.toml + runbook + integration + CHANGELOG

**Files:**
- Modify: `packaging/etc/mthydra/controller.toml.example`, `doc/runbook.md`, `CHANGELOG.md`
- Create: `tests/integration/test_desync_strategy_canary_gate.py`

- [ ] **Step 1: Integration test — canary gate end to end**

```python
# tests/integration/test_desync_strategy_canary_gate.py
# stage -> sign (omits, not promoted) -> mark-proven -> promote -> sign (emits).
# Assert a v3 descriptor with desync_strategy only appears after promote, and a
# promote of a non-proven candidate raises CanaryGateError.
```

Write it against the real rotator + state modules (no network).

- [ ] **Step 2: controller.toml.example + runbook**

`controller.toml.example` — document that the desync strategy is managed via CLI (staged/promoted in DB), not a static TOML key, so the canary gate can enforce #36. Add a commented pointer.

`doc/runbook.md` — new section "§V.2 — Rolling a desync strategy safely": stage → distribute to a canary shard (spec H) → watch the V5 `eu_exit_handshake_degraded` signal stays clear for the soak window → `desync-strategy-mark-proven` → `desync-strategy-promote`. Note the nfqws binary is built from a pinned zapret revision and published to B2; the seed's `nfqws_url`/`nfqws_sha256` are the trust anchors (provisioning writes them).

- [ ] **Step 3: nfqws build/publish note**

Add an ops checklist item (runbook §V.2 or the image/build doc): build `nfqws` from the pinned zapret tag, compute sha256, upload to B2 (reuse `S3Destination` helpers), and set the provisioning config so new seeds carry `nfqws_url`/`nfqws_sha256`. This is deployment, not pytest (honest residual, spec §4.6 / §10.4).

- [ ] **Step 4: Run all changed-scope suites**

Run: `python -m pytest tests/unit/ru_agent/ tests/unit/descriptor/ tests/unit/controller/ tests/integration/test_desync_strategy_canary_gate.py -q`
Expected: PASS. Lint changed files only (ruff-version memo).

- [ ] **Step 5: CHANGELOG + push**

```markdown
- feat(V2): nfqws desync layer — zapret nfqws supervised on the RU box,
  NFQUEUE-desyncing only the RU->EU exit:443 Reality flow; strategy carried
  in the signed descriptor (v3) and gated by a controller-enforced canary
  marker (#36). Seed v3 carries the sha256-verified nfqws binary URL.
  CLI: desync-strategy stage/show/promote/mark-proven. Invariants #34–#36.
```

```bash
git add -A
git commit -m "docs(V2): CHANGELOG + runbook + integration — nfqws desync canary gate"
git push origin main
```

---

## Self-Review (completed during authoring)

- **Spec coverage:** §4.1 binary distribution (Task 1 seed fields + Task 9 ops), §4.2 descriptor strategy (Task 2), §4.3 wiring/rules (Tasks 3,5), §4.4 supervision + tick-verify (Tasks 4,5), §4.5/§4.6 canary gate (Tasks 6–8) + residual notes (Task 9), §6.1/§6.2 seed+descriptor amendments (Tasks 1,2), §7 invariants #34 (Task 5 verify), #35 (Task 3 token-exact + Task 5 re-apply), #36 (Task 6 promote gate + Task 7 signer), §8 CLI (Task 8).
- **Placeholder scan:** logic modules (`desync.py`, `desync_strategy.py`) are complete. Deferred-to-implementer items are *existing* conventions (seed test fixture, controller-DB fixture, CLI option style, the recheck loop's current-exit-set threading) — each flagged with the grep target. The descriptor→controller import hazard is called out with the threaded resolution (Task 7) so an implementer can't accidentally break RU import isolation.
- **Type consistency:** `desync_strategy: str | None` across payload, sign, scheduler, and state; `exit_ips: list[str]` of `"host:port"` consistently in `desync.install/verify_installed`, split by `split_exit_ips`; `qnum` int everywhere (`DESYNC_QNUM = 200`). Chain name `MTHYDRA_DESYNC`, table `mangle`. Supervisor child key `nfqws_cmd`.
- **Ordering:** depends on V1 (schema v3) and is validated by V5 (handshake-health); built last per the spec build order.
```
