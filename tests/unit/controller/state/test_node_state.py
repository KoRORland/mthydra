"""Spec F — node_state singleton repository."""
import pytest

from mthydra.controller.state.audit import recent_events
from mthydra.controller.state.db import connect
from mthydra.controller.state.node_state import (
    NodeState, current_node_state, set_node_role,
)
from mthydra.controller.state.schema import apply_schema


@pytest.fixture
def conn(tmp_db_path):
    c = connect(tmp_db_path)
    apply_schema(c)
    return c


def test_current_node_state_returns_seeded_active(conn):
    ns = current_node_state(conn)
    assert ns.role == "active"
    assert ns.promoted_at is None
    assert ns.previous_role is None


def test_set_node_role_to_active_after_promotion(conn):
    set_node_role(
        conn, role="active",
        promoted_at="2026-05-20T01:00:00Z",
        previous_role="standby",
        promotion_case="A",
        promotion_backup_generation=42,
    )
    ns = current_node_state(conn)
    assert ns.role == "active"
    assert ns.promoted_at == "2026-05-20T01:00:00Z"
    assert ns.previous_role == "standby"
    assert ns.promotion_case == "A"
    assert ns.promotion_backup_generation == 42


def test_set_node_role_emits_audit(conn):
    set_node_role(
        conn, role="active",
        promoted_at="2026-05-20T01:00:00Z",
        previous_role="standby",
        promotion_case="B",
        promotion_backup_generation=99,
    )
    ev = recent_events(conn, limit=1)
    assert ev[0].action == "node_role_set"
    assert "active" in (ev[0].details_json or "")


def test_set_node_role_rejects_invalid_role(conn):
    with pytest.raises(ValueError, match="role"):
        set_node_role(
            conn, role="invalid",
            promoted_at=None, previous_role=None,
            promotion_case=None, promotion_backup_generation=None,
        )
