from __future__ import annotations

import json

from mthydra.controller.data_exit import session_reader as sr


def test_parses_box_ids_from_inbounduser():
    body = json.dumps({"connections": [
        {"metadata": {"inboundUser": "box-1"}},
        {"metadata": {"inboundUser": "box-2"}},
        {"metadata": {"inboundUser": "box-1"}},  # duplicate -> set dedupes
    ]})
    assert sr.parse_connections(body) == {"box-1", "box-2"}


def test_parses_user_fallback_key():
    body = json.dumps({"connections": [{"metadata": {"user": "box-9"}}]})
    assert sr.parse_connections(body) == {"box-9"}


def test_empty_and_userless_connections_ignored():
    body = json.dumps({"connections": [
        {"metadata": {}},                    # no user -> skipped
        {"metadata": {"inboundUser": ""}},   # empty -> skipped
    ]})
    assert sr.parse_connections(body) == set()


def test_missing_connections_key_is_empty():
    assert sr.parse_connections("{}") == set()


def test_malformed_body_is_empty_not_raise():
    assert sr.parse_connections("not json") == set()
