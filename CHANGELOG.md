# Changelog

Operator-facing release notes for the mthydra controller.

Format: each release lists the operator-visible changes, then a brief note on
what (if anything) the operator must do when upgrading.

---

## Unreleased — 2026-06-06

- feat(V5): RU-egress self-measurement — RealityHandshakeObserver probes EU
  exits from a RU vantage (reality-handshake helper), capturing handshake
  health + emitted JA3; controller flags fingerprint staleness vs an
  operator-maintained reference. Signals tls_fingerprint_stale /
  eu_exit_handshake_degraded surface in the observability snapshot +
  remediation. CLI: fingerprint-staleness-show. Config: [ru_egress] ja3_reference_path.
- feat(V1): uTLS fingerprint freshness + diversity — signed-descriptor v3
  `tls_fingerprints` weighted list; each RU box deterministically self-picks
  a stable, diverse fingerprint the controller rolls fleet-wide without
  re-imaging. Verifier accepts v2 and v3. Invariant #33 (known-fp at sign).

**RU→EU connectivity check (K3).** RU boxes now run an end-to-end tunnel
self-check: they open a TCP connection to a Telegram DC that their own
iptables→sing-box→EU-exit path carries, and write the verdict to
`/run/mthydra/health.json` plus a loud journal line (`EU tunnel check FAILED …`).
This closes the blind spot where a box answered `telnet :443` while Telegram
could not connect through it — TCP being open is no longer mistaken for working.
On the controller side, the active EU node reads its co-located sing-box's
(localhost-only) clash_api and raises a plain-language `box_eu_tunnel_unseen`
alert for any live box not seen tunnelling within the freshness window. No new
RU-box credential or outbound is added. Schema → v18 (forward-only migration,
no operator action). To debug a flagged box: SSH in and
`cat /run/mthydra/health.json` + `journalctl -u mthydra-agent`.

---

## Unreleased — 2026-06-05

**Stale "user not registered" alerts self-clear.** A `dist_user_unregistered`
alert is now cleared when its user is no longer assigned to a shard (deleted or
unassigned) — previously it could orphan forever, because only still-iterated
users got the per-user clear. Its remediation text is also fixed: it now
explains the user needs their Telegram/email registered (`user-onboard`), rather
than the wrong "has no descriptor".

**`/start` always delivers + the bot speaks on failure.** Tapping `/start` (an
explicit request) now bypasses the unchanged-subset dedupe and always
re-delivers your current proxies — previously a repeat `/start` for an unchanged
box was silently deduped. `dist-publish-now --user-id` now genuinely forces that
user too (matching its help). And if delivery throws, the bot now sends the user
a plain "couldn't prepare your proxy, operator notified" message instead of going
silent.

**Readable alerts.** Operator alert bodies are now plain language: coded verdicts
(e.g. `soft_threshold_reached`) are spelled out, internal references (probe-result
row ids) are hidden, and list values are de-snaked. The `probe_kill_pending`
"What to do" now explains that repeated check failures usually mean the box is
unreachable, tells you to confirm it's down first, and warns that terminating
burns the cover domain — so you don't have to read the source to act.

**Filterable operator email.** Every operator-facing email subject (heartbeat,
alerts, and the backup-monitor gap alarm) is now prefixed with `[MTHYDRA] ` so
you can catch all of it with a single mail filter. User-facing distribution mail
is deliberately left untagged to stay innocuous.

**Granny-usable proxy links (spec K2).** The distribution bot now delivers a
tappable `https://t.me/proxy?…` link + a QR image per box instead of raw JSON.
The mtg FakeTLS secret derivation is single-sourced in `mthydra.proxy_link`
(shared by the RU box's `config_gen` and the EU payload builder) so the link
always matches what the box accepts. New runtime dependency: `segno`
(pure-Python QR). The internal payload keeps its structured fields (now incl.
`proxy_url`) in `distribution_log` for audit; `subset_hash` is unchanged. Boxes
without a `reality_uuid` are omitted from a user's delta (they can't form a
usable link).

