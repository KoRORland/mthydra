# Spec P — EU-side RU automation (image, agent, SSH probes)

Status: **Draft, awaiting operator review.**
Predecessor: `doc/specs/2026-05-28-O-ru-bringup-and-image-cycle.md` (ru-bringup wizard + image-cycle); `doc/specs/2026-05-25-I-probe-vantage-harness.md` (I-D1 deferred probe execution — this spec lifts that deferral for the three objective probers).
Successors blocked on this: none.

---

## 1. Purpose

Spec O shipped the EU-side wizards that orchestrate per-box bring-up and per-release image cycles. In practice the operator still had to: type the release tag, host the mtg binary manually, package the ru-agent tarball, presign URLs by hand, and submit every `probe-record` from each vantage by hand. The MVP quickstart (2026-05-30) hit that wall on the first real deployment.

Spec P fills the remaining gaps so the operator's only RU-side touchpoint is: paste cloud-init in the TimeWeb console, give back the IP. Everything else lives on the EU controller:

- **`mthydra-ops image-prepare`** — resolves "latest" against GitHub, picks the right arch, auto-generates a minimal profile, builds + (optionally) promotes the image. One command per release refresh.
- **`mthydra-ops agent-publish`** — tars the running `mthydra/ru_agent` code, uploads to the operator's S3 bucket via the controller's existing credentials, presigns, records the URL in `/var/lib/mthydra/agent.json`. Reused automatically by `ru-bringup`.
- **`probe runner wheel`** — a daemon thread inside `serve` that periodically SSHes into each vantage and runs the three objective probers (`tls_fall_through`, `cover_domain_consistency`, `surface_scan`) for each live RU box, ingesting results via `probe-record`. Operator configures vantages once with `vantage-set-ssh`; the wheel runs unattended.

After this spec ships, `ru-bringup --provider X --region Y` works with no other flags (defaults resolve image + agent automatically), and probe-coverage stays green without operator input as long as vantages and boxes are reachable.

---

## 2. Locked design decisions

Approved during brainstorming session 2026-05-30.

| ID | Decision | Rationale |
|---|---|---|
| P-D1 | **One spec, three integrated subsystems.** Image, agent, and probes ship together because they jointly remove the same MVP busywork. Each subsystem is independently testable. | Decomposing into three specs would defer the user-visible payoff (a single `ru-bringup` call that works). Tight coupling by purpose, loose coupling by implementation. |
| P-D2 | **`agent-publish` reuses the `[backup]` S3 bucket** under the `agent/` key prefix; `agent/mthydra-ru-agent-<sha12>.tar.gz` is content-addressed (same tarball ⇒ same key ⇒ idempotent re-publish). Presigned URLs valid for 7 days. | The controller already holds boto3-usable S3 credentials in the DB. Forcing a second bucket would duplicate IAM, endpoint, region config for no real isolation gain (the agent code is open-source — leakage is not a confidentiality risk). Content-addressing makes re-publish safe and cheap. |
| P-D3 | **Probe runner is unconditional by default** (`cfg.probe.runner_enabled = true`). Operators who want to disable can set it false in `controller.toml`. | MVP value of the runner is huge (eliminates the only sustained ongoing operator burden after install). Opt-in would mean operators forget and accumulate `probe_coverage_pending` alerts forever — exactly the failure mode the runner exists to prevent. Off-switch retained for unusual deployments. |
| P-D4 | **`/var/lib/mthydra/agent.json`** is the published-agent state file: `{url, sha256, published_at, expires_at}`. `ru-bringup` reads it when `--agent-source-url` is not provided; auto-runs `agent-publish` if the file is missing or `expires_at` is within 24h. | Putting agent state in the DB would add a schema migration for a fundamentally transient artifact (presigned URLs expire; tarball content changes when code changes). A small JSON file is the right scope. The expiry-window check (re-publish if <24h left) avoids handing out stale URLs to the cloud-init that's about to be pasted. |
| P-D5 | **SSH key-only authentication.** `/usr/bin/ssh` via stdlib subprocess; no `paramiko`. `StrictHostKeyChecking=yes` + per-vantage `UserKnownHostsFile`. Operator places the private key on the EU host (recommended path: `/var/lib/mthydra/ssh/<vantage_id>.key`) and pre-populates known_hosts via `ssh-keyscan`. | Passwords would land in `ps`, env vars, and the audit log. `paramiko` is a 2MB dependency we don't otherwise need and has its own CVE history. The stdlib `ssh` CLI is the same tool every operator already knows; debugging is `ssh -v` away. |
| P-D6 | **Three MVP probers**: `tls_fall_through`, `cover_domain_consistency`, `surface_scan`. The remaining three from spec I (`valid_path_liveness`, `latency_loss`, `behavioural_identity`) stay operator-driven for now. | The MVP three are objective and trivially scriptable via `openssl s_client` / `nc`. The other three need real MTProto traffic and a circle-member-as-relay — that's spec-Q territory, not part of removing MVP busywork. |
| P-D7 | **Image promotion always requires interactive operator confirmation.** `image-prepare` prompts `[y/N]` after build succeeds; `--yes` available for non-interactive runs but is NOT the default. | Promotion is a judgment call (spec D), the runbook §3.6 is explicit about it, and spec O's `ru-image-cycle` already enforces this. Don't regress here. |
| P-D8 | **Latest-release resolution** uses GitHub's `repos/{owner}/{repo}/releases/latest` endpoint, NOT the per-tag endpoint. Resolved tag is then passed to existing `image-build` machinery unchanged. | Single API call, follows GitHub's notion of "latest" (which excludes drafts and prereleases). The existing `image-build` flow needs no other modification. |
| P-D9 | **`image-prepare` is synchronous** (operator-blocking wizard); the **probe runner is a daemon wheel** inside `serve`. | `image-prepare` is a one-shot operator action with a confirm prompt — wizard shape. The probe runner runs continuously and re-derives state on each tick — APScheduler wheel shape, matching the existing 14 wheels in `serve`. |

