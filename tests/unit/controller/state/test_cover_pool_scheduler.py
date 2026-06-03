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
    conn.execute("UPDATE ru_boxes SET shard_id='default_shard' WHERE box_id=?", (box_id,))
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


# ---------------------------------------------------------------------------
# U-D1 — CoverPoolAutoReverifySweep
# ---------------------------------------------------------------------------


def test_auto_reverify_sweep_stamps_proof_on_any_pass(db):
    """U-D1: when at least one verified domain passes the smell test, the
    singleton cover_pool_reverify_pass_proven obligation must be stamped.
    Replaces the operator-driven 60-day cover-attest-verified cadence."""
    from mthydra.controller.state.cover_pool_scheduler import CoverPoolAutoReverifySweep
    _add_attested(db, "fresh.org", at="2026-06-01T00:00:00Z")
    sweep = CoverPoolAutoReverifySweep(
        db_path=db, sweep_interval_seconds=3600, mode="offline",
        clock=lambda: "2026-06-01T01:00:00Z",
        check_fn=lambda domain: (True, "tls-handshake-ok"),
    )
    result = sweep.run_once()
    assert result == {"passed": ["fresh.org"], "failed": [], "auto_burned": []}
    conn = connect(db)
    obs = {o.obligation_id for o in list_obligations(conn)}
    assert "cover_pool_reverify_pass_proven" in obs
    conn.close()


def test_auto_reverify_sweep_raises_drift_on_per_domain_failure(db):
    """U-D1: a domain that fails the smell test gets a per-domain anti
    obligation that the operator can inspect. The sweep keeps running
    against the other domains — one bad domain doesn't break the sweep."""
    from mthydra.controller.state.cover_pool_scheduler import CoverPoolAutoReverifySweep
    _add_attested(db, "good.org", at="2026-06-01T00:00:00Z")
    _add_attested(db, "bad.org", at="2026-06-01T00:00:00Z")
    def fake_check(domain):
        return (True, "ok") if domain == "good.org" else (False, "timeout")
    sweep = CoverPoolAutoReverifySweep(
        db_path=db, sweep_interval_seconds=3600, mode="offline",
        clock=lambda: "2026-06-01T01:00:00Z",
        check_fn=fake_check,
    )
    sweep.run_once()
    conn = connect(db)
    obs = {o.obligation_id: o for o in list_obligations(conn)}
    assert "cover_pool_reverify_pass_proven" in obs
    drift_id = "cover_pool_reverify_drift_pending::bad.org"
    assert drift_id in obs
    assert "timeout" in (obs[drift_id].details or "")
    conn.close()


def test_auto_reverify_sweep_clears_drift_on_recovery(db):
    """U-D1: when a previously-failing domain passes again, its drift
    anti-obligation must be cleared. Operator gets the 'cleared' state
    automatically — no manual ack needed."""
    from mthydra.controller.state.cover_pool_scheduler import CoverPoolAutoReverifySweep
    _add_attested(db, "flaky.org", at="2026-06-01T00:00:00Z")
    verdicts = iter([(False, "timeout"), (True, "ok")])
    sweep = CoverPoolAutoReverifySweep(
        db_path=db, sweep_interval_seconds=3600, mode="offline",
        clock=lambda: "2026-06-01T01:00:00Z",
        check_fn=lambda d: next(verdicts),
    )
    sweep.run_once()  # first: fail → drift raised
    conn = connect(db)
    obs = {o.obligation_id for o in list_obligations(conn)}
    assert "cover_pool_reverify_drift_pending::flaky.org" in obs
    conn.close()
    sweep.run_once()  # second: pass → drift cleared
    conn = connect(db)
    obs = {o.obligation_id for o in list_obligations(conn)}
    assert "cover_pool_reverify_drift_pending::flaky.org" not in obs
    assert "cover_pool_reverify_pass_proven" in obs
    conn.close()


def test_auto_reverify_sweep_empty_pool_is_noop(db):
    """U-D1: empty pool → no stamps, no errors. The sweep is safe to run
    on a freshly-bootstrapped controller before any domains are added."""
    from mthydra.controller.state.cover_pool_scheduler import CoverPoolAutoReverifySweep
    sweep = CoverPoolAutoReverifySweep(
        db_path=db, sweep_interval_seconds=3600, mode="offline",
        clock=lambda: "2026-06-01T01:00:00Z",
        check_fn=lambda d: (True, "ok"),
    )
    result = sweep.run_once()
    assert result == {"passed": [], "failed": [], "auto_burned": []}
    conn = connect(db)
    obs = {o.obligation_id for o in list_obligations(conn)}
    # No domains in scope → proof obligation NOT stamped (empty pool isn't
    # proven by itself; it's an unprovable state).
    assert "cover_pool_reverify_pass_proven" not in obs
    conn.close()


