"""Pure kill-decision evaluator — spec I §5, §8 Job 2.

`evaluate_box(...)` reads recent probe_results for a box and applies:
  * single hard_fail anywhere -> 'hard_kill'
  * N-of-M soft|hard fails across >= min_distinct_vantages distinct
    vantages within the last M cycles -> 'soft_threshold_reached'
  * fewer than N or fewer than min_distinct vantages -> 'soft_pending'
  * everything else -> 'healthy'

W-2: min_distinct_vantages is auto-tuned from the active vantage fleet.
With 1 vantage registered, requiring "2 distinct" is impossible; the
gate becomes perpetual-yellow. effective_min_distinct_vantages() scales
the threshold with fleet size (default: max(1, active // 2)). Operator
can pin an explicit floor via cfg.probe.min_distinct_vantages > 0; the
value 0 (or absent in config) selects auto.

The function refuses to evaluate a box whose image_version has no
image_profiles row (T3's "compared against the wrong reference" failure;
§8 §357). Caller (the audit wheel) surfaces this as a separate obligation.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass


def count_active_vantages(conn: sqlite3.Connection) -> int:
    """Count probe_vantages rows in state='active'. Helper for W-2 auto-tune."""
    row = conn.execute(
        "SELECT COUNT(*) FROM probe_vantages WHERE state='active'"
    ).fetchone()
    return int(row[0]) if row else 0


def effective_min_distinct_vantages(
    *, active_count: int, config_value: int,
) -> int:
    """W-2: derive the minimum-distinct-vantages threshold actually used by
    evaluate_box / evaluate_promotion_gate.

      config_value <= 0 → auto-tune: max(1, active_count // 2)
                          (1 vantage → 1, 2 → 1, 4 → 2, 10 → 5)
      config_value > 0  → use the operator's explicit value, BUT capped
                          at the fleet size (never demand more than
                          physically possible). Floor 1.

    The cap matters: an operator who set `min_distinct_vantages = 3` and
    later shrank their fleet to 2 vantages shouldn't get a perma-yellow
    gate — they should get 2 (the realistic max), with the inherent
    safety reduction visible elsewhere (vantage count itself is a
    metric).
    """
    if config_value <= 0:
        return max(1, active_count // 2)
    return max(1, min(config_value, max(1, active_count)))


@dataclass(frozen=True)
class ProbeConfigView:
    """The subset of ProbeConfig the evaluator needs. Decoupled from config.py."""
    soft_fail_window_M: int
    soft_fail_threshold_N: int
    min_distinct_vantages: int


@dataclass(frozen=True)
class EvaluationResult:
    box_id: str
    verdict: str             # 'healthy' | 'soft_pending' | 'hard_kill' | 'soft_threshold_reached'
    offending_checks: tuple[str, ...]
    distinct_vantages_consulted: int
    evidence_pointer: tuple[int, ...]   # probe_results.id refs


class EvaluationError(RuntimeError):
    """Raised when evaluation cannot proceed (e.g. missing image profile)."""


def evaluate_box(
    conn: sqlite3.Connection,
    *,
    box_id: str,
    cfg: ProbeConfigView,
    now: str,
) -> EvaluationResult:
    # Confirm the box's current image has a pinned profile.
    img = conn.execute(
        "SELECT image_version FROM ru_boxes WHERE box_id=?", (box_id,)
    ).fetchone()
    if img is None:
        raise EvaluationError(f"unknown box {box_id!r}")
    image_version = img[0]
    has_profile = conn.execute(
        "SELECT 1 FROM image_profiles WHERE image_version=?", (image_version,)
    ).fetchone()
    if has_profile is None:
        raise EvaluationError(
            f"image profile missing for {image_version!r}; pin via `profile-pin`"
        )

    # Pull the most-recent M cycles (window M; one row per (vantage, check, cycle)).
    # M is a count of *probe_results rows*, not cycles — the evaluator looks at
    # the last M raw rows. This is simpler and consistent with the §8 design
    # treating "cycles" and "checks" interchangeably for the kill decision.
    rows = conn.execute(
        "SELECT id, vantage_id, check_type, status FROM probe_results "
        "WHERE box_id=? ORDER BY cycle_at DESC, id DESC LIMIT ?",
        (box_id, cfg.soft_fail_window_M),
    ).fetchall()

    if not rows:
        return EvaluationResult(
            box_id=box_id, verdict="healthy",
            offending_checks=(), distinct_vantages_consulted=0,
            evidence_pointer=(),
        )

    # Hard-kill: single occurrence.
    hard_rows = [r for r in rows if r[3] == "hard_fail"]
    if hard_rows:
        return EvaluationResult(
            box_id=box_id, verdict="hard_kill",
            offending_checks=tuple(sorted({r[2] for r in hard_rows})),
            distinct_vantages_consulted=len({r[1] for r in rows}),
            evidence_pointer=tuple(r[0] for r in hard_rows),
        )

    # Soft-fail N-of-M with distinct-vantage requirement.
    # W-2: auto-tune the distinct-vantage threshold from the active fleet
    # so a 1-vantage MVP isn't perma-yellow. Config value is a hard cap
    # (operator opt-in); 0 / absent selects pure auto-derive.
    fail_rows = [r for r in rows if r[3] == "soft_fail"]
    distinct_vantages = len({r[1] for r in fail_rows})
    effective_min = effective_min_distinct_vantages(
        active_count=count_active_vantages(conn),
        config_value=cfg.min_distinct_vantages,
    )
    if len(fail_rows) >= cfg.soft_fail_threshold_N:
        if distinct_vantages >= effective_min:
            return EvaluationResult(
                box_id=box_id, verdict="soft_threshold_reached",
                offending_checks=tuple(sorted({r[2] for r in fail_rows})),
                distinct_vantages_consulted=distinct_vantages,
                evidence_pointer=tuple(r[0] for r in fail_rows),
            )
        # N reached but not enough distinct vantages.
        return EvaluationResult(
            box_id=box_id, verdict="soft_pending",
            offending_checks=tuple(sorted({r[2] for r in fail_rows})),
            distinct_vantages_consulted=distinct_vantages,
            evidence_pointer=tuple(r[0] for r in fail_rows),
        )

    if fail_rows:
        return EvaluationResult(
            box_id=box_id, verdict="soft_pending",
            offending_checks=tuple(sorted({r[2] for r in fail_rows})),
            distinct_vantages_consulted=distinct_vantages,
            evidence_pointer=tuple(r[0] for r in fail_rows),
        )

    return EvaluationResult(
        box_id=box_id, verdict="healthy",
        offending_checks=(),
        distinct_vantages_consulted=len({r[1] for r in rows}),
        evidence_pointer=(),
    )
