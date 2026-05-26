# Spec J2 — Operator Alert Acknowledgment

Status: **Draft, awaiting operator review.**
Predecessor: `doc/specs/2026-05-25-J-observability-service.md` (§11 residual #4: "no alert acknowledgement mechanism").
Successors blocked on this: none.

---

## 1. Purpose

Spec J's alerter dedupes alerts by `(dedupe_key, severity)` within per-severity windows. A `crit` alert that has not been resolved continues to re-fire every 15 minutes (default) until the underlying anti-obligation clears. For long-tail incidents the operator may be aware and working on the issue but still get re-pinged.

Spec J2 adds an **explicit acknowledgment**: the operator can mark a `dedupe_key` as acked for a bounded TTL. While ack is in effect, the alerter skips dispatch for that key.

**Out of scope:** auto-ack (the design's premise is that operator silence means the alert was missed, not acknowledged); per-channel ack (an ack covers both Telegram + email simultaneously).

---

## 2. Locked design decisions

| ID | Decision | Rationale |
|---|---|---|
| J2-D1 | **Ack is keyed on `dedupe_key`, not on the underlying obligation row.** Acking `probe_kill_pending::b1` suppresses dispatch of that specific alert; if the obligation clears and re-fires (different dedupe_key with the same prefix is impossible since the key includes the target), the ack does NOT cover the new firing. | Acking the obligation itself would conflate "I read the alert" with "I'm acting on it." The dedupe_key is the dispatchable unit. |
| J2-D2 | **Acks have a bounded TTL.** `--expires-in <duration>` (default `24h`, max `7d`). Past TTL, the ack expires and the alerter resumes dispatch. | An open-ended ack would silently re-introduce the silence-is-missed failure mode. TTL forces the operator to actively re-ack if the incident is still ongoing. |
| J2-D3 | **No revocation.** An ack runs until its TTL. Operator cannot manually un-ack a row. | Avoids "I acked, then changed my mind" race that would surprise other operators. If the operator wants the alert back sooner, they wait it out (or re-fire by clearing + restoring the underlying obligation — not a sane UX, deliberate). |
| J2-D4 | **`alert_acks` table is append-only.** Every ack is a row. The alerter reads `MAX(expires_at)` per dedupe_key — the most-permissive still-valid ack wins. No UPDATE, no DELETE. | Same discipline as `alert_log`. Acks are themselves audit-relevant: post-mortem may want to know "did the operator ack at all?" |
| J2-D5 | **`audit_log` row per ack creation.** | Matches every prior pattern. |

---

## 3. Schema additions (v12 → v13)

```sql
CREATE TABLE alert_acks (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  dedupe_key  TEXT NOT NULL,
  acked_at    TEXT NOT NULL,
  acked_by    TEXT NOT NULL,
  expires_at  TEXT NOT NULL,
  evidence    TEXT NOT NULL
);

CREATE INDEX ix_alert_acks_dedupe_expires
  ON alert_acks(dedupe_key, expires_at DESC);

CREATE TRIGGER alert_acks_no_update
BEFORE UPDATE ON alert_acks
BEGIN
  SELECT RAISE(ABORT, 'alert-acks: append-only');
END;

CREATE TRIGGER alert_acks_no_delete
BEFORE DELETE ON alert_acks
WHEN NOT EXISTS (SELECT 1 FROM compactor_marker WHERE table_name='alert_acks')
BEGIN
  SELECT RAISE(ABORT, 'alert-acks: append-only (spec M: acquire compactor_marker first)');
END;
```

`SCHEMA_VERSION = 13`; `migrate_v12_to_v13` creates the table + index + triggers. Spec M's compactor learns to also handle `alert_acks` (the cutoff column is `expires_at` — once an ack has expired it's no longer load-bearing).

---

## 4. Repository API

`mthydra.controller.state.alert_acks`:

```python
@dataclass(frozen=True)
class AlertAck:
    id: int
    dedupe_key: str
    acked_at: str
    acked_by: str
    expires_at: str
    evidence: str

def ack(conn, *, dedupe_key, acked_by, evidence, at, expires_at) -> int: ...
def is_acked(conn, dedupe_key: str, *, now: str) -> bool: ...
def list_active(conn, *, now: str) -> list[AlertAck]: ...
def list_all(conn, *, limit: int = 50) -> list[AlertAck]: ...
```

---

## 5. Alerter integration

In `AlertSweep._is_deduped` (or a new sibling check), before the dedupe-window check, call `alert_acks.is_acked(conn, dedupe_key, now=now)`. If acked: skip dispatch with an `alert_acked` audit row instead of `alert_deduped`. The alert_log row is NOT appended (no attempt was made; the row would be misleading).

---

## 6. CLI surface

```
mthydra-controller obs-alert-ack <dedupe_key> \
    --evidence <text> \
    [--expires-in <duration>]  # default 24h, max 7d

mthydra-controller obs-alert-ack-list [--include-expired] [--json]
```

`--expires-in` parses `Nh`/`Nd`/`Nm`/`Ns`. The 7d cap is enforced by argparse-level validation.

---

## 7. Test plan

- Unit: `ack` appends + audit row; `is_acked` returns True while active, False past expires_at; concurrent acks all visible.
- CLI: `obs-alert-ack` with valid + invalid duration; `obs-alert-ack-list` with + without `--include-expired`.
- Alerter: anti-obligation present + active ack → no dispatch + audit `alert_acked`; same with expired ack → dispatch + dedupe normally.

---

## 8. Honest residuals

1. **No per-user ack ownership.** Any operator can ack any key; the audit row captures who. A misbehaving operator could mass-ack themselves into silence. Mitigated by audit; not solved.
2. **Ack scope is the dedupe_key only.** An ack on `probe_kill_pending::b1` does NOT cover an ack on `probe_coverage_pending::b1` even though they describe the same physical box. By design — the operator may be acting on the kill but not on coverage.
3. **No "ack everything for this incident" command.** Operator would `obs-alert-ack` each key individually. Could be wrapped in a shell loop; not built.
