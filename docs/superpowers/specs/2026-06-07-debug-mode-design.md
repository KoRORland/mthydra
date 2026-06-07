# Debug Mode — Design

**Date:** 2026-06-07
**Status:** Draft for review
**Scope:** Operator-triggered diagnostic logging for both node types (EU controller, RU agent).

## Problem

When a node misbehaves there is no way to turn up diagnostic detail. Runtime
code emits terse `print(..., flush=True)` lines to stdout/stderr (→ journald);
there is no `logging`-based verbosity control, no structured capture of
incoming connections / DB activity / network talks, and no operator switch.

The two node types have opposite constraints:

- **EU (controller):** trusted operator host, persistent disk
  (`/var/log/mthydra`, `/var/lib/mthydra`), `controller.env` EnvironmentFile,
  `Restart=on-failure`. **Restarting the service is safe.**
- **RU (agent):** seizure-risk host. Everything on tmpfs (`/run/mthydra/`),
  journald `Storage=volatile`, swap off, core dumps off. `hardening.py`
  *refuses to run* unless `/var/log` and `/run/mthydra` are tmpfs. The seed
  lives on tmpfs and cloud-init is once-per-instance, so **a restart or
  power-off kills the box permanently.** No persistent storage allowed.

## Decisions (locked with operator)

1. **RU toggle:** flag file `/run/mthydra/debug.flag` (tmpfs), polled by a
   live agent. `touch` to enable, `rm` to disable. No restart.
2. **EU toggle:** `mthydra-controller debug enable|disable|status` CLI
   subcommand that flips a flag and restarts the unit.
3. **Redaction posture:** **full verbose, operator owns risk** — debug output
   may contain raw IPs, session identifiers, secrets. No redaction in the
   debug path. Gated behind explicit enable + a loud banner.
4. **EU log lifecycle:** size-rotated (`RotatingFileHandler`) **and**
   auto-expire — debug reverts to normal after a TTL (default 24h) unless
   re-armed, so it never silently fills disk or lingers forgotten.

## Architecture

Three pieces: a shared stdlib-only logging core, EU wiring (CLI + serve +
auto-expire), RU wiring (flag poller). Instrumentation calls are added at
chosen flow points in both node types.

```
                 mthydra/debuglog.py   (stdlib only; importable by both)
                 ┌───────────────────────────────────────────┐
                 │ enable(sink, rotate, backups) / disable()   │
                 │ is_enabled() · log(category, msg, **fields) │
                 │ logger "mthydra.debug"  (level toggled)     │
                 └───────────────────────────────────────────┘
        EU (controller)                         RU (ru_agent)
  debug CLI → flag file →               flag file /run/mthydra/debug.flag
  systemctl restart                       (tmpfs) ← operator touch/rm
        │                                        │
  _cmd_serve reads flag on start          poller thread (~5s) stats flag
  → debuglog.enable(/var/log/mthydra/      → debuglog.enable(/run/mthydra/
       debug.log, 10MB×5)                       debug/agent-debug.log, tmpfs)
  → banner                                  → banner
        │                                        │
  expiry-watcher thread (60s):            no TTL (box is ephemeral; reboot
   at TTL → disable + rm flag             wipes tmpfs anyway). rm flag = off.
```

### Shared core — `src/mthydra/debuglog.py` (NEW, stdlib only)

Lives at the `mthydra` top level (not under `controller`) so `ru_agent` can
import it without violating the "no controller imports" AST guard. Pure
stdlib (`logging`, `logging.handlers`, `pathlib`).

Public surface:

- `enable(*, sink: Path | None, max_bytes: int, backups: int) -> None` —
  set logger `mthydra.debug` to `DEBUG`; attach a `StreamHandler(stderr)` and,
  if `sink` is given, a `RotatingFileHandler(sink, maxBytes, backupCount)`.
  Idempotent (re-enable replaces handlers). Emits a one-line **banner**:
  `DEBUG MODE ON — verbose, UNREDACTED diagnostics (IPs/secrets may appear)`.
- `disable() -> None` — detach handlers, set level back to `WARNING`. Emits
  `DEBUG MODE OFF`.
- `is_enabled() -> bool` — cheap guard for hot paths.
- `log(category: str, msg: str, **fields) -> None` — no-op when disabled;
  otherwise emits `category=<cat> msg | k=v k=v`. Categories: `conn`, `db`,
  `net`, `refresh`, `child`, `seed`, `iptables`, `desync`, `tunnel`,
  `backup`, `sched`.

No redaction by design (decision 3). The banner is the safety contract.

### EU wiring (controller)

**CLI** — new subparser group in `src/mthydra/controller/cli.py`:

- `debug enable [--ttl-hours 24] [--no-restart]` — write flag file
  `/var/lib/mthydra/debug.flag` (JSON `{enabled_at, expires_at, ttl_hours}`),
  then `systemctl restart mthydra-controller` unless `--no-restart`.
- `debug disable` — remove flag, restart (unless `--no-restart`).
- `debug status` — read flag, print state + remaining TTL (no restart).

Flag helpers (parse / write / is-expired) live in a small
`src/mthydra/controller/debug_flag.py` so they are unit-testable without
touching `systemctl`. `systemctl` is invoked via `subprocess.run` and mocked
in tests.

