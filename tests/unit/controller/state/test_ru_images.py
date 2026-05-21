"""Spec D — ru_images catalog repository."""
import pytest

from mthydra.controller.state.audit import recent_events
from mthydra.controller.state.db import connect
from mthydra.controller.state.obligations import list_obligations, set_obligation
from mthydra.controller.state.ru_images import (
    RUImage, current_promoted, get_image, insert_candidate, list_images,
    promote, retire,
)
from mthydra.controller.state.schema import apply_schema


@pytest.fixture
def conn(tmp_db_path):
    c = connect(tmp_db_path)
    apply_schema(c)
    return c


NOW = "2026-05-21T00:00:00Z"


def _candidate(conn, image_version="iv1", release="v2.1.7"):
    insert_candidate(
        conn,
        image_version=image_version,
        upstream_release=release,
        upstream_repo="9seconds/mtg",
        binary_url=f"images/{image_version}/mtg",
        manifest_url=f"images/{image_version}/manifest.json",
        binary_sha256=image_version,
        binary_size_bytes=1024,
        built_at=NOW,
    )


def test_insert_candidate_round_trips(conn):
    _candidate(conn, "iv1", "v2.1.7")
    n = get_image(conn, "iv1")
    assert n.upstream_release == "v2.1.7"
    assert n.state == "candidate"
    assert n.promoted_at is None
    assert n.retired_at is None


def test_insert_candidate_emits_audit(conn):
    _candidate(conn, "iv1")
    ev = recent_events(conn, limit=1)
    assert ev[0].action == "image_built"
    assert ev[0].target == "iv1"


def test_promote_candidate(conn):
    _candidate(conn, "iv1", "v2.1.7")
    promote(conn, "iv1", at="2026-05-21T01:00:00Z", evidence="ran probes from RU vps")
    n = get_image(conn, "iv1")
    assert n.state == "promoted"
    assert n.promoted_at == "2026-05-21T01:00:00Z"


def test_promote_retires_prior_promoted(conn):
    _candidate(conn, "iv1", "v2.1.6")
    promote(conn, "iv1", at="2026-05-20T00:00:00Z", evidence="first")
    _candidate(conn, "iv2", "v2.1.7")
    promote(conn, "iv2", at="2026-05-21T00:00:00Z", evidence="second")
    n1 = get_image(conn, "iv1")
    n2 = get_image(conn, "iv2")
    assert n1.state == "retired"
    assert n1.retired_at == "2026-05-21T00:00:00Z"
    assert n2.state == "promoted"


def test_promote_refuses_non_candidate(conn):
    _candidate(conn, "iv1")
    promote(conn, "iv1", at=NOW, evidence="x")
    with pytest.raises(ValueError, match="candidate"):
        promote(conn, "iv1", at=NOW, evidence="x")


def test_promote_clears_upstream_release_obligation(conn):
    _candidate(conn, "iv1", "v2.1.7")
    set_obligation(
        conn,
        obligation_id="t4_upstream_release_available::v2.1.7",
        last_proven_at=NOW, proven_by="tracker",
        next_due_at=NOW,
    )
    promote(conn, "iv1", at=NOW, evidence="x")
    obs = {o.obligation_id for o in list_obligations(conn)}
    assert "t4_upstream_release_available::v2.1.7" not in obs
    assert "t4_image_promoted" in obs


def test_retire_candidate(conn):
    _candidate(conn, "iv1")
    retire(conn, "iv1", at="2026-05-21T02:00:00Z", reason="build_failed_validation")
    n = get_image(conn, "iv1")
    assert n.state == "retired"
    assert n.retired_at == "2026-05-21T02:00:00Z"


def test_retire_promoted(conn):
    _candidate(conn, "iv1")
    promote(conn, "iv1", at=NOW, evidence="x")
    retire(conn, "iv1", at="2026-05-21T03:00:00Z", reason="found_regression_in_wild")
    n = get_image(conn, "iv1")
    assert n.state == "retired"


def test_retire_refuses_already_retired(conn):
    _candidate(conn, "iv1")
    retire(conn, "iv1", at=NOW, reason="x")
    with pytest.raises(ValueError, match="already retired"):
        retire(conn, "iv1", at=NOW, reason="x")


def test_current_promoted_none_when_no_image(conn):
    assert current_promoted(conn) is None


def test_current_promoted_returns_singleton(conn):
    _candidate(conn, "iv1")
    promote(conn, "iv1", at=NOW, evidence="x")
    n = current_promoted(conn)
    assert n is not None
    assert n.image_version == "iv1"


def test_list_images_filters_by_state(conn):
    _candidate(conn, "iv1", "v2.1.6")
    promote(conn, "iv1", at=NOW, evidence="x")
    _candidate(conn, "iv2", "v2.1.7")
    cands = list_images(conn, state="candidate")
    proms = list_images(conn, state="promoted")
    assert [i.image_version for i in cands] == ["iv2"]
    assert [i.image_version for i in proms] == ["iv1"]
