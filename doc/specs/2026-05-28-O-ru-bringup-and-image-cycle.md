# Spec O — RU bring-up + image-cycle wizards

Status: **Draft, awaiting operator review.**
Predecessor: `doc/runbook.md` §3 (image lifecycle), §3.3 (provision canary boxes), §3.4 (soak), §3.5–§3.6 (gate + promote), §3.7 (age-out); `doc/specs/2026-05-21-G-ru-provisioning-artifact-generator.md` (`provision-seed`, G-D5 "operator runs `ru-box-mark-live` — spec I/J will later automate live-detection"); `doc/specs/2026-05-25-I-probe-vantage-harness.md` (I-D1 "probe execution is operator-driven for MVP; result ingest is automated via CLI"); `doc/specs/2026-05-21-D-ru-image-build-pipeline.md` (`image-build`, `image-promote-status`, `image-promote`); `doc/specs/2026-05-28-N-eu-host-installer.md` (orchestrator + secret-discipline patterns reused).
Successors blocked on this: none. Future spec L (T1 dormant health) and a future automated probe-execution spec (post-spec-I MVP) would compose cleanly with these wizards without changing their interface.

---

## 1. Purpose

The runbook's image-lifecycle and per-box bring-up flows are dozens of operator-typed commands across §3.1–§3.7 and §7. Spec O fills the **two operator-facing wizards** that complete the EU-side automation, leaving only the one step that cannot be remote-controlled — paste the cloud-init bundle into a RU/CIS provider console and report the resulting public IP:

- `mthydra-ops ru-bringup` — per-box: mint provision-seed → wait for the VM's first reachability on `:443` → mark-live. Replaces the §3.3 + §3.7 manual sequence and the post-§5 replacement-box motion.
- `mthydra-ops ru-image-cycle` — release-wide: image-build → N × `ru-bringup --canary` → poll-and-display the soak progress while the operator submits `probe-record` from registered vantages → operator-confirmed `image-promote`. Wraps the entire §3.1–§3.6 flow.