**vantage-setup hardening (spec T2).** `mthydra-ops vantage-setup` now opens a
vantage by `--root-key`, `--password` (interactive, prompted on your terminal,
never stored — for providers that only allow password login at first boot), or
`--print-pubkey` (print the shared probe pubkey for you to install on a
root-capable user — for providers that forbid password auth). After provisioning
the `probe` user it **verifies** probe-key login on a fresh connection and then
**locks sshd down** to `probe`-key-only (`AllowUsers probe`,
`PasswordAuthentication no`, `PermitRootLogin no`); from then on the only way
into the vantage is the controller's probe key (future root access is
provider-console only). The probe keypair now lives in the state DB (table
`controller_probe_key`, schema **v17**) instead of a per-vantage file, so it
rides the encrypted backup: a promoted warm standby restores the DB,
rematerializes the identical key on startup, and resumes probing every vantage
with no manual re-provisioning. Re-running `vantage-setup` on an
already-set-up vantage is a safe no-op — it detects the probe key already
works and skips the (now-impossible, post-lockdown) root login, just
re-pinning the host key and re-registering.

**Upgrade note:** schema migrates v16 → v17 automatically on first start (adds
one table; no data migration). Existing vantages keep working; re-run
`vantage-setup` on each to move it onto the shared key and apply the lockdown.

---

## Unreleased — 2026-06-03

**New: `mthydra-controller user-onboard` — one-command user onboarding with
Telegram deep-link enrollment.** Replaces the four-step
`user-add` / `user-channels-set` / `shard-create` / `dist-test` sequence.
The command creates the user, assigns them to a shard (uses `default_shard` if
`--shard` is omitted; auto-creates the shard if missing), mints a one-time
enrollment token, and prints a `https://t.me/<distbot>?start=<token>` deep-link
(or the bare token if the bot username can't be resolved). The operator sends
that link to the user out-of-band. The user taps it, taps **Start** — the
controller's enrollment poller auto-captures their `chat_id` and delivers
their first proxy delta. No `getUpdates` call, no chat-id transcription, no bot
DM required from the user beforehand. Email is optional; Telegram-only is
allowed (produces a warning). Re-running `user-onboard` reissues the token.
Token TTL is controlled by `distribution.enrollment_token_ttl_hours` (default 24h).

**New: `provision-seed --shard <id>`.** Boxes now auto-bind to `default_shard`
at provisioning when `--shard` is omitted. Pass `--shard <id>` to place a box
in a dedicated shard. `mark_live` refuses a shard-less box; `default_shard` is
exempt from the empty-active-shard invariant (check 36).

**Alert UX improvements.**
- The active EU node's own never-published heartbeat no longer triggers a page.
- Telegram alert messages are rendered as HTML; `snake_case` identifiers are
  no longer unintentionally italicised by Telegram's Markdown parser.
- Alert subjects and bodies are human-readable with per-obligation remediation
  lines, so the operator knows the fix without consulting the runbook.

**Operator action:** none required. `mthydra-ops upgrade` to pick up
`user-onboard`. Existing users are unaffected; re-run `user-onboard` for any
user whose `chat_id` you want to capture via the new enrollment flow.

---

## v0.0.10 — 2026-06-03

First end-to-end RU box: the controller and RU agent now bring up a live box that
boots clean on amd64. This release is the accumulation of the MVP bring-up fixes
plus a containerised agent-boot harness (`make agent-harness`) that validates the
full agent boot locally before any cloud VM. Highlights below; the bulk are
fixes to the RU agent boot path and the image pipeline surfaced by real boxes and
the new harness.

**Fix: image-prepare defaults to the RU-box arch (amd64), not the controller's.**
The mtg image runs on the RU box, but `image-prepare` defaulted `--arch` to the
*controller's* arch via host auto-detect. On the common setup (cheap arm64 EU
controller + amd64 RU boxes) that built an arm64 mtg that died with
`Exec format error` (ENOEXEC) on the amd64 RU box. The default is now
`linux-amd64`; override `--arch` for arm64/etc RU hosts. The `--arch` help now
says it's the RU box's arch.

**Fix: image-build extracts the mtg ELF from the release tarball.** mtg ships as
`mtg-<ver>-<arch>.tar.gz`; build_image verified the tarball against the upstream
checksum (correct) but then stored the **tarball** as the image. The RU agent
execs the image directly, so the box got a gzip blob at `/run/mthydra/mtg` that
can't run (`file` reported gzip data, not ELF — first RU box, 2026-06-02). It now
extracts the `mtg` member after verifying the archive and registers the ELF
(image_version = sha of the binary). New `--force` on `image-build` /
`mthydra-ops image-prepare` rebuilds a release whose prior artifact was bad
(the same-release idempotency shortcut otherwise returns the old image).

