"""Spec F — eu_nodes inventory repository."""
import pytest

from mthydra.controller.state.audit import recent_events
from mthydra.controller.state.db import connect
from mthydra.controller.state.eu_nodes import (
    EUNode, add_eu_node, get_eu_node, get_node, list_eu_nodes, retire_eu_node,
    set_data_exit_identity, set_data_exit_state, update_heartbeat,
)
from mthydra.controller.state.schema import apply_schema


@pytest.fixture
def conn(tmp_db_path):
    c = connect(tmp_db_path)
    apply_schema(c)
    return c


NOW = "2026-05-20T00:00:00Z"


def test_add_eu_node_default_role_is_standby(conn):
    add_eu_node(
        conn, node_id="eu-standby-de-1", hostname="standby.example",
        provider="hetzner", region="de", added_at=NOW,
    )
    n = get_eu_node(conn, "eu-standby-de-1")
    assert n.role == "standby"
    assert n.hostname == "standby.example"


def test_add_eu_node_active_role(conn):
    add_eu_node(
        conn, node_id="eu-active-fr-1", hostname="active.example",
        provider="aws", region="fr", role="active", added_at=NOW,
    )
    n = get_eu_node(conn, "eu-active-fr-1")
    assert n.role == "active"


def test_add_eu_node_refuses_second_active(conn):
    add_eu_node(conn, node_id="eu-active-fr-1", hostname="a", provider="aws",
                region="fr", role="active", added_at=NOW)
    with pytest.raises(ValueError, match="only one active"):
        add_eu_node(conn, node_id="eu-active-fr-2", hostname="b", provider="aws",
                    region="fr", role="active", added_at=NOW)


def test_add_eu_node_emits_audit(conn):
    add_eu_node(conn, node_id="eu-standby-de-1", hostname="h", provider="hetzner",
                region="de", added_at=NOW)
    ev = recent_events(conn, limit=1)
    assert ev[0].action == "eu_node_added"
    assert ev[0].target == "eu-standby-de-1"


def test_retire_eu_node(conn):
    add_eu_node(conn, node_id="eu-standby-de-1", hostname="h", provider="hetzner",
                region="de", added_at=NOW)
    retire_eu_node(conn, "eu-standby-de-1", at="2026-05-21T00:00:00Z")
    n = get_eu_node(conn, "eu-standby-de-1")
    assert n.role == "retired"
    assert n.retired_at == "2026-05-21T00:00:00Z"


def test_update_heartbeat_idempotent_on_same_etag(conn):
    add_eu_node(conn, node_id="eu-standby-de-1", hostname="h", provider="hetzner",
                region="de", added_at=NOW)
    update_heartbeat(conn, "eu-standby-de-1", at="2026-05-20T01:00:00Z", b2_etag="abc")
    n_first = get_eu_node(conn, "eu-standby-de-1")
    audit_count_first = len(recent_events(conn, limit=20))

    update_heartbeat(conn, "eu-standby-de-1", at="2026-05-20T01:01:00Z", b2_etag="abc")
    n_second = get_eu_node(conn, "eu-standby-de-1")
    audit_count_second = len(recent_events(conn, limit=20))

    assert n_first.last_heartbeat_at == "2026-05-20T01:00:00Z"
    assert n_second.last_heartbeat_at == "2026-05-20T01:00:00Z"
    assert audit_count_first == audit_count_second


def test_update_heartbeat_writes_on_new_etag(conn):
    add_eu_node(conn, node_id="eu-standby-de-1", hostname="h", provider="hetzner",
                region="de", added_at=NOW)
    update_heartbeat(conn, "eu-standby-de-1", at="2026-05-20T01:00:00Z", b2_etag="abc")
    update_heartbeat(conn, "eu-standby-de-1", at="2026-05-20T01:01:00Z", b2_etag="xyz")
    n = get_eu_node(conn, "eu-standby-de-1")
    assert n.last_heartbeat_at == "2026-05-20T01:01:00Z"
    assert n.last_heartbeat_b2_etag == "xyz"


def test_list_eu_nodes_filters_by_role(conn):
    add_eu_node(conn, node_id="a", hostname="h", provider="p", region="r",
                role="active", added_at=NOW)
    add_eu_node(conn, node_id="s1", hostname="h", provider="p", region="r",
                role="standby", added_at=NOW)
    add_eu_node(conn, node_id="s2", hostname="h", provider="p", region="r",
                role="standby", added_at=NOW)
    standbys = list_eu_nodes(conn, role="standby")
    assert {n.node_id for n in standbys} == {"s1", "s2"}


def test_set_data_exit_identity_and_state(conn):
    add_eu_node(
        conn, node_id="eu1", hostname="eu1.example", provider="p",
        region="r", role="active", added_at=NOW,
    )
    set_data_exit_identity(
        conn, "eu1", cover_sni="cover.example", reality_pubkey="PUBKEY",
    )
    set_data_exit_state(
        conn, "eu1", state="healthy", started_at="2026-05-23T00:01:00Z",
    )
    row = conn.execute(
        "SELECT cover_sni, reality_pubkey, data_exit_state, data_exit_started_at "
        "FROM eu_nodes WHERE node_id='eu1'"
    ).fetchone()
    assert row == ("cover.example", "PUBKEY", "healthy", "2026-05-23T00:01:00Z")


def test_set_data_exit_identity_unknown_node_raises(conn):
    with pytest.raises(KeyError):
        set_data_exit_identity(
            conn, "missing", cover_sni="c", reality_pubkey="k",
        )


def test_set_data_exit_state_validates_state(conn):
    add_eu_node(conn, node_id="eu1", hostname="h", provider="p", region="r",
                role="active", added_at=NOW)
    with pytest.raises(ValueError, match="invalid data_exit_state"):
        set_data_exit_state(conn, "eu1", state="bogus")


def test_set_data_exit_state_without_started_at_preserves(conn):
    add_eu_node(conn, node_id="eu1", hostname="h", provider="p", region="r",
                role="active", added_at=NOW)
    set_data_exit_state(conn, "eu1", state="healthy", started_at="2026-05-23T00:01:00Z")
    set_data_exit_state(conn, "eu1", state="degraded")
    row = conn.execute(
        "SELECT data_exit_state, data_exit_started_at FROM eu_nodes WHERE node_id='eu1'"
    ).fetchone()
    assert row == ("degraded", "2026-05-23T00:01:00Z")


def test_get_node_returns_dict_with_v6_cols(conn):
    add_eu_node(conn, node_id="eu1", hostname="eu1.example", provider="p",
                region="r", role="active", added_at=NOW)
    set_data_exit_identity(conn, "eu1", cover_sni="cover", reality_pubkey="pk")
    set_data_exit_state(conn, "eu1", state="healthy", started_at=NOW)
    d = get_node(conn, "eu1")
    assert d is not None
    assert d["node_id"] == "eu1"
    assert d["cover_sni"] == "cover"
    assert d["reality_pubkey"] == "pk"
    assert d["data_exit_state"] == "healthy"
    assert d["data_exit_started_at"] == NOW


def test_get_node_missing_returns_none(conn):
    assert get_node(conn, "nope") is None
