"""Spec I integration — probe harness lifecycle end-to-end.

  1. Bootstrap controller; provision a box; image-promote + profile-pin.
  2. Add 3 vantages; attest 2 active.
  3. Record passing probes from both vantages -> evaluator returns 'healthy'.
  4. Record one hard_fail -> evaluator returns 'hard_kill'; sweep emits
     probe_kill_pending::b1.
  5. Operator runs ru-box-terminate --reason=compromise; spec H reshuffle
     hook fires; sweep clears the kill_pending obligation on the next tick.
  6. Burn one vantage; assert probe-record from it now refuses.
"""
from __future__ import annotations


def test_probe_harness_lifecycle(tmp_path, age_recipient):
    from mthydra.controller.cli import run
    from mthydra.controller.probe.audit_wheel import ProbeAuditWheel
    from mthydra.controller.probe.evaluator import ProbeConfigView
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
        "[probe]\nsoft_fail_window_M=4\nsoft_fail_threshold_N=3\n"
        "min_distinct_vantages=2\ncoverage_window_seconds=3600\n"
        "probe_vantage_ttl_days=14\nprobe_audit_sweep_interval='5m'\n"
    )

    # 1. Bootstrap.
    run([
        "init", "--db-path", str(db),
        "--age-recipient", age_recipient,
        "--provider-credential", "b2=id:secret",
    ])

    # Add a user + shard + box, attach an image with a pinned profile.
    run(["user-add", "u1", "--out-of-band-channel", "email", "--db-path", str(db)])
    run(["shard-create", "s1", "--members", "u1",
         "--db-path", str(db), "--config", str(cfg)])
    conn = connect(db)
    conn.execute(
        "INSERT INTO ru_images (image_version, upstream_release, upstream_repo, "
        "binary_url, manifest_url, binary_sha256, binary_size_bytes, state, "
        "built_at, promoted_at) "
        "VALUES ('v1', 'r', 'r', 'u', 'm', 'sha', 1, 'promoted', "
        "'2026-05-25T00:00:00Z', '2026-05-25T00:00:00Z')"
    )
    insert_box(conn, "b1", "p", "r", "10.0.0.1", "sni-b1.example",
               "v1", "2026-05-25T00:00:00Z")
    assign_box_to_shard(conn, box_id="b1", shard_id="s1",
                        at="2026-05-25T00:00:01Z")
    mark_live(conn, "b1", public_ip="10.0.0.1", at="2026-05-25T00:00:02Z")
    issue_credential(conn, "b1", b"\x00" * 10, "2026-05-25T00:00:02Z",
                     authority_generation=1)
    conn.execute("UPDATE ru_boxes SET reality_uuid='uuid-b1' WHERE box_id='b1'")
    conn.execute(
        "INSERT INTO cover_domain_pool (domain, state, last_verified_at, "
        "verified_from_vantage, assigned_box_id, added_at, entered_in_use_at) "
        "VALUES ('sni-b1.example', 'in_use', '2026-05-25T00:00:00Z', 'op', 'b1', "
        "'2026-05-25T00:00:00Z', '2026-05-25T00:00:00Z')"
    )
    conn.commit()
    conn.close()

    # Pin profile via CLI.
    profile_path = tmp_path / "profile.json"
    profile_path.write_text('{"surface": ":443"}')
    rc = run(["profile-pin", "v1",
              "--profile-json", str(profile_path),
              "--recorded-by", "op",
              "--db-path", str(db)])
    assert rc == 0

    # 2. Three vantages; attest two.
    for vid, label in [("va", "kz1"), ("vb", "by1"), ("vc", "tr1")]:
        rc = run(["vantage-add", vid,
                  "--label", label, "--source-kind", "cloud-cis",
                  "--db-path", str(db)])
        assert rc == 0
    run(["vantage-attest-active", "va", "--db-path", str(db)])
    run(["vantage-attest-active", "vb", "--db-path", str(db)])

    # 3. Passing probes from va + vb.
    for vid, ts in [("va", "2026-05-25T01:00:00Z"),
                     ("vb", "2026-05-25T01:01:00Z")]:
        rc = run(["probe-record",
                  "--box-id", "b1", "--vantage", vid,
                  "--check", "tls_fall_through", "--status", "pass",
                  "--cycle-at", ts,
                  "--db-path", str(db)])
        assert rc == 0
    # Evaluator returns healthy.
    cfg_view = ProbeConfigView(soft_fail_window_M=4,
                                soft_fail_threshold_N=3,
                                min_distinct_vantages=2)
    from mthydra.controller.probe.evaluator import evaluate_box
    conn = connect(db)
    res = evaluate_box(conn, box_id="b1", cfg=cfg_view,
                       now="2026-05-25T01:02:00Z")
    assert res.verdict == "healthy"
    conn.close()

    # 4. One hard_fail -> sweep emits kill_pending.
    rc = run(["probe-record",
              "--box-id", "b1", "--vantage", "va",
              "--check", "cover_domain_consistency",
              "--status", "hard_fail",
              "--cycle-at", "2026-05-25T01:03:00Z",
              "--db-path", str(db)])
    assert rc == 0

    wheel = ProbeAuditWheel(
        db, cfg=cfg_view, coverage_window_seconds=3600,
        probe_vantage_ttl_days=14, sweep_interval_seconds=300,
        mode="offline",
        clock=lambda: "2026-05-25T01:04:00Z",
    )
    result = wheel.run_once()
    assert "b1" in result["kill_pending"]

    # 5. ru-box-terminate --reason compromise; sweep then clears the row.
    rc = run(["ru-box-terminate", "b1", "--reason", "compromise",
              "--db-path", str(db)])
    assert rc == 0
    wheel.run_once()
    conn = connect(db)
    n = conn.execute(
        "SELECT COUNT(*) FROM obligation_clocks "
        "WHERE obligation_id='probe_kill_pending::b1'"
    ).fetchone()[0]
    assert n == 0
    # Spec H compromise reshuffle fired.
    reshuffle_rows = conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE action='shard_reshuffle' "
        "AND details_json LIKE '%\"reason\": \"compromise\"%'"
    ).fetchone()[0]
    assert reshuffle_rows == 1
    conn.close()

    # 6. Burn a vantage; subsequent probe-record refuses.
    run(["vantage-burn", "vb", "--reason", "leaked",
         "--db-path", str(db)])
    # Provision a new box so probe-record can target *something* live; the
    # test still demonstrates the refusal regardless of box state.
    conn = connect(db)
    insert_box(conn, "b2", "p", "r", "10.0.0.2", "sni-b2.example",
               "v1", "2026-05-25T02:00:00Z")
    conn.commit()
    conn.close()
    rc = run(["probe-record",
              "--box-id", "b2", "--vantage", "vb",
              "--check", "surface_scan", "--status", "pass",
              "--cycle-at", "2026-05-25T02:01:00Z",
              "--db-path", str(db)])
    assert rc == 2
