# Design — Operator-friendly onboarding + deep-link end-user enrollment

Date: 2026-06-03
Status: **Approved in brainstorming; awaiting written-spec review.**
Amends: spec K (user distribution channel), spec G (provisioning), spec H (shard manager).
Author: brainstorming session 2026-06-03.

---

## 1. Purpose

Two user-facing frictions, one design:

1. **Operator friction.** Onboarding a user today is five commands (`user-add`,
   `user-channels-set`, `shard-create`, `shard-assign-box`, `dist-test`), and the
   quickstart's `shard-assign-box … --auto` line doesn't match the real CLI. The
   operator wants maximum automation and minimal typing.

2. **End-user friction.** A non-technical circle member ("granny") cannot create
   a Telegram bot or read a `chat_id` out of `getUpdates`. The most she can do is
   tap a link and tap a button.

A third, structural problem surfaced while diagnosing this: a live RU box can
exist with no shard (`mark_live` never required one), which violates invariant
check 33 and blocks `startup-check`/upgrade. The root cause is that **box→shard
binding had no default and no enforcement point**. This design closes that window
by binding the box to a shard at provisioning time.

---

## 2. Background: two shard bindings

Delivery depends on two independent shard pointers agreeing:

- `ru_boxes.shard_id` — which shard a **box** serves.
- `users.current_shard_id` — which shard a **user** belongs to.

The publisher (spec K, K-D2) sends each user the boxes from
`list_shard_boxes(user.current_shard_id)`, i.e. boxes whose `ru_boxes.shard_id`
equals the user's shard. So a user only receives boxes when a live box is bound
to the same shard. The design makes **both** default to a single well-known
shard, so the common (family) case "just works" with no extra typing, while
`--shard` overrides on either side for compromise containment.

---

## 3. Locked design decisions

| ID | Decision | Rationale |
|---|---|---|
| O-D1 | **End-user enrollment is a deep-link to the distribution bot, not a shared channel.** The operator issues a one-time token; the link is `https://t.me/<distbot>?start=<token>`. The user taps it and taps **Start**. | A shared channel would break spec K's per-user model: no per-shard subsets, no single-user kill on compromise/coercion, and the membership list leaks the circle binding (spec K explicitly warns against a shared group). Deep-link keeps a private 1:1 DM and is *easier* for the user than a channel invite. |
| O-D2 | **Chat-id capture is automatic via long-poll `getUpdates`.** A new active-only enrollment poller reads the distribution bot's updates, matches `/start <token>`, and writes `user_channels.telegram_chat_id`. | Removes the operator's manual `getUpdates` step — the only genuinely painful part. Long-poll (not webhook) needs no public HTTPS endpoint or cert on the controller. |
| O-D3 | **Tokens are operator-issued, single-use, expiring (default 24h), stored hashed.** Minted by `user-onboard`; reissued by re-running it. | Preserves spec K K-D4 ("no open self-service"): the token *is* the authentication, and the operator still vets every contact-point. Hashing + expiry + single-use limit replay and spam. |
| O-D4 | **`default_shard` is created lazily at provision time, NOT seeded at bootstrap.** *(Revised during implementation 2026-06-03.)* The original plan seeded an empty `default_shard` in the schema migration, but an empty active shard violates invariant **check 36** ("no empty active shard") and changed shard counts fleet-wide. Instead the shard is created on the first shard-less provision, and **check 36 exempts the well-known `default_shard`** (it is legitimately box-only until users are onboarded). | Keeps box-less DBs free of empty active shards (the upgrade-blocker class) while preserving the invariant for every other shard. Operator's existing DBs are unaffected. |
| O-D5 | **Box→shard binds at provisioning.** `provision_box` assigns `default_shard` (creating it on demand if absent) unless `--shard <S>` is passed; an explicit `--shard` must already exist and be active. | Every box has a shard from creation → check 33 is unviolatable, and the box lands where users in that shard will look. |
| O-D6 | **`ru-box-mark-live` guards against a NULL shard.** Refuses (clear error) if the box has no `shard_id`. | Defense for legacy/edge rows created before O-D5; turns a silent invariant violation into an actionable error at the mutation, not just at startup-check. |
| O-D7 | **`user-onboard` is the single onboarding command.** Creates the user, assigns them to the shard (`default_shard` or `--shard`, auto-created), mints a token, prints the deep-link. Email optional. | One command replaces four. Email-optional matches K-D1 (Telegram-only allowed, with a warning). |
| O-D8 | **First delivery is triggered on successful enrollment.** When the poller captures a `chat_id`, it enqueues/triggers the user's first subset publish so the user sees proxy links immediately rather than waiting for the next delta tick. | Confirms to operator + user that the channel works, end-to-end, at enrollment time. |

