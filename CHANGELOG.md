# Changelog

Operator-facing release notes for the mthydra controller.

Format: each release lists the operator-visible changes, then a brief note on
what (if anything) the operator must do when upgrading.

---

## v0.0.6 — 2026-06-01

**Tuning + content pass** — three follow-ups to 0.0.5 driven by operator
feedback:

- **W-1 — heartbeat cadence default → daily** (was hourly). Hourly
  emails became background noise; operators set up auto-archive rules
  and the dead-man's-switch stopped switching. Daily survives unread
  better. The `obs_heartbeat_proven` obligation goes overdue at
  interval × 2 = 48h, so one missed day is OK; two flags. Breach
  threshold (3 consecutive missed dispatches) unchanged. Existing
  TOMLs with explicit `heartbeat_interval` keep their value; only the
  default (and the install template) changed.

- **W-2 — `min_distinct_vantages` auto-tunes from active fleet**.
  Default config value was 2; a 1-vantage MVP was perma-yellow on the
  kill evaluator and image-promote gate. Now: `0` (or absent) selects
  auto-derive `max(1, active_count // 2)`; explicit positive values
  are honored but capped at fleet size so shrinking the fleet doesn't
  break the gate. Applies to `probe.evaluator` kill decisions AND
  `image.gate` canary promotion.

- **W-3 — heartbeat + alert bodies enumerate overdue obligations +
  remediation hints**. The daily email body now lists each overdue
  obligation with `[severity] obligation_id (overdue Nh) → operator
  action` inline, plus all anti-obligations with their details_json
  snippet. New `observability.remediation` module with a static
  per-obligation hint map (covers 18 known obligation kinds plus
  per-target prefixes like `credential_rotation_proven::<provider>`).
  Operator triages from the email; no doc-hunting.

**Operator action when upgrading from 0.0.5:** none required.
- The daily-cadence default only affects new installs. Existing
  controllers keep their `heartbeat_interval` from their `controller.toml`.
  Want daily? Edit the TOML: `heartbeat_interval = "24h"` and `systemctl
  restart mthydra-controller`.
- The auto-tune kicks in on every probe evaluation tick automatically.
- The richer heartbeat body lands on the next scheduled heartbeat.

```bash
sudo -u mthydra /opt/mthydra/venv/bin/mthydra-ops upgrade
```

---

## v0.0.5 — 2026-06-01

**Quickstart §7 automation.** Two of the manual steps in Part 7 are now
one-command:

- **T-1** (partial) — `mthydra-controller descriptor-publish-now`. The
  controller uploads the latest signed descriptor to S3 at
  `descriptors/current` and prints a presigned URL with 30-day TTL.
  Replaces the manual `aws s3 presign` step on the operator's laptop
  (which previously pointed at a key the controller had never uploaded;
  RU boxes would 404 on descriptor refresh). Storing the URL in the DB
  + having `ru-bringup` auto-read it is deferred to a follow-up.

- **T-2** — `mthydra-ops vantage-setup` collapses §7.7's seven manual
  steps (ssh-keygen on EU, scp pubkey, adduser+install on vantage,
  ssh-keyscan, vantage-set-ssh) into one wizard. End-to-end idempotent.

**V-3 — provider credential rotation reminders.** Calendar-driven
`credential_rotation_proven::<provider>` obligation stamped at init
(when credentials are first seeded) and on every
`rotate-provider-credential` call. Default cadences: aws/gmail = 90d,
b2 = 180d, fallback 90d. Override with `--rotation-days N`. The
operator sees the overdue obligation in `obs-status` / `daily-check`
before the credential silently fails in production.

**V-2 — backup integrity smoke sweep + CLI.** Weekly sweep
(`BackupIntegritySweep`) picks a random recent backup gen, downloads
the encrypted blob from S3, re-hashes, and compares to the sha256
recorded at write time. Catches silent S3 corruption, wrong-bucket
reads, and post-upload mutations — failure modes nothing else
surfaces. Stamps `backup_integrity_proven` (singleton) on pass,
raises `backup_integrity_failed::<generation>` per-target on
mismatch. New `mthydra-controller backup-integrity-now
[--generation N]` for operator-triggered runs.

**V-1 — cover-domain auto-rotate on drift (slack-gated).** Extension of
U-D1. When the auto-reverify sweep detects drift on a
`candidate_verified` domain AND burning it would leave the pool at
`>= freeze_threshold` healthy candidate_verified rows, the sweep
silently burns it (audit row `cover_auto_burned`,
`burned_domains.reason = "auto_reverify_drift"`) instead of raising
`cover_pool_reverify_drift_pending::<domain>`. `in_use` drift is
never auto-burned — burning the SNI orphans every box pointing at it,
which is a box-replacement flow, not a sweep flow. Multi-domain drift
in one tick burns until the pool reaches threshold and raises the
remainder.