**serve startup** (`_cmd_serve`, ~line 1887): after config load, read the
flag; if present and not expired, call `debuglog.enable(sink=
/var/log/mthydra/debug.log, max_bytes=10*1024*1024, backups=5)`. If the flag
exists but is already expired, ignore it and remove it.

**Auto-expire watcher:** a daemon thread started in `_cmd_serve` that wakes
every 60s; when `now >= expires_at` it calls `debuglog.disable()`, removes
the flag file, and logs the revert. Downgrades the *live* process — no
restart required to turn debug off at TTL.

**Defaults config (optional):** a `[debug]` section in `controller.toml`
(`rotate_max_mb = 10`, `rotate_backups = 5`, `default_ttl_hours = 24`).
Parsed in `config.py` as a frozen `DebugConfig` with these defaults so an
absent section keeps current behaviour. CLI flags override.

**Instrumentation points** (EU "incoming connections, DB, diagnostics"):

- `data_exit/exit_observer.py` (`EuExitObserver`) — observed incoming
  sessions / exit selection → `log("conn", ...)`.
- `state/db.py` `connect()` and query helpers — connection open + slow/all
  queries → `log("db", ...)`.
- `backup/triggers.py`, descriptor rotation, scheduler sweep ticks in
  `_cmd_serve` → `log("sched"/"backup"/"refresh", ...)`.

Existing `print` lines stay; debug calls are additive and gated.

### RU wiring (agent)

**Flag poller:** a new daemon thread in `src/mthydra/ru_agent/__main__.py`
(started alongside the descriptor-refresh and periodic-recheck threads). Every
~5s it `stat`s `/run/mthydra/debug.flag`:

- flag appears → `debuglog.enable(sink=/run/mthydra/debug/agent-debug.log,
  max_bytes=2*1024*1024, backups=2)` (tmpfs; small cap). Creates the
  `/run/mthydra/debug/` dir (tmpfs, mode 0700).
- flag disappears → `debuglog.disable()`.

No TTL on RU: the box is ephemeral, a reboot wipes tmpfs, and the operator
removes the flag to stop. Constant `DEBUG_FLAG_PATH = "/run/mthydra/debug.flag"`,
`DEBUG_DIR = "/run/mthydra/debug"`, `DEBUG_POLL_SECONDS = 5`.

**Hardening compatibility:** `/run/mthydra/debug/` is under the already-tmpfs
`/run/mthydra`; `hardening.verify_all()` checks swap/coredumps/journald/tmpfs
mounts only, so a subdir does not trip it. Debug output **never** touches
persistent storage.

**stdlib-only:** `debuglog.py` is stdlib-only and `ru_agent` imports only it,
so the `ru_agent` AST guard test continues to pass.

**Instrumentation points** (RU): `seed.py` verify, `binary.py`
download/verify, `config_gen.py` render, `iptables.py` / `desync.py`
install/verify, `supervisor.py` child launch/restart, `descriptor_refresh.py`
ticks, `tunnel_check.py` verdicts → `log("seed"/"net"/"iptables"/"desync"/
"child"/"refresh"/"tunnel", ...)`.

## Security

Full-verbose output can contain user IPs, session identifiers, Reality UUIDs,
and other secrets (decision 3). Mitigations:

- **Off by default**, behind an explicit operator action, with a loud banner
  in the log on every enable.
- **RU output is tmpfs-only** — dies on reboot/power-off; never persisted.
  A seized *running* box already exposes `/run`, so this matches the existing
  threat model rather than widening it. Runbook will warn: do not enable RU
  debug on a box you cannot promptly wipe.
- **EU output is rotated + auto-expiring** (24h default) so it cannot grow
  unbounded or linger forgotten.
- Debug path is **separate** from `install.py`'s `RedactingLog` (which keeps
  masking secrets in its own append-only operator log — unchanged).

## Testing

- `debuglog`: enable/disable level transitions, `is_enabled` gating, file
  sink + rotation, banner emission, `log()` no-op when disabled.
- `debug_flag` (EU): write/parse/round-trip, expiry boundary, corrupt-flag
  tolerance.
- EU CLI: `debug enable/disable/status` with `subprocess.run` (systemctl)
  mocked; `--no-restart`; TTL flag plumbing.
- EU auto-expire watcher: fake clock → disable + flag removal at TTL.
- RU poller: appear/disappear transitions with mocked flag + time; dir
  creation under tmpfs path; debug output goes only to `/run/mthydra`.
- `ru_agent` AST guard: still passes (no new controller imports).

## Packaging / docs

- `tmpfiles`: `/var/log/mthydra` already created by `ops install`; confirm it
  exists for EU `debug.log`. RU `/run/mthydra/debug` is created at runtime by
  the agent.
- `controller.toml.example`: add a commented `[debug]` section.
- `doc/runbook.md`: new "Debug mode" section for EU (`debug enable/disable/
  status`, TTL, where logs land) and RU (touch/rm flag, tmpfs-only, seizure
  warning).
- `CHANGELOG.md`: feature entry.

## Out of scope

- Remote/centralised debug collection (logs stay local to each node).
- Per-category runtime selection (enable is all-categories; categories exist
  for grepping, not for selective enable). Can be added later if needed.
- Changing the existing `print`-based runtime logging.