---

## 4. Schema additions

Version bump (next migration). New table:

```sql
CREATE TABLE pending_enrollments (
  user_id      TEXT PRIMARY KEY REFERENCES users(user_id),
  token_hash   TEXT NOT NULL,          -- sha256 of the issued token
  created_at   TEXT NOT NULL,
  expires_at   TEXT NOT NULL,
  consumed_at  TEXT                    -- NULL until a /start matches
);
CREATE INDEX ix_pending_enrollments_expires ON pending_enrollments(expires_at);
```

- One pending row per user; re-running `user-onboard` replaces it (new token, new
  expiry, `consumed_at` reset to NULL).
- A consumed row is retained for audit/idempotency; a second `/start` with the
  same token is ignored (already consumed).

Migration also seeds `default_shard` (O-D4) via the existing `create_shard`
path if the row is absent.

Bot-update offset (for `getUpdates`) is persisted so the poller resumes without
reprocessing and can't be replayed:

```sql
CREATE TABLE bot_offsets (
  bot_purpose  TEXT PRIMARY KEY,   -- 'distribution'
  last_offset  INTEGER NOT NULL,
  updated_at   TEXT NOT NULL
);
```

---

## 5. Components

Each unit is independently testable with an injected fake Telegram client (the
existing `sinks.py` pattern — no live network in tests).

1. **Enrollment token service** (`distribution/enrollment.py`)
   - `mint(conn, user_id, *, ttl_seconds, now, rng) -> token` — generates ≥64-bit
     random token, upserts hashed row, returns the plaintext token (shown once).
   - `match(conn, token, *, now) -> user_id | None` — hash-compares against
     non-expired, non-consumed rows; marks consumed on hit; returns the user.
   - `deep_link(bot_username, token) -> str`.

2. **Distribution bot receive client** (extends `distribution/sinks.py`)
   - `get_me()` → bot `@username` (cached).
   - `get_updates(offset)` → list of updates; pure transport, fake-injectable.

3. **Enrollment poller** (`distribution/enroll_poller.py`, active-only scheduler)
   - Per tick: `get_updates(offset)`; for each `/start <token>` message, call
     `match`; on hit, upsert `user_channels.telegram_chat_id`, audit, and trigger
     first delivery (O-D8); advance + persist offset. Unknown/expired tokens are
     ignored + audited. Mirrors the active-only / offline-skip discipline of every
     other scheduler (K-D8/K-D10).

4. **`user-onboard` CLI** (`cli.py`)
   - `user-onboard <user_id> [--shard S] [--email ADDR] [--display-name NAME]
     [--out-of-band-channel STR] [--ttl-hours N]`
   - Steps: `add_user` (idempotent) → ensure shard (default/`--shard`, auto-create)
     → `assign_user_to_shard` → set email channel if given (warn if absent) →
     `enrollment.mint` → print the deep-link + short operator instructions.

5. **Provisioning change** (`provisioning/seed.py` + `provision-seed` CLI)
   - `provision_box(..., shard_id: str | None = None)`; when None, use
     `default_shard`. CLI gains `--shard`. The `ru_boxes` insert sets `shard_id`.

6. **`mark_live` guard** (`state/ru_boxes.py`)
   - Refuse with a clear error if the target box has `shard_id IS NULL`.

7. **Config** (`config.py`)
   - `[shard_manager] default_shard_id = "default_shard"`.
   - `[distribution] enrollment_token_ttl_hours = 24`,
     `enroll_poll_interval_seconds` (sane default, active-only).
   - Distribution bot token already exists (K-D1); receive reuses it.

---

## 6. Data flow (end-user enrollment)