**Fix: RU-side traffic capture uses REDIRECT, not TPROXY (and is idempotent).**
mtg's connections to the Telegram DCs are locally generated (OUTPUT chain), but
the agent hooked a TPROXY-bearing chain into OUTPUT — and `xt_TPROXY` is
PREROUTING-only, so the kernel rejected it with `Invalid argument`. The retry
then failed on `Chain already exists` because `install` wasn't idempotent. The
agent now captures via `nat`/`REDIRECT` in OUTPUT (valid for local traffic) into
sing-box's `redirect` inbound (config_gen switched from `tproxy` to `redirect`),
and `install` tears down any prior chain first. Loop-safe: only Telegram-DC
destinations are redirected, so sing-box's own tunnel to the EU exit isn't
recaptured. (Proven on the first RU box, 2026-06-02.)

**Fix: agent read the wrong descriptor key (`exits` vs `eu_exit_set`).** The
controller signs the exit list under `eu_exit_set` (`descriptor.payload`), but the
agent's `config_gen` read `descriptor_payload.get("exits")` — which never exists —
so every real box refused with `descriptor contains no exits` regardless of
configured exits. The unit test had used the same wrong key, so it passed while
the integration was broken. Fixed the reader and rebuilt the test on the
controller's own `canonical_bytes`, so any future key/field drift between signer
and agent fails in CI instead of on a live box.

**RU agent hardening: applies core_pattern + accepts tmpfs-backed paths.** Two
proven bring-up blockers (first RU box, 2026-06-02, diagnosed from the box):
- `kernel.core_pattern` — apport's service overwrites it at boot *after*
  cloud-init's `bootcmd` (the live value was apport's pattern; nothing in
  `sysctl.d`). The agent now re-asserts `|/bin/false` itself at startup
  (`hardening.apply_best_effort()`) — it's root and runs after apport.
- `/run/mthydra is not on tmpfs` — it's a directory under the `/run` tmpfs
  (`df` confirmed `/run` is tmpfs with no separate `/run/mthydra` mount), i.e.
  already in RAM, but the check demanded a dedicated mountpoint. `_path_on_tmpfs`
  now accepts any path *backed by* tmpfs (longest-matching mount fstype).

**RU agent no longer powers off the box on a startup hiccup.** The agent is
fail-closed: any startup step (hardening, seed, mtg download, iptables) called
`shutdown -h now`. But the mtg download commonly fails transiently at boot — the
VM clock is still at epoch (TLS/presigned-URL expiry reject) or the network/S3
isn't ready — and powering off then was catastrophic: cloud-init is
once-per-instance and the seed lives on tmpfs, so the box came back bare and
unrecoverable, with the failure reason lost to volatile logs. Startup now retries
(10× / 15s) and, if it still can't come up, **stays up** (exit non-zero) so the
box is reachable and `journalctl -u mthydra-agent` shows why. Fail-closed
shutdown is now reserved for *runtime* tamper (the periodic hardening-regression
check). In practice this means the box self-heals once the clock syncs.

**`mthydra-ops ru-bringup` is now one command (was four).** Quickstart §7.1–7.4
collapse into `mthydra-ops ru-bringup --provider <p> --region <r>`. The wizard now
auto-handles every controller-side prerequisite: ensures a promoted mtg image
(runs `image-prepare` only if none exists), publishes the agent tarball (already
did), and **publishes + presigns the descriptor-refresh URL itself** — so
`--descriptor-refresh-url` is optional and you never hand-copy a presigned URL
(the `&` in those URLs was a repeated shell-quoting footgun). All three
underlying commands still exist for manual/cron use. Quickstart Part 7 rewritten.

**`ru-bringup` now mints a 24h image-download URL (was 1h).** The mtg binary URL
baked into the seed is fetched by the agent at VM boot; the 1h default meant that
if you took longer than an hour to paste cloud-init, create the VM, and boot it,
the box came up with an expired download URL and never started mtg. `ru-bringup`
now requests a 24h TTL for that URL.

