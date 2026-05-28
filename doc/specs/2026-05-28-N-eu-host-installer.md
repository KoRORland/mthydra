# Spec N — EU Host Installer (one-shot active + warm-standby setup)

Status: **Draft, awaiting operator review.**
Predecessor: `doc/runbook.md` §1 (Initial deployment), §10 (Controller restore / promotion), §11 (Periodic maintenance); `doc/specs/2026-05-20-F-eu-node-setup.md` (role gating + `promote-active`); existing `mthydra-ops` orchestrator (`src/mthydra/ops/main.py`).
Successors blocked on this: none. A future spec will cover RU-image build automation and RU-node provisioning automation — both **explicitly out of scope here** (see §2 N-D2).

---

## 1. Purpose

Turn runbook §1 (and the §10.2 promotion path) from a 40-step hand-run checklist into **two tested one-shot orchestrators** that bring a naked Ubuntu 24.04 root shell to a running controller:

- `mthydra-ops install` — first EU node (role `active`).
- `mthydra-ops install-standby [--promote --case A|B]` — warm substitute (role `standby`), optionally promoted to active in the same run.

The runbook today is correct but error-prone: dozens of copy-paste commands across §1.1–§1.11, easy to skip the load-bearing §1.8 sink check, easy to forget the systemd units and the §11 maintenance cadence, and no memory of what is already done across a re-run. This spec specifies an installer that is **idempotent** (re-runnable, state-derived), **chatty by default / fully verbose on demand**, **secret-safe** (no secret on any child `argv`; redacted install log), and **TDD-covered**.

Out of scope (deferred to separate automation, per operator decision 2026-05-28):
- **RU image build** (runbook §3.2 `image-build`) — needs an upstream release + profile + B2 upload; belongs in its own pipeline.
- **RU-node provisioning instructions / artifacts** (runbook §3.3, spec G `ru-provision`) — needs a cover-domain pool and attested vantages that a fresh host does not have.

This installer finishes at "EU control-plane host is live and self-monitoring." Everything RU-facing is a later, separate command.

---

## 2. Locked design decisions

Approved during brainstorming session 2026-05-28.

