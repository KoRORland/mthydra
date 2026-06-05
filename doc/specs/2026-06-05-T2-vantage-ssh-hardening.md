# Spec T2 — Vantage SSH Hardening & Failover-Safe Probe Key

## 1. Purpose

Harden the probe-vantage SSH model and make it survive controller failover
without manual re-provisioning. Two problems with the T-Task `vantage-setup`
wizard (`src/mthydra/ops/vantage_setup.py`) as shipped:

1. **Single access method.** It only opens the vantage via `--root-key`. Some
   VPS providers expose *only* password auth for first login; others forbid
   password auth entirely and require the operator to pre-install a pubkey.
   Neither is supported.
2. **No failover survival.** Each controller generates its own per-vantage key
   on its local disk, and only the controller that ran `vantage-setup` is ever
   authorized on the vantage. The system runs an *active + warm-standby*
   controller pair (`design.md:39`, `install-standby --promote`). When a
   standby is promoted, its key was never authorized on any vantage and the
   active's key file is not in the backup — so probing silently breaks until
   the operator re-runs `vantage-setup` from the new active.

This spec also leaves the vantage in a **locked-down** state: after setup, the
only way in is the `probe` user with the controller's key — no password, no
root, no other user.

## 2. Scope

In scope: `mthydra-ops vantage-setup` access methods, sshd hardening, a single
shared probe keypair persisted in `state.sqlite`, and the startup/promotion
materialization path.

Out of scope (deliberate non-goals, with rationale in §3):

- Per-controller probe keys + an `authorized_keys` reconcile loop.
- A `sudo`-capable probe user.
- Password auth in the *steady-state* probe runner (spec P-D5 stands).

## 3. Decisions

| # | Decision | Rationale |
|---|----------|-----------|
| T2-D1 | **One shared probe keypair** for all vantages and all controllers, replacing today's per-vantage `<vantage_id>.key`. | The private key lives only on the controller (the vantage holds only the pubkey), so per-vantage keys isolate nothing. One key is strictly simpler with no security loss. |
| T2-D2 | **Source of truth for the keypair is a single-row table in `state.sqlite`**, not a loose file. The file at `/var/lib/mthydra/ssh/probe.key` is a 0600 *cache* materialized from the DB. | The backup pipeline (spec A §6.2) snapshots the SQLite DB *only*; `/var/lib/mthydra/ssh/` is never backed up. Putting the key in the DB makes it ride the existing encrypted backup for free — no change to `do_backup`/restore. The DB is already secrets-equivalent (spec A §57, 0600, age-encrypted off-box). |
| T2-D3 | **Three entry methods** converge on the same end state: (a) `--root-key`; (b) interactive password via TTY passthrough; (c) `--print-pubkey` for operator-pre-populated key. | Matches what providers actually offer. (b) and (c) cover providers that respectively *only* allow, or entirely *forbid*, password auth at first login. |
| T2-D4 | **Password is never handled via `sshpass`/argv/env/tempfile** — only interactive TTY passthrough to real `ssh`. | `sshpass` re-creates exactly the leak spec P-D5 rejects (password in `ps`, env, audit log). TTY passthrough keeps the password out of our process memory, argv, disk, and logs. This is what makes a one-time setup password safe. |
| T2-D5 | **A one-time setup password is acceptable** even though the steady-state runner forbids passwords (P-D5). | P-D5 targets the 30-min wheel, where a password would leak on *every* tick. A setup password used in a single TTY session and then made inert by hardening (T2-D7) is a different risk class. |
| T2-D6 | **Verify before harden.** After provisioning, open a *fresh* connection as `probe` with the shared key and confirm it works; only then harden. | Harden-before-verify means one typo'd key bricks the box, recoverable only via the provider's console/rescue. |
| T2-D7 | **Full lockdown (model A):** `AllowUsers probe` + `PasswordAuthentication no` + `PermitRootLogin no`; validate with `sshd -t`, then restart. | After setup the vantage is a dedicated probe host running no secrets. Restricting to `probe`+key kills the password/brute-force surface and the root path entirely. Operator accepts that future root access is provider-console-only. |
| T2-D8 | **`probe` user has no `sudo`.** | The three probers (`tls_fall_through`, `cover_domain_consistency`, `surface_scan`) run `openssl`/`ncat` unprivileged. Sudo would be over-privilege, not security. The only root action (one-time `apt-get install`) happens during the initial session, before lockdown. |
| T2-D9 | **Shared key, not per-controller keys + reconcile.** | The reconcile model only survives failover if the active pushes the standby's key *while still alive* — a timing footgun. With exactly two co-trusted, operator-owned controllers (a breach of either already exposes full state + backups + age keys), per-controller key isolation buys almost nothing. The shared key in the backup eliminates the timing trap. |

## 4. Schema

New single-row table (migration v16 → v17):

```sql
CREATE TABLE controller_probe_key (
  id           INTEGER PRIMARY KEY CHECK (id = 1),  -- enforce single row
  private_key  TEXT NOT NULL,    -- OpenSSH ed25519 private key (PEM)
  public_key   TEXT NOT NULL,    -- authorized_keys line
  created_at   TEXT NOT NULL,
  comment      TEXT              -- e.g. mthydra-probe-runner
);
```