**Fix: the agent tarball now ships its `mthydra.descriptor` dependency.** The RU
agent imports `mthydra.descriptor.authority`, but `package_agent` bundled only
`mthydra/ru_agent/*`, so the box died on boot with
`ModuleNotFoundError: No module named 'mthydra.descriptor'` and never listened on
:443. The tarball now includes the `descriptor` package too (its only runtime
deps are stdlib + `cryptography`, which the box installs via apt). `controller/*`
is still excluded — the agent never imports it and it shouldn't land on an
exposed box. Re-publish the agent (`mthydra-ops` auto-republishes on the next
`ru-bringup`) so new boxes get the complete tarball.

**Fix: `ru-bringup` resume command now actually runs.** When you defer at the
public-IP prompt, the tool prints `ru-bringup --box-id <id> --public-ip <ip>` to
resume — but that command was rejected because `--provider`, `--region`, and
`--descriptor-refresh-url` were marked unconditionally required, even though
resume skips the mint that uses them. Those three are now required only for a
fresh mint (enforced in the command with a clean error + exit 2 if missing); a
resume needs only `--box-id` + `--public-ip`.

**Fix: a reclaimed cover domain is now actually reusable.** `ru-box-reclaim`
returns a never-live box's cover domain to `candidate_verified`, but the
terminated box still occupied `ru_boxes.sni` (a `UNIQUE` column), so
re-provisioning that domain died with
`UNIQUE constraint failed: ru_boxes.sni`. A terminated box has no claim on an
SNI — `provision-seed` now releases any terminated box's claim on the chosen
domain before inserting the new box. No migration needed; the next
`provision-seed` self-heals an existing stuck domain. Also: the catch-all in
`provision-seed` no longer mislabels every error as "B2 URL minting failed".

