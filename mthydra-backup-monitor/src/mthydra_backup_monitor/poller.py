"""Generation-gap evaluator — pure function, state-in / state-out.

The runtime poll loop (cli.py) drives this. Keeping the evaluator as a pure
function makes it easily testable without touching real timers or S3.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any


@dataclass(frozen=True)
class GapMonitorState:
    last_seen_gen: int | None
    first_observed_at: str | None
    last_alarm_at: str | None


def _parse(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def evaluate_gap(
    index: dict[str, Any] | None,
    state: GapMonitorState,
    now_iso: str,
    alarm_threshold_hours: int,
    alarm_repeat_hours: int,
) -> tuple[GapMonitorState, bool]:
    """Update gap-monitor state based on the latest index.json snapshot.

    Returns (new_state, should_alarm_now).

    Logic (spec A §8):
    - If index is absent (bucket empty / unreachable), leave state unchanged.
    - If highest_gen has advanced, reset the timer and clear alarm.
    - If highest_gen is stuck past alarm_threshold_hours, fire once then every
      alarm_repeat_hours while the gap persists.
    """
    if index is None:
        return state, False

    current_gen = int(index.get("highest_gen", 0))

    if state.last_seen_gen is None or current_gen > state.last_seen_gen:
        # Generation advanced — reset
        return GapMonitorState(
            last_seen_gen=current_gen,
            first_observed_at=now_iso,
            last_alarm_at=None,
        ), False

    if state.first_observed_at is None:
        return replace(state, first_observed_at=now_iso), False

    age = _parse(now_iso) - _parse(state.first_observed_at)
    if age < timedelta(hours=alarm_threshold_hours):
        return state, False  # within the window — no alarm yet

    # Past threshold — fire if we haven't alarmed yet or if repeat window elapsed
    if state.last_alarm_at is None:
        return replace(state, last_alarm_at=now_iso), True

    since_alarm = _parse(now_iso) - _parse(state.last_alarm_at)
    if since_alarm >= timedelta(hours=alarm_repeat_hours):
        return replace(state, last_alarm_at=now_iso), True

    return state, False
