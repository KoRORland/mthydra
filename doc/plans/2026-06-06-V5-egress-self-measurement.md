# Unit V5 — RU-Egress Self-Measurement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Instrument the RU→EU hop so the controller can *see* (a) when a deployed uTLS fingerprint goes stale versus the real browser population, and (b) when EU-exit handshakes start failing/being-reset from a RU vantage — surfaced through the existing observability snapshot + remediation pipeline.

**Architecture:** A new functional prober runs on an existing RU probe vantage (vantages are already RU VPSes — see the cover-domain `--vantage <ru-vps-id>` attestation flow), dials each EU exit with box-equivalent Reality params, and reports handshake outcome + the ClientHello JA3 it emitted. A pure parser turns the remote stdout into a result; a pure staleness evaluator compares observed JA3s against an operator-maintained reference set. Two new anti-obligation signals (`eu_exit_handshake_degraded::<node>`, `tls_fingerprint_stale::<fp>`) flow through `obligation_clocks` → `snapshot` → `remediation`, exactly like the K3 `box_eu_tunnel_unseen` signal.

**Tech Stack:** Python 3.14, stdlib, sqlite3, pytest. No live network in tests — the prober takes an injected `ssh_cmd_fn`, the evaluators are pure functions. **Depends on Unit V1** (uses `KNOWN_UTLS_FINGERPRINTS` and the v3 `tls_fingerprints` field).

**Spec:** `doc/specs/2026-06-06-V-ru-egress-obfuscation.md` §5, §6.4, §7, §8.

---

## File Structure

- `src/mthydra/controller/probe_runner/probers.py` — MODIFY: add `probe_reality_handshake` + `parse_handshake_probe_output`.
- `src/mthydra/controller/observability/fingerprint_staleness.py` — CREATE: pure staleness evaluator + reference-set loader.
- `src/mthydra/controller/observability/snapshot.py` — MODIFY: register the two new anti-obligation prefixes.
- `src/mthydra/controller/observability/remediation.py` — MODIFY: add the two remediation lines.
- `src/mthydra/controller/observability/severity.py` — MODIFY: severity for the new kinds (if not covered by the generic per-target path).
- `src/mthydra/controller/probe_runner/wheel.py` — MODIFY: run the reality-handshake prober + write/clear the anti-obligation rows.
- `src/mthydra/controller/cli.py` — MODIFY: `fingerprint-staleness-show`.
- `packaging/etc/mthydra/controller.toml.example` — MODIFY: reference-set path config.
- `doc/runbook.md` — MODIFY: how to maintain the JA3 reference set.
- Tests under `tests/unit/controller/probe_runner/`, `tests/unit/controller/observability/`.

---

## Task 1: Parse the remote handshake-probe output (pure)

The RU vantage runs a small helper that dials `exit_ip:port` with the box's Reality params and prints a defined one-line contract to stdout. The Python side only parses; the helper binary is a deployment artifact (documented residual, spec §5.3). Output contract:

```
mthydra-rh result=ok   ja3=771,4865-4866-...,0-23-65281-...,29-23-24,0  ttfb_ms=42
mthydra-rh result=reset detail=connection_reset_by_peer
mthydra-rh result=timeout detail=handshake_timeout
mthydra-rh result=tcp_fail detail=connection_refused
```

**Files:**
- Modify: `src/mthydra/controller/probe_runner/probers.py`
- Test: `tests/unit/controller/probe_runner/test_parse_handshake.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/controller/probe_runner/test_parse_handshake.py
from mthydra.controller.probe_runner.probers import (
    parse_handshake_probe_output, HandshakeProbeResult,
)


def test_parse_ok():
    r = parse_handshake_probe_output(
        "mthydra-rh result=ok ja3=771,4865-4866,0-23,29-23,0 ttfb_ms=42\n"
    )
    assert r == HandshakeProbeResult(
        result="ok", ja3="771,4865-4866,0-23,29-23,0",
        ttfb_ms=42, detail=None,
    )


def test_parse_reset():
    r = parse_handshake_probe_output("mthydra-rh result=reset detail=rst_by_peer")
    assert r.result == "reset"
    assert r.ja3 is None
    assert r.detail == "rst_by_peer"


def test_parse_garbage_is_error_result():
    r = parse_handshake_probe_output("totally unexpected text")
    assert r.result == "error"
    assert r.ja3 is None


def test_parse_empty_is_error():
    assert parse_handshake_probe_output("").result == "error"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/controller/probe_runner/test_parse_handshake.py -v`
