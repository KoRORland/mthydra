# Spec K3 — RU→EU End-to-End Connectivity Check

## 1. Purpose

Close a blind spot that produced a silent failure in production: an RU box was
reachable on `:443` (TCP open, `telnet` succeeded) but Telegram could not
connect through it, because the RU→EU leg of the path — `iptables` REDIRECT →
sing-box → Reality tunnel → EU exit → Telegram — was broken. Nothing observed
that leg. The only health signal the system has about RU boxes is the EU
**vantage probe harness** (spec I), which scans the box's *outward surface* from
the internet; it cannot see whether the box can reach its own EU exit.

This amendment adds a **meaningful end-to-end connectivity check** that
exercises the exact path a real client uses, surfaced two ways: a local
diagnostic on the box (for SSH debugging) and a proactive controller-side alert
derived from infrastructure we fully own (no new RU-side credential, outbound,
or network fingerprint).

## 2. Background — what is and isn't observable today

The data path for one proxied connection:

```
TG client ──FakeTLS:443──▶ mtg ──iptables REDIRECT──▶ sing-box ──Reality──▶ EU exit ──▶ Telegram
```

- `telnet :443` proves only that **mtg's TCP listener is up**. It says nothing
  about the FakeTLS handshake or anything past mtg.
- The vantage probe (spec I) scans the box from outside; it never traverses the
  RU→EU tunnel.
- The RU agent is **pull-only**: it fetches a signed descriptor from a
  presigned S3/B2 **GET** URL (`descriptor_refresh_url`, ~30-day TTL) and holds
  **no write credential** — a seized box must not yield bucket access. There is
  no agent→controller phone-home channel.

Two facts make a cheap, safe design possible:

1. The **EU exit is a sing-box VLESS/Reality server we fully own**
   (`data_exit/config_writer.py`), and every RU box authenticates to it as a
   per-box user named by `box_id` (allowlist keyed on `reality_uuid`). A working
   RU→EU tunnel is, by definition, a live inbound session at a box we control.
2. The **active EU node is the controller host**: `DataExitWheel` reads the
   Reality key from a local path, writes the exit's sing-box config locally, and
   SIGHUPs the local `sing-box.service`. So "controller reads the exit's live
   sessions" is a **localhost read on the active node**, not a remote channel.

## 3. Decisions

| # | Decision | Rationale |
|---|----------|-----------|
| K3-D1 | **The check lives on the RU box.** It opens a TCP connection to a Telegram DC IP:443 drawn from `seed.telegram_dcs`, which the box's own `MTHYDRA_DCS` iptables REDIRECT pushes into sing-box → Reality → EU exit → Telegram. | The RU→EU leg is only directly observable from the box. Targeting a Telegram-DC IP is what makes the connection traverse the real tunnel (the REDIRECT only fires for those CIDRs). |
| K3-D2 | **Success predicate proves the upstream established, not just the local accept.** Connect, send a minimal probe, then a short bounded read. A broken tunnel makes sing-box close the local socket promptly (it could not dial the exit) → fast EOF/error = **FAIL**; a healthy tunnel reaches Telegram and the peer holds/answers = **OK**. The exact byte-level predicate is validated during implementation against a real broken-tunnel reproduction. | A bare `connect()` returns on sing-box's *local* accept and would pass even when the upstream is dead — exactly the failure we are trying to catch. The predicate must require evidence the bytes reached the far end. |
| K3-D3 | **Probe runs inside the agent's existing 15-min recheck loop** (`ru_agent/__main__._periodic_recheck`), not a new thread. Verdict is written to `/run/mthydra/health.json` (`{checked_at, verdict, detail, telegram_dc_tried}`) and logged one line to the journal; a FAIL line is loud and greppable (`agent: EU tunnel check FAILED — …`). | Reuses an existing cadence and thread (the loop already re-verifies hardening + iptables). 15 min is adequate for a health signal; a faster cadence is unnecessary churn (YAGNI). `/run` is tmpfs — the file is ephemeral state, correct for a liveness verdict. |
| K3-D4 | **Controller corroboration via localhost-bound sing-box `clash_api`** enabled in the EU exit config. A lightweight poller on the active node reads `/connections`, maps each live VLESS session to its `box_id`, and upserts `eu_exit_observed(box_id, last_seen_at)`. | The exit already names users by `box_id`, so the mapping is free. Binding the API to `127.0.0.1` only adds **no external surface**. This is the "catch it even if the box can't report" half. |
| K3-D5 | **The existing alerter sweep flags** any `state='live'` box whose `eu_exit_observed.last_seen_at` is older than `K3_UNSEEN_THRESHOLD` (≈ 3× the box self-check interval) as anti-obligation **`box_eu_tunnel_unseen::<box_id>`**, with a plain-language remediation in `remediation.py`. | Reuses the obligation/alert/remediation plumbing wholesale. The box's periodic self-check itself generates a periodic observable session, so even a zero-user box stays visible; a box that is down or lying simply never appears and is flagged. |
| K3-D6 | **No new RU-side credential, outbound destination, or network fingerprint.** The box only reads (as before); all new visibility is on infrastructure we own (the local exit + controller). | This is the property the hybrid was chosen for. Realizing the alternative (box PUTs a status object) would have required minting a presigned PUT URL into the seed — a write capability on a seizable box, plus a ~30-day expiry with no refresh path. Rejected. |
| K3-D7 | **Out of scope:** mtg-layer FakeTLS secret verification (a *client*-secret concern, orthogonal to RU→EU), per-user path attribution, and historical session graphing. | YAGNI; each is a separate concern with its own failure mode and would dilute this change. |

