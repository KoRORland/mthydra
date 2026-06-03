# Agent-boot container harness — design

Status: approved (brainstorm) — 2026-06-03
Owner: ops/RU-agent

## 1. Problem

The RU-agent boot path (hardening → seed verify → mtg download → config render →
iptables install → supervisor launch) was unit-tested in mocked pieces and never
run end-to-end against a real target. Every layer's first contact with a live
TimeWeb VM surfaced the next gap, costing a VM provision per bug. The bugs were
all *artifact/runtime* failures that mocks structurally cannot catch:

- agent tarball missing `mthydra.descriptor` (ModuleNotFoundError)
- descriptor read under the wrong key (`exits` vs `eu_exit_set`)
- `/run` is `noexec` → mtg can't exec
- image was the `.tar.gz`, not the extracted ELF
- image built for the controller's arch (arm64), not the RU box (amd64) → ENOEXEC
- sing-box not installed on the box

A local container that actually **runs the agent on amd64** would have caught all
of these in seconds.

## 2. Goal / non-goals

**Goal:** `make agent-harness` builds an amd64 container that mirrors a TimeWeb RU
box, runs the real `python3 -m mthydra.ru_agent` against a real seed and a real
amd64 mtg ELF, and asserts the agent boots fully (both children launched, mtg
listening). Deterministic, no cloud VM, no external network dependency on the EU
exit or Telegram.

**Non-goals:**
- Real Telegram traffic / the RU→EU Reality tunnel (needs the live EU exit;
  validated against the deployed box, not here).
- Reboot-safety of the box (separate concern: cloud-init once-per-instance +
  tmpfs).
- Running inside `pytest` / CI wiring (explicit `make` target now; a thin pytest
  wrapper can be added later).

## 3. Host requirement

Runs on a **native amd64** Docker host (so mtg/sing-box exec and iptables behave
normally). Not the arm64 controller. Documented in the target's help and a
preflight check in `run.sh` (warn if `uname -m` != x86_64).

## 4. Components

All under `harness/agent-boot/`.

### 4.1 `run.sh` (host; entrypoint of `make agent-harness`)
1. Preflight: docker present; warn if host arch != x86_64.
2. mtg: download `mtg-<ver>-linux-amd64.tar.gz` from the upstream release into
   `harness/agent-boot/.cache/` (skip if cached); extract the ELF by calling our
   own `mthydra.controller.image.builder._extract_runnable(...)` (exercises that
   code); record `sha256(elf)`.
3. seed: generate a real `seed.json` (see 4.4) with `image.url =
   file:///opt/harness/mtg` and `image.sha256 =` the ELF sha.
4. `docker build --platform linux/amd64` the image (4.2) with the agent source,
   the mtg ELF, `seed.json`, the entrypoint, and the shims.
5. `docker run --rm --platform linux/amd64 --privileged` the container.
   `--privileged` is used (throwaway test container on a dev box) because the
   boot needs all of: `NET_ADMIN` for iptables, mount for the tmpfs mounts, and
   the core_pattern handling in §4.3. Propagate the container's exit code as the
   harness result.

### 4.2 `Dockerfile`
- `FROM ubuntu:24.04` (matches the RU box).
- Install `python3 python3-cryptography iptables` and sing-box via the same
  install script the cloud-init uses.
- Copy: the `mthydra` agent packages (ru_agent + descriptor, mirroring
  `package_agent`), the mtg ELF → `/opt/harness/mtg`, `seed.json`, `entrypoint.sh`,
  and the `journalctl` + `shutdown` shims into `/usr/local/bin`.

### 4.3 `entrypoint.sh` (in-container, root)
Replicates the cloud-init host setup, then runs + polls the agent:
- `swapoff -a` (no-op), mount **exec** tmpfs at `/run/mthydra` and `/var/log`.
- Isolate `kernel.core_pattern`: it is a **host-global** sysctl (not namespaced),
  so the agent's apply/verify would otherwise read/mutate the host's value.
  `mount --bind` a file containing `|/bin/false` over
  `/proc/sys/kernel/core_pattern` so the agent operates on an isolated file and
  the host is never touched. (Requires `--privileged`; this is why §4.1 uses it.)
- Copy `seed.json` → `/run/mthydra/seed.json` (after the tmpfs mount).
- Launch `python3 -m mthydra.ru_agent` (PYTHONPATH at the agent code) in the
  background.
- Poll up to N seconds for the success criteria (§5); print the agent log; exit
  0 on success, non-zero otherwise.

### 4.4 Seed generation (host helper, e.g. `make_seed.py`)
Reuses controller code against a throwaway sqlite DB:
- real Ed25519 authority + descriptor signing key,
- an EU exit registered in `eu_exit_set` **with cover_sni + reality_pubkey** and a
  dummy endpoint (`192.0.2.1:443`, TEST-NET) so the signed descriptor carries an
  exit and `config_gen` renders,
- promoted image + a `candidate_verified` cover domain,
- `provision_box(...)` → real `SeedBundle`; then override `image.url`/`image.sha256`
  to the local file + ELF sha and write `seed.json`.

### 4.5 Shims (in-container, `/usr/local/bin`)
- `journalctl`: prints a `--header` that references `/run/log/journal` (and not
  `/var/log/journal`) so `hardening._journald_volatile()` passes — simulates the
  volatile-journald state the real cloud-init configures, without systemd-in-Docker.
- `shutdown`: records the invocation and exits non-zero so a power-off attempt is
  caught as a harness **failure** rather than silently no-op'd.

## 5. Success criteria

After launching the agent, poll up to N (default 30) seconds and require ALL:
- the `shutdown` shim was **not** invoked,
- the **mtg** process is alive,
- the **sing-box** process is alive,
- **mtg is listening on :443**.

The dummy EU exit (`192.0.2.1`) is unreachable; sing-box starts, retries the
outbound, and stays up — so the boot path is validated without the EU exit.
`sing-box check -c <rendered config>` runs first as a config-syntax gate.

## 6. Build-time ELF/arch guard (included)

Independently of the harness, harden `build_image`: after `_extract_runnable`,
verify the bytes are an ELF (`\x7fELF`) whose `e_machine` matches the expected
arch; otherwise `BuildError`. The expected arch is parsed from the
`linux-<arch>` token in `asset_filename` (the asset name already encodes it,
e.g. `mtg-2.2.8-linux-amd64.tar.gz`); map amd64→0x3E, arm64→0xB7,
armv7/armv6→0x28, 386→0x03. If the token is unrecognised, verify ELF magic only
(don't block unknown arches). Rejects a gzip-blob or wrong-arch artifact at
build time on the controller, before it reaches any box. Unit-tested with
crafted ELF headers (no real binary needed).

## 7. Testing

- The harness itself is the integration test; `make agent-harness` is the runner.
- `_extract_runnable` and the ELF/arch guard get unit tests (crafted bytes).
- The harness must FAIL loudly if any boot step regresses (that's its purpose):
  it surfaces the agent's stderr/log on failure.

## 8. File layout

```
harness/agent-boot/
  run.sh           # make target entrypoint (host)
  Dockerfile       # amd64 RU-box mirror
  entrypoint.sh    # in-container host-setup + run + poll
  make_seed.py     # real seed generator (host)
  shims/journalctl # volatile-journald shim
  shims/shutdown   # power-off detector
  .cache/          # cached mtg download (gitignored)
Makefile           # + agent-harness target
```
