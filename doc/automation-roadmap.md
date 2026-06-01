# Automation roadmap

The single load-bearing metric for mthydra's operator is **time spent
per week of healthy operation**. Every alert that does not require human
judgment is a tax on that budget. Every recurring manual task is a tax
on it. Past some threshold, operators stop reading alerts carefully and
start treating them as noise to clear — at which point the *real* alerts
get triaged the same way.

This document is the running list of: what's already off the operator's
plate, what's in flight, and what's queued. It is updated as work lands.

---

## Operating principles

1. **If the controller has the means, the controller does the work.**
   SSH credentials, S3 credentials, the operation is mechanical — the
   only reason an operator should hold the keyboard is if the operation
   requires judgment the controller can't make.
2. **Operator alerts must be actionable in <5 minutes.** If the only
   useful operator action is "go look at logs," that's a controller bug,
   not an alert. The controller should look at the logs and put the
   verdict in the alert.
3. **Self-heal before alerting, alert before failing.** Three layers:
   transient errors retry silently; persistent errors raise an
   anti-obligation with a clear remediation; only existential errors
   page (CRIT).
4. **The operator's standing time-budget is ~30 min/week.** Anything
   that takes more than that on a steady-state healthy fleet is a bug
   in the automation, not in the operator.
5. **Anti-roadmap discipline.** Some things deliberately stay manual —
   compromise verdicts, new cover-domain selection, user onboarding.
   See §5.

---

## §1 — Already automated

Listed by the spec that introduced the automation; cumulative.

| Capability | Spec | Replaces |
|---|---|---|
| Pre-upgrade backup + auto-rollback | Q | manual git-pull / pip / restart loop, manual restore-from-backup |
| Schema-migrate via subcommand | R (R-7) | python one-liner calling apply_schema directly |
| GitHub Release fallback to `git ls-remote --tags` | S (S-2) | manual `gh release create` per tag |
| polkit rule for mthydra → systemctl | S (S-4) | per-call interactive auth prompts |
| `chown -R mthydra:mthydra /opt/mthydra/{src,venv}` on install | S (S-1) | EACCES on every self-update |
| Heartbeat enriched with version + host + schema + SHA | R (R-8) | "mthydra heartbeat" with no fleet identification |
| AWS region auto-derive from endpoint URL | R (R-2) | manual `MTHYDRA_BACKUP_REGION` env var |
| Image-prepare end-to-end | P | manual mtg release fetch + build + promote |
| Agent tarball publish | P | manual tar + S3 upload + presign |
| Probe runner (SSH-driven probes from EU side) | P | manual `probe-record` per box per probe |
| Periodic obligation checks (daily, weekly, monthly) | F | manual `daily-check` / `alert-summary` / `monthly-compact` |
| Auto-restart of controller on crash | F (systemd unit) | manual restart |
| Standby promotion + credential rotation | F (T2) | bare-metal restore-from-laptop |
| Descriptor publish + presign | (T-1 partial) | manual `aws s3 presign` on laptop (against a non-existent key, no less) |
| Vantage SSH provisioning | (T-2) | 7 manual commands across two hosts |

---

## §2 — In flight (spec U: obligation auto-resolution)

| ID | What | Status |
|---|---|---|
| U-D1 | Cover-domain auto-reverify (replaces 60-day operator attestation) | next up |
| U-D2 | Probe runner per-vantage failover (one dead vantage ≠ N box-level alerts) | queued |
| U-D3 | Shard wheel attempts resolution before raising overdue/unassigned | queued |
| U-D4 | Heartbeat-breach self-diagnosis (SMTP smoke + last N exception strings → details_json) | queued |

---

## §3 — Queued (no spec yet, ordered by load-reduction value)

Each entry: what it replaces / its operator-load tax today / why it can be
automated.

### High value

- **Descriptor publish on a timer + URL persisted in DB + ru-bringup
  auto-reads.** T-Task 1 shipped the manual command (operator runs
  `descriptor-publish-now` every ~25 days); persist the URL in the DB
  with expiry, run the publish on a 24-day timer, raise
  `descriptor_url_expiry_pending` only if the auto-refresh fails. Then
  `ru-bringup` reads the URL from the DB, no more piping output between
  commands.

- **Auto-rollback the upgrade on `image-promote-status crit` after N
  hours.** Today the operator must `image-rollback` when canary probes
  go bad. Precedent: Spec Q's auto-rollback (revert on health-check
  failure). Apply same pattern at the image layer.

