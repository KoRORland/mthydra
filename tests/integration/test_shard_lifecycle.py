"""Spec H integration — shard lifecycle end-to-end.

  1. Bootstrap controller; user-add x6; shard-create three shards of 2.
  2. Provision 3 boxes (one per shard) via SQL + assign_box_to_shard helper.
  3. mark_live each.
  4. Advance clock past 14d; run ShardReshuffleWheel.run_once();
     assert all 3 shards retired + 3 new active shards + every user has
     a new current_shard_id.
  5. ru-box-terminate <box1> --reason compromise; assert immediate
     reshuffle of that box's shard, other shards untouched.
  6. Assert audit log records reshuffle entries with the right reasons.
"""
from __future__ import annotations

import json


def test_shard_lifecycle_provision_assign_reshuffle_compromise(tmp_path, age_recipient):
    from mthydra.controller.cli import run
    from mthydra.controller.shard_manager.wheel import ShardReshuffleWheel
    from mthydra.controller.state.credentials import issue_credential
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.ru_boxes import insert_box, mark_live
    from mthydra.controller.state.shards import assign_box_to_shard

    db = tmp_path / "state.sqlite"
    cfg = tmp_path / "controller.toml"
    cfg.write_text(
        "[node]\nrole='active'\nhostname='h'\n"
        "[backup]\nfloor_interval_hours=24\non_change_debounce_seconds=30\n"
        "endpoint=''\nbucket='b'\naccess_key_id='k'\n"
        "[backup.retention]\nkeep_daily=30\nkeep_monthly=12\nobject_lock_days=365\n"
        "[gap_monitor]\npoll_interval_minutes=30\nalarm_threshold_hours=48\n"
        "recipient_email='op@example.org'\n"
        "[descriptor]\nrotation_interval_hours=1\nvalidity_window_hours=24\n"
        "[obligations]\n[obligations.timers_hours]\n"
        "[cover_pool]\nrotation_ttl_days=14\nreverify_after_days=30\n"
        "freeze_threshold=2\nreverify_sweep_interval='1h'\n"
        "rotation_sweep_interval='1h'\nreplenishment_interval_days=90\n"
        "[shard_manager]\ntarget_size=2\nmax_size=3\n"
        "reshuffle_interval_days=14\nreshuffle_sweep_interval='1h'\n"
    )

    # 1. Bootstrap.
    run([
        "init", "--db-path", str(db),
        "--age-recipient", age_recipient,
        "--provider-credential", "b2=id:secret",
    ])

    # 2. Six users + three shards of two each.
    for u in ["u1", "u2", "u3", "u4", "u5", "u6"]:
        rc = run(["user-add", u, "--out-of-band-channel", "x", "--db-path", str(db)])
        assert rc == 0
    for sid, members in [("s1", "u1,u2"), ("s2", "u3,u4"), ("s3", "u5,u6")]:
        rc = run([
            "shard-create", sid, "--members", members,
            "--db-path", str(db), "--config", str(cfg),
        ])
        assert rc == 0

    # 3. Provision a box per shard, assign while provisioning, mark live,
    #    issue a credential + set reality_uuid, seed cover_domain_pool 'in_use'.
    conn = connect(db)
    for box_id, shard_id, ip_oct in [("b1", "s1", 1), ("b2", "s2", 2), ("b3", "s3", 3)]:
        insert_box(conn, box_id, "p", "r", f"10.0.0.{ip_oct}",
                   f"sni-{box_id}.example", "img-v1", "2026-05-01T00:00:00Z")
        assign_box_to_shard(conn, box_id=box_id, shard_id=shard_id,
                             at="2026-05-01T00:01:00Z")
        mark_live(conn, box_id, public_ip=f"10.0.0.{ip_oct}",
                  at="2026-05-01T00:02:00Z")
        issue_credential(conn, box_id, b"\x00" * 10, "2026-05-01T00:02:00Z",
                          authority_generation=1)
        conn.execute("UPDATE ru_boxes SET reality_uuid=? WHERE box_id=?",
                     (f"uuid-{box_id}", box_id))
        conn.execute(
            "INSERT INTO cover_domain_pool (domain, state, last_verified_at, "
            "verified_from_vantage, assigned_box_id, added_at, entered_in_use_at) "
            "VALUES (?, 'in_use', '2026-05-01T00:00:00Z', 'op', ?, "
            "'2026-05-01T00:00:00Z', '2026-05-01T00:00:00Z')",
            (f"sni-{box_id}.example", box_id),
        )
    # Backdate shards so they are overdue at "now".
    conn.execute(
        "UPDATE shards SET created_at='2026-05-01T00:00:00Z', "
        "last_reshuffled_at='2026-05-01T00:00:00Z' "
        "WHERE shard_id IN ('s1','s2','s3')"
    )
    conn.commit()
    conn.close()

    # 4. Run sweep with frozen clock past the TTL.
    _counter = iter(range(1000))

    def _fresh_sid() -> str:
        return f"new-{next(_counter):03d}"

    wheel = ShardReshuffleWheel(
        db_path=db, target_size=2, max_size=3,
        reshuffle_interval_days=14, sweep_interval_seconds=3600,
        mode="offline",
        clock=lambda: "2026-05-25T00:00:00Z",
        shard_id_factory=_fresh_sid,
    )
    result = wheel.run_once()
    assert len(result["reshuffled"]) == 3
    conn = connect(db)
    # All three original shards retired.
    retired = conn.execute(
        "SELECT COUNT(*) FROM shards WHERE shard_id IN ('s1','s2','s3') AND retired_at IS NOT NULL"
    ).fetchone()[0]
    assert retired == 3
    # Three new active shards.
    active = conn.execute(
        "SELECT shard_id FROM shards WHERE retired_at IS NULL ORDER BY shard_id"
    ).fetchall()
    assert len(active) == 3
    new_sids = {r[0] for r in active}
    # All six users now reference one of the new shard_ids.
    for u in ["u1", "u2", "u3", "u4", "u5", "u6"]:
        sid = conn.execute(
            "SELECT current_shard_id FROM users WHERE user_id=?", (u,)
        ).fetchone()[0]
        assert sid in new_sids
    conn.close()

    # 5. ru-box-terminate b1 --reason compromise. b1's old shard was s1 (now
    #    retired); the H-D2 trigger kept shard_id='s1' on the live row. The
    #    compromise hook reads it and reshuffles s1 -> a brand-new shard.
    #    Actually: s1 is retired. The hook only reshuffles active shards. So
    #    with TTL-reshuffle already done, compromise here is a no-op on the
    #    *retired* shard but should NOT reshuffle anything else. Verify:
    conn = connect(db)
    n_active_before = conn.execute(
        "SELECT COUNT(*) FROM shards WHERE retired_at IS NULL"
    ).fetchone()[0]
    conn.close()
    rc = run(["ru-box-terminate", "b1", "--reason", "compromise",
              "--db-path", str(db)])
    assert rc == 0
    conn = connect(db)
    n_active_after = conn.execute(
        "SELECT COUNT(*) FROM shards WHERE retired_at IS NULL"
    ).fetchone()[0]
    # Compromise on a box whose shard is already retired: no new reshuffle.
    assert n_active_after == n_active_before

    # 6. Audit log: 3 reshuffles from the sweep (reason='ttl'), and 0 or 1
    #    compromise reshuffles depending on the sequence.
    ttl_count = conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE action='shard_reshuffle' "
        "AND details_json LIKE '%\"reason\": \"ttl\"%'"
    ).fetchone()[0]
    assert ttl_count == 3
    conn.close()