The DB is already 0600 and age-encrypted in the backup, so the private key
inherits that protection. No new secrets-handling surface.

## 5. Key materialization

A helper resolves the working key file from the DB. Used at controller startup,
after promotion/restore, and at the top of `vantage-setup`:

```
ensure_probe_key(conn, ssh_dir) -> (key_path, pubkey):
  row = SELECT * FROM controller_probe_key WHERE id = 1
  if row is None:
    generate ed25519 keypair (in a temp dir)
    INSERT into controller_probe_key
    row = the new row
  key_path = ssh_dir / "probe.key"
  if key_path missing OR contents != row.private_key:
    atomically write row.private_key -> key_path (0600), row.public_key -> key_path.pub (0644)
  return key_path, row.public_key
```

DB is the source of truth; the file is a regenerable cache. A promoted standby
restores the DB, calls `ensure_probe_key` on startup, and writes the identical
key file — whose pubkey is already authorized on every vantage.

## 6. `vantage-setup` flow

```
mthydra-ops vantage-setup
    --vantage-id ru-msk-1
    --vantage-host <IPv4>
    [ --root-key <path> | --password | --print-pubkey ]
    [ --vantage-port 22 ] [ --ssh-dir /var/lib/mthydra/ssh ]
    [ --db-path /var/lib/mthydra/state.sqlite ]
```

1. `ensure_probe_key` → shared key path + pubkey (generates + persists to DB on
   first ever run).
2. **Entry method:**
   - `--print-pubkey`: print the pubkey and instructions, exit 0. Operator
     installs it (root or any sudo-capable user) out of band, then re-runs with
     no entry flag — `vantage-setup` connects *as `probe` with the shared key*
     if `probe` already exists, else *as the operator-chosen user* to bootstrap
     `probe`. (See §6.1.)
   - `--root-key <path>`: connect as `root` with that key (today's path).
   - `--password`: TTY passthrough — `exec ssh` with no `BatchMode`, operator
     types the password into ssh's own prompt. Never captured.
3. **Provision** over the opened session (idempotent):
   - `id probe || adduser --disabled-password --gecos '' probe`
   - install shared pubkey to `/home/probe/.ssh/authorized_keys` (0600, owned `probe`)
   - `apt-get install -y openssl ncat`
4. **Verify:** open a *fresh* `probe@host` connection with the shared key; run
   `true`/`echo OK`. Abort (no hardening) on failure.
5. **Harden (T2-D7):** write a drop-in to `/etc/ssh/sshd_config.d/` with
   `AllowUsers probe`, `PasswordAuthentication no`, `PermitRootLogin no`;
   `sshd -t`; on success restart sshd. Idempotent (overwrite drop-in).
6. **keyscan:** `ssh-keyscan` host key → `<ssh-dir>/known_hosts`.
7. **Register:** `mthydra-controller vantage-set-ssh <id> --user probe
   --key-path <shared key> --known-hosts <path> ...`.

### 6.1 The `--print-pubkey` (2c) path

For providers that forbid password auth and where the operator won't hand us a
root key. The operator installs the printed pubkey against a user with root or
sudo (their choice), then re-runs `vantage-setup` without an entry flag. The
wizard connects as that user (default `root`; override with `--bootstrap-user`)
using the shared key to run step 3, then proceeds. Once `probe` exists and is
authorized, subsequent runs connect directly as `probe`.

## 7. Failover & rotation

- **Failover:** standby promoted → restores DB → `ensure_probe_key` writes the
  identical key file → its pubkey is already in every vantage's
  `authorized_keys` → the probe wheel resumes unattended. Zero manual steps.
- **Rotation** (rare, manual; the only way to change keys post-lockdown, since
  root/password are gone): over the *current* authorized `probe` session,
  append the new pubkey to every vantage's `authorized_keys`, swap the
  `controller_probe_key` row + re-materialize the file, confirm the new key
  works everywhere, then remove the old pubkey. A `vantage-rotate-key`
  subcommand may automate this later; out of scope here.

## 8. Migration / compatibility

- The v16 → v17 migration adds `controller_probe_key`. On first `vantage-setup` (or
  controller startup) after upgrade, the keypair is generated and persisted.
- Existing per-vantage `<vantage_id>.key` files are left in place but no longer
  used; existing `probe_vantages.ssh_key_path` rows are repointed to the shared
  key by the next `vantage-setup` (or a one-shot `vantage-repoint-key` migration
  helper). Vantages already locked down with an old per-vantage key keep working
  until repointed (the old pubkey is still authorized); repoint runs over that
  authorized session and adds the shared pubkey before dropping the old.

## 9. Testing

- `controller_probe_key` round-trip + single-row CHECK constraint.
- `ensure_probe_key`: generate-on-empty, cache-hit, cache-rewrite-on-mismatch.
- `vantage-setup` flow with a fake `ssh` shim: each entry method reaches the
  same provisioned/verified/hardened end state; idempotent on re-run.
- Verify-before-harden ordering: a forced verify failure leaves sshd un-hardened.
- `sshd -t` failure aborts the restart.
- Failover sim: wipe the key file, restore a DB row, confirm `ensure_probe_key`
  rematerializes an identical key.
