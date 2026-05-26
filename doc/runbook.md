# mthydra Operator Runbook

> **Audience:** The single operator of a private mthydra deployment. This document is the **complete, ordered set of procedures** for standing up, running, and recovering an mthydra fleet. If you are not the operator, you should not be reading this — but if you must, treat every checklist as load-bearing rather than optional.
>
> **Tooling note:** Many of the longer procedures here have a shortcut via the `mthydra-ops` helper (shipped in the same wheel as `mthydra-controller`). Wherever a procedure has a one-shot equivalent, it is called out with **`→ mthydra-ops <subcommand>`**. The shortcuts orchestrate the underlying `mthydra-controller` commands in the documented order; the long-form procedures remain authoritative if you need to do anything non-default. Run `mthydra-ops --help` for the full list of subcommands.
>
> **Reading this document:** Each procedure is numbered. The numbered order is the order to execute. Where a procedure references another (`see §X.Y`), do the referenced one first if you haven't already. Bracketed comments like `[A-D5]` reference a locked decision in a spec — you can look it up in `doc/specs/` if you want to know why a step is the way it is, but you don't need to read the spec to follow the procedure.
>
> **What you need before you start:**
> - A laptop you trust, outside Russia.
> - A B2 (Backblaze) account or another S3-compatible object store for backups.
> - A Telegram account.
> - A mailbox you read every day.
> - One or more EU VPS hosts you can SSH into.
> - Out-of-band contact for every person in your trusted circle (phone number for Signal, an email they read, a physical meeting cadence — anything *not* dependent on Telegram).

---

## Table of contents

