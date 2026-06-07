from __future__ import annotations

import json

from mthydra.controller.observability.fingerprint_staleness import (
    StaleFinding,
    evaluate_fingerprint_staleness,
    load_reference_set,
)


def test_match_is_not_stale():
    observed = {"chrome": "ja3-current"}
    reference = {"chrome": {"ja3-current", "ja3-previous"}}
    assert evaluate_fingerprint_staleness(observed, reference) == []


def test_drifted_ja3_is_stale():
    observed = {"chrome": "ja3-new-unrecognized"}
    reference = {"chrome": {"ja3-current", "ja3-previous"}}
    assert evaluate_fingerprint_staleness(observed, reference) == [
        StaleFinding(fingerprint="chrome", observed_ja3="ja3-new-unrecognized"),
    ]


def test_no_reference_for_fingerprint_is_stale():
    observed = {"firefox": "ja3-something"}
    reference = {"chrome": {"ja3-current"}}
    assert evaluate_fingerprint_staleness(observed, reference) == [
        StaleFinding(fingerprint="firefox", observed_ja3="ja3-something"),
    ]


def test_missing_observation_is_skipped():
    observed = {"chrome": None, "firefox": "ja3-something-else"}
    reference = {"chrome": {"ja3-current"}, "firefox": {"ja3-something-else"}}
    assert evaluate_fingerprint_staleness(observed, reference) == []


def test_load_reference_set_from_file(tmp_path):
    p = tmp_path / "reference.json"
    p.write_text(json.dumps({"chrome": ["ja3-a", "ja3-b"], "firefox": ["ja3-c"]}))
    ref = load_reference_set(p)
    assert ref == {"chrome": {"ja3-a", "ja3-b"}, "firefox": {"ja3-c"}}


def test_load_reference_set_missing_file_returns_empty(tmp_path):
    assert load_reference_set(tmp_path / "does-not-exist.json") == {}