**Spec U — operator-obligation auto-resolution.** Four reductions in
operator alert load:

- **U-D1** — `mthydra-controller cover-reverify-now` + scheduled
  `CoverPoolAutoReverifySweep`. Replaces the 60-day operator
  `cover-attest-verified` cadence with a controller-side hourly
  TLS-handshake smell-test. Operator only sees a domain via the new
  `cover_pool_reverify_drift_pending::<domain>` anti-obligation when
  the automated check actually fails. Self-clears on recovery.

- **U-D2** — Probe runner pre-flights SSH per vantage before iterating
  probes. A dead vantage now raises ONE
  `probe_vantage_unreachable::<id>` anti-obligation instead of
  spamming a `soft_fail` probe row per (box × prober). Self-clears
  next tick where SSH succeeds.

- **U-D3** — Shard reshuffle wheel hardened: per-shard try/except
  isolation so a single failing shard doesn't crash the whole sweep.
  Failed shards get the existing `shard_overdue_pending::<sid>` with
  the exception class + message in details_json; the rest of the tick
  continues. New `shard_unassigned_pending` singleton raises when the
  unassigned fold-in step itself fails.

- **U-D4** — Heartbeat-breach details_json now carries a self-diagnosis
  verdict: deduplicated recent error strings + an SMTP connect/EHLO
  smoke against `[observability.email]`. Operator triages from the
  alert body instead of opening logs. New `smtp_smoke()` helper in
  the heartbeat module.

**No required operator actions** for any of the above — additive
behaviour. Existing manual cover-attest-verified flows still work; the
auto-sweep just makes them redundant for the common case.

**Operator action when upgrading from 0.0.4:** none required. All
0.0.5 additions are opt-in or additive — existing flows keep working.
On install, fresh credentials will pick up the V-3 rotation
reminders; pre-existing credentials only get the reminder on the next
`rotate-provider-credential` call (re-stamp manually with
`obligation-proven credential_rotation_proven::<provider>
--next-due-hours 2160` for an immediate calendar entry, or just wait
for the next real rotation).

```bash
sudo -u mthydra /opt/mthydra/venv/bin/mthydra-ops upgrade
```

`mthydra-ops upgrade` (no `--ref` needed from 0.0.4+ thanks to S-2's
git-ls-remote fallback). No schema migration in 0.0.5.

---

## v0.0.4 — 2026-06-01

**Upgrade-tool hardening.** Four refinements that came out of the first prod
`mthydra-ops upgrade` run.

- **S-1** — Install now `chown`s `/opt/mthydra/{src,venv}` to `mthydra`. Required
  for `mthydra-ops upgrade` (which runs as the `mthydra` service user) to be
  able to update the venv's entry-point scripts.
- **S-2** — `resolve_latest_tag` falls back to `git ls-remote --tags` when the
  GitHub Releases endpoint returns 404. Means `mthydra-ops upgrade` (no `--ref`)
  works for repos that ship via `git tag` + `git push` without cutting Releases.
- **S-3** — `mthydra-ops upgrade` now detects partial state — when source HEAD
  advanced but the venv didn't catch up (e.g. previous run failed at
  `pip-install`). Logs a warning and re-runs the install phases instead of
  short-circuiting as "already at target".
- **S-4** — Install drops `/etc/polkit-1/rules.d/50-mthydra-systemd.rules`
  permitting `mthydra` to `systemctl` its own service. Without it, every
  `systemctl stop/start mthydra-controller` from upgrade prompts for an
  interactive auth.

**Operator action when upgrading from 0.0.3:**
```bash
# Last time you need --ref. 0.0.3's resolve_latest_tag is the old version
# without the git-ls-remote fallback.
sudo -u mthydra /opt/mthydra/venv/bin/mthydra-ops upgrade --ref v0.0.4
```

After 0.0.4 is installed, all future upgrades work as bare `mthydra-ops upgrade`.

**Pre-existing host migration (if you installed 0.0.1 by hand and S-1/S-4 never
ran from the installer):**
```bash
chown -R mthydra:mthydra /opt/mthydra/{src,venv}
# Drop the polkit rule — see runbook §13.2
```

---

## v0.0.3 — 2026-06-01

**Install + upgrade hardening (spec R).** Eight fixes that turn the install
and upgrade flow from "needs expert hand-holding" into something you can
follow end-to-end. **The most impactful one (R-1) silently broke backups on
every 0.0.1/0.0.2 install** — your backups are still safe (the encrypted
blobs that DID land in S3 are fine), but no new generations were being
written until you upgraded to 0.0.3.

