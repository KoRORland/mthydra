"""Tests for observability.alert_text — human-readable alert rendering."""
from __future__ import annotations

from mthydra.controller.observability.alert_text import (
    human_age,
    humanize_label,
    kind_title,
    render_details,
    severity_word,
)


def test_severity_word_maps_known_buckets():
    assert severity_word("crit") == "CRITICAL"
    assert severity_word("warn") == "Warning"
    assert severity_word("info") == "Info"


def test_severity_word_falls_back_for_unknown():
    assert severity_word("weird") == "WEIRD"


def test_humanize_label_desnakes_and_sentence_cases():
    assert humanize_label("candidate_verified") == "Candidate verified"
    assert humanize_label("freeze_threshold") == "Freeze threshold"


def test_kind_title_uses_curated_phrase():
    assert kind_title("cover_pool_rotation_frozen") == "Cover-domain rotation is paused"
    assert kind_title("eu_heartbeat_stale") == "EU node heartbeat is stale"


def test_kind_title_unknown_falls_back_to_desnaked():
    # Never leak a raw snake_case identifier; de-snake unknown kinds.
    assert "_" not in kind_title("some_brand_new_kind")
    assert kind_title("some_brand_new_kind") == "Some brand new kind"


def test_human_age_scales_units():
    assert human_age(45) == "45 seconds"
    assert human_age(120) == "2 minutes"
    assert human_age(7200) == "2 hours"
    assert human_age(2 * 86400) == "2 days"


def test_render_details_humanizes_keys():
    out = render_details('{"candidate_verified": 0, "freeze_threshold": 1}')
    assert "Candidate verified: 0" in out
    assert "Freeze threshold: 1" in out
    # No raw snake_case keys leak through.
    assert "candidate_verified" not in out


def test_render_details_handles_none_and_garbage():
    assert render_details(None) == ""
    # Non-JSON falls back to the raw string rather than crashing.
    assert "not json" in render_details("not json")


def test_render_details_humanizes_probe_kill_body():
    out = render_details(
        '{"verdict": "soft_threshold_reached", '
        '"offending_checks": ["surface_scan"], "evidence_pointer": [3, 2, 1]}'
    )
    # Coded verdict becomes a plain phrase.
    assert "repeated health checks failed" in out
    assert "soft_threshold_reached" not in out
    # List value is de-snaked, not a raw Python repr.
    assert "Surface scan" in out
    assert "['surface_scan']" not in out
    # Internal-only reference is hidden from the operator body.
    assert "evidence" not in out.lower()
    assert "[3, 2, 1]" not in out
