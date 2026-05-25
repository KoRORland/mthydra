"""Pure kill-decision evaluator — spec I §5, §8 Job 2.

`evaluate_box(...)` reads recent probe_results for a box and applies:
  * single hard_fail anywhere -> 'hard_kill'
  * N-of-M soft|hard fails across >= min_distinct_vantages distinct
    vantages within the last M cycles -> 'soft_threshold_reached'
  * fewer than N or fewer than min_distinct vantages -> 'soft_pending'
  * everything else -> 'healthy'

The function refuses to evaluate a box whose image_version has no
image_profiles row (T3's "compared against the wrong reference" failure;
§8 §357). Caller (the audit wheel) surfaces this as a separate obligation.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass


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
    fail_rows = [r for r in rows if r[3] == "soft_fail"]
    distinct_vantages = len({r[1] for r in fail_rows})
    if len(fail_rows) >= cfg.soft_fail_threshold_N:
        if distinct_vantages >= cfg.min_distinct_vantages:
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
