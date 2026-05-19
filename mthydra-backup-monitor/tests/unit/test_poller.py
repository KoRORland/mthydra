"""Tests for the gap-evaluator pure function."""
from mthydra_backup_monitor.poller import GapMonitorState, evaluate_gap


def test_evaluate_gap_first_observation():
    state = GapMonitorState(last_seen_gen=None, first_observed_at=None, last_alarm_at=None)
    new_state, should_alarm = evaluate_gap(
        index={"highest_gen": 7},
        state=state,
        now_iso="2026-05-18T01:00:00Z",
        alarm_threshold_hours=48,
        alarm_repeat_hours=24,
    )
    assert new_state.last_seen_gen == 7
    assert new_state.first_observed_at == "2026-05-18T01:00:00Z"
    assert not should_alarm


def test_evaluate_gap_fires_after_threshold():
    state = GapMonitorState(
        last_seen_gen=7,
        first_observed_at="2026-05-18T01:00:00Z",
        last_alarm_at=None,
    )
    _, should_alarm = evaluate_gap(
        index={"highest_gen": 7},
        state=state,
        now_iso="2026-05-20T02:00:00Z",
        alarm_threshold_hours=48,
        alarm_repeat_hours=24,
    )
    assert should_alarm


def test_evaluate_gap_does_not_fire_within_threshold():
    state = GapMonitorState(
        last_seen_gen=7,
        first_observed_at="2026-05-18T01:00:00Z",
        last_alarm_at=None,
    )
    _, should_alarm = evaluate_gap(
        index={"highest_gen": 7},
        state=state,
        now_iso="2026-05-19T01:00:00Z",  # exactly 24h < 48h threshold
        alarm_threshold_hours=48,
        alarm_repeat_hours=24,
    )
    assert not should_alarm


def test_evaluate_gap_clears_on_advancement():
    state = GapMonitorState(
        last_seen_gen=7,
        first_observed_at="2026-05-18T01:00:00Z",
        last_alarm_at="2026-05-20T02:00:00Z",
    )
    new_state, should_alarm = evaluate_gap(
        index={"highest_gen": 8},
        state=state,
        now_iso="2026-05-20T04:00:00Z",
        alarm_threshold_hours=48,
        alarm_repeat_hours=24,
    )
    assert new_state.last_seen_gen == 8
    assert new_state.first_observed_at == "2026-05-20T04:00:00Z"
    assert new_state.last_alarm_at is None
    assert not should_alarm


def test_evaluate_gap_handles_missing_index():
    state = GapMonitorState(last_seen_gen=None, first_observed_at=None, last_alarm_at=None)
    new_state, should_alarm = evaluate_gap(
        index=None,
        state=state,
        now_iso="2026-05-18T01:00:00Z",
        alarm_threshold_hours=48,
        alarm_repeat_hours=24,
    )
    assert new_state == state
    assert not should_alarm


def test_evaluate_gap_repeat_alarm():
    state = GapMonitorState(
        last_seen_gen=7,
        first_observed_at="2026-05-18T00:00:00Z",
        last_alarm_at="2026-05-20T02:00:00Z",
    )
    # 25h after last alarm — should re-fire
    _, should_alarm = evaluate_gap(
        index={"highest_gen": 7},
        state=state,
        now_iso="2026-05-21T03:00:00Z",
        alarm_threshold_hours=48,
        alarm_repeat_hours=24,
    )
    assert should_alarm


def test_evaluate_gap_no_repeat_too_soon():
    state = GapMonitorState(
        last_seen_gen=7,
        first_observed_at="2026-05-18T00:00:00Z",
        last_alarm_at="2026-05-20T02:00:00Z",
    )
    # Only 12h after last alarm — should NOT re-fire
    _, should_alarm = evaluate_gap(
        index={"highest_gen": 7},
        state=state,
        now_iso="2026-05-20T14:00:00Z",
        alarm_threshold_hours=48,
        alarm_repeat_hours=24,
    )
    assert not should_alarm
