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


def test_load_reference_set_top_level_list_returns_empty(tmp_path):
    p = tmp_path / "reference.json"
    p.write_text(json.dumps(["chrome", "firefox"]))
    assert load_reference_set(p) == {}


def test_load_reference_set_top_level_scalar_returns_empty(tmp_path):
    p = tmp_path / "reference.json"
    p.write_text(json.dumps("just-a-string"))
    assert load_reference_set(p) == {}


def test_load_reference_set_bare_string_value_is_skipped(tmp_path):
    p = tmp_path / "reference.json"
    p.write_text(json.dumps({"chrome": "ja3x"}))
    ref = load_reference_set(p)
    # The bare string must NOT be character-iterated into a set of single chars.
    assert ref.get("chrome") is None
    assert ref == {}


def test_load_reference_set_mixed_good_and_malformed(tmp_path):
    p = tmp_path / "reference.json"
    p.write_text(json.dumps({"chrome": ["ja3-a", "ja3-b"], "firefox": "ja3x"}))
    ref = load_reference_set(p)
    assert ref == {"chrome": {"ja3-a", "ja3-b"}}