1. [§1 — Initial deployment](#1--initial-deployment)
2. [§2 — Day-2 operations (the §12 obligation clock)](#2--day-2-operations-the-12-obligation-clock)
3. [§3 — Image lifecycle (T4 + spec D2)](#3--image-lifecycle-t4--spec-d2)
4. [§4 — Cover-domain discipline (T5)](#4--cover-domain-discipline-t5)
5. [§5 — User onboarding (per-circle)](#5--user-onboarding-per-circle)
6. [§6 — Shard management (T6)](#6--shard-management-t6)
7. [§7 — Probe discipline (T7 + T3 Job 1/2)](#7--probe-discipline-t7--t3-job-12)
8. [§8 — Alert response (spec J + J2)](#8--alert-response-spec-j--j2)
9. [§9 — Image rollback](#9--image-rollback)
10. [§10 — Controller restore / promotion (T2)](#10--controller-restore--promotion-t2)
11. [§11 — Periodic maintenance](#11--periodic-maintenance)
12. [§12 — Honest residuals + obscurity discipline](#12--honest-residuals--obscurity-discipline)

---

## §1 — Initial deployment

This procedure brings an empty machine to a running active controller. Follow steps strictly in order. **Do not skip the verification at the end of each section.**

### §1.1 — Provision the EU active host

**→ `mthydra-ops setup-host`** (idempotent; runs the steps below as root)

You need one EU VPS that will become the active controller. Recommend Ubuntu 24.04 LTS (the only OS tested by spec F).

```bash
# On the EU host as root:
apt update && apt install -y python3.12 python3.12-venv python3-pip git age
adduser --system --group mthydra
mkdir -p /etc/mthydra /var/lib/mthydra /var/log/mthydra
chown mthydra:mthydra /var/lib/mthydra /var/log/mthydra
```

**Verify:**
- `python3.12 --version` shows `Python 3.12.x`.
- `id mthydra` shows the user exists.
- `/var/lib/mthydra` is owned by `mthydra:mthydra`.

### §1.2 — Generate the operator age key (LOCAL, NOT ON THE EU HOST)

**→ `mthydra-ops gen-age-key`** (warns if invoked on a host with a server-looking hostname)

The age key encrypts every controller state backup. **It must never live on any deployed machine.** Generate it on your laptop:

```bash
# On your laptop:
age-keygen -o ~/.config/mthydra/operator.age
chmod 600 ~/.config/mthydra/operator.age
grep "public key:" ~/.config/mthydra/operator.age
# Copy the printed line: age1...
```

**Back it up to two non-cloud locations:**
- A passphrase-encrypted USB drive in a desk drawer.
- A passphrase-encrypted USB drive at someone else's house, on a different floor of the same building, or in a safe-deposit box.

**Verify:** without the key, no backup can be decrypted. Lose it = lose the fleet.

### §1.3 — Set up B2 (or equivalent) credentials

Create a Backblaze B2 bucket with **Object Lock = Compliance, 30 days**. Object Lock prevents backup deletion (including by you under coercion).

```bash
# Capture from B2 web UI:
#   B2_KEY_ID=...
#   B2_APPLICATION_KEY=...
#   B2_BUCKET=mthydra-prod-state
#   B2_ENDPOINT=https://s3.us-west-002.backblazeb2.com  (your region's)
```

**Verify:** the bucket exists in B2 web UI, Object Lock retention shows 30d (Compliance mode).

### §1.4 — Install mthydra on the EU host

```bash
# On the EU host as mthydra user:
su - mthydra
python3.12 -m venv /home/mthydra/venv
. /home/mthydra/venv/bin/activate
pip install -e /path/to/checked-out/mthydra-source
# Or, if shipped as a wheel:
# pip install mthydra-X.Y.Z-py3-none-any.whl

mthydra-controller --help    # smoke test
```

**Verify:** `mthydra-controller --help` prints the subcommand list. If not, the install is broken — STOP, don't proceed.

### §1.5 — Bootstrap controller state

**→ `mthydra-ops bootstrap`** combines §1.5 (init), §1.6 (authority migration), and §1.7 (write controller.toml from a template) into one command. See `mthydra-ops bootstrap --help` for the full argument list; you'll need all the credentials from §1.3 + the bot tokens from §1.7.

The bootstrap creates the SQLite state file with all schema migrations applied. Once it succeeds, the file's contents are the operator's responsibility — refuse to bootstrap a second time over an existing file.

```bash
# On the EU host as mthydra user:
mthydra-controller init \
    --db-path /var/lib/mthydra/state.sqlite \
    --age-recipient "age1...PASTED_FROM_LAPTOP" \
    --provider-credential "b2=$B2_KEY_ID:$B2_APPLICATION_KEY" \
    --role active

# Save the recipient where _cmd_serve can find it.
echo "age1...PASTED_FROM_LAPTOP" > /etc/mthydra/age-recipient.txt
chown mthydra:mthydra /etc/mthydra/age-recipient.txt
chmod 600 /etc/mthydra/age-recipient.txt
```

**Verify:** `ls -la /var/lib/mthydra/state.sqlite` shows mode `0600` and owner `mthydra:mthydra`. Run `mthydra-controller startup-check --db-path /var/lib/mthydra/state.sqlite` — must exit 0.

### §1.6 — Migrate the credential authority off the placeholder

Bootstrap inserts a placeholder Ed25519 key. Run the migration to install a real one (the spec G command writes a fresh Ed25519 keypair using `cryptography`):

```bash
mthydra-controller authority-migrate-placeholder \
    --db-path /var/lib/mthydra/state.sqlite
```

**Verify:** `sqlite3 /var/lib/mthydra/state.sqlite "SELECT generation, length(privkey_pem) FROM credential_authority;"` shows generation 1 with a multi-hundred-byte key (PEM-encoded Ed25519).

### §1.7 — Write `controller.toml`

Copy `packaging/etc/mthydra/controller.toml.example` to `/etc/mthydra/controller.toml` and edit. **Every section is required** for active mode.

Critical fields to fill in:

| Section | Field | What to fill |
|---|---|---|
| `[node]` | `hostname` | this EU host's external DNS name |
| `[backup]` | `endpoint`, `bucket`, `access_key_id` | from §1.3 (the secret part of the B2 key lives in the DB via `--provider-credential`, not here) |
| `[gap_monitor]` | `recipient_email` | YOUR email (the operator) |
| `[observability.telegram]` | `bot_token`, `chat_id` | a **DEDICATED** operator-alerts bot (NOT a user-facing bot). Create at `@BotFather` on Telegram. `chat_id` is your DM with the bot — start the bot in DM, then GET https://api.telegram.org/botTOKEN/getUpdates to see the chat_id. |
| `[observability.email]` | all six fields | SMTP server, app password. Gmail: enable 2FA, then create an app password under "App passwords." Same for Outlook, Yandex, etc. |
| `[distribution.telegram]` | `bot_token` | a **SECOND** bot, distinct from the operator-alerts bot. This is the user-facing distribution bot. |
| `[distribution.email]` | all five fields | A second SMTP setup is fine; same host with a different from_addr is also fine. **Per-user to_addr lives in `user_channels.email_addr`, NOT in this section.** |

```bash
chmod 600 /etc/mthydra/controller.toml
chown mthydra:mthydra /etc/mthydra/controller.toml
```

**Verify:** `mthydra-controller startup-check --db-path /var/lib/mthydra/state.sqlite --config /etc/mthydra/controller.toml` exits 0.

### §1.8 — Confirm both alert sinks work BEFORE serve

**→ `mthydra-ops preflight`** (runs obs-alert-test crit + obs-heartbeat-now + startup-check; refuses to declare success if any of them fail)

This is the single most important pre-flight check. If either sink is broken, you must NOT proceed.

```bash
# Operator-alerts Telegram + email:
mthydra-controller obs-alert-test --severity crit \
    --message "deploy-time crit test from $(hostname)" \
    --db-path /var/lib/mthydra/state.sqlite \
    --config /etc/mthydra/controller.toml
```

**Verify both arrived:**
- Your operator-alert Telegram bot DM shows the message.
- Your email shows the message (check spam folder; if it landed there, whitelist the From address NOW).

If either is missing: STOP. Fix the credentials in `controller.toml`. Re-run `obs-alert-test`. Only proceed when both arrive within seconds.

### §1.9 — Start the controller daemon

```bash
# As mthydra user, in a tmux/screen session:
mthydra-controller serve \
    --db-path /var/lib/mthydra/state.sqlite \
    --config /etc/mthydra/controller.toml
```

You should see one line: `serve: backup orchestrator + descriptor rotator + cover-pool sweeps + standby poller + upstream tracker + shard wheel + probe audit wheel + alerter + obs heartbeat + dist publisher + dist user heartbeat armed`.

If you see `serve: refusing — [observability.X] are required for active mode` or `serve: refusing — [distribution.X] are required for active mode`, you missed credentials in §1.7. Fix and re-run.

**Productionise** (after smoke-testing in tmux): write a systemd unit. Template:

```ini
# /etc/systemd/system/mthydra-controller.service
[Unit]
Description=mthydra controller
After=network.target

[Service]
User=mthydra
Group=mthydra
WorkingDirectory=/var/lib/mthydra
ExecStart=/home/mthydra/venv/bin/mthydra-controller serve \
    --db-path /var/lib/mthydra/state.sqlite \
    --config /etc/mthydra/controller.toml
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload
systemctl enable --now mthydra-controller
systemctl status mthydra-controller
journalctl -u mthydra-controller -f
```

**Verify:** within 5 minutes you should see the first heartbeat email arrive. If it doesn't, re-run `obs-heartbeat-now` and read its output.

### §1.10 — Sign the first descriptor

```bash
mthydra-controller descriptor-sign-now \
    --db-path /var/lib/mthydra/state.sqlite
```

**Verify:** `mthydra-controller descriptor-show --db-path /var/lib/mthydra/state.sqlite --json | jq '.generation'` returns `1`.

### §1.11 — Stand up the warm standby (optional but recommended)

A second EU host, configured per §1.1 + §1.4 but bootstrapped with `--role standby`:

```bash
# On the standby host:
mthydra-controller init \
    --db-path /var/lib/mthydra/state.sqlite \
    --age-recipient "age1...SAME_AS_ACTIVE" \
    --provider-credential "b2=$B2_KEY_ID:$B2_APPLICATION_KEY" \
    --role standby
# Standby has no controller.toml requirements for sinks — it doesn't emit them.
# But it still needs [backup] to fetch heartbeats.
mthydra-controller serve --db-path /var/lib/mthydra/state.sqlite \
    --config /etc/mthydra/controller.toml
```

**Verify:** on the active node, `mthydra-controller eu-node-list --json` should show both EU nodes after you `eu-node-add` the standby (next step).

```bash
# On the active node:
mthydra-controller eu-node-add eu-standby-1 \
    --hostname standby.example.com \
    --provider hetzner --region fsn1 \
    --role standby \
    --db-path /var/lib/mthydra/state.sqlite
```

---

## §2 — Day-2 operations (the §12 obligation clock)

The **single load-bearing metric** of mthydra is "time since each obligation was last proven." If you stop running these procedures, the safety controls become aspirational and the fleet drifts toward failure silently.

### §2.1 — Check obligation status (run weekly minimum, ideally daily)

**→ `mthydra-ops daily-check`** (one command; exits non-zero if any overdue obligation or crit anti-obligation, suitable for `cron` with email-on-failure)

```bash
mthydra-controller obs-status --json | jq '.obligations_overdue[] | .obligation_id'
```

**Action:** for each overdue obligation, run the procedure that proves it (table below). DO NOT mark "looks fine" without running the procedure.

| Obligation | Procedure |
|---|---|
| `backup_restore_dryrun` | §10.1 (T2 Case A dry-run) |
| `t2_dryrun_caseA` / `t2_dryrun_caseB` | §10.1 / §10.2 |
| `t1_dormant_health` | (deferred — spec L not yet built) |
| `t3_vantage_revalidation` | §7.2 (vantage re-attest) |
| `t3_profile_repin` | §3.2 (rebuild image with `--profile-json`) |
| `t4_upstream_check` | §3.1 (upstream tracker) |
| `t4_image_promoted` | §3 (build → soak → promote) |
| `t5_pool_revalidation` | §4.2 (cover-attest-verified) |
| `cover_pool_reverify_pass_proven` | §4.2 |
| `cover_pool_replenishment_proven` | §4.1 (cover-add) |
| `eu_standby_drill_proven` | §10.2 (standby promotion drill) |
| `g_provision_drill_proven` | §5 (provision a test box, terminate, count) |
| `shard_reshuffle_proven` | §6.1 (the scheduler does it; just confirm it ran) |
| `shard_disjointness_check_proven` | startup-check on the active node (runs invariants #33–#36) |
| `e_ru_agent_provision_replace_drill_proven` | §11.4 |
| `e_data_exit_drill_proven` | §11.5 |
| `probe_audit_sweep_ran` | check `journalctl -u mthydra-controller` for sweep heartbeats |
| `probe_coverage_proven` | §7.3 (submit fresh probe results) |
| `probe_vantage_rotation_proven` | §7.2 |
| `obs_alerter_sweep_ran` | should be hourly automatically; if overdue, the controller is wedged — restart |
| `obs_heartbeat_proven` | check your email for the hourly heartbeat |
| `dist_publish_sweep_ran` | automatic; restart if overdue |
| `descriptor_signing_key_rotation` | annual; §11.7 |

### §2.2 — Watch the anti-obligations (the warning queue)

Anti-obligations are "something is currently broken." They route through spec J's alerter as warn or crit.

```bash
mthydra-controller obs-status --json | jq '.anti_obligations'
mthydra-controller probe-due --json
mthydra-controller shard-stats --json
```

If anything fires `crit`, you should have an email AND a Telegram message. If you have one but not the other, run §8.5 to debug the silent channel.

### §2.3 — Inspect the alert log

```bash
mthydra-controller obs-alerts-recent --limit 50 --json | jq '.'
mthydra-controller obs-alerts-recent --severity crit --json
```

A row with `delivered_at: null` means delivery failed. Investigate that channel.

---

## §3 — Image lifecycle (T4 + spec D2)

mthydra never upgrades a box. New images replace old ones via natural rotation.

### §3.1 — Track upstream

The controller polls `9seconds/mtg` GitHub releases weekly. To check manually:

```bash
mthydra-controller upstream-check \
    --db-path /var/lib/mthydra/state.sqlite \
    --config /etc/mthydra/controller.toml
```

**When a new release appears**, the controller emits a `t4_upstream_release_available::v<ver>` obligation. Treat any evasion-motivated release as a prompt to run §3.2 within 7 days.

### §3.2 — Build a candidate image

**→ `mthydra-ops image-build-template > /tmp/profile.json`** prints a JSON skeleton with the right field names; edit, then pass to `mthydra-controller image-build --profile-json /tmp/profile.json`.

**Before building**, capture a known-good profile for the new release. This profile is the reference every probe compares against (T3 §269). For the MVP, the profile is a JSON blob the operator writes. Minimal content:

```json
{
  "image_version": "iv-<your version tag>",
  "transport_build_hash": "<binary sha256>",
  "tls_handshake": {
    "expected_cipher_order": ["TLS_AES_128_GCM_SHA256", "..."],
    "expected_extensions": ["server_name", "supported_versions", "..."]
  },
  "malformed_input_response": {
    "tcp_reset_within_ms": 250,
    "no_application_layer_response": true
  },
  "expected_surface": [443],
  "baseline_latency_ms": {"p50": 50, "p95": 200},
  "notes": "Captured on $DATE against $BOX"
}
```

Save to `/tmp/profile-v2.json` (or wherever). **Then:**

```bash
mthydra-controller image-build \
    --release v2.1.7 \
    --profile-json /tmp/profile-v2.json \
    --db-path /var/lib/mthydra/state.sqlite \
    --config /etc/mthydra/controller.toml
```

This downloads upstream's release, verifies its sha256, uploads to B2, and **atomically** records both the `ru_images` row and the `image_profiles` row. The image is now in `state='candidate'` — not yet provisioning new boxes.

**Verify:** `mthydra-controller image-list --json | jq '.[] | select(.state=="candidate")'` shows the new version.

### §3.3 — Provision canary boxes

**Provision at least the minimum cohort** (`cfg.image.canary.min_boxes`, default 1; consider raising to 2-3 for higher-paranoia deployments):

```bash
# Each canary box requires fresh inputs — see §5 for the full provisioning prereqs.
for i in 1 2; do
  mthydra-controller provision-seed \
      --provider hetzner --region fsn1 \
      --canary \
      --agent-source-url "https://b2.example/agent/v0.1.0.tar.gz" \
      --agent-source-sha256 "..." \
      --descriptor-refresh-url "https://b2.example/descriptors/current" \
      --db-path /var/lib/mthydra/state.sqlite \
      --config /etc/mthydra/controller.toml \
      > /tmp/canary-$i-seed.cloud-init
done
```

Boot each canary VM with the cloud-init output. Within ~10 minutes, the RU agent should call home, the box transitions `provisioning → live`, and you can confirm:

```bash
mthydra-controller ru-box-list --json | jq '.[] | select(.is_canary==1)'
```

### §3.4 — Run the soak (manual probe collection)

For each canary box, you must collect probe results from **at least `cfg.probe.min_distinct_vantages` (default 2) distinct Russia-approximating vantages**, with **at least `cfg.image.canary.min_cycles_per_box` (default 4) cycles each**.

Run probes from each vantage (cloud VPS in Kazakhstan, Belarus, or Russia-adjacent — see §7.1 for sourcing). For each probe cycle, capture the results and ingest:

```bash
# For each (box, vantage, check_type, status) tuple:
mthydra-controller probe-record \
    --box-id <canary-box-id> \
    --vantage <vantage-id> \
    --check tls_fall_through \
    --status pass \
    --cycle-at "$(date -u -Iseconds | sed 's/+00:00/Z/')" \
    --evidence "ssh log; openssl s_client output OK against cover" \
    --db-path /var/lib/mthydra/state.sqlite
```

Repeat for each check type per the spec I §8 table. **Soak should run for a minimum wall-clock time** (recommend 24h+); the controller counts cycles, not wall-clock, so the operator must enforce real soak duration manually.

### §3.5 — Check the gate

```bash
mthydra-controller image-promote-status iv-<new> \
    --json \
    --db-path /var/lib/mthydra/state.sqlite \
    --config /etc/mthydra/controller.toml
```

The output's `passed` field tells you whether `image-promote` would succeed. If `false`, the `reasons` array lists each gate condition that fails. Common ones:
- `image_profiles row missing` — re-run §3.2 with `--profile-json`.
- `insufficient canary boxes` — provision more (§3.3).
- `canary B below threshold: cycles=2 (need >= 4)` — run more probe cycles (§3.4).
- `canary boxes have pending kill verdicts` — your canary is showing signs of compromise. **Investigate.** Do NOT promote.

### §3.6 — Promote

Once `image-promote-status` returns `passed: true`:

```bash
mthydra-controller image-promote iv-<new> \
    --evidence "soak from $START to $END; vantages: $V1, $V2; cover-site behaviour: stable; latency baseline: $X" \
    --db-path /var/lib/mthydra/state.sqlite \
    --config /etc/mthydra/controller.toml
```

The previous promoted image automatically moves to `state='retired'`. **No fleet-wide migration happens.** Existing boxes keep their old image until they age out via normal rotation (replace-on-burn).

### §3.7 — Age out

Wait. Watch `mthydra-controller obs-status` for `cover_pool_rotation_pending` rows that fire on the old image's boxes. Run `ru-box-terminate --reason aged_out` as they appear, and the controller will provision replacements from the new promoted image.

---

## §4 — Cover-domain discipline (T5)

A cover domain is the SNI value a Fake-TLS box advertises. Domains move through the state machine: `candidate_unverified → candidate_verified → in_use → burned`. **Burned domains never come back.**

### §4.1 — Add a new candidate

```bash
mthydra-controller cover-add www.example-cover.invalid \
    --notes "found via @cdn-watch; spotchecked Russia route" \
    --db-path /var/lib/mthydra/state.sqlite
```

**Source good candidates by:**
- Major-CDN-fronted sites (Akamai, CloudFront, Fastly) that are unblocked in Russia.
- Sites with a real visitor pattern (not parked domains).
- Sites whose cert + handshake behavior is consistent (not behind 30 different load balancers showing different fingerprints).
- Sites you do NOT have any organizational connection to.

### §4.2 — Attest a candidate as verified

**Verification must happen from a Russia-approximating vantage** (spec I §11 #1). You probe the candidate from that vantage and confirm it looks like normal browsing.

If you have registered vantages (§7.2): the `--vantage` argument must match an `active` vantage label.

```bash
mthydra-controller cover-attest-verified www.example-cover.invalid \
    --vantage kz1 \
    --evidence "openssl s_client -servername www.example-cover.invalid -connect 1.2.3.4:443; cert chain matches; HTTP/2 GET / returns 200; no captive portal interstitial" \
    --db-path /var/lib/mthydra/state.sqlite
```

### §4.3 — Rotate a domain off the pool

Domain `in_use` → `burned`:

```bash
mthydra-controller cover-rotate www.old-cover.invalid \
    --reason "Akamai changed default cipher order; T3 fingerprint drift" \
    --db-path /var/lib/mthydra/state.sqlite
```

The controller refuses to re-add this domain ever. Don't try.

### §4.4 — Check pool health

```bash
mthydra-controller cover-pool-stats --json \
    --db-path /var/lib/mthydra/state.sqlite \
    --config /etc/mthydra/controller.toml
```

If `rotation_frozen=true`, your verified pool dropped below `cfg.cover_pool.freeze_threshold` (default 2). New box provisioning will fail until you add + attest more candidates.

---

## §5 — User onboarding (per-circle)

Each new circle member gets one full pass through this section.

### §5.1 — Out-of-band setup (BEFORE you touch the controller)

Meet the user in person or on a Signal/WhatsApp call. Confirm:
1. They have a phone running Telegram.
2. They have an email mailbox they check daily that is NOT linked to a Russian provider.
3. They understand: "if Telegram stops working, I will message you on [Signal/email/etc] saying 'switch,' and you must open [break-glass app] and tap Connect." (Break-glass is the deferred spec L; for now, the message is the warning + email distribution backstop is the recovery.)
4. They give you their **distribution Telegram chat_id** (have them start a DM with the user-distribution bot from §1.7, then `getUpdates` to read their chat_id) and their backstop email address.

### §5.2 — Add user to the controller

**→ `mthydra-ops user-onboard alice --out-of-band signal:+1... --chat-id 12345 --email alice@example.org`** runs user-add + user-channels-set + dist-test in one shot.

```bash
# 1. Register the user.
mthydra-controller user-add alice \
    --out-of-band-channel "signal:+1555..." \
    --display-name "Alice" \
    --db-path /var/lib/mthydra/state.sqlite

# 2. Register their channels.
mthydra-controller user-channels-set alice \
    --telegram 123456789 \
    --email alice@example.org \
    --db-path /var/lib/mthydra/state.sqlite
```

### §5.3 — Assign user to a shard

For very small circles (≤ 3 users), use `shard-create` directly:

```bash
mthydra-controller shard-create s-001 \
    --members alice \
    --db-path /var/lib/mthydra/state.sqlite \
    --config /etc/mthydra/controller.toml
```

For larger circles, let the reshuffle sweep auto-assign (next sweep within `reshuffle_sweep_interval`, default 1h).

### §5.4 — Confirm receipt

```bash
mthydra-controller dist-test --user-id alice \
    --db-path /var/lib/mthydra/state.sqlite \
    --config /etc/mthydra/controller.toml
```

**On Alice's side:** she should receive ONE test message in Telegram (via the distribution bot) AND ONE in email. **On the operator side:** ask Alice (out-of-band) to confirm both arrived. If only one did, fix `controller.toml` `[distribution.X]` and re-test.

---

## §6 — Shard management (T6)

Shards group users into small disjoint cohorts. The controller enforces disjointness structurally (spec H invariants #33-#36). You should rarely need to touch shards manually.

### §6.1 — Trust the sweep

The `ShardReshuffleWheel` reshuffles every `reshuffle_interval_days` (default 14d). Just check that obligations are advancing:

```bash
mthydra-controller obs-status --json | jq '.obligations_healthy[] | select(.obligation_id=="shard_reshuffle_proven")'
```

### §6.2 — Manual reshuffle (out-of-band, e.g. on suspicion)

```bash
mthydra-controller shard-reshuffle s-001 \
    --reason "operator suspicion of social-engineering attempt against user alice" \
    --db-path /var/lib/mthydra/state.sqlite \
    --config /etc/mthydra/controller.toml
```

A new shard with a fresh ID is created; the old one is retired; users are rebalanced.

### §6.3 — Inspect shard state

```bash
mthydra-controller shard-stats --json --db-path /var/lib/mthydra/state.sqlite
mthydra-controller shard-list --include-retired --json
mthydra-controller shard-show <shard_id> --json
```

---

## §7 — Probe discipline (T7 + T3 Job 1/2)

The single most fragile control. The whole edifice rests on "probes from a Russia-approximating vantage notice compromise within minutes." If vantages drift, every probe is a false green.

### §7.1 — Source disposable vantages

A vantage is a host from which you probe RU boxes. The design demands:
- **Russia-approximating:** the route from this host to RU goes through plausibly-Russian transit (KZ, BY, Russia itself, or transit-via-Russia from CIS).
- **Disposable:** you can burn it tomorrow without losing data.
- **Non-attributable:** doesn't share infrastructure / billing / DNS with your other vantages or with the EU controller.

**Practical sources:**
- KZ / BY VPS providers (FirstVDS, JustHost.ru, Beget, Selectel — but check that their egress IP doesn't appear in adversary fingerprint lists).
- Tor exit nodes that exit in Russia / KZ (poor latency; useful as a tiebreaker not a primary).
- A friend's home connection in the relevant region (single-source risk: if their home gets seized, your probes go silent; do not rely solely on this).

**Don't:**
- Use a single cloud provider for all vantages (correlation risk).
- Use a paid service that gives you "Russian IPs from Hetzner" — those are EU IPs with Russian-sounding labels; routes are EU-to-RU which trips RKN differently than RU-domestic traffic.
- Skip the registry (next section).

### §7.2 — Register a vantage

```bash
mthydra-controller vantage-add kz1 \
    --label kz1 \
    --source-kind "cloud-cis" \
    --region-hint "KZ-almaty" \
    --notes "FirstVDS-kz; provisioned 2026-05-26; rotates 2026-08-26" \
    --db-path /var/lib/mthydra/state.sqlite
```

Then **confirm** the vantage actually sees what a Russian user sees. Probe a known-good public site from the vantage; verify the cert chain matches what `openssl s_client` shows from a real Russian connection (ask a circle member). Once confirmed:

```bash
mthydra-controller vantage-attest-active kz1 \
    --evidence "openssl s_client to mail.ru shows expected cert; tested 2026-05-26" \
    --db-path /var/lib/mthydra/state.sqlite
```

**Rotate vantages every `cfg.probe.probe_vantage_ttl_days` (default 14d).** The controller emits `probe_vantage_rotation_pending::<id>` when overdue.

### §7.3 — Submit probe results

For each (box, vantage, check_type) per the §8 spec table:

```bash
mthydra-controller probe-record \
    --box-id <box> --vantage <vantage> \
    --check tls_fall_through \
    --status pass \
    --cycle-at "$(date -u -Iseconds | sed 's/+00:00/Z/')" \
    --evidence "openssl s_client -connect $BOX_IP:443 -servername $BOX_SNI; cipher order matches profile" \
    --db-path /var/lib/mthydra/state.sqlite
```

`--status` options: `pass` | `soft_fail` | `hard_fail`. **Hard-fail any of: TLS fall-through fails, cover-domain cert/handshake mismatch, surface scan responsive beyond :443, behavioural-identity fails.** Soft-fail latency/loss divergence and intermittent path failures.

### §7.4 — Probe credentials (spec I2)

If your probes need a credential bound to the (box, vantage) pair (most box implementations do):

```bash
mthydra-controller probe-credential-issue \
    --box <box-id> --vantage <vantage-id> \
    --evidence "rotation 2026-05-26" \
    --db-path /var/lib/mthydra/state.sqlite
```

The controller generates a fresh Ed25519-signed credential; the operator pulls the cred_id from the audit log (or via `probe-credential-list`) and conveys it to the vantage operator out-of-band.

Rotate via `probe-credential-revoke` + `probe-credential-issue` together.

### §7.5 — Burn a compromised vantage

**→ `mthydra-ops rotate-vantage --old <id> --new <id> --new-label <label> --burn-reason "..." --attest-evidence "..."`** runs vantage-burn + vantage-add + vantage-attest-active atomically.

If you suspect a vantage's egress IP is in a fingerprint list, or it has otherwise become correlatable:

```bash
mthydra-controller vantage-burn <vantage-id> \
    --reason "egress IP appeared in PassiveDNS public ranges on 2026-05-26" \
    --db-path /var/lib/mthydra/state.sqlite
```

Burned vantages are monotonic — the label can never be re-used. Provision a fresh vantage with a different label.

---

## §8 — Alert response (spec J + J2)

You will get alerts. Most are warns; respond on the day. Crits are 1-hour SLA.

### §8.1 — Alert arrived. Now what?

1. Read both channels (Telegram AND email). If only one fired, see §8.5.
2. Match the `dedupe_key` in the message to an obligation kind.
3. Use the table:

| dedupe_key prefix | Action |
|---|---|
| `probe_kill_pending::<box>` | §8.2 |
| `probe_coverage_pending::<box>` | §8.3 |
| `cover_pool_rotation_pending::<domain>` | §4.3 |
| `cover_pool_rotation_frozen` | §4.1, urgently |
| `shard_overdue_pending::<shard>` | controller is wedged; restart and check `journalctl` |
| `shard_unassigned_pending::<user>` | §6.1 (wait for sweep) or §5.3 manually |
| `dist_user_unregistered::<user>` | §5.2 |
| `dist_user_heartbeat_breach::<user>` | the user's distribution bot is silent for them; check their telegram_chat_id is current via §5.2 |
| `obs_dead_mans_switch_breach` | your SMTP is down OR the operator's mailbox is gone. Recover SMTP; if mailbox lost, set up a new one and re-deploy. |
| `image_rollback_pending::<box>` | §9.2 |
| `image_promote_refused` | the gate refused promotion; rerun §3.5 to see why |

### §8.2 — `probe_kill_pending` (the dangerous one)

A box has either a single hard_fail or N-of-M soft_fails across distinct vantages. **You have ~1 hour to act before the design's "short dwell on anything bad" guarantee starts decaying.**

```bash
# 1. Inspect the verdict.
mthydra-controller probe-evaluate --box-id <box> --json
# 2. If you agree this box should die:
mthydra-controller ru-box-terminate <box> --reason compromise \
    --db-path /var/lib/mthydra/state.sqlite
```

The spec H compromise reshuffle hook fires automatically: the shard the dead box served gets reshuffled (members migrated to a fresh shard_id). The distribution publisher emits new deltas to affected users.

### §8.3 — `probe_coverage_pending`

The named box has not had probe results recorded in `cfg.probe.coverage_window_seconds` (default 1h). Either:
- Your vantage operators are slacking — submit fresh probe-record calls (§7.3).
- Your vantages are silently down — check, replace, burn the dead ones (§7.5).

If it stays open >6h, severity escalates to crit automatically.

### §8.4 — Acknowledge an alert you're already working on (spec J2)

You don't want to keep getting paged every 15 minutes about an alert you've seen.

```bash
mthydra-controller obs-alert-ack "probe_kill_pending::<box-id>" \
    --evidence "investigating; new box provisioned; awaiting cutover" \
    --expires-in 4h \
    --db-path /var/lib/mthydra/state.sqlite
```

The alerter will not dispatch any `probe_kill_pending::<box-id>` alert (in either channel) until 4h pass. The underlying obligation row is unaffected; only the dispatch is silenced.

To see active acks:
```bash
mthydra-controller obs-alert-ack-list --json --db-path /var/lib/mthydra/state.sqlite
```

### §8.5 — Silent channel debugging

If Telegram fired but email didn't (or vice versa):

```bash
# Force one heartbeat email NOW; observe success/failure.
mthydra-controller obs-heartbeat-now \
    --db-path /var/lib/mthydra/state.sqlite \
    --config /etc/mthydra/controller.toml
# Tail recent alert_log for the silent channel.
mthydra-controller obs-alerts-recent --limit 20 --json | \
    jq '.[] | select(.sink=="email" and .delivered_at==null)'
```

Investigate the error string. Common: SMTP auth failed (rotated app password without updating `controller.toml`); Telegram bot revoked (regenerate at `@BotFather`, update `[observability.telegram].bot_token`).

---

## §9 — Image rollback

You promoted v2 and now v2 is misbehaving. v1 (the prior promoted image) is currently `state='retired'`.

### §9.1 — Decide

Before rolling back, confirm:
- Is v2 truly worse than v1? Don't roll back on a single noisy canary.
- Is there a v3 candidate you could promote forward instead? Forward is always cheaper than back.
- Will rollback to v1 reintroduce a vulnerability that v2 fixed? If yes, do NOT roll back — fix forward.

### §9.2 — Execute

```bash
mthydra-controller image-rollback iv-v2 \
    --to iv-v1 \
    --evidence "v2 promoted 2026-05-25; cover-domain handshake regression observed against $VANTAGE on 2026-05-26 14:00 UTC; multiple cycles; v1 known good" \
    --db-path /var/lib/mthydra/state.sqlite
```

What happens atomically:
- `iv-v2` → `retired`.
- `iv-v1` → `promoted` again.
- Every `live` box currently running `iv-v2` gets an `image_rollback_pending::<box>` anti-obligation row (spec J alerts these as `crit`).

### §9.3 — Drain the v2 boxes

For each box in `image_rollback_pending::*`:

```bash
mthydra-controller ru-box-terminate <box-id> --reason compromise \
    --db-path /var/lib/mthydra/state.sqlite
```

Or, if you don't want the shard reshuffle that `compromise` triggers (and you're sure v2 didn't expose user IPs):
```bash
mthydra-controller ru-box-terminate <box-id> --reason rollback \
    --db-path /var/lib/mthydra/state.sqlite
```

Provision replacements from v1 via §3.3 (without `--canary`).

---

## §10 — Controller restore / promotion (T2)

The active controller has been lost. The standby is up but not yet primary. Recovery is operator-driven and assumes the old controller is compromised by default.

### §10.1 — Case A: active host intact, restore from B2 backup

```bash
# On the surviving host (could be the original after reboot, or a fresh one):
# 1. Pull the most recent backup from B2.
mthydra-controller restore-list --bucket $B2_BUCKET --json
# 2. Decrypt with the operator age key (on your laptop) and inspect.
mthydra-controller restore-decrypt --generation N --out /tmp/restored.sqlite \
    --age-identity ~/.config/mthydra/operator.age
# 3. Sanity-check.
mthydra-controller restore-summary /tmp/restored.sqlite --json
# 4. Adopt.
mthydra-controller adopt-restored-state /tmp/restored.sqlite \
    --evidence "Case A restore from B2 generation N; previous controller lost to disk failure"
```

### §10.2 — Case B: active host compromised, promote standby

This is the more dangerous case — assume the old controller leaked everything.

```bash
# On the standby:
# 1. Fetch the most recent active heartbeat.
mthydra-controller standby-heartbeat-check --json
# 2. Promote.
mthydra-controller promote-active --case B \
    --evidence "Case B: active host taken; compromise of all credentials assumed"
# 3. ROTATE all credentials.
mthydra-controller authority-rotate --evidence "post-Case-B"
mthydra-controller signing-key-rotate --evidence "post-Case-B"
# Provider API credentials — manual; rotate B2 key in B2 UI, update [observability.email] app password, etc.
# Telegram bot tokens — revoke at @BotFather, mint new ones, update controller.toml.
# 4. Standby resume serve (now as active).
systemctl restart mthydra-controller
```

### §10.3 — Verify recovery

Every recovery procedure ends with:
```bash
mthydra-controller obs-status --json
mthydra-controller obs-alert-test --severity crit \
    --message "post-recovery test from $(hostname) Case A/B"
```
Both sinks must fire. Email reaches operator. Health sweep green from an independent Russia-approximating vantage.

Update `*_dryrun_caseA` / `*_dryrun_caseB` obligations:
```bash
mthydra-controller obligation-proven t2_dryrun_caseA \
    --evidence "exercised post-recovery $(date)"
```

---

## §11 — Periodic maintenance

### §11.1 — Daily

- Glance at heartbeat email arrival in your inbox. Missing? Run §8.5.
- `mthydra-controller obs-status --json | jq '.summary_line'`

### §11.2 — Weekly

- `mthydra-controller obs-alerts-recent --limit 50 --json | jq '.[] | select(.delivered_at == null)'` — any silent failures?
- Confirm no `probe_coverage_pending` rows past 4h.
- §7.3 spot-check: pick one box, one vantage, submit one probe-record.

### §11.3 — Monthly

- Rotate distribution Telegram bot token (regenerate at `@BotFather`, update `[distribution.telegram].bot_token`, run §5.4 for one user to confirm).
- Rotate `[observability.email].password` (Gmail/Outlook app passwords expire; regenerate, update, run §1.8).
- §7.1 review: are vantages still routing through plausibly-Russian transit? Re-attest those that drifted (§7.2) or burn them (§7.5).
- §10.1 OR §10.2 dry-run (alternate months; you should exercise both within 6 months).
- `mthydra-controller compact-logs --table all --before $(date -u -d '30 days ago' -Iseconds | sed 's/+00:00/Z/') --no-dry-run --evidence "monthly retention purge"` — compact rows older than 30 days. **First run as a dry-run** (omit `--no-dry-run`) to see the row counts. **→ `mthydra-ops monthly-compact`** does the dry-run + (optional) real run in one shot.

### §11.4 — Quarterly

- §10.2 Case B dry-run on throwaway infra. Real standby promotion, real credential rotation, real verification that nothing depends on the lost-active host.
- §7.1 review: source replacement vantages, retire ones approaching 90d age.
- T8 cold-acquisition check: pick a random `live` RU box, attempt to reboot it, image the disk, confirm tmpfs/no-swap/volatile-journal yields a blank disk. **Document the result.** (T8 has no spec yet; this is operator runbook discipline pending an automated check.)

### §11.5 — Annually

- §11.3 + plus rotate the descriptor signing key: `mthydra-controller signing-key-rotate --evidence "annual"`.
- Revisit `controller.toml.example` for any new sections shipped in releases — your `controller.toml` may be missing defaults.
- Read this runbook end to end. If a procedure no longer matches reality, update the runbook (PR or commit).

### §11.6 — On every image refresh

- §3 in full.

### §11.7 — On every credential authority rotation

- After `authority-rotate`, every existing onward credential is now bound to the OLD generation. New boxes from new provision-seed get the NEW generation. Old boxes still hold old credentials until they age out. Don't panic — this is the design (replace-on-burn handles credential rotation by attrition, not by mass replacement).

---

## §12 — Honest residuals + obscurity discipline

> Quoted from design.md §13. Treat any future change to this list as a regression, not an improvement.

| Item | Status | What carries it |
|---|---|---|
| Front-line transport detectability | Accepted, not solved | T4 currency + T5 cover-domain; recovered (degraded) by T1 |
| Compromised in-point sees a user IP | Bounded, not closed | T6 shard size × T3 dwell time |
| Global timing/volume correlation | Not solved at any scale | Mitigated by staying small/private/undiscovered |
| Operator legal exposure | Assumed low, not proven | Rests on out-of-jurisdiction premise |

### T12 — the obscurity discipline

The design rests on **N being small, the circle being private, and the deployment being undiscovered**. This means:

1. **Do not advertise.** No public Telegram channel saying "DM me for proxy links." No GitHub README claiming this is a tool other people should use.
2. **Do not grow.** Each new circle member doubles the social-graph leak surface. Refuse "can my cousin join?" politely. The default answer to "can N+1?" is NO.
3. **Do not co-locate.** If you and your circle members all use the same EU host's provider for normal life (Hetzner for hosting, Backblaze for backups), and that provider has issues, every channel goes dark at once. Spread risk.
4. **Do not skip the runbook.** Skipping §2.1 once is fine; skipping it five times means the obligation clock has stopped and you don't notice. The single load-bearing metric — *time since each obligation was last proven* — only works if you keep running these procedures.
5. **Do not assume.** If the cover-domain pool drops below freeze threshold and you don't notice, the design hasn't failed — *you* did. Recoverable; don't recover by lowering the threshold.

The obscurity assumption is a control. Treat it like one.

---

*End of runbook. If anything here is wrong or out of date, that is a bug.*