**`mthydra-ops upgrade` now defaults to `main`.** Previously, with no `--ref`,
upgrade resolved the *latest GitHub release tag* — but the project doesn't tag
per fix (fixes land on `main`), so plain `mthydra-ops upgrade` never picked up
new code and silently no-op'd on "already at target ref". This contradicted the
documented contract (AGENTS.md: "fixes … are picked up via `mthydra-ops
upgrade`"). Now: no `--ref` → `main`; `--ref latest` → newest release tag;
`--ref <branch|tag|sha>` → that ref. Tracking main is safe — upgrade still takes
a pre-upgrade backup, runs preflight health, gates schema migration, and
auto-rolls-back on failure.

**`mthydra-ops ru-bringup` now auto-reclaims on a failed mint.** `provision-seed`
commits the box row and consumes a cover domain before the cloud-init bundle is
written. If that post-commit step crashes (the bug class that stranded box
`c1a72a8d` holding `www.cloudflare.com`), `ru-bringup` now auto-runs
`ru-box-reclaim` on the just-minted box and re-raises the original error — so a
failed bring-up leaves no orphan and no consumed cover domain. Reclaim only fires
in the window before any VM exists; once you have the bundle, deferred/unreachable
boxes stay resumable as before. A reclaim that itself fails prints a manual-fix
hint and never masks the original error.

**New: `mthydra-controller ru-box-reclaim <box_id>`.** Cleans up a box stuck
in `provisioning` that never went live — the residue left behind when
provisioning crashes *after* `provision-seed` commits (it writes the `ru_boxes`
row and flips the cover domain `candidate_verified → in_use` before the VM
exists). Reclaim terminates the box and returns its cover domain to
`candidate_verified` so it can be **reused** — it does **not** burn the SNI.
This is the difference from `ru-box-terminate`, which burns the SNI (correct
only for a box that actually went live and exposed it). Reclaim refuses `live`
boxes for that reason. Operational failures (unknown box, wrong state) print a
clean error and exit 2 — no traceback.

**MVP bootstrap fix.** `mthydra-ops image-prepare --yes` (quickstart §7.1)
failed on a fresh install for two compounding reasons:

- **S-1** — The promotion gate required at least one canary box before
  allowing the first-ever image promotion, but you cannot provision a box
  without a promoted image. The gate now auto-detects when no image has
  ever been promoted and skips canary-cohort, cycles, vantages, and
  pending-kill checks. After the first promotion the gate enforces the
  configured thresholds as before.

- **S-2** — `image-build` crashed with
  `UNIQUE constraint failed: ru_images.image_version` on retry when the
  same upstream release was requested a second time. `build_image()` now
  checks for an existing `ru_images` row by `upstream_release + upstream_repo`
  before downloading anything. If found, it returns the existing
  `image_version` immediately. Makes `image-prepare` safe to retry.

**T-1 — `mthydra-ops agent-publish` crashed** with
`dataclasses.FrozenInstanceError` because `_load_cfg` tried to set a field
on the frozen config dataclass. Fixed by threading `db_path` as an explicit
argument through `_get_s3_credentials`, `_make_s3_client`, and
`publish_agent`. The same bug existed in `ru_bringup.py` and is fixed there
too.

**Agent-publish also broke on the secret-only credential form** with
`RuntimeError: provider credential malformed (expected KEY:SECRET)`. The
R-D1 fix made the backup pipeline accept either `KEY:SECRET` or just
`SECRET` (falling back to `cfg.backup.access_key_id`), but `agent_ops`
had its own credential parser that still required the colon. After the
R-D1 workaround flow rotated credentials to secret-only, `backup-now`
worked but `agent-publish` refused. `_get_s3_credentials` now mirrors
the `_build_destination` split-or-fallback logic. No operator action;
fix is code-only.

**Agent-publish then crashed on `cfg.backup.region`** —
`AttributeError: 'BackupConfig' object has no attribute 'region'`.
`agent_ops._make_s3_client` had its own boto3-client construction that
read a `region` field that does not exist on `BackupConfig` (region is
derived from the endpoint, R-D2). This — and the credential bug above —
reached prod because every `agent_ops` test mocked `_make_s3_client`
out, and the test fixture invented a `region` attribute the real config
lacks. Root-caused and fixed at the class level:
- New shared `s3_dest.resolve_region(endpoint)` is now the single
  region resolver; `controller.cli._resolve_backup_region` delegates to
  it and `agent_ops._make_s3_client` uses it (plus `endpoint or None`
  to match `_build_destination` exactly).
- The lying test fixture is corrected to mirror the real `BackupConfig`
  field set, and two new moto-backed integration tests exercise the
  real `_make_s3_client` / `publish_agent` path end-to-end (no S3 mock)
  against the actual `BackupConfig` dataclass — so a config-shape
  divergence can't slip past mocked tests again.
- Audit confirmed `agent_ops` was the only module that reimplemented the
  S3 client + credential parsing; `ru_bringup` shells out to
  `mthydra-controller` (inherits correct handling) and all 9 cli
  credential consumers route through `_build_destination`.
No operator action; fix is code-only.

**T-2 — Installer now writes `/var/lib/mthydra/.bash_profile`** so
`sudo -u mthydra -i` gives a login shell with `mthydra-controller` and
`mthydra-ops` on PATH. No more typing `/opt/mthydra/venv/bin/` prefixes.

**Operator action:** none required for T-1 (code fix only). For T-2, new
installs get the file automatically. Existing hosts: write it once:

```bash
# As root on the EU host:
printf '# Added by mthydra installer.\nexport PATH="/opt/mthydra/venv/bin:$PATH"\n' \
    > /var/lib/mthydra/.bash_profile
chmod 644 /var/lib/mthydra/.bash_profile
```

Then upgrade:

```bash
sudo -u mthydra -i -c 'mthydra-ops upgrade'
```

---

## v0.0.9 — 2026-06-01

**Bug fix.** `mthydra-controller image-build` was sending
`Accept: application/octet-stream` for every HTTP call — including
the GitHub release-metadata API request that wants JSON. GitHub
correctly responds 415 Unsupported Media Type and the build aborts.

Fix: drop the inappropriate Accept header. The same helper is used
for the API call (JSON) AND the binary asset download (which
redirects through to GitHub's CDN and ignores Accept), so omitting
Accept entirely works for both. Regression test pins the no-Accept
behavior.

**Operator action when upgrading from 0.0.8:** none required.
`mthydra-ops upgrade` after this lands, and `image-prepare` /
`image-build` start working again.

```bash
sudo -u mthydra /opt/mthydra/venv/bin/mthydra-ops upgrade
```

---

## v0.0.8 — 2026-06-01

**Two upgrade-flow fixes** caught by a real `mthydra-ops upgrade` run.

- **check-42 staleness threshold is now cadence-aware.** With W-1's
  daily heartbeat default, the hardcoded 2h staleness window in the
  startup invariant was tripping on every restart that took longer
  than 2h — i.e. all of them under a daily cadence. Threshold is now
  `max(2h, heartbeat_interval × 2)`, matching the
  `obs_heartbeat_proven` obligation's next-due semantics. The 2h
  floor protects against an aggressive ½h cadence yielding a flaky
  1h window.

- **`mthydra-ops upgrade` runs pre-flight health BEFORE any state
  change.** Previously the tool went straight from `resolve-target`
  → `record-prior (backup)` → `fetch` → ... → `verify`. If the host
  was already unhealthy (stale heartbeat, broken sink), the verify
  AFTER the upgrade failed AND auto-rollback failed for the same
  reason (DB state, not code). Operator was left mid-upgrade with
  the controller refusing to start.

  Now: new `phase 2b: pre-flight health check + heartbeat` runs
  startup-check and then forces a fresh heartbeat BEFORE the backup.
  - startup-check fail → exit 11, no state change
  - heartbeat fail → exit 12, no state change (operator needs to fix
    the sink first since post-restart alerts wouldn't be deliverable
    anyway)

  Forcing the heartbeat also advances the dead-man's-switch clock
  so the post-restart startup-check has fresh ground to stand on.

**Operator action when upgrading from 0.0.7:** none required. The
upgrade tool now self-protects against the failure mode the user hit
in 0.0.6 → 0.0.7 (stale heartbeat at restart).

```bash
sudo -u mthydra /opt/mthydra/venv/bin/mthydra-ops upgrade
```

If pre-flight fails: fix the reported issue (likely SMTP creds or a
stale heartbeat — force one via `mthydra-controller obs-heartbeat-now`
manually) and retry. The upgrade is idempotent on retry.

---

## v0.0.7 — 2026-06-01

**Incremental-upgrade backfills.** User on a host that went 0.0.1 →
... → 0.0.6 incrementally reported the heartbeat body listed
`shard_disjointness_check_proven` as overdue with a misleading hint
pointing at `startup-check` — but `startup-check` returned OK and
didn't clear the obligation. Root cause: the obligation was registered
at install with 24h cadence but nothing ever stamped it. Audit found
one more silent gap: V-3's `credential_rotation_proven::<provider>`
rows don't exist on hosts that didn't run a fresh 0.0.5+ init.

Two fixes:

- `startup-check` and the `serve` daemon's startup pass now stamp
  `shard_disjointness_check_proven` on success. The remediation hint
  was also wrong ("startup-check is failing") — corrected to explain
  the stamp model.

- New `_backfill_credential_rotation_obligations(conn, now)` runs at
  serve startup. For each provider credential in
  `provider_api_credentials` that lacks a corresponding
  `credential_rotation_proven::<provider>` obligation, it stamps one
  with `last_proven_at = now` and the per-provider default cadence
  (aws/gmail = 90d, b2 = 180d). Idempotent — providers that already
  have a stamp are left alone. New fresh installs are unaffected
  (init already stamps; backfill is a no-op).

**Upgrade audit summary** for hosts going 0.0.1→0.0.7 incrementally:

| Version jump | New state needing backfill |
|---|---|
| 0.0.2 (spec Q) | none — new CLI tool only |
| 0.0.3 (spec R) | schema v14→v15 (handled by `mthydra-ops upgrade --allow-schema-migration` already) |
| 0.0.4 (spec S) | none — install/code-only changes |
| 0.0.5 (T+U+V) | V-3 `credential_rotation_proven::<provider>` (handled by 0.0.7 auto-backfill); V-2 `backup_integrity_proven` (self-stamps on first sweep tick, no backfill needed) |
| 0.0.6 (W) | none — tuning/output changes only |
| 0.0.7 | `shard_disjointness_check_proven` stamping fix (this release) |

**Operator action when upgrading from 0.0.6:** none required. Backfill
runs automatically at serve startup on the first restart after the
upgrade.

```bash
sudo -u mthydra /opt/mthydra/venv/bin/mthydra-ops upgrade
```

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
