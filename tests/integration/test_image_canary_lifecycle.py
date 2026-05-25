"""Spec D2 integration — image canary lifecycle end-to-end.

  1. Build candidate v2 with profile JSON via image-build.
  2. Provision two canary boxes from v2 via provision-seed --canary.
  3. Without probe data: image-promote-status v2 shows failing reasons.
  4. Record probe_results: 4 cycles per canary across 2 distinct vantages, all pass.
  5. image-promote-status v2 now passes; image-promote v2 --evidence "..." succeeds.
  6. image-rollback v2 --to v1: retires v2, re-promotes v1, sets per-box pending.
"""
from __future__ import annotations

import json


def test_image_canary_full_lifecycle(tmp_path, age_recipient, monkeypatch):
    from mthydra.controller.backup.s3_dest import S3Destination
    from mthydra.controller.cli import run
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.image_profiles import pin
    from mthydra.controller.state.probe_results import record
    from mthydra.controller.state.probe_vantages import add_candidate, attest_active
    from mthydra.controller.state.ru_boxes import mark_live
    from mthydra.controller.state.ru_images import insert_candidate, promote

    db = tmp_path / "state.sqlite"
    cfg_path = tmp_path / "controller.toml"
    cfg_path.write_text(
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
        "[image.canary]\nmin_boxes=1\nmin_cycles_per_box=4\n"
    )
    run(["init", "--db-path", str(db),
         "--age-recipient", age_recipient,
         "--provider-credential", "b2=id:secret"])

    # Step 1: Pre-seed v1 (previously promoted, will be the rollback target).
    conn = connect(db)
    insert_candidate(conn, image_version="v1", upstream_release="r1",
                     upstream_repo="r", binary_url="x", manifest_url="x",
                     binary_sha256="x", binary_size_bytes=1,
                     built_at="2026-05-25T00:00:00Z")
    promote(conn, "v1", at="2026-05-25T00:01:00Z", evidence="initial")
    conn.close()

    # Step 1b: image-build v2 via the CLI flow would need network; sidestep by
    # using insert_candidate + pin_profile directly (the CLI path is exercised by
    # the unit tests; this integration test focuses on the gate + rollback dance).
    conn = connect(db)
    insert_candidate(conn, image_version="v2", upstream_release="r2",
                     upstream_repo="r", binary_url="x", manifest_url="x",
                     binary_sha256="x2", binary_size_bytes=1,
                     built_at="2026-05-25T01:00:00Z")
    pin(conn, image_version="v2", profile_json='{"surface":":443"}',
        recorded_by="operator", at="2026-05-25T01:00:01Z",
        notes="auto-pinned at image-build time")
    conn.close()

    # Step 3: Without probe data, gate fails.
    capsys_buf = []
    import sys as _sys
    import contextlib
    import io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = run(["image-promote-status", "v2", "--json",
                  "--db-path", str(db), "--config", str(cfg_path)])
    assert rc == 0
    out = json.loads(buf.getvalue())
    assert out["passed"] is False
    # Reasons should include canary cohort + cycles.
    assert any("insufficient canary boxes" in r for r in out["reasons"])

    # Step 2: Provision 2 canary boxes from v2 (via direct seeding — provision-seed
    # CLI requires B2 stubs).
    from mthydra.controller.state.ru_boxes import insert_box
    conn = connect(db)
    for box_id, ip in [("b-canary-1", "10.0.0.1"), ("b-canary-2", "10.0.0.2")]:
        insert_box(conn, box_id, "p", "r", ip, f"sni-{box_id}",
                   "v2", "2026-05-25T02:00:00Z", is_canary=True)
        mark_live(conn, box_id, public_ip=ip, at="2026-05-25T02:01:00Z")
    conn.close()

    # Step 4: Record 4 probe cycles per canary across 2 distinct vantages.
    conn = connect(db)
    add_candidate(conn, vantage_id="vk", label="kz1", source_kind="x",
                  at="2026-05-25T02:00:00Z")
    attest_active(conn, "vk", at="2026-05-25T02:00:01Z")
    add_candidate(conn, vantage_id="vb", label="by1", source_kind="x",
                  at="2026-05-25T02:00:00Z")
    attest_active(conn, "vb", at="2026-05-25T02:00:01Z")
    for box_id in ["b-canary-1", "b-canary-2"]:
        for i, vid in enumerate(["vk", "vk", "vb", "vb"]):
            record(conn, box_id=box_id, vantage_id=vid,
                   cycle_at=f"2026-05-25T0{i + 3}:00:00Z",
                   check_type="surface_scan", status="pass",
                   evidence_json=None, image_version="v2",
                   recorded_at=f"2026-05-25T0{i + 3}:00:01Z")
    conn.close()

    # Step 5: gate now passes.
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = run(["image-promote-status", "v2", "--json",
                  "--db-path", str(db), "--config", str(cfg_path)])
    assert rc == 0
    out = json.loads(buf.getvalue())
    assert out["passed"] is True
    assert out["canary_probe_rows"] == 8
    assert out["canary_distinct_vantages"] == 2

    # Step 5b: image-promote v2 succeeds.
    rc = run(["image-promote", "v2", "--evidence", "soak ok",
              "--db-path", str(db), "--config", str(cfg_path)])
    assert rc == 0
    conn = connect(db)
    state = conn.execute(
        "SELECT state FROM ru_images WHERE image_version='v2'"
    ).fetchone()[0]
    assert state == "promoted"
    # v1 retired by the promote machinery.
    state_v1 = conn.execute(
        "SELECT state FROM ru_images WHERE image_version='v1'"
    ).fetchone()[0]
    assert state_v1 == "retired"
    conn.close()

    # Step 6: rollback v2 -> v1. v1 is currently retired; rollback re-promotes it.
    # The canary boxes from v2 should be flagged for replacement.
    rc = run(["image-rollback", "v2",
              "--to", "v1",
              "--evidence", "v2 regressed under load",
              "--db-path", str(db)])
    assert rc == 0
    conn = connect(db)
    state_v1, state_v2 = (
        conn.execute(
            "SELECT state FROM ru_images WHERE image_version=?", (v,)
        ).fetchone()[0]
        for v in ("v1", "v2")
    )
    assert state_v1 == "promoted"
    assert state_v2 == "retired"
    # Both canary boxes flagged.
    pending = {
        r[0] for r in conn.execute(
            "SELECT obligation_id FROM obligation_clocks "
            "WHERE obligation_id LIKE 'image_rollback_pending::%'"
        ).fetchall()
    }
    assert pending == {
        "image_rollback_pending::b-canary-1",
        "image_rollback_pending::b-canary-2",
    }
    conn.close()