Expected: FAIL — names not defined.

- [ ] **Step 3: Implement in `probers.py`**

```python
from dataclasses import dataclass


@dataclass(frozen=True)
class HandshakeProbeResult:
    result: str            # 'ok' | 'reset' | 'timeout' | 'tcp_fail' | 'error'
    ja3: str | None = None
    ttfb_ms: int | None = None
    detail: str | None = None


def parse_handshake_probe_output(stdout: str) -> HandshakeProbeResult:
    """Parse the `mthydra-rh` one-line contract. Never raises — unrecognised
    output becomes result='error' so a broken helper degrades to a signal,
    not a crash."""
    line = next((l for l in stdout.splitlines() if l.startswith("mthydra-rh ")), None)
    if line is None:
        return HandshakeProbeResult(result="error", detail="no mthydra-rh line")
    fields: dict[str, str] = {}
    for tok in line.split()[1:]:
        if "=" in tok:
            k, _, v = tok.partition("=")
            fields[k] = v
    result = fields.get("result", "error")
    ttfb = fields.get("ttfb_ms")
    return HandshakeProbeResult(
        result=result,
        ja3=fields.get("ja3"),
        ttfb_ms=int(ttfb) if (ttfb and ttfb.isdigit()) else None,
        detail=fields.get("detail"),
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/controller/probe_runner/test_parse_handshake.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add src/mthydra/controller/probe_runner/probers.py tests/unit/controller/probe_runner/test_parse_handshake.py
git commit -m "feat(V5): parse reality-handshake probe output contract"
```

---

## Task 2: `probe_reality_handshake` (injected ssh_cmd_fn)

**Files:**
- Modify: `src/mthydra/controller/probe_runner/probers.py`
- Test: `tests/unit/controller/probe_runner/test_reality_handshake_prober.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/controller/probe_runner/test_reality_handshake_prober.py
from mthydra.controller.probe_runner.probers import probe_reality_handshake


def test_ok_handshake():
    def fake_ssh(cmd):  # cmd is the remote shell string
        assert "9.9.9.9" in cmd and "443" in cmd and "cover.example" in cmd
        return "mthydra-rh result=ok ja3=771,4865,0,29,0 ttfb_ms=30\n"
    r = probe_reality_handshake(
        fake_ssh, exit_endpoint="9.9.9.9:443",
        cover_sni="cover.example", reality_pubkey="pub==", fingerprint="chrome",
    )
    assert r.result == "ok"
    assert r.ja3 == "771,4865,0,29,0"


def test_ssh_failure_becomes_error():
    def boom(cmd):
        raise RuntimeError("ssh down")
    r = probe_reality_handshake(
        boom, exit_endpoint="9.9.9.9:443", cover_sni="c", reality_pubkey="p",
        fingerprint="chrome",
    )
    assert r.result == "error"
    assert "ssh down" in (r.detail or "")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/controller/probe_runner/test_reality_handshake_prober.py -v`
Expected: FAIL — `probe_reality_handshake` not defined.

- [ ] **Step 3: Implement**

```python
from collections.abc import Callable


def probe_reality_handshake(
    ssh_cmd_fn: Callable[[str], str],
    *,
    exit_endpoint: str,
    cover_sni: str,
    reality_pubkey: str,
    fingerprint: str,
) -> HandshakeProbeResult:
    """Dial an EU exit from a RU vantage with box-equivalent Reality params and
    report the handshake outcome + emitted JA3. `ssh_cmd_fn` runs a shell string
    on the vantage and returns stdout. Any transport error -> result='error'."""
    host, _, port = exit_endpoint.rpartition(":")
    cmd = (
        f"mthydra-rh --host {host} --port {port} "
        f"--sni {cover_sni} --pubkey {reality_pubkey} --fingerprint {fingerprint}"
    )
    try:
        out = ssh_cmd_fn(cmd)
    except Exception as e:  # transport failure is itself a signal, not a crash
        return HandshakeProbeResult(result="error", detail=str(e))
    return parse_handshake_probe_output(out)
```

