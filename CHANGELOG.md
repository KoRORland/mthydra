# Changelog

Operator-facing release notes for the mthydra controller.

Format: each release lists the operator-visible changes, then a brief note on
what (if anything) the operator must do when upgrading.

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