## 4. Components & boundaries

New units, each with one purpose and an explicit interface:

- **`ru_agent/tunnel_check.py`** — `check_eu_tunnel(seed, *, timeout) -> Verdict`
  (pure logic + socket I/O) and `write_health(path, verdict)`. Depends on:
  `seed.telegram_dcs`, the OS socket layer. No controller/DB dependency.
- **`controller/state/eu_exit_observed.py`** — table repo:
  `record_seen(conn, box_id, at)`, `last_seen(conn, box_id) -> str | None`.
  Pure DB.
- **`controller/data_exit/session_reader.py`** —
  `poll_active_sessions(clash_api_url) -> set[box_id]`. Depends only on the
  clash_api HTTP endpoint; returns box ids, no DB writes (caller persists).

Edited:

- `ru_agent/__main__.py` — call `tunnel_check` inside `_periodic_recheck`; write
  health.json; loud journal line on FAIL.
- `data_exit/config_writer.py` — add `experimental.clash_api` bound to
  `127.0.0.1:<port>` (port from `DataExitConfig`).
- The alerter sweep — new `box_eu_tunnel_unseen` check (D5).
- `controller/observability/remediation.py` — new remediation line.
- Schema — new `eu_exit_observed` table + `SCHEMA_VERSION` bump + migration.

## 5. Data flow

```
RU box (every ~15 min):
  check_eu_tunnel() ──▶ /run/mthydra/health.json  +  journal line
        │ (side effect: a real tunnel session to the EU exit)
        ▼
EU exit sing-box (localhost clash_api /connections)
        ▼
active node poller: poll_active_sessions() ──▶ eu_exit_observed(box_id, last_seen_at)
        ▼
alerter sweep: live box, last_seen_at older than threshold
        ──▶ anti-obligation box_eu_tunnel_unseen::<box_id> ──▶ operator alert
```

## 6. Error handling

- Box check: any exception during the probe is caught and recorded as a FAIL
  verdict with the exception detail — the probe never crashes the recheck loop
  (which also re-verifies hardening + iptables and must keep running).
- clash_api unreachable from the poller: logged once per sweep, treated as "no
  observations this tick" — it must not itself raise `box_eu_tunnel_unseen` for
  every box (that would invert the signal). A dedicated obligation for "exit
  session API unreadable" MAY be added if the poller fails persistently.
- Threshold tuning: `K3_UNSEEN_THRESHOLD` and the poll cadence are module
  constants, documented so they can be adjusted without a schema change.

## 7. Testing

- Unit: `tunnel_check` OK vs. FAIL classification against a fake socket that
  models (a) healthy peer-holds-open, (b) broken fast-EOF; health.json writer.
- Unit: `eu_exit_observed` repo round-trip; `session_reader` parses a sample
  clash_api `/connections` body into box ids.
- Unit: config_writer emits localhost-bound clash_api; existing exit-config
  tests updated.
- Unit: alerter raises `box_eu_tunnel_unseen` for a live box past threshold and
  not for a recently-seen one; remediation lookup returns the new line.
- Integration: a broken-tunnel reproduction yields FAIL locally; an unseen live
  box surfaces the alert end-to-end through the sweep.
- Migration: schema ladder applies the new table forward from the prior version.

## 8. Upgrade path

Forward-only schema migration adds `eu_exit_observed`; no backfill needed
(rows accrue from the first poll). The EU exit picks up the localhost clash_api
on its next `DataExitWheel` tick (config hash changes → SIGHUP). RU boxes pick
up the self-check when they next refresh to an image carrying the new agent;
existing boxes are unaffected until reimaged but are still covered by the
controller-side corroboration once they next establish any session.