- **Auto-rotate cover-domain when ONE drifts but siblings are healthy.**
  Spec U U-D1 raises `cover_pool_reverify_drift_pending::<domain>`. If
  the pool has ≥`freeze_threshold + 1` healthy alternatives, just rotate
  the drifted domain to `retired` and pick from the verified candidates.
  Only raise to operator when the pool would breach the freeze threshold.

- **GitHub Release creation in `git tag` flow.** Today operator manually
  creates Releases in the web UI (or skips, falling through to S-2's
  ls-remote fallback). A `make release VERSION=v0.0.5` target that
  tags + pushes + uses `gh release create` (or the GitHub API directly)
  would close that loop.

### Medium value

- **Backup integrity smoke test.** Weekly: download a random recent
  generation, decrypt, hash-check against the gen's recorded sha256.
  Raise `backup_integrity_failed::<gen>` on mismatch. Today the operator
  does this manually (or not at all).

- **`authority-rotate` cadence + automated triggering.** Today purely
  operator-triggered ("rotate keys periodically per your threat model").
  Could ship a config-driven cadence (`[authority] rotation_interval_days`)
  + an automated trigger; operator still has the override.

- **Provider credential rotation reminders.** B2/AWS app keys typically
  expire (Gmail app passwords every 90d, AWS root-key rotation per
  org policy). Today the operator gets a vague "SMTP failed" alert.
  Add a calendar-driven `credential_rotation_due::<provider>` obligation
  with the provider's typical cadence as default.

- **Self-service for `dist_user_heartbeat_breach::<user>`.** When a
  user's channel breaches, automated probe of the channel (send a
  no-content keepalive instead of waiting for the next real publish)
  before alerting. Filters out network blips from real channel breaks.

### Speculative / needs more thought

- **Auto-provisioning RU boxes when a region drops below target count.**
  Currently operator-triggered (`ru-bringup`). Could be automatic IF
  the operator has pre-funded their RU provider account and the API
  is automatable. Most RU providers require manual KYC each new VM, so
  this is provider-specific — TimeWeb maybe, others no.

- **Vantage auto-provisioning.** Same caveat — vantages run on
  RU-adjacent hosters where API automation is rare.

- **Auto-onboarding via Telegram bot conversation.** "Hi, here's your
  invite code, I'll set up your channel now." Skips the operator-driven
  `user-onboard` step entirely. Big UX win, raises the social-graph
  leak surface (anti-roadmap §5).

---

## §4 — Process automations (not controller code)

- **Pre-merge: ruff + tests pass on every push.** GitHub Actions
  workflow runs the unit suite on every PR. Today: developer must
  remember to run `make test` + `make lint`.
- **Per-tag: build + publish a wheel to PyPI** (private index, or
  GitHub releases as artifacts). Today: install path is `git clone`
  + `pip install -e .`, which works but breaks reproducibility — two
  hosts can be at the same git SHA but different installed states if
  one ran `pip install` at a different time.
- **Per-release: auto-generate CHANGELOG entry from commit messages
  between tags.** Today: manual append to CHANGELOG.md per release.

---

## §5 — Anti-roadmap (deliberately NOT automated)

Some things stay manual because the failure mode of automation is worse
than the cost of human attention.

| Capability | Why we don't automate |
|---|---|
| Compromise verdicts (`probe_kill_pending` → terminate box) | Wrong-positive auto-termination drops a real service for users. Human eyes are cheaper than the wrong call. |
| New cover-domain selection | Quality judgment (does the domain look like normal traffic? is the operator legally exposed by selecting it?). Algorithmic selection encodes the operator's biases without showing them. |
| User onboarding decisions | Each user added doubles the social-graph leak surface (per design.md §13). The friction is the feature. |
| Tag / release decisions | A bad release that gets auto-pushed to the active controller is worse than a manual `git tag`. Keep humans on the "ship it" button. |
| Bucket creation / IAM policy attachment | One-time setup; the cost of automating is higher than the cost of doing it once. (Quickstart §1.2 is the documentation; that's enough.) |
| Cross-jurisdiction provider procurement | "I need a VPS in Moscow this week" can't be automated without exposing payment instruments to RU-side processors. Operator decides. |

---

## §6 — How to evolve this document

When you find yourself doing the same operator task more than once, ask:
- Does the controller have the means to do it itself? (credentials, network, the operation logic)
- Is the failure mode of automation worse than the cost of doing it manually?
- Is there a clean place to surface failures to the operator when automation can't handle the case?

If yes / no / yes — add an entry to §3. Move it to §1 when it ships.
Move it to §5 if you decide automation is wrong.

The roadmap is a living document. Out-of-date entries are bugs.