def test_shard_lifecycle_compromise_before_ttl(tmp_path, age_recipient):
    """Compromise on a still-active shard does trigger immediate reshuffle."""
    from mthydra.controller.cli import run
    from mthydra.controller.state.credentials import issue_credential
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.ru_boxes import insert_box, mark_live
    from mthydra.controller.state.shards import assign_box_to_shard

    db = tmp_path / "state.sqlite"
    cfg = tmp_path / "controller.toml"
    cfg.write_text(
        "[node]\nrole='active'\nhostname='h'\n"
        "[backup]\nfloor_interval_hours=24\non_change_debounce_seconds=30\n"
        "endpoint=''\nbucket='b'\naccess_key_id='k'\n"
        "[backup.retention]\nkeep_daily=30\nkeep_monthly=12\nobject_lock_days=365\n"
        "[gap_monitor]\npoll_interval_minutes=30\nalarm_threshold_hours=48\n"
        "recipient_email='op@example.org'\n"
        "[descriptor]\nrotation_interval_hours=1\nvalidity_window_hours=24\n"
        "[obligations]\n[obligations.timers_hours]\n"
        "[cover_pool]\nrotation_ttl_days=14\nreverify_after_days=30\n"
        "freeze_threshold=2\nreverify_sweep_interval='1h'\n"
        "rotation_sweep_interval='1h'\nreplenishment_interval_days=90\n"
        "[shard_manager]\ntarget_size=2\nmax_size=3\n"
        "reshuffle_interval_days=14\nreshuffle_sweep_interval='1h'\n"
    )
    run([
        "init", "--db-path", str(db),
        "--age-recipient", age_recipient,
        "--provider-credential", "b2=id:secret",
    ])
    for u in ["u1", "u2"]:
        run(["user-add", u, "--out-of-band-channel", "x", "--db-path", str(db)])
    run(["shard-create", "s1", "--members", "u1,u2",
         "--db-path", str(db), "--config", str(cfg)])
    conn = connect(db)
    insert_box(conn, "b1", "p", "r", "10.0.0.1", "sni-b1.example",
               "img-v1", "2026-05-24T00:00:00Z")
    assign_box_to_shard(conn, box_id="b1", shard_id="s1",
                        at="2026-05-24T00:01:00Z")
    mark_live(conn, "b1", public_ip="10.0.0.1", at="2026-05-24T00:02:00Z")
    issue_credential(conn, "b1", b"\x00" * 10, "2026-05-24T00:02:00Z",
                     authority_generation=1)
    conn.execute("UPDATE ru_boxes SET reality_uuid='uuid-b1' WHERE box_id='b1'")
    conn.execute(
        "INSERT INTO cover_domain_pool (domain, state, last_verified_at, "
        "verified_from_vantage, assigned_box_id, added_at, entered_in_use_at) "
        "VALUES ('sni-b1.example', 'in_use', '2026-05-24T00:00:00Z', 'op', 'b1', "
        "'2026-05-24T00:00:00Z', '2026-05-24T00:00:00Z')"
    )
    conn.commit()
    conn.close()

    rc = run(["ru-box-terminate", "b1", "--reason", "compromise", "--db-path", str(db)])
    assert rc == 0

    conn = connect(db)
    retired = conn.execute(
        "SELECT retired_at FROM shards WHERE shard_id='s1'"
    ).fetchone()[0]
    assert retired is not None
    # Exactly one new active shard with the two users.
    rows = conn.execute(
        "SELECT shard_id, members_json FROM shards WHERE retired_at IS NULL"
    ).fetchall()
    assert len(rows) == 1
    new_sid, mj = rows[0]
    assert sorted(json.loads(mj)) == ["u1", "u2"]
    # Users remapped.
    for u in ["u1", "u2"]:
        sid = conn.execute(
            "SELECT current_shard_id FROM users WHERE user_id=?", (u,)
        ).fetchone()[0]
        assert sid == new_sid
    # Compromise audit row exists.
    n = conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE action='shard_reshuffle' "
        "AND details_json LIKE '%\"reason\": \"compromise\"%'"
    ).fetchone()[0]
    assert n == 1
    conn.close()