def test_auto_reverify_check_returns_short_reason_string():
    """The reason must be a one-line string suitable for details_json —
    not a multi-line stack trace."""
    from mthydra.controller.state.cover_pool_scheduler import auto_reverify_check
    # A bogus host that won't resolve → fast failure, no real network.
    ok, reason = auto_reverify_check(
        "this-host-cannot-exist-12345.invalid", timeout_s=1.0)
    assert ok is False
    assert "\n" not in reason
    assert len(reason) < 200


# ---------------------------------------------------------------------------
# V-Task 1 — auto-rotate on drift (when pool has slack above freeze threshold)
# ---------------------------------------------------------------------------


def _add_attested_n(p, domains: list[str], at: str) -> None:
    """Bulk-add several candidate_verified domains for V-1 pool-slack tests."""
    conn = connect(p)
    for d in domains:
        add_candidate(conn, d, added_at=at)
        attest_verified(conn, d, from_vantage="ru-vps-01", at=at)
    conn.close()


def test_drifted_candidate_verified_auto_burns_when_pool_has_slack(db):
    """V-1: a drifted candidate_verified domain in a pool of 4 with
    freeze_threshold=2 must be silently burned, not raise an anti-obligation.
    The pool ends at 3 verified, still above threshold."""
    from mthydra.controller.state.cover_pool_scheduler import CoverPoolAutoReverifySweep
    _add_attested_n(db, ["a.org", "b.org", "c.org", "drifted.org"],
                    at="2026-06-01T00:00:00Z")
    sweep = CoverPoolAutoReverifySweep(
        db_path=db, sweep_interval_seconds=3600, mode="offline",
        clock=lambda: "2026-06-01T01:00:00Z",
        freeze_threshold=2,
        check_fn=lambda d: (d != "drifted.org", "ok" if d != "drifted.org" else "tls-handshake-failed"),
    )
    result = sweep.run_once()
    assert result["auto_burned"] == ["drifted.org"]
    conn = connect(db)
    # Domain is gone from cover_domain_pool, present in burned_domains.
    in_pool = conn.execute(
        "SELECT 1 FROM cover_domain_pool WHERE domain='drifted.org'"
    ).fetchone()
    assert in_pool is None
    burned = conn.execute(
        "SELECT reason FROM burned_domains WHERE domain='drifted.org'"
    ).fetchone()
    assert burned == ("auto_reverify_drift",)
    # No anti-obligation raised — operator never sees this.
    drift_ob = conn.execute(
        "SELECT 1 FROM obligation_clocks "
        "WHERE obligation_id='cover_pool_reverify_drift_pending::drifted.org'"
    ).fetchone()
    assert drift_ob is None
    conn.close()


def test_drifted_candidate_verified_NOT_burned_when_pool_at_threshold(db):
    """V-1: a drift that would drop the pool to freeze_threshold is NOT
    auto-burned — operator gets the anti-obligation to triage. Pool
    invariants beat noise reduction here."""
    from mthydra.controller.state.cover_pool_scheduler import CoverPoolAutoReverifySweep
    # 2 candidate_verified, freeze_threshold=2. After hypothetical burn:
    # count=1 < threshold → refuse to burn.
    _add_attested_n(db, ["a.org", "drifted.org"], at="2026-06-01T00:00:00Z")
    sweep = CoverPoolAutoReverifySweep(
        db_path=db, sweep_interval_seconds=3600, mode="offline",
        clock=lambda: "2026-06-01T01:00:00Z",
        freeze_threshold=2,
        check_fn=lambda d: (d == "a.org", "ok"),
    )
    result = sweep.run_once()
    assert result["auto_burned"] == []
    conn = connect(db)
    # Domain stays in pool — operator decides.
    in_pool = conn.execute(
        "SELECT 1 FROM cover_domain_pool WHERE domain='drifted.org'"
    ).fetchone()
    assert in_pool is not None
    # Anti-obligation IS raised.
    drift_ob = conn.execute(
        "SELECT details FROM obligation_clocks "
        "WHERE obligation_id='cover_pool_reverify_drift_pending::drifted.org'"
    ).fetchone()
    assert drift_ob is not None
    conn.close()