| ID | Decision | Rationale |
|---|---|---|
| N-D1 | **Thin shell bootstrap + Python orchestrator.** `scripts/install.sh` does only what must precede Python (apt prereqs, `git clone`, venv build), then `exec`s `mthydra-ops install[-standby]`. All real logic lives in tested Python (`src/mthydra/ops/install.py`). | A naked host has no `mthydra-ops` yet, so *something* dumb must bootstrap. Keeping that layer trivial (and rarely-changing) puts the testable logic where TDD can reach it. Mirrors mature installers (rustup, k3s). |
| N-D2 | **EU-host only. No RU image build, no RU provisioning** in this installer. | A fresh first host has no cover pool, no attested vantages, no soak — so an image could at best be a `candidate` and never promote, and RU boxes cannot be provisioned anyway. Operator will build separate automation for both. |
| N-D3 | **Two orchestrators, shared phase library.** `install` (active) and `install-standby`. Not one `--role` switch. | The role flows differ enough (sink config requirements, preflight vs standby readiness, promotion path) that two named, self-documenting entry points beat a mode flag. The common work (preconditions, setup-host, verify-install, bootstrap, service, summary) is a shared phase library. |
| N-D4 | **Idempotent, state-derived phases.** Each phase has an `is_satisfied(ctx)` probe that inspects the live system; the orchestrator skips satisfied phases and completes only the missing pieces. No hand-kept progress file. | Directly answers the operator's complaint: "remembering what bits are set up doesn't scream finished product." Re-running after any partial failure Just Works. State of truth is the system itself (DB, files, systemd), never a sidecar checklist. |
| N-D5 | **`--promote` on `install-standby`; `--case A|B` controls credential rotation.** Case B (active compromised) additionally runs `authority-rotate` + `signing-key-rotate` and prints the manual rotation checklist; it does **not** auto-rotate provider/SMTP/bot creds. | Matches runbook §10.2 and spec F-D6: the A/B classification is a judgement call, and auto-rotating everything on a mis-classified B is the failure path. The installer orchestrates the in-band rotations and *prints* the out-of-band ones. |
| N-D6 | **Promotion implies full config.** A passive standby needs only the reduced config (no `[observability.*]` / `[distribution.*]` sinks — it emits nothing, §1.11). `--promote` makes it active, which *must* emit alerts → the loader requires the full config when `--promote` is set. | A promoted standby that cannot page the operator is a silent-failure trap. Enforcing this at config-validation time fails fast, before any irreversible step. |
| N-D7 | **The §1.8 sink confirmation is a hard human gate.** Preflight sends the crit test + heartbeat and checks return codes automatically, then refuses to enable the controller service until the operator confirms (interactively, or `assume_sinks=true` in the ini) that **both** sinks actually arrived out-of-band. | §1.8 is "the single most important pre-flight check." A green return code only proves the send didn't error — not that the message landed. The installer must not declare success on an unverified channel. |
| N-D8 | **Secrets never touch a child `argv`; install log is redacted.** Secrets come from the ini (0600 root file) or env; when invoking `mthydra-controller`, secrets are passed via the child **environment** (the pattern `cmd_bootstrap` already uses). The always-on install log routes through a redacting writer. | Preserves the existing `ps`-leak guard. An install log that captured a bot token or B2 key would be a regression worse than the manual runbook. |
| N-D9 | **Scheduler = systemd timers** (`Persistent=true`), not cron. | The repo already ships a systemd controller unit; timers give journald logging and missed-run catch-up across reboots. More "mature product" on 24.04 than `/etc/cron.d`. (Operator's literal "cron jobs" wording superseded after weighing this.) |
| N-D10 | **`--dry-run` prints the plan, never executes.** Lists each phase, its satisfied/not-satisfied state, and the exact commands it *would* run. | Operator confidence on a destructive-looking one-shot, and a clean test seam. |

---

## 3. Components

### 3.1 `scripts/install.sh` — bootstrap (N-D1)

POSIX `sh`, no non-coreutils deps. Responsibilities, in order:

1. `set -eu`; trap to print the failing line.
2. Assert `id -u` = 0; refuse otherwise.
3. Detect OS: read `/etc/os-release`; if not Ubuntu 24.04, **warn and continue** (spec F tests only 24.04, but a forced run on 24.10/Debian should be possible).
4. `apt-get update` + `apt-get install -y` the *build* prereqs only: `python3.12 python3.12-venv git age build-essential`. (The full canonical runtime package set + user + dirs are owned by the Python `setup-host` phase, idempotently.)
5. Resolve source: flags `--git-url`, `--git-ref` (default a pinned release tag), `--src-dir` (default `/opt/mthydra/src`). If `--src-dir` already contains a checkout, `git fetch && git checkout <ref>` instead of clone (idempotent).
6. Build venv at `--venv-dir` (default `/opt/mthydra/venv`); `pip install` the source.
7. Smoke: `"$VENV/bin/mthydra-ops" --help` >/dev/null; abort with a clear message if it fails.
8. `exec "$VENV/bin/mthydra-ops" "$SUBCMD" "$@"` where `$SUBCMD` is `install` unless `--standby` was passed (then `install-standby`), forwarding `--config`, `--promote`, `--case`, `--verbose`, `--quiet`, `--dry-run`.

The shell holds **no** mthydra domain logic and **no** secrets. Testing: `shellcheck` in CI + one smoke invocation with a stub `mthydra-ops` on `PATH` asserting the right subcommand/args are `exec`'d.

### 3.2 `src/mthydra/ops/install.py` — orchestrator

Four units, each independently testable:

**(a) `Config` loader.** Reads the ini via `configparser`, then for any required-but-missing or invalid field, prompts interactively (`input()` for non-secret, `getpass.getpass()` for secret) — unless `--non-interactive`, which turns a missing required field into a hard error. Produces a frozen typed `Config`. Knows which fields are secret (`SECRET_FIELDS`) and which are required for `active` vs `passive-standby` vs `promoted-standby` (N-D6). Validations: `age.recipient` starts with `age1` and is **not** `AGE-SECRET-KEY-` (N-D8/§1.2 hard refusal); email contains `@`; ports are ints; hostname non-empty.

**(b) `RedactingLog`.** A writer wrapping the log file (and, in verbose mode, a tee to the terminal). Given the set of secret *values* from `Config`, replaces each occurrence with `***REDACTED:<field>***`; plus regex fallbacks for `AGE-SECRET-KEY-1[0-9A-Z]+` and Telegram bot-token shape `\d{8,10}:[A-Za-z0-9_-]{35}`. Every command line and every captured subprocess byte goes through it.

**(c) `Phase` + `Runner`.** `Phase = (name, is_satisfied(ctx)->bool, run(ctx)->None)`. `Runner` takes an ordered `list[Phase]` and a `ctx` (holds `Config`, `RedactingLog`, verbosity, `dry_run`). For each phase: log `[i/N] <name> …`; if `is_satisfied` → log `already satisfied → skip`; elif `dry_run` → log the commands it would run; else `run`, and on exception abort the whole pipeline with the phase name and the redacted error (no `--resume-from` needed — re-running is the resume, N-D4).

**(d) Orchestrator entry points.** `cmd_install(args)` and `cmd_install_standby(args)` build their phase lists (below) and a `ctx`, then drive the `Runner`.

### 3.3 Reused / refactored cores

`cmd_setup_host`, `cmd_bootstrap`, `cmd_preflight` in `main.py` are refactored so their bodies become callables — `setup_host(dry_run) -> int`, `bootstrap(cfg) -> int`, `preflight(db, config) -> int` — that *both* the existing standalone subcommands and the new install phases call. The existing subcommand behaviour and CLI surface are preserved (regression-tested); the install phases wrap these callables in `is_satisfied` probes. Subprocess invocation continues through the existing `_run_controller(..., env=...)` helper so the secret-via-env property (N-D8) is inherited unchanged.

---

## 4. Phase lists

### 4.1 `install` (active)

| # | Phase | `is_satisfied(ctx)` | `run(ctx)` |
|---|---|---|---|
| 1 | `preconditions` | always `False` (cheap re-check) | root; OS note; network reachability; `Config` fully loaded+validated; **refuse age secret key** |
| 2 | `setup-host` | `id mthydra` ok **and** the three dirs exist with expected owner+mode | full apt runtime set + `adduser --system --group mthydra` + dirs (`setup_host()` core) |
| 3 | `verify-install` | `mthydra-controller --help` rc 0 | (nothing to do; if unsatisfied, abort — install.sh build is broken, §1.4) |
| 4 | `bootstrap` | sub-probed: DB exists & `startup-check` rc 0; authority gen ≥ 1; `controller.toml` present; `age-recipient.txt` present | `bootstrap()` core, executing only the missing sub-steps (init / authority-migrate / write toml / write age-recipient.txt) |
| 5 | `preflight` | n/a (always runs unless `assume_sinks`) | `obs-alert-test crit` + `obs-heartbeat-now` + `startup-check`; then **human gate** (N-D7): prompt confirm-both-arrived; abort if denied |
| 6 | `service` | `systemctl is-active mthydra-controller` ok | install `packaging/systemd/mthydra-controller.service`, `daemon-reload`, `enable --now` |
| 7 | `first-descriptor` | `descriptor-show --json` `.generation ≥ 1` | `descriptor-sign-now` |
| 8 | `maintenance-timers` | each timer `is-enabled` | install + enable `mthydra-daily-check`, `mthydra-weekly-scan`, `mthydra-monthly-compact` `.timer`/`.service` |
| 9 | `summary` | always `False` | print running state + remaining out-of-band TODOs + install-log path |

`summary` TODO text is explicit and load-bearing: **(1)** confirm the §1.8 sinks if you skipped the gate; **(2)** back up the operator age key to two non-cloud locations (§1.2) — it is NOT on this host and must never be; **(3)** stand up the warm standby (`install-standby`) and `eu-node-add` it from here (§1.11); **(4)** RU image build and RU-node provisioning are separate automation, not yet run.

### 4.2 `install-standby`

Phases 1–3 identical. Then:

| # | Phase | Notes |
|---|---|---|
| 4 | `bootstrap` | `--role standby`; reduced config (no sink sections) unless `--promote` (N-D6) |
| 5 | `standby-readiness` | replaces `preflight`: run `startup-check` against the standby DB — must pass. There is **no** `standby-heartbeat-check` CLI in the current build (the runbook §10.2 reference predates it), and serve's own B2 polling (spec F-D5) is what tracks the active, so install verifies health via `startup-check` and reminds the operator to confirm liveness from the active's `eu-node-list` after `eu-node-add` |
| 6 | `service` | install controller unit, `enable --now` (serves as standby) |
| 6b | `promote` *(only if `--promote`)* | `promote-active --case <A\|B>`; **Case B** also `authority-rotate` + `signing-key-rotate`, then print the manual rotation checklist (B2 key, SMTP app passwords, bot tokens). After this phase the node is active. |
| 7 | `summary` | standby-specific TODOs: run `eu-node-add` on the active for this node (§1.11); if promoted, complete the printed Case-B manual rotations and re-run `obs-alert-test` to confirm sinks |

A **passive** standby installs no maintenance timers: it emits nothing, systemd auto-restarts the `serve` loop, and there is no standby-specific check command to schedule (dropped from an earlier draft for YAGNI). If `--promote`, the node becomes active, so the active `maintenance-timers` phase (#8 from §4.1) is appended before `summary`.

---

## 5. Config schema (`install.ini`)

```ini
[install]
git_url   = https://github.com/<you>/mthydra.git
git_ref   = v1.0.0
src_dir   = /opt/mthydra/src
venv_dir  = /opt/mthydra/venv
scheduler = systemd            ; only value supported (N-D9)
assume_sinks = false           ; true ⇒ skip the §1.8 human gate (non-interactive)

[node]
hostname = eu1.example.com

[age]
recipient = age1xxxxxxxx        ; PUBLIC key only; installer refuses AGE-SECRET-KEY-

[backup]                        ; B2 / S3-compatible
endpoint = https://s3.us-west-002.backblazeb2.com
bucket   = mthydra-prod-state
key_id   = 0012abc...
application_key = SECRET        ; secret — or set env B2_APPLICATION_KEY (env wins)

; --- below required for active and for promoted standby; omitted for passive standby ---
[observability.telegram]
bot_token = SECRET
chat_id   = 12345678
[observability.email]
smtp_host = smtp.example.com
smtp_port = 587
from_addr = alerts@example.com
to_addr   = operator@example.com
username  = alerts@example.com
password  = SECRET
[distribution.telegram]
bot_token = SECRET
[distribution.email]
smtp_host = smtp.example.com
smtp_port = 587
from_addr = dist@example.com
username  = dist@example.com
password  = SECRET
```

`SECRET_FIELDS = {backup.application_key, observability.telegram.bot_token, observability.email.password, distribution.telegram.bot_token, distribution.email.password}`. The ini file itself must be `0600`; the loader warns if it is group/world-readable. Ship `packaging/etc/mthydra/install.ini.example` and `install-standby.ini.example` (the latter without the sink sections, with a comment that `--promote` requires them).

---

## 6. Logging & verbosity

- **default (chatty):** `[i/N] <phase> …` banners; one result line per sub-step; long subprocess output suppressed from terminal with `(full output → <log>)`; reassurance lines on known-slow ops (apt, pip, git clone).
- **`--verbose`:** every subprocess's stdout/stderr also streamed to the terminal (through `RedactingLog`).
- **`--quiet`:** errors only.
- **always:** `/var/log/mthydra/install-<UTC-ISO>.log`, append-only, capturing every command + full captured output, redacted (N-D8). The chosen log path is printed in the `summary` phase.

---

## 7. Testing (TDD)

Tests live in `tests/` (pytest, matching existing suite). Written test-first per phase of the plan.

1. **Config loader** — ini parse; env-wins-over-ini for `application_key`; interactive prompt fills missing field (monkeypatched `input`/`getpass`); `--non-interactive` errors on missing required; **age-secret-key refusal**; passive-standby allows missing sinks, `--promote` rejects missing sinks (N-D6); malformed email/port/hostname rejected.
2. **RedactingLog** — every secret value masked; bot-token & age-secret regex fallbacks; non-secret text passes verbatim; multi-secret lines.
3. **Phase/Runner** — satisfied phases skipped; unsatisfied run; exception aborts pipeline at the right phase; `--dry-run` runs nothing and prints the plan; ordering preserved.
4. **`is_satisfied` probes** — each probe with mocked `subprocess`/filesystem returns correct skip/run (setup-host present/absent, DB present + startup-check rc, authority generation, controller.toml present, service active, descriptor generation, timer enabled).
5. **Orchestrator wiring** — `install` builds the 9-phase active list; `install-standby` builds the standby list; `--promote` inserts the `promote` phase and swaps timers; `--case B` adds the rotation calls.
6. **Refactor regression** — `setup_host()`/`bootstrap()`/`preflight()` cores behave exactly as the pre-refactor subcommands (existing tests continue to pass; add direct-call tests).
7. **`scripts/install.sh`** — `shellcheck` clean; smoke test with a stub `mthydra-ops` asserts the correct subcommand + forwarded args are `exec`'d, and that non-root / wrong-subcommand paths error.

A full live end-to-end (real B2, real SMTP, real systemd) remains a manual smoke documented in the `Makefile` (consistent with the existing `make smoke`), since it cannot run without real external credentials and a real host.

---

## 8. Out of scope / explicit non-goals

- RU image build (separate automation, N-D2).
- RU-node provisioning artifacts/instructions (separate automation, N-D2).
- Provisioning the EU VPS itself (operator brings a naked 24.04 root shell).
- Generating the operator age key (laptop-only, §1.2; installer consumes the public recipient).
- Auto-rotating provider/SMTP/bot credentials on Case B (printed checklist only, N-D5).
- Cross-host `eu-node-add` of the standby (documented post-step in `summary`).

---

*End of spec N. If anything here contradicts the runbook, the runbook is authoritative and this is a bug.*