The wizards reuse every existing `mthydra-controller` subcommand intact; they add **orchestration + waiting + idempotent resume**, not new control-plane primitives. Probe execution stays operator-driven (per spec I-D1) and the promote prompt stays a judgement-call gate (per the runbook's §3.6 discipline).

Out of scope (deferred, see §8): probe-execution automation (spec I-D1 territory), provider-API VM provisioning (spec G2 territory; impractical for the RU/CIS providers this fleet targets), auto-promote without operator confirmation, auto-rollback, per-box `probe-credential-issue` orchestration (spec I2 already covers).

---

## 2. Locked design decisions

Approved during brainstorming session 2026-05-28.

| ID | Decision | Rationale |
|---|---|---|
| O-D1 | **Two layered subcommands.** `ru-bringup` is per-box; `ru-image-cycle` calls `ru-bringup --canary` N times. | The per-box flow is the routine §3.7 replace-on-burn work; the release-wide flow is the rarer §3 cycle. One tested per-box implementation feeds both. Building only the cycle would force operators back to a manual sequence for every replacement; building only the bring-up would leave the release work as a hand-orchestrated procedure. |
| O-D2 | **Probe execution stays operator-driven** (per spec I-D1). The wizard polls `image-promote-status` and pretty-prints what (box, vantage, check_type) cycles are still short; the operator submits `probe-record` from each vantage out-of-band. | Spec I-D1 is explicit that probe execution is deferred for honest reasons (vantage sourcing, network egress, payload generation). Folding probe execution in here would (a) build a substantial SSH-into-vantage subsystem the design didn't choose, and (b) couple this wizard to the not-yet-built probe-runner protocol. Orchestrating around the existing manual probes lands the user value now without prejudicing the later spec. |
| O-D3 | **`image-build` is folded into `ru-image-cycle`** as the first phase (download upstream `mtg` release + sha-verify + B2 upload + register candidate). | The operator's literal ask was "minimal manual intervention" — one command per release. `image-build` is otherwise a separate copy-paste step right before the cycle anyway. Keeping `mthydra-controller image-build` as a standalone command preserves the direct path; the cycle simply calls it. |
| O-D4 | **Reachability check = TCP-connect + TLS handshake to `<ip>:443` using the box's assigned SNI**, *without* certificate-chain validation. | Fake-TLS boxes present a cover-domain cert that won't validate against a public CA chain; the question we need answered is "is the box accepting :443 connections and completing a TLS handshake," not "is the cert valid." This is a coarse liveness check, intentionally distinct from spec I-grade probes which run from Russia-approximating vantages and judge *content*. Documenting the difference here prevents future confusion. |
| O-D5 | **Soak is operator-paced. Ctrl-C during `ru-image-cycle` is non-destructive** — writes the resume state and exits cleanly; re-running with the same `--release` (or `--resume`) picks up. | Soak duration depends on real wall-clock cycles from real vantages and cannot be force-completed. A wizard that blocks an operator's terminal for 24h+ is hostile. Resume-from-state is the only humane shape. |
| O-D6 | **Resume state lives in `/var/lib/mthydra/ru-cycle/<release>.json`** (one file per in-flight release). `ru-bringup` itself needs no extra resume file — the controller's `ru_boxes` row is the truth; `--box-id <id>` re-enters the per-box flow. | One source-of-truth per scope: the cycle has cross-phase state (which canaries minted, which IPs collected) that has no home in the DB; the per-box flow's state is already in `ru_boxes.state`. Splitting cleanly between "in DB" and "in cycle state file" avoids a parallel truth. |
| O-D7 | **`image-promote` always requires an interactive operator confirmation.** No `--yes`-style auto-promote. `--evidence` is auto-composed from soak data; the operator can override. | The runbook's §3.6 is explicit: promotion is a judgement call. An automated rubber-stamp on a passing soak defeats the control. The auto-composed evidence string saves typing without removing the human decision. |
| O-D8 | **Sequential wizard style, not the install.py Phase/Runner framework.** New module `src/mthydra/ops/ru_bringup.py`. | The install.py phases are idempotent state-derived steps that probe and skip. This flow is fundamentally sequential and operator-interactive (prompt for IP, wait for soak, confirm promote). Reusing Phase/Runner here would be procrustean. Borrowing install.py's secret-discipline patterns (run via `_run_controller`, no secrets on argv) is correct; borrowing its phase abstraction is not. |
| O-D9 | **Canary cohort spec = repeated `--canary-target provider=…,region=…` flags by default; `--cohort <file>` is a YAML alternative.** | A 2-canary cohort fits two flags; a 3-canary fits three. The YAML file is for the operator who wants to keep cohort configs under version control. Neither is mandatory infrastructure for the MVP — the flags ship first. |

---

## 3. Components

### 3.1 `src/mthydra/ops/ru_bringup.py`

Public surface (used by `main.py`'s dispatch):
- `cmd_ru_bringup(args) -> int` — per-box wizard entry.
- `cmd_ru_image_cycle(args) -> int` — release-wide wizard entry.

Internal helpers (each independently testable):
- `mint_seed(provider, region, *, canary, agent_source_url, agent_source_sha256, descriptor_refresh_url, cloud_init_out) -> str` — wraps `provision-seed`, returns the minted `box_id` (parsed from the controller's stderr per the existing `ru-provision` pattern).
- `wait_for_reachable(host, port, sni, *, timeout_s, poll_s=10, on_progress=None) -> bool` — TCP + `ssl.wrap_socket(server_hostname=sni)` handshake; `check_hostname=False`, no chain validation. Returns `True` on first success; `False` after `timeout_s`. `on_progress` is called each retry for UX.
- `mark_live(box_id, public_ip)` — wraps `ru-box-mark-live`.
- `box_state(box_id) -> str` — reads `ru-box-list --json` and returns the row's `state` (so the wizard can skip already-completed steps on resume).
- `wait_for_soak(image_version, *, poll_interval_s, on_progress, state_writer) -> SoakResult` — polls `image-promote-status --json` until `passed=true`. `on_progress` receives the parsed `reasons` list each tick for display. `state_writer` is called periodically so a Ctrl-C lands the latest state on disk.
- `CycleState` — frozen dataclass + `load(path)`/`save(path)` JSON serializer for `/var/lib/mthydra/ru-cycle/<release>.json`. Fields: `release`, `image_version`, `profile_path`, `image_built: bool`, `canaries: list[{box_id, provider, region, public_ip, marked_live_at}]`, `started_at`.
- `parse_cohort(flags_or_file) -> list[CanaryTarget]` — accepts either repeated `--canary-target provider=…,region=…` flags or a YAML file with the same shape.

### 3.2 `src/mthydra/ops/main.py` wiring

Two new subparsers (`ru-bringup`, `ru-image-cycle`) and two lazy dispatch wrappers (`_dispatch_ru_bringup`, `_dispatch_ru_image_cycle`) matching the pattern spec N installed for the installer subcommands (avoids circular import at module load).

---

## 4. `ru-bringup` flow

| # | Phase | Skip-if (idempotent) | Effect |
|---|---|---|---|
| 1 | mint | `--box-id <id>` was passed (resume) | Calls `provision-seed`; captures `box_id`; writes cloud-init to `--cloud-init-out` (mode 0600). |
| 2 | boot-handoff | `--public-ip` was passed | Prints the cloud-init path and a numbered cheatsheet; prompts `Public IP when VM is up:`. Ctrl-C prints the resume command (`mthydra-ops ru-bringup --box-id <id> --public-ip <ip>`) and exits 0. |
| 3 | reachability | `box_state(box_id) == "live"` | `wait_for_reachable(ip, 443, sni, timeout_s=--reach-timeout)`; on timeout, print a one-paragraph diagnostic and offer re-prompt or abort. |
| 4 | mark-live | `box_state(box_id) == "live"` | `ru-box-mark-live <box_id> --public-ip <ip>`. |
| 5 | summary | always runs | One paragraph: canary? what's next? (§3.4 soak for canary; in rotation for non-canary). |

**Flags:** `--provider`, `--region`, `--canary`, `--agent-source-url`, `--agent-source-sha256`, `--descriptor-refresh-url`, `--cloud-init-out` (default `/tmp/ru-cloud-init-<box>.yaml`), `--public-ip`, `--box-id` (resume), `--reach-timeout` (default 600s), `--non-interactive`, `--verbose`/`--quiet`/`--dry-run`.

The `--agent-source-url` / `--sha256` / `--descriptor-refresh-url` triple may also be read from `[ru]` in an optional `--config <file>` so the operator doesn't retype them per box. If both flag and config are present, the flag wins.

---

## 5. `ru-image-cycle` flow

| # | Phase | Skip-if (resume) | Effect |
|---|---|---|---|
| 1 | image-build | `state.image_built and image exists` | `image-build --release <ver> --profile-json <path>` (downloads upstream, sha-verifies, B2-uploads, records candidate). Records `image_version` into state. |
| 2 | canaries | each cohort entry whose `box_id` already in state as `marked_live_at` is set | For each `(provider, region)` in the parsed cohort: call into the same code as `cmd_ru_bringup` (with `--canary` set), prompting IP per box; record each completed canary in the state file as it finishes. |
| 3 | soak | always polls (no skip — the soak is the work) | `wait_for_soak(image_version, poll_interval_s=--soak-poll)`. Pretty-prints each tick's pending reasons (`box b-c1 / vantage kz1 / tls_fall_through: 2/4 cycles`, etc.). Operator submits `probe-record` from each vantage out-of-band. Ctrl-C is non-destructive (`state_writer` already persisted). |
| 4 | promote | `image-current` shows `image_version` as promoted | Interactive confirm prompt with the auto-composed evidence string; on `y`, `image-promote <image_version> --evidence "<auto-or-overridden>"`. On `n`, exit 0 (operator can decide to defer or rollback themselves). |
| 5 | summary | always | "iv-<ver> promoted; iv-<prev> retired. Existing boxes age out via replace-on-burn (§3.7) — use `ru-bringup` for replacements as they're terminated. No fleet-wide migration." Removes the state file. |

**Flags:** `--release` (required), `--profile-json` (required for phase 1; not required on resume past it), `--canaries N` **with** `--canary-target provider=…,region=…` (N times) **or** `--cohort <file>` (YAML: `targets: [{provider: selectel, region: ru-msk-1}, …]`), `--agent-source-url`/`--sha256`/`--descriptor-refresh-url` (or `--config`), `--soak-poll` (default 60s), `--soak-timeout` (default 0 = unlimited; operator-paced per O-D5), `--evidence` (override the auto-composed string), `--resume` (load state without re-parsing release args), plus the same verbosity/dry-run flags as `ru-bringup`.

**Auto-composed evidence** template:
```
soak from <started_at> to <now>; canaries: <box ids>; vantages: <distinct vantages contributing pass results>; cover-site behaviour: stable per probe_results; latency baseline within profile bounds
```
The operator overrides with `--evidence` if real-world circumstances need a different note.

---

## 6. Reachability check (O-D4 detail)

```python
def wait_for_reachable(host: str, port: int, sni: str, *,
                       timeout_s: int, poll_s: int = 10,
                       on_progress=None) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=5) as sock:
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE   # Fake-TLS cert; we want liveness only
                with ctx.wrap_socket(sock, server_hostname=sni) as tls:
                    tls.do_handshake()
                    return True
        except (OSError, ssl.SSLError) as e:
            if on_progress is not None:
                on_progress(e)
            time.sleep(poll_s)
    return False
```

**Why no cert validation:** Fake-TLS boxes present whatever cert the cover domain's upstream presents; the chain doesn't validate against the public CA store from outside Russia and isn't expected to. Validating would *break* the check on the boxes that are working correctly. Validating cert *content* — that the chain matches the cover domain's real chain as seen from Russia — is the spec I `cover_domain_consistency` probe's job, run from a vantage, ingested through `probe-record`. Spec O explicitly doesn't try to do that work here.

---

## 7. Testing (TDD)

Tests live in `tests/unit/ops/test_ru_bringup.py` (pytest, matching existing layout). Written test-first per plan task.

1. **`wait_for_reachable`** — local fixture spins up a tiny TLS server (stdlib `ssl` + `socket`) with a self-signed cert; assert success. Assert timeout when no listener. Assert backoff respected (mock `time.sleep`).
2. **`mint_seed`** — monkeypatch `_run_controller`; assert argv contains `provision-seed`, `--provider`, `--region`, `--canary` (when set); assert returned `box_id` parsed from the stderr line `box_id minted: b-…` (matches the existing `ru-provision` contract).
3. **`mark_live`** — monkeypatch `_run_controller`; assert argv contains `ru-box-mark-live`, the box_id, `--public-ip`.
4. **`box_state`** — monkeypatch `_run_controller` returning JSON; assert returned state string.
5. **`wait_for_soak`** — monkeypatch `_run_controller` to return `passed:false` once then `passed:true`; assert loop exits, `on_progress` was called once with the parsed reasons.
6. **`CycleState`** — round-trip save/load; resume from a partial state file (image_built=true, canaries=[one done, one not]).
7. **`parse_cohort`** — flags form, file form, mismatched count vs `--canaries` raises.
8. **`cmd_ru_bringup`** — end-to-end with monkeypatched `_run_controller`, `input()`, and `wait_for_reachable`; assert phase order (mint → handoff → reach → mark-live) and that `--box-id` resume skips mint.
9. **`cmd_ru_image_cycle`** — end-to-end with monkeypatched primitives; assert build → 2× bringup → soak-wait → promote → state file removed. Resume from a state file with `image_built=true, canaries=[one done]` skips the done work.
10. **Ctrl-C resume** — simulate KeyboardInterrupt mid-soak; assert state file contains the latest known progress and the process exits 0 with the resume command printed.

Live end-to-end (real B2, real RU VM, real vantages) remains a manual smoke documented in the `Makefile` (a `smoke-ru-cycle` target alongside `smoke-install`).

---

## 8. Out of scope / explicit non-goals

- **Probe execution automation.** Spec I-D1 defers this; spec O honours that. A future "probe runner" spec can slot in by adding a phase between 3 (soak start) and 3-poll without changing this spec's surface.
- **Provider-API VM provisioning.** Spec G2 territory; impractical for RU/CIS providers (no usable API from outside the cordoned network).
- **Auto-promote.** O-D7: promotion is always operator-confirmed.
- **Auto-rollback.** Runbook §9 stays operator-driven. The wizard's summary tells the operator how to rollback if needed.
- **`probe-credential-issue` orchestration.** Spec I2 already provides the per-(box, vantage) credential lifecycle; the operator runs those out-of-band of this wizard.
- **Shard assignment.** Spec H's reshuffle wheel handles it; not the wizard's job.
- **EU-side wait for the agent's first descriptor fetch from B2 as proof-of-life.** O-D4's `:443` reachability is the proof; reading B2 access logs to confirm descriptor fetch would couple to B2 and add no new information.

---

*End of spec O. If anything here contradicts the runbook or specs G/I/D, the named spec/runbook is authoritative and this is a bug.*
