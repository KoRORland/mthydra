from __future__ import annotations

import pytest

from mthydra.controller.distribution import enrollment
from mthydra.controller.state.db import connect
from mthydra.controller.state.schema import apply_schema
from mthydra.controller.state.users_shards import add_user


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "s.sqlite")
    apply_schema(c)
    add_user(c, "granny", "Granny", "phone:+0", "2026-06-03T00:00:00Z")
    yield c
    c.close()


def test_mint_then_match_happy_path(conn):
    tok = enrollment.mint(conn, "granny", ttl_seconds=3600,
                          now="2026-06-03T10:00:00Z")
    assert tok and isinstance(tok, str)
    assert enrollment.match(conn, tok, now="2026-06-03T10:30:00Z") == "granny"


def test_match_single_use(conn):
    tok = enrollment.mint(conn, "granny", ttl_seconds=3600,
                          now="2026-06-03T10:00:00Z")
    assert enrollment.match(conn, tok, now="2026-06-03T10:30:00Z") == "granny"
    assert enrollment.match(conn, tok, now="2026-06-03T10:31:00Z") is None


def test_match_expired_rejected(conn):
    tok = enrollment.mint(conn, "granny", ttl_seconds=3600,
                          now="2026-06-03T10:00:00Z")
    assert enrollment.match(conn, tok, now="2026-06-03T11:00:01Z") is None


def test_match_unknown_token(conn):
    enrollment.mint(conn, "granny", ttl_seconds=3600, now="2026-06-03T10:00:00Z")
    assert enrollment.match(conn, "bogus", now="2026-06-03T10:30:00Z") is None


def test_reissue_replaces_prior(conn):
    t1 = enrollment.mint(conn, "granny", ttl_seconds=3600,
                         now="2026-06-03T10:00:00Z")
    t2 = enrollment.mint(conn, "granny", ttl_seconds=3600,
                         now="2026-06-03T10:05:00Z")
    assert t1 != t2
    assert enrollment.match(conn, t1, now="2026-06-03T10:06:00Z") is None
    assert enrollment.match(conn, t2, now="2026-06-03T10:06:00Z") == "granny"


def test_deep_link_format():
    assert enrollment.deep_link("myfam_bot", "ABC") == \
        "https://t.me/myfam_bot?start=ABC"


def test_match_at_exact_expiry_is_rejected(conn):
    # expires_at is exclusive (expires_at > now), so presenting at the exact
    # expiry instant is rejected.
    tok = enrollment.mint(conn, "granny", ttl_seconds=3600,
                          now="2026-06-03T10:00:00Z")
    assert enrollment.match(conn, tok, now="2026-06-03T11:00:00Z") is None


def test_match_rejects_already_consumed_even_if_unexpired(conn):
    tok = enrollment.mint(conn, "granny", ttl_seconds=3600,
                          now="2026-06-03T10:00:00Z")
    assert enrollment.match(conn, tok, now="2026-06-03T10:10:00Z") == "granny"
    # Still well within TTL, but already consumed -> rejected.
    assert enrollment.match(conn, tok, now="2026-06-03T10:20:00Z") is None