- **R-1 (CRITICAL)** — Fixed the credential consumer bug. The DB stored
  S3 credentials as `keyid:secret` but the consumer passed the whole
  string to boto3 as the secret — every PutObject failed silently with
  `SignatureDoesNotMatch`. 0.0.3 splits the string correctly. No action
  needed; existing DBs are read correctly.
- **R-2** — Auto-derive AWS region from the endpoint URL (`s3.<region>.amazonaws.com`).
  You can drop the `MTHYDRA_BACKUP_REGION` env var from your systemd unit's
  Environment= line if you added one.
- **R-3** — Quickstart IAM policy fixed. The old policy listed
  `s3:GetObjectRetention` (read-only); the new one has
  `s3:PutObjectRetention` + `s3:PutObjectLegalHold` so PutObject under
  Object Lock COMPLIANCE actually succeeds. **If your bucket was created
  using the old quickstart, update the IAM policy** — copy the new JSON
  from quickstart §1.2 and replace the policy on the `mthydra-controller`
  user.
- **R-4** — Friendly stderr (not raw `OSError` tracebacks) when an
  operator-supplied file path can't be read.
- **R-5** — Install runs a forced `backup-now` as its final step. If
  R-1/R-2/R-3 are still broken anywhere, install fails immediately with
  a diagnostic, instead of waiting 30 days for retention-violation alerts.
- **R-6** — `mthydra-ops upgrade` forces `umask 022` when running
  `pip install -e`. Without this, an operator whose root shell has
  `umask 077` ended up with `mode 600 root:root` files in the venv that
  the `mthydra` user couldn't read.
- **R-7** — New `mthydra-controller schema-migrate` subcommand. Walks
  `apply_schema()` to bring an existing DB up to the code's
  SCHEMA_VERSION. `mthydra-ops upgrade --allow-schema-migration` now
  actually triggers the migration (in 0.0.2 the flag was a gate-only
  no-op — the migration never ran).
- **R-8** — Heartbeat emails now include version + hostname + schema
  version + git HEAD SHA in subject + body. Silence in your inbox now
  identifies what went silent.

**Operator action when upgrading from 0.0.2:**
- Replace the IAM policy on your `mthydra-controller` AWS user (R-3) — see
  quickstart §1.2.
- Run `mthydra-ops upgrade --ref v0.0.3 --allow-schema-migration`. The
  `--ref` is needed because 0.0.2's `resolve_latest_tag` (pre-S-2) hits
  GitHub Releases which doesn't exist for this repo.
- After upgrade, you can drop `MTHYDRA_BACKUP_REGION=eu-west-1` from your
  systemd unit overrides (R-2).

---

## v0.0.2 — 2026-05-31

**`mthydra-ops upgrade` one-command controller upgrade (spec Q).** Eliminates
the manual git-pull / pip-install / restart loop.

- New `mthydra-ops upgrade` subcommand. Default target is the latest GitHub
  release tag; override with `--ref <branch|tag|sha>`.
- Pre-upgrade forced `backup-now` is the recovery floor. Auto-rollback on
  health-check failure is on by default (disable with `--no-auto-rollback`).
- Schema migrations across SCHEMA_VERSION boundaries are forward-only and
  require explicit `--allow-schema-migration` acknowledgement.
- 8 phases (preflight, resolve-target, record-prior, fetch-and-checkout,
  pip-install, stop-service, start-and-verify, summary). Idempotent on
  re-run.

**Operator action when upgrading from 0.0.1:** must be done by hand (the tool
that automates this only exists *in* 0.0.2). See runbook §13.1 for the
manual procedure.

**Known issues fixed in 0.0.3:**
- Backups had been silently failing since install (R-1). Upgrade to 0.0.3.
- `--allow-schema-migration` flag is gate-only; it doesn't actually run the
  migration (R-7).
- `pip install` inside upgrade inherits the caller's umask (R-6).

---

## v0.0.1 — 2026-05-28

Initial release. Spec N installer, spec O ru-bringup wizard, spec P
EU-side RU automation.

Known issues fixed in 0.0.3:
- Quickstart IAM policy doesn't include `s3:PutObjectRetention` so the
  first backup-now fails AccessDenied (R-3).
- No `mthydra-controller schema-migrate` subcommand (R-7).
- No heartbeat identification fields (R-8).
- Raw `OSError` tracebacks on bad file paths (R-4).
- Backups silently broken end-to-end (R-1, R-2).
