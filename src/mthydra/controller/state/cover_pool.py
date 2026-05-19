"""Cover-domain pool repository — spec C §5.

State machine (spec C §4):
    ∅ → candidate_unverified → candidate_verified → in_use → burned

Every state transition emits one audit_log row. Audit is the durable
record of operator-attested verification (C-D1).
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime

from mthydra.controller.state.audit import log_event


@dataclass(frozen=True)
class CoverDomain:
    domain: str
    state: str
    last_verified_at: str | None
    verified_from_vantage: str | None
    assigned_box_id: str | None
    added_at: str
    notes: str | None
    entered_in_use_at: str | None


@dataclass(frozen=True)
class PoolHealth:
    candidate_unverified: int
    candidate_verified: int
    in_use: int
    burned: int
    rotation_frozen: bool
    oldest_in_use_at: str | None
    oldest_unverified_at: str | None
    last_attest_at: str | None


_SELECT_COLS = (
    "domain, state, last_verified_at, verified_from_vantage, "
    "assigned_box_id, added_at, notes, entered_in_use_at"
)


def add_candidate(
    conn: sqlite3.Connection,
    domain: str,
    *,
    added_at: str,
    notes: str | None = None,
    actor: str = "operator",
) -> None:
    """Insert a fresh candidate. Trigger raises if domain is burned."""
    conn.execute(
        "INSERT INTO cover_domain_pool (domain, state, added_at, notes) "
        "VALUES (?, 'candidate_unverified', ?, ?)",
        (domain, added_at, notes),
    )
    log_event(
        conn, ts=added_at, actor=actor, action="cover_added",
        target=domain, details_json=json.dumps({"notes": notes}) if notes else None,
    )
    conn.commit()


def attest_verified(
    conn: sqlite3.Connection,
    domain: str,
    *,
    from_vantage: str,
    at: str,
    evidence: str | None = None,
    actor: str = "operator",
) -> None:
    """candidate_unverified → candidate_verified (C-D1, operator-attested for MVP)."""
    cur = conn.execute(
        "UPDATE cover_domain_pool SET state='candidate_verified', "
        "verified_from_vantage=?, last_verified_at=? "
        "WHERE domain=? AND state='candidate_unverified'",
        (from_vantage, at, domain),
    )
    if cur.rowcount == 0:
        raise ValueError(
            f"domain {domain!r} is not in candidate_unverified state"
        )
    details = json.dumps(
        {"vantage": from_vantage, "evidence": evidence},
        separators=(",", ":"),
    )
    log_event(
        conn, ts=at, actor=actor, action="cover_attest_verified",
        target=domain, details_json=details,
    )
    conn.commit()


def downgrade_stale_verified(
    conn: sqlite3.Connection,
    *,
    now: str,
    reverify_after_days: int,
    actor: str = "reverify_sweep",
) -> list[str]:
    """candidate_verified → candidate_unverified for rows past reverify TTL.

    Returns the list of downgraded domains; emits one audit row per.
    """
    cutoff = _iso_minus_days(now, reverify_after_days)
    rows = conn.execute(
        "SELECT domain FROM cover_domain_pool "
        "WHERE state='candidate_verified' AND last_verified_at < ?",
        (cutoff,),
    ).fetchall()
    downgraded: list[str] = [r[0] for r in rows]
    for domain in downgraded:
        conn.execute(
            "UPDATE cover_domain_pool SET state='candidate_unverified', "
            "verified_from_vantage=NULL "
            "WHERE domain=? AND state='candidate_verified'",
            (domain,),
        )
        log_event(
            conn, ts=now, actor=actor, action="cover_downgraded_stale",
            target=domain,
            details_json=json.dumps({"cutoff": cutoff}),
        )
    conn.commit()
    return downgraded


def assign_to_box(
    conn: sqlite3.Connection,
    domain: str,
    *,
    box_id: str,
    at: str,
    actor: str = "controller",
) -> None:
    """candidate_verified → in_use. Sets entered_in_use_at + assigned_box_id.

    Raises if domain is not in candidate_verified state (covers stale-verified
    after sweep downgrade). Does NOT consult freeze_threshold — freeze affects
    only the rotation sweep (spec C-D4).
    """
    cur = conn.execute(
        "UPDATE cover_domain_pool SET state='in_use', assigned_box_id=?, "
        "entered_in_use_at=? "
        "WHERE domain=? AND state='candidate_verified'",
        (box_id, at, domain),
    )
    if cur.rowcount == 0:
        raise ValueError(f"domain {domain!r} is not in candidate_verified state")
    log_event(
        conn, ts=at, actor=actor, action="cover_assigned",
        target=domain, details_json=json.dumps({"box_id": box_id}),
    )
    conn.commit()


def list_by_state(conn: sqlite3.Connection, state: str) -> list[CoverDomain]:
    rows = conn.execute(
        f"SELECT {_SELECT_COLS} FROM cover_domain_pool WHERE state=? ORDER BY domain",
        (state,),
    ).fetchall()
    return [CoverDomain(*r) for r in rows]


def list_due_for_rotation(
    conn: sqlite3.Connection,
    *,
    now: str,
    rotation_ttl_days: int,
) -> list[CoverDomain]:
    """Return in_use rows where now - entered_in_use_at > rotation_ttl_days."""
    cutoff = _iso_minus_days(now, rotation_ttl_days)
    rows = conn.execute(
        f"SELECT {_SELECT_COLS} FROM cover_domain_pool "
        "WHERE state='in_use' AND entered_in_use_at IS NOT NULL "
        "AND entered_in_use_at < ? ORDER BY entered_in_use_at",
        (cutoff,),
    ).fetchall()
    return [CoverDomain(*r) for r in rows]


def pool_health(
    conn: sqlite3.Connection,
    *,
    freeze_threshold: int = 2,
) -> PoolHealth:
    counts = {
        "candidate_unverified": 0,
        "candidate_verified": 0,
        "in_use": 0,
    }
    for state, n in conn.execute(
        "SELECT state, COUNT(*) FROM cover_domain_pool GROUP BY state"
    ).fetchall():
        counts[state] = n
    burned = conn.execute("SELECT COUNT(*) FROM burned_domains").fetchone()[0]

    oldest_in_use = conn.execute(
        "SELECT MIN(entered_in_use_at) FROM cover_domain_pool WHERE state='in_use'"
    ).fetchone()[0]
    oldest_unverified = conn.execute(
        "SELECT MIN(added_at) FROM cover_domain_pool WHERE state='candidate_unverified'"
    ).fetchone()[0]
    last_attest = conn.execute(
        "SELECT MAX(last_verified_at) FROM cover_domain_pool "
        "WHERE last_verified_at IS NOT NULL"
    ).fetchone()[0]

    return PoolHealth(
        candidate_unverified=counts["candidate_unverified"],
        candidate_verified=counts["candidate_verified"],
        in_use=counts["in_use"],
        burned=burned,
        rotation_frozen=counts["candidate_verified"] < freeze_threshold,
        oldest_in_use_at=oldest_in_use,
        oldest_unverified_at=oldest_unverified,
        last_attest_at=last_attest,
    )


def rotate_and_burn(
    conn: sqlite3.Connection,
    domain: str,
    *,
    reason: str,
    last_box_id: str,
    at: str,
    details: str | None = None,
    actor: str = "operator",
) -> None:
    """in_use → burned (atomic via burned.mark_burned).

    Asserts state='in_use' first; emits a cover_rotated audit row before
    mark_burned commits the burn. mark_burned itself emits a cover_burned
    audit row (introduced by spec A) — both rows survive.
    """
    from mthydra.controller.state.burned import mark_burned

    row = conn.execute(
        "SELECT state FROM cover_domain_pool WHERE domain=?", (domain,)
    ).fetchone()
    if row is None:
        raise ValueError(f"domain {domain!r} not in cover_domain_pool")
    if row[0] != "in_use":
        raise ValueError(
            f"cover-pool: {domain!r} is not in_use (state={row[0]})"
        )
    log_event(
        conn, ts=at, actor=actor, action="cover_rotated",
        target=domain,
        details_json=json.dumps({"reason": reason, "last_box_id": last_box_id}),
    )
    mark_burned(conn, domain, reason, last_box_id, at, details)


def _iso_minus_days(iso: str, days: int) -> str:
    from datetime import timedelta
    t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return (t - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