> Match the real vantage SSH-exec helper's signature: existing probers receive an `ssh_cmd_fn`; confirm whether it takes a single command string or `(host, cmd)` by reading `probe_tls_fall_through` in this file, and align `probe_reality_handshake`'s first param to the same convention used by the wheel.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/controller/probe_runner/test_reality_handshake_prober.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/mthydra/controller/probe_runner/probers.py tests/unit/controller/probe_runner/test_reality_handshake_prober.py
git commit -m "feat(V5): reality-handshake prober over injected vantage ssh"
```

---

## Task 3: Fingerprint-staleness evaluator (pure)

**Files:**
- Create: `src/mthydra/controller/observability/fingerprint_staleness.py`
- Test: `tests/unit/controller/observability/test_fingerprint_staleness.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/controller/observability/test_fingerprint_staleness.py
from mthydra.controller.observability.fingerprint_staleness import (
    StaleFinding, evaluate_fingerprint_staleness, load_reference_set,
)


def test_match_is_not_stale():
    findings = evaluate_fingerprint_staleness(
        observed_ja3_by_fp={"chrome": "771,A,B,C,0"},
        reference={"chrome": {"771,A,B,C,0", "771,X,Y,Z,0"}},
    )
    assert findings == []


def test_drifted_fingerprint_is_stale():
    findings = evaluate_fingerprint_staleness(
        observed_ja3_by_fp={"chrome": "771,OLD,OLD,OLD,0"},
        reference={"chrome": {"771,NEW,NEW,NEW,0"}},
    )
    assert findings == [StaleFinding(
        fingerprint="chrome", observed_ja3="771,OLD,OLD,OLD,0",
    )]


def test_no_reference_for_fp_is_stale():
    findings = evaluate_fingerprint_staleness(
        observed_ja3_by_fp={"safari": "771,S,S,S,0"},
        reference={"chrome": {"771,C,C,C,0"}},
    )
    assert findings[0].fingerprint == "safari"


def test_missing_observation_skipped():
    # No JA3 captured (probe never returned ok) -> not a staleness finding.
    findings = evaluate_fingerprint_staleness(
        observed_ja3_by_fp={"chrome": None},
        reference={"chrome": {"771,C,C,C,0"}},
    )
    assert findings == []


def test_load_reference_set(tmp_path):
    p = tmp_path / "ja3_reference.json"
    p.write_text('{"chrome": ["771,A,0", "771,B,0"], "firefox": ["771,F,0"]}')
    ref = load_reference_set(p)
    assert ref["chrome"] == {"771,A,0", "771,B,0"}
    assert ref["firefox"] == {"771,F,0"}


