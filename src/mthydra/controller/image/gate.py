"""Validation gate for image-promote — spec D2 §4.

Pure function: reads ru_images, image_profiles, ru_boxes (is_canary),
probe_results, obligation_clocks. Returns a structured GateResult naming
every failing reason so the operator gets actionable feedback.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class GateConfigView:
    """Subset of cfg.image.canary + cfg.probe needed by the evaluator."""
    min_canary_boxes: int
    min_cycles_per_box: int
    min_distinct_vantages: int


@dataclass(frozen=True)
class GateResult:
    image_version: str
    passed: bool
    reasons: tuple[str, ...]                 # empty if passed
    canary_box_ids: tuple[str, ...]
    canary_probe_rows: int
    canary_distinct_vantages: int
    pending_kills: tuple[str, ...]


def evaluate_promotion_gate(
    conn: sqlite3.Connection,
    image_version: str,
    *,
    cfg: GateConfigView,
) -> GateResult:
    reasons: list[str] = []

    # 1. image_profiles row must exist.
    profile = conn.execute(
        "SELECT 1 FROM image_profiles WHERE image_version=?",
        (image_version,),
    ).fetchone()
    if profile is None:
        reasons.append(
            f"image_profiles row missing for {image_version!r}; "
            "run image-build with --profile-json"
        )

    # 2. canary cohort — boxes in live OR terminated state for this image_version
    #    (terminated counts because a canary's death during soak is itself a hard
    #    signal the operator may have observed).
    canary_rows = conn.execute(
        "SELECT box_id, state FROM ru_boxes "
        "WHERE image_version=? AND is_canary=1 "
        "AND state IN ('live','terminated') "
        "ORDER BY box_id",
        (image_version,),
    ).fetchall()
    canary_box_ids = tuple(r[0] for r in canary_rows)
    if len(canary_box_ids) < cfg.min_canary_boxes:
        reasons.append(
            f"insufficient canary boxes for {image_version!r}: "
            f"have {len(canary_box_ids)}, need >= {cfg.min_canary_boxes}"
        )

    # 3. per-canary probe cycle + distinct-vantage thresholds.
    cycles_total = 0
    distinct_vantages_overall: set[str] = set()
    if canary_box_ids:
        per_box_short: list[tuple[str, int, int]] = []
        for box_id in canary_box_ids:
            box_rows = conn.execute(
                "SELECT vantage_id FROM probe_results WHERE box_id=?",
                (box_id,),
            ).fetchall()
            cycles_total += len(box_rows)
            box_vantages = {r[0] for r in box_rows}
            distinct_vantages_overall |= box_vantages
            if (
                len(box_rows) < cfg.min_cycles_per_box
                or len(box_vantages) < cfg.min_distinct_vantages
            ):
                per_box_short.append(
                    (box_id, len(box_rows), len(box_vantages))
                )
        for box_id, cycles, vants in per_box_short:
            reasons.append(
                f"canary {box_id!r} below threshold: "
                f"cycles={cycles} (need >= {cfg.min_cycles_per_box}), "
                f"distinct_vantages={vants} (need >= {cfg.min_distinct_vantages})"
            )

    # 4. no live canary may carry a probe_kill_pending obligation.
    pending: list[str] = []
    if canary_box_ids:
        # Only flag *live* canaries — a terminated canary may legitimately have
        # a stale kill_pending row that the audit wheel will sweep on next tick.
        live_canaries = tuple(
            r[0] for r in canary_rows if r[1] == "live"
        )
        if live_canaries:
            placeholders = ",".join("?" * len(live_canaries))
            keys = [f"probe_kill_pending::{b}" for b in live_canaries]
            for k in keys:
                row = conn.execute(
                    "SELECT obligation_id FROM obligation_clocks WHERE obligation_id=?",
                    (k,),
                ).fetchone()
                if row is not None:
                    # Strip prefix to get box_id.
                    pending.append(k.split("::", 1)[1])
        if pending:
            reasons.append(
                f"canary boxes have pending kill verdicts: {sorted(pending)}"
            )

    return GateResult(
        image_version=image_version,
        passed=len(reasons) == 0,
        reasons=tuple(reasons),
        canary_box_ids=canary_box_ids,
        canary_probe_rows=cycles_total,
        canary_distinct_vantages=len(distinct_vantages_overall),
        pending_kills=tuple(sorted(pending)),
    )
