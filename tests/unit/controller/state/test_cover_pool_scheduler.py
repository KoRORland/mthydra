"""Spec C — cover-pool reverify + rotation sweep schedulers."""
import pytest

from mthydra.controller.state.cover_pool import (
    add_candidate,
    assign_to_box,
    attest_verified,
    list_by_state,
)
from mthydra.controller.state.cover_pool_scheduler import (
    CoverPoolReverifySweep,
    CoverPoolRotationSweep,
)
from mthydra.controller.state.db import connect
from mthydra.controller.state.obligations import list_obligations
from mthydra.controller.state.ru_boxes import insert_box, mark_live
from mthydra.controller.state.schema import apply_schema


@pytest.fixture
def db(tmp_path):
    p = tmp_path / "state.sqlite"
    conn = connect(p)
    apply_schema(conn)
    conn.close()
    return p


def _add_attested(p, domain: str, at: str) -> None:
    conn = connect(p)
    add_candidate(conn, domain, added_at=at)
    attest_verified(conn, domain, from_vantage="ru-vps-01", at=at)
    conn.close()


def test_reverify_sweep_downgrades_stale(db):
    _add_attested(db, "stale.org", at="2026-04-01T00:00:00Z")
    sweep = CoverPoolReverifySweep(
        db_path=db, reverify_after_days=30, sweep_interval_seconds=3600,
        mode="offline",
        clock=lambda: "2026-05-19T00:00:00Z",
    )
    sweep.run_once()
    conn = connect(db)
    rows = list_by_state(conn, "candidate_unverified")
    assert [r.domain for r in rows] == ["stale.org"]
    conn.close()


def test_reverify_sweep_proves_obligation(db):
    _add_attested(db, "fresh.org", at="2026-05-19T00:00:00Z")
    sweep = CoverPoolReverifySweep(
        db_path=db, reverify_after_days=30, sweep_interval_seconds=3600,
        mode="offline",
        clock=lambda: "2026-05-19T01:00:00Z",
    )
    sweep.run_once()
    conn = connect(db)
    obs = {o.obligation_id: o for o in list_obligations(conn)}
    assert "cover_pool_reverify_sweep_ran" in obs
    assert obs["cover_pool_reverify_sweep_ran"].last_proven_at == "2026-05-19T01:00:00Z"
    conn.close()


def _seed_box(p, box_id="box-1", sni="sni.invalid"):
    conn = connect(p)
    insert_box(conn, box_id, "aws", "eu-west-1", "10.0.0.1", sni, "img-v1", "2026-04-01T00:00:00Z")
    mark_live(conn, box_id, public_ip="10.0.0.1", at="2026-04-01T00:00:00Z")
    conn.close()


def _assign_old_domain(p, domain, box_id, at_entered):
    conn = connect(p)
    add_candidate(conn, domain, added_at=at_entered)
    attest_verified(conn, domain, from_vantage="ru-vps-01", at=at_entered)
    assign_to_box(conn, domain, box_id=box_id, at=at_entered)
    conn.close()


def test_rotation_sweep_flags_overdue(db):
    _seed_box(db, "box-1")
    _assign_old_domain(db, "old.org", "box-1", "2026-04-01T00:00:00Z")
    # Need >= freeze_threshold of verified to avoid the freeze path
    _add_attested(db, "spare-a.org", at="2026-05-19T00:00:00Z")
    _add_attested(db, "spare-b.org", at="2026-05-19T00:00:00Z")
    sweep = CoverPoolRotationSweep(
        db_path=db, rotation_ttl_days=14, freeze_threshold=2,
        sweep_interval_seconds=3600, mode="offline",
        clock=lambda: "2026-05-19T00:00:00Z",
    )
    flagged = sweep.run_once()
    assert flagged == ["old.org"]
    conn = connect(db)
    obs = {o.obligation_id: o for o in list_obligations(conn)}
    assert "cover_pool_rotation_pending::old.org" in obs
    assert "cover_pool_rotation_frozen" not in obs
    assert "cover_pool_rotation_sweep_ran" in obs
    conn.close()


def test_rotation_sweep_freezes_when_pool_low(db):
    _seed_box(db, "box-1")
    _assign_old_domain(db, "old.org", "box-1", "2026-04-01T00:00:00Z")
    # only 0 verified left after assignment -> below freeze_threshold of 2
    sweep = CoverPoolRotationSweep(
        db_path=db, rotation_ttl_days=14, freeze_threshold=2,
        sweep_interval_seconds=3600, mode="offline",
        clock=lambda: "2026-05-19T00:00:00Z",
    )
    flagged = sweep.run_once()
    assert flagged == []
    conn = connect(db)
    obs = {o.obligation_id: o for o in list_obligations(conn)}
    assert "cover_pool_rotation_frozen" in obs
    # no rotation_pending rows when frozen
    assert not any(k.startswith("cover_pool_rotation_pending::") for k in obs)
    conn.close()


def test_rotate_clears_rotation_pending_obligation(db, tmp_path):
    """Spec §7.3: cover-rotate must clear cover_pool_rotation_pending::<domain>."""
    from mthydra.controller.cli import run
    from mthydra.controller.state.obligations import list_obligations

    _seed_box(db, "box-1")
    _assign_old_domain(db, "old.org", "box-1", "2026-04-01T00:00:00Z")
    # Refill pool so sweep is not frozen
    _add_attested(db, "spare-a.org", at="2026-05-19T00:00:00Z")
    _add_attested(db, "spare-b.org", at="2026-05-19T00:00:00Z")

    sweep = CoverPoolRotationSweep(
        db_path=db, rotation_ttl_days=14, freeze_threshold=2,
        sweep_interval_seconds=3600, mode="offline",
        clock=lambda: "2026-05-19T00:00:00Z",
    )
    sweep.run_once()
    conn = connect(db)
    obs_before = {o.obligation_id for o in list_obligations(conn)}
    assert "cover_pool_rotation_pending::old.org" in obs_before
    conn.close()

    rc = run(["cover-rotate", "old.org", "--db-path", str(db)])
    assert rc == 0

    conn = connect(db)
    obs_after = {o.obligation_id for o in list_obligations(conn)}
    assert "cover_pool_rotation_pending::old.org" not in obs_after
    conn.close()


def test_rotation_sweep_clears_freeze_when_refilled(db):
    _seed_box(db, "box-1")
    _assign_old_domain(db, "old.org", "box-1", "2026-04-01T00:00:00Z")
    sweep = CoverPoolRotationSweep(
        db_path=db, rotation_ttl_days=14, freeze_threshold=2,
        sweep_interval_seconds=3600, mode="offline",
        clock=lambda: "2026-05-19T00:00:00Z",
    )
    sweep.run_once()
    # Refill the pool
    _add_attested(db, "spare-a.org", at="2026-05-19T00:30:00Z")
    _add_attested(db, "spare-b.org", at="2026-05-19T00:30:00Z")
    sweep.run_once()
    conn = connect(db)
    obs = {o.obligation_id for o in list_obligations(conn)}
    assert "cover_pool_rotation_frozen" not in obs
    conn.close()