def test_load_missing_reference_returns_empty(tmp_path):
    assert load_reference_set(tmp_path / "nope.json") == {}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/controller/observability/test_fingerprint_staleness.py -v`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement the module**

```python
# src/mthydra/controller/observability/fingerprint_staleness.py
"""Spec V §5.2 — flag deployed uTLS fingerprints whose probe-captured JA3 no
longer matches the operator-maintained current-browser reference set.

Pure logic + a tolerant JSON loader. The reference set is manual ops data
(same maintenance class as [data_exit.telegram_dcs]); a missing/empty file
yields no findings rather than crashing the snapshot."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class StaleFinding:
    fingerprint: str
    observed_ja3: str


def load_reference_set(path: Path | str) -> dict[str, set[str]]:
    """Load {fp: {ja3, ...}} from a JSON file. Missing/invalid -> {}."""
    p = Path(path)
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    return {str(fp): {str(j) for j in ja3s} for fp, ja3s in raw.items()}


def evaluate_fingerprint_staleness(
    observed_ja3_by_fp: dict[str, str | None],
    reference: dict[str, set[str]],
) -> list[StaleFinding]:
    """A deployed fingerprint is STALE if we captured a JA3 for it and that JA3
    is not among the reference JA3s for that fingerprint (including the case
    where the reference knows nothing about that fingerprint at all).

    No captured JA3 (None) -> skipped (the handshake-health signal covers that
    failure mode separately)."""
    findings: list[StaleFinding] = []
    for fp, observed in sorted(observed_ja3_by_fp.items()):
        if observed is None:
            continue
        known = reference.get(fp, set())
        if observed not in known:
            findings.append(StaleFinding(fingerprint=fp, observed_ja3=observed))
    return findings
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/controller/observability/test_fingerprint_staleness.py -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add src/mthydra/controller/observability/fingerprint_staleness.py tests/unit/controller/observability/test_fingerprint_staleness.py
git commit -m "feat(V5): pure fingerprint-staleness evaluator + reference loader"
```

---

## Task 4: Surface both signals as anti-obligations (snapshot + remediation)

Follow the K3 `box_eu_tunnel_unseen` template exactly: a producer writes an `obligation_clocks` row keyed `<prefix>::<target>`; `snapshot._ANTI_PREFIXES` must include the prefix so it renders; `remediation._REMEDIATIONS` must have a line.

**Files:**
- Modify: `src/mthydra/controller/observability/snapshot.py`
- Modify: `src/mthydra/controller/observability/remediation.py`
- Test: `tests/unit/controller/observability/test_new_anti_obligations.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/controller/observability/test_new_anti_obligations.py
from mthydra.controller.observability.snapshot import _ANTI_PREFIXES, _classify_obligation
from mthydra.controller.observability.remediation import remediation_for


def test_prefixes_registered():
    assert "tls_fingerprint_stale" in _ANTI_PREFIXES
    assert "eu_exit_handshake_degraded" in _ANTI_PREFIXES


def test_classify_per_target():
    assert _classify_obligation("tls_fingerprint_stale::chrome") == (
        "tls_fingerprint_stale", "per_target", "chrome",
    )
    assert _classify_obligation("eu_exit_handshake_degraded::eu-node-1") == (
        "eu_exit_handshake_degraded", "per_target", "eu-node-1",
    )


def test_remediation_present():
    assert remediation_for("tls_fingerprint_stale::chrome")
    assert remediation_for("eu_exit_handshake_degraded::eu-node-1")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/controller/observability/test_new_anti_obligations.py -v`
Expected: FAIL — prefixes/remediations not present.

- [ ] **Step 3: Implement**

In `snapshot.py`, add to `_ANTI_PREFIXES`:

```python
    # Spec V V5 — egress self-measurement signals.
    "tls_fingerprint_stale",
    "eu_exit_handshake_degraded",
```

In `remediation.py` `_REMEDIATIONS`, add:

```python
    "tls_fingerprint_stale": (
        "a deployed uTLS fingerprint's JA3 no longer matches a current popular "
        "browser (Details has the fp). Roll the pool: edit "
        "[descriptor.tls_fingerprints] in controller.toml and wait for the next "
        "descriptor rotation (or 'mthydra-controller descriptor-sign-now'). "
        "Confirm with 'mthydra-controller fingerprint-staleness-show'."
    ),
    "eu_exit_handshake_degraded": (
        "the RU vantage is seeing TLS handshakes to this EU exit fail or get "
        "reset (Details has the verdict) — possible active blocking/throttling. "
        "Check the exit is up ('mthydra-controller data-exit-status' on the EU "
        "node) and reachable on :443 from RU; if a fingerprint or desync change "
        "preceded this, consider rolling it back."
    ),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/controller/observability/test_new_anti_obligations.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Severity check**

The generic per-target severity path (`severity_for_anti(kind, age_seconds=...)`) already handles unknown kinds. Add a test asserting a non-`info` severity for the new kinds if the project convention is to give handshake-degraded `crit`:

Run: `python -m pytest tests/unit/controller/observability/ -q`
If `severity_for_anti` returns `"info"` for unknown kinds and that's too quiet, add the kinds to its severity table in `severity.py` (mirror how `box_eu_tunnel_unseen` is rated) — `eu_exit_handshake_degraded` → `crit`, `tls_fingerprint_stale` → `warn`. Add a unit test for whichever mapping you choose.

- [ ] **Step 6: Commit**

```bash
git add src/mthydra/controller/observability/ tests/unit/controller/observability/test_new_anti_obligations.py
git commit -m "feat(V5): surface tls_fingerprint_stale + eu_exit_handshake_degraded signals"
```

---

## Task 5: Probe-runner wheel — run the prober, write/clear the rows

**Files:**
- Modify: `src/mthydra/controller/probe_runner/wheel.py`
- Test: `tests/unit/controller/probe_runner/test_wheel_reality_handshake.py`

Read the existing wheel first: it already iterates vantages × targets, runs probers via `ssh_cmd_fn`, and writes results. Follow how the K3 producer sets/clears the `box_eu_tunnel_unseen` anti-obligation (grep `box_eu_tunnel_unseen` to find the `obligations` set/clear helper — reuse it).

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/controller/probe_runner/test_wheel_reality_handshake.py
# Drive one wheel pass with a fake ssh that returns a 'reset' for one exit and
# an 'ok' (with a stale JA3) for the fingerprint, then assert:
#   - eu_exit_handshake_degraded::<node> row exists for the reset exit
#   - tls_fingerprint_stale::<fp> row exists when observed JA3 not in reference
#   - rows are CLEARED on a subsequent healthy pass
#
# Build a controller DB conn with: one live EU exit (eu_nodes + eu_exit_set),
# a v3 descriptor carrying tls_fingerprints, and a JA3 reference file missing
# the observed JA3. Use the existing wheel entrypoint (grep `def ` in wheel.py;
# follow the signature the serve loop calls).
```

Write the concrete test using the project's real wheel entrypoint and DB fixtures (mirror `tests/integration/` K3 tunnel-unseen test structure). Assert via `collect_snapshot(...).anti_obligations`.

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/controller/probe_runner/test_wheel_reality_handshake.py -v`
Expected: FAIL — wheel doesn't run the new prober yet.

- [ ] **Step 3: Implement in the wheel**

After the existing per-exit probes, for each live EU exit and each deployed fingerprint:

```python
from mthydra.controller.observability import fingerprint_staleness as fps_mod
from mthydra.controller.probe_runner.probers import probe_reality_handshake

# For each live exit (endpoint, cover_sni, reality_pubkey, node_id) and a
# representative fingerprint per box population (the descriptor's tls_fingerprints,
# or "chrome" fallback):
observed_ja3_by_fp: dict[str, str | None] = {}
for fp, _weight in (descriptor_tls_fingerprints or (("chrome", 0),)):
    res = probe_reality_handshake(
        ssh_cmd_fn, exit_endpoint=endpoint, cover_sni=cover_sni,
        reality_pubkey=reality_pubkey, fingerprint=fp,
    )
    observed_ja3_by_fp.setdefault(fp, res.ja3)
    if res.result in ("reset", "timeout", "tcp_fail", "error"):
        _set_anti(conn, f"eu_exit_handshake_degraded::{node_id}",
                  now=now, details=f"{fp}:{res.result}:{res.detail or ''}")
    else:
        _clear_anti(conn, f"eu_exit_handshake_degraded::{node_id}")

# Staleness, once per pass:
reference = fps_mod.load_reference_set(ja3_reference_path)
for finding in fps_mod.evaluate_fingerprint_staleness(observed_ja3_by_fp, reference):
    _set_anti(conn, f"tls_fingerprint_stale::{finding.fingerprint}",
              now=now, details=f"observed_ja3={finding.observed_ja3}")
# Clear stale rows for fingerprints that now match:
stale_fps = {f.fingerprint for f in
             fps_mod.evaluate_fingerprint_staleness(observed_ja3_by_fp, reference)}
for fp in observed_ja3_by_fp:
    if fp not in stale_fps:
        _clear_anti(conn, f"tls_fingerprint_stale::{fp}")
```

`_set_anti` / `_clear_anti` are the existing anti-obligation set/clear helpers the K3 producer uses (grep `box_eu_tunnel_unseen` to find them in `mthydra.controller.state.obligations`); call them with the same argument shape. `ja3_reference_path` and `descriptor_tls_fingerprints` are threaded in from config/DB the same way the wheel already receives its other inputs.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/controller/probe_runner/test_wheel_reality_handshake.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/mthydra/controller/probe_runner/wheel.py tests/unit/controller/probe_runner/test_wheel_reality_handshake.py
git commit -m "feat(V5): wheel runs reality-handshake prober; sets/clears V5 signals"
```

---

## Task 6: `fingerprint-staleness-show` CLI + config + runbook

**Files:**
- Modify: `src/mthydra/controller/cli.py`
- Modify: `packaging/etc/mthydra/controller.toml.example`
- Modify: `doc/runbook.md`
- Test: `tests/unit/controller/test_cli_fingerprint_staleness_show.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/controller/test_cli_fingerprint_staleness_show.py
from mthydra.controller import cli


def test_show_lists_findings(capsys, controller_toml_path, ja3_reference_file):
    # Pre-seed an observed-JA3 source the command reads (latest probe_results),
    # or accept that with no probe data the command prints "no observations".
    rc = cli.main(["fingerprint-staleness-show", "--config", str(controller_toml_path)])
    assert rc == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/controller/test_cli_fingerprint_staleness_show.py -v`
Expected: FAIL — unknown subcommand.

- [ ] **Step 3: Implement**

Register `fingerprint-staleness-show`: load the reference set, read the most recent observed JA3 per fingerprint from the wheel's stored probe results (or report "no observations yet"), run `evaluate_fingerprint_staleness`, print each fp as `OK` or `STALE (observed_ja3=...)`.

- [ ] **Step 4: Config + runbook**

`controller.toml.example`, new section:

```toml
[ru_egress]
# Path to the operator-maintained current-browser JA3 reference set (JSON:
# {"chrome": ["<ja3>", ...], "firefox": [...]}). Missing file => staleness
# checks are skipped (no findings). Maintenance: runbook §V.1.
ja3_reference_path = "/etc/mthydra/ja3_reference.json"
```

`doc/runbook.md`, new section "§V.1 — Maintaining the JA3 reference set": how to capture current-browser JA3s (e.g. from a JA3 fingerprint database / a controlled browser run), the JSON shape, and that a stale-but-uncurated reference produces false `tls_fingerprint_stale` alerts — refresh it when browsers do a major release.

- [ ] **Step 5: Run + commit**

Run: `python -m pytest tests/unit/controller/test_cli_fingerprint_staleness_show.py -v`
Expected: PASS.

```bash
git add src/mthydra/controller/cli.py packaging/etc/mthydra/controller.toml.example doc/runbook.md tests/unit/controller/test_cli_fingerprint_staleness_show.py
git commit -m "feat(V5): fingerprint-staleness-show CLI + ja3 reference config + runbook"
```

---

## Task 7: Full-suite regression + CHANGELOG

- [ ] **Step 1: Run the changed-scope suites**

Run: `python -m pytest tests/unit/controller/ -q`
Expected: PASS. Lint changed files only (ruff-version memo).

- [ ] **Step 2: CHANGELOG**

```markdown
- feat(V5): RU-egress self-measurement — reality-handshake prober on RU
  vantages captures EU-exit handshake health + emitted JA3; controller
  flags fingerprint staleness vs an operator-maintained reference. New
  signals tls_fingerprint_stale / eu_exit_handshake_degraded surface in the
  observability snapshot + remediation. CLI: fingerprint-staleness-show.
```

- [ ] **Step 3: Commit + push**

```bash
git add CHANGELOG.md
git commit -m "docs(V5): CHANGELOG — egress self-measurement"
git push origin main
```

---

## Self-Review (completed during authoring)

- **Spec coverage:** §5.1 prober (Tasks 1,2) + wheel run (Task 5); §5.2 staleness (Task 3) consuming probe-captured JA3 (Task 5); surfacing via snapshot/remediation like K3 (Task 4); §8 CLI (Task 6); §5.3 residuals — reference set as manual ops data with a runbook entry (Task 6); reuse of existing RU vantages (no new vantage class) noted in Architecture.
- **Placeholder scan:** the only deferred-to-implementer spellings are *existing* symbols (the wheel entrypoint, the `_set_anti`/`_clear_anti` helpers the K3 producer already uses, the vantage `ssh_cmd_fn` convention) — confirmed by grepping `box_eu_tunnel_unseen` and `probe_tls_fall_through`. The pure logic (parser, evaluator) is complete and tested.
- **Type consistency:** `HandshakeProbeResult.ja3: str | None` flows into `observed_ja3_by_fp: dict[str, str | None]`, which `evaluate_fingerprint_staleness` consumes; `StaleFinding(fingerprint, observed_ja3)` is the only finding type. Anti-obligation keys are `tls_fingerprint_stale::<fp>` and `eu_exit_handshake_degraded::<node_id>` consistently across snapshot, remediation, and the wheel.
- **Dependency:** uses V1's `tls_fingerprints` descriptor field — V5 runs after V1 per build order.