---

## 3. Components

### 3.1 `mthydra-ops image-prepare`

New subcommand in `src/mthydra/ops/image_ops.py` (new module). Flags:

- `--release` (default `latest`) — release tag or the literal `latest`.
- `--arch` (default `linux-amd64`) — picks the asset `mtg-<ver>-<arch>.tar.gz`.
- `--profile-json` (default `auto`) — if `auto`, writes a minimal placeholder JSON to a temp file and uses that (MVP only; the placeholder has the schema fields with sentinel values).
- `--yes` (default false) — auto-promote after build (skips the y/N prompt; spec P-D7).
- standard `--db-path` / `--config` / `--verbose` / `--quiet` / `--dry-run`.

Flow: resolve latest tag (one HTTPS GET to api.github.com); choose asset name; generate placeholder profile if needed; shell out to `mthydra-controller image-build --release <tag> --asset <name> --profile-json <path>`; prompt unless `--yes`; shell out to `mthydra-controller image-promote iv-<tag>`. Standard error surfacing (matching the fix landed in spec O's `mint_seed`).

### 3.2 `mthydra-ops agent-publish`

New subcommand in `src/mthydra/ops/agent_ops.py`. Flags:

- `--ttl-days` (default 7) — presigned URL lifetime.
- `--source-dir` (default `/opt/mthydra/src/src`) — root containing `mthydra/ru_agent/` and `mthydra/__init__.py`.
- standard `--db-path` / `--config` / `--verbose`.

Flow: tar `mthydra/ru_agent` + `mthydra/__init__.py` (excluding `__pycache__`, `*.pyc`); compute sha256; resolve S3 key `agent/mthydra-ru-agent-<sha12>.tar.gz`; upload via `boto3.client(endpoint_url=cfg.backup.endpoint, ...)` using credentials from `tokens.get_provider_credential(conn, "b2")`; mint presigned GET URL via `generate_presigned_url`; write `/var/lib/mthydra/agent.json` atomically (tempfile + fsync + rename — same pattern as spec M17 fix).

Idempotent: if `agent.json` already references a URL that hasn't expired AND its sha matches the tarball we'd produce, skip upload + presign (return existing URL).

### 3.3 `ru-bringup` agent auto-fetch

`cmd_ru_bringup` in `src/mthydra/ops/ru_bringup.py`:

- If `--agent-source-url` not given (currently required), read `/var/lib/mthydra/agent.json`.
- If file missing OR `expires_at` within 24h, auto-call `agent_ops.publish()` (importable function form of the subcommand) first.
- Pass the resolved URL + sha into `mint_seed`.

CLI flags `--agent-source-url` + `--agent-source-sha256` become optional (still accepted; explicit values override the auto-fetched ones).

### 3.4 Probe runner wheel

New schema column: `probe_vantages.ssh_host`, `ssh_port`, `ssh_user`, `ssh_key_path`, `ssh_known_hosts_path`. Migration v? → v?+1.

New CLI: `mthydra-controller vantage-set-ssh <vantage-id> --host --user --key-path [--port 22] [--known-hosts <path>]`. Updates the row; the runner picks it up on next tick.

New module `src/mthydra/controller/probe_runner/`:
- `wheel.py` — APScheduler tick at `cfg.probe.runner_interval_seconds` (default 1800s). For each `(box, vantage)` where both are live/active AND vantage has SSH configured, dispatch the three probers. Sequential per (box, vantage), parallel across pairs via a bounded `ThreadPoolExecutor(max_workers=4)`.
- `probers.py` — three functions, each returning `(status, evidence)`:
  - `probe_tls_fall_through(ssh_cmd, box_ip, cover_sni)` — runs `openssl s_client -connect <box>:443 -servername <sni> </dev/null 2>&1 | head -30` on the vantage; status=pass iff `Verify return code: 0` AND issuer matches the cover's known issuer (cached per cover_domain).
  - `probe_cover_consistency(ssh_cmd, box_ip, cover_sni)` — fetches `openssl s_client` against both `<box>:443` and `<cover_sni>:443` from the vantage; compares cert SAN + issuer + signature algorithm; status=pass iff all match.
  - `probe_surface_scan(ssh_cmd, box_ip)` — `nc -zv -w3 <box> 80 443 8080 22 53` from the vantage; status=pass iff ONLY 443 answers; hard_fail otherwise.
- `ssh.py` — `ssh_cmd(vantage_row, *cmd_parts) -> CompletedProcess`. Builds `/usr/bin/ssh -i <key> -p <port> -o StrictHostKeyChecking=yes -o UserKnownHostsFile=<known_hosts> -o ConnectTimeout=10 -o BatchMode=yes <user>@<host> --` then the command. No shell-quoting — commands are passed as separate argv to keep injection surface zero.

Each probe ingest is a `probe-record` subprocess call (matches spec I-D1 — same ingest path; just automated).

### 3.5 Config additions

`controller.toml` `[probe]` section:
- `runner_enabled = true` (P-D3)
- `runner_interval_seconds = 1800`
- `runner_max_concurrent = 4`

Defaults applied in `config.py`; absent section is treated as "all defaults."

---

## 4. Testing (TDD)

Tests in `tests/unit/ops/test_image_ops.py`, `tests/unit/ops/test_agent_ops.py`, `tests/unit/controller/test_probe_runner.py`.

- `image-prepare`: `latest`-resolution mocked against a fake GitHub JSON; asset selection by arch; placeholder profile generation; promote prompt path (yes / no / `--yes`).
- `agent-publish`: tar contents (assert `mthydra/ru_agent/__main__.py` present, no `__pycache__`); sha256 stable across re-runs of identical source; S3 upload mocked via monkeypatched boto3 client; agent.json atomic write; idempotent skip when URL is fresh AND sha matches.
- `ru-bringup` integration: auto-publish kick-in when agent.json missing; respect --agent-source-url override; expiry-within-24h triggers re-publish.
- Probe runner: `probe_tls_fall_through` parser against captured `openssl s_client` outputs (golden files in `tests/fixtures/openssl/`); `probe_surface_scan` parser against `nc` outputs; wheel dispatch with a fake SSH backend asserts ordering, concurrency, and `probe-record` ingest argv.
- `vantage-set-ssh`: schema migration; round-trip with `vantage-show`.

Live end-to-end (real S3, real vantage, real RU box) stays a `Makefile` `smoke-eu-automation` target — paste-the-procedure style, same as existing smoke targets.

---

## 5. Out of scope (deliberately)

- Password-based vantage SSH (key-only, P-D5).
- Building mtg from source on the controller (we use upstream releases; if upstream goes dark, that's a separate decision).
- Auto-promoting images without operator confirmation (safety gate stays, P-D7).
- `valid_path_liveness` / `latency_loss` / `behavioural_identity` probers (need real MTProto + circle-member relay; separate spec).
- Auto-resolving the `--arch` from the running OS (the EU host is amd64; the RU box is whatever — operator passes `--arch arm64` if their TimeWeb plan is Arm).
- An operator-facing GUI / web dashboard for any of this (CLI + state files only).

---

*End of spec P. Subsumes the "remaining busywork" complaints from the 2026-05-30 MVP install.*