def test_drifted_in_use_domain_is_never_auto_burned(db):
    """V-1: in_use drift is the operator's call — burning the SNI orphans
    every box pointing at it. Even with massive pool slack, we don't burn."""
    from mthydra.controller.state.cover_pool import assign_to_box
    from mthydra.controller.state.cover_pool_scheduler import CoverPoolAutoReverifySweep
    _add_attested_n(db, ["a.org", "b.org", "c.org", "d.org", "in_use.org"],
                    at="2026-06-01T00:00:00Z")
    # Promote in_use.org to in_use via the existing assign primitive.
    conn = connect(db)
    # ru_boxes row required by assign_to_box.
    conn.execute(
        "INSERT INTO ru_boxes (box_id, provider, region, public_ip, sni, "
        "state, image_version, created_at) "
        "VALUES ('b-1', 'tw', 'ru-msk-1', '1.2.3.4', 'in_use.org', "
        "'live', 'iv-v1', ?)",
        ("2026-06-01T00:00:00Z",),
    )
    conn.commit()
    assign_to_box(conn, "in_use.org", box_id="b-1",
                  at="2026-06-01T00:00:00Z")
    conn.close()

    sweep = CoverPoolAutoReverifySweep(
        db_path=db, sweep_interval_seconds=3600, mode="offline",
        clock=lambda: "2026-06-01T01:00:00Z",
        freeze_threshold=2,
        check_fn=lambda d: (d != "in_use.org", "ok"),
    )
    result = sweep.run_once()
    assert result["auto_burned"] == []
    conn = connect(db)
    # Still in cover_domain_pool (state=in_use).
    in_pool = conn.execute(
        "SELECT state FROM cover_domain_pool WHERE domain='in_use.org'"
    ).fetchone()
    assert in_pool == ("in_use",)
    # Anti-obligation raised so operator can decide on box replacement.
    drift_ob = conn.execute(
        "SELECT details FROM obligation_clocks "
        "WHERE obligation_id='cover_pool_reverify_drift_pending::in_use.org'"
    ).fetchone()
    assert drift_ob is not None
    conn.close()


def test_multi_domain_drift_burns_until_threshold_then_raises(db):
    """V-1: in a single tick with N drifted candidate_verified domains
    and pool of K > threshold, burn (K - threshold) and raise the rest."""
    from mthydra.controller.state.cover_pool_scheduler import CoverPoolAutoReverifySweep
    # Pool of 5 candidate_verified, all drifted; threshold=2.
    # Expect: burn 3 (5 → 4 → 3 → 2), raise the last 2.
    drifted = [f"drift{i}.org" for i in range(5)]
    _add_attested_n(db, drifted, at="2026-06-01T00:00:00Z")
    sweep = CoverPoolAutoReverifySweep(
        db_path=db, sweep_interval_seconds=3600, mode="offline",
        clock=lambda: "2026-06-01T01:00:00Z",
        freeze_threshold=2,
        check_fn=lambda d: (False, "tls-fail"),
    )
    result = sweep.run_once()
    assert len(result["auto_burned"]) == 3
    assert len(result["failed"]) == 5
    conn = connect(db)
    remaining = {r[0] for r in conn.execute(
        "SELECT domain FROM cover_domain_pool "
        "WHERE state='candidate_verified'"
    )}
    assert len(remaining) == 2  # exactly threshold
    # Burned set + remaining = full original set.
    burned_set = set(result["auto_burned"])
    assert burned_set | remaining == set(drifted)
    conn.close()


def test_v1_burn_failure_falls_back_to_anti_obligation(db, monkeypatch):
    """V-1: if mark_burned raises (e.g. concurrent state change), the
    sweep must NOT crash — fall through to the existing anti-obligation
    path so the operator gets visibility."""
    from mthydra.controller.state import cover_pool_scheduler as sched_mod
    _add_attested_n(db, ["a.org", "b.org", "c.org", "drifted.org"],
                    at="2026-06-01T00:00:00Z")
    def boom(conn, domain, *, reason, at):
        raise RuntimeError("concurrent modification")
    monkeypatch.setattr(sched_mod, "_self_burn", boom)
    sweep = sched_mod.CoverPoolAutoReverifySweep(
        db_path=db, sweep_interval_seconds=3600, mode="offline",
        clock=lambda: "2026-06-01T01:00:00Z",
        freeze_threshold=2,
        check_fn=lambda d: (d != "drifted.org", "ok" if d != "drifted.org" else "tls-fail"),
    )
    result = sweep.run_once()
    assert result["auto_burned"] == []
    conn = connect(db)
    # Domain still in pool, anti-obligation raised.
    assert conn.execute(
        "SELECT 1 FROM cover_domain_pool WHERE domain='drifted.org'"
    ).fetchone() is not None
    assert conn.execute(
        "SELECT 1 FROM obligation_clocks "
        "WHERE obligation_id='cover_pool_reverify_drift_pending::drifted.org'"
    ).fetchone() is not None
    conn.close()
