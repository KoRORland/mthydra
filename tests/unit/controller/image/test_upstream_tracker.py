"""Spec D — UpstreamReleaseTracker."""
import json
from unittest.mock import MagicMock

import pytest

from mthydra.controller.image.upstream_tracker import UpstreamReleaseTracker
from mthydra.controller.state.audit import recent_events
from mthydra.controller.state.db import connect
from mthydra.controller.state.obligations import list_obligations
from mthydra.controller.state.schema import apply_schema


@pytest.fixture
def db(tmp_path):
    p = tmp_path / "state.sqlite"
    conn = connect(p)
    apply_schema(conn)
    conn.close()
    return p


def _mk_http(tag, status=200):
    def _get(url):
        resp = MagicMock()
        resp.status = status
        if status == 200:
            resp.read.return_value = json.dumps({"tag_name": tag}).encode("utf-8")
        else:
            resp.read.return_value = b""
        return resp
    return _get


def test_first_run_sets_anti_obligation(db):
    tracker = UpstreamReleaseTracker(
        db_path=db, upstream_repo="9seconds/mtg",
        github_api_url="https://api.github.com",
        poll_interval_seconds=3600, mode="offline",
        clock=lambda: "2026-05-21T00:00:00Z",
        http_client=_mk_http("v2.1.7"),
    )
    latest = tracker.run_once()
    assert latest == "v2.1.7"
    conn = connect(db)
    obs = {o.obligation_id for o in list_obligations(conn)}
    assert "t4_upstream_release_available::v2.1.7" in obs
    assert "t4_upstream_check" in obs
    conn.close()


def test_repeat_with_same_tag_does_not_double_announce(db):
    """Re-running with the same upstream tag should not produce duplicate audit
    rows beyond the per-tick check-stamp."""
    tracker = UpstreamReleaseTracker(
        db_path=db, upstream_repo="9seconds/mtg",
        github_api_url="https://api.github.com",
        poll_interval_seconds=3600, mode="offline",
        clock=lambda: "2026-05-21T00:00:00Z",
        http_client=_mk_http("v2.1.7"),
    )
    tracker.run_once()
    audit_count_first = len(recent_events(connect(db), limit=50))
    tracker.run_once()
    audit_count_second = len(recent_events(connect(db), limit=50))
    assert audit_count_second == audit_count_first + 1


def test_run_once_returns_none_on_github_failure(db):
    tracker = UpstreamReleaseTracker(
        db_path=db, upstream_repo="9seconds/mtg",
        github_api_url="https://api.github.com",
        poll_interval_seconds=3600, mode="offline",
        clock=lambda: "2026-05-21T00:00:00Z",
        http_client=_mk_http("ignored", status=503),
    )
    latest = tracker.run_once()
    assert latest is None
    conn = connect(db)
    obs = {o.obligation_id for o in list_obligations(conn)}
    assert "t4_upstream_check" not in obs
    conn.close()


def test_known_tag_does_not_re_emit_anti_obligation(db):
    """If the tag is already in ru_images (was built/promoted), no anti-obligation."""
    conn = connect(db)
    from mthydra.controller.state.ru_images import insert_candidate
    insert_candidate(
        conn,
        image_version="iv1",
        upstream_release="v2.1.7",
        upstream_repo="9seconds/mtg",
        binary_url="x", manifest_url="x", binary_sha256="iv1",
        binary_size_bytes=100,
        built_at="2026-05-21T00:00:00Z",
    )
    conn.close()
    tracker = UpstreamReleaseTracker(
        db_path=db, upstream_repo="9seconds/mtg",
        github_api_url="https://api.github.com",
        poll_interval_seconds=3600, mode="offline",
        clock=lambda: "2026-05-21T01:00:00Z",
        http_client=_mk_http("v2.1.7"),
    )
    tracker.run_once()
    conn = connect(db)
    obs = {o.obligation_id for o in list_obligations(conn)}
    assert "t4_upstream_release_available::v2.1.7" not in obs
    assert "t4_upstream_check" in obs
    conn.close()