```
operator: user-onboard granny
  └─ create user → assign default_shard → mint token T → print t.me/<bot>?start=T
operator hands link to granny (out-of-band)
granny: taps link → Telegram opens bot → taps Start → bot receives "/start T"
controller enroll poller (active):
  getUpdates → sees "/start T" → enrollment.match(T) → user_id=granny
            → user_channels.telegram_chat_id = <granny chat> (audited)
            → trigger first subset delivery (granny gets proxy links)
            → mark token consumed, advance offset
```

Box side (provisioning): `provision-seed` (no `--shard`) → box bound to
`default_shard` → on `ru-box-mark-live`, guard passes → box appears in
`list_shard_boxes('default_shard')` → users in `default_shard` receive it.

---

## 7. Security

- Token: ≥64-bit CSPRNG entropy, stored as sha256, single-use, 24h default expiry.
- Unknown / expired / already-consumed `/start` payloads: ignored + audited; never
  create or mutate a user_channels row.
- `getUpdates` offset persisted so updates aren't reprocessed and a captured
  update stream can't be replayed into a second capture.
- No open self-service: a `chat_id` is only ever bound when it presents a live
  operator-issued token (O-D3 preserves K-D4).
- Anti-correlation preserved: enrollment is a 1:1 DM; no shared membership surface
  (the property a channel would have destroyed).
- Active-only + offline-skip + active-mode credential refusal mirror K-D8/K-D10.

---

## 8. Testing

- **Token service:** mint→match happy path; expiry rejection; single-use (second
  match fails); unknown-token miss; reissue replaces prior token.
- **Enroll poller:** fake updates with a valid `/start` → chat_id captured + first
  delivery triggered + offset advanced; unknown token → no capture, audited;
  consumed token replay → ignored; offline mode → does not arm.
- **Provisioning:** box bound to `default_shard` by default; `--shard` honored;
  resulting row satisfies check 33.
- **mark_live guard:** NULL-shard box refused; bound box succeeds.
- **`user-onboard`:** end-to-end — user created, assigned, token minted, link
  printed; email omitted → warning but success; re-run reissues token.
- **Migration:** `default_shard` seeded on fresh init and on upgrade-if-absent;
  idempotent.

---

## 9. Documentation

Rewrite quickstart Part 8:
- 8.1 → a single `user-onboard me` (+ optional `--email`), then "send yourself the
  link, tap Start." Remove the bot-DM/`getUpdates`/manual `shard-create` dance.
- 8.2 → "send each person their link; they tap it and tap Start." Delete the
  broken `shard-assign-box <their-name> --auto` line entirely.
- Note `--shard` for putting higher-risk contacts in a dedicated shard.
- Part 7: note that boxes auto-bind to `default_shard` (or `--shard`).

---

## 10. Out of scope (YAGNI)

- Webhook mode (long-poll is sufficient; avoids public HTTPS/cert on controller).
- Self-service without an operator token.
- Per-user bot tokens.
- Auto-applying the proxy URL on the phone (client/onboarding-kit problem, spec K).
- Broadcast/announcement channel (the hybrid option) — not requested for MVP.

---

## 11. Residual risks

1. **Long-poll vs webhook coexistence:** Telegram forbids `getUpdates` while a
   webhook is set. If the distribution bot ever had a webhook configured, the
   poller must detect the conflict and surface it (delete-webhook or error), not
   silently no-op. Covered by an explicit error path + audit.
2. **Default-shard concentration:** everyone in `default_shard` shares one box
   subset; a single compromise affects the whole default shard. Accepted for the
   small-family MVP; `--shard` is the mitigation when containment matters.
3. **Token hand-off channel:** the deep-link must reach the user out-of-band; if
   sent over a compromised channel, an attacker could enroll first. Single-use +
   short expiry + operator awareness limit the window; documented in the runbook.
4. **`default_shard` name is half-configurable (known coupling).** `user-onboard`
   resolves the shard via `cfg.shard_manager.default_shard_id`, but the box-side
   lazy-create (`provision_box`) and the check-36 exemption (`invariants.py`)
   hardcode the literal `"default_shard"`. They agree only while the config keeps
   its default. **Do not change `default_shard_id` from `"default_shard"`** until
   the box-side + invariant are plumbed to read the config too. Tracked as a
   follow-up; harmless at the current single-operator profile.
