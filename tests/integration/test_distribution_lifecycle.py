"""Spec K integration — end-to-end publisher with fake sinks.

  1. Bootstrap; add user; create shard; provision box; mark live; register channels.
  2. DistributionPublisher.run_once() → both sinks delivered, log rows appended.
  3. Re-run → deduped (no new rows).
  4. Spec H compromise-reshuffle (via terminate) changes the shard's box set →
     next sweep re-dispatches with a new hash.
"""
from __future__ import annotations

import json

import pytest


from mthydra.controller.distribution.publisher import DistributionPublisher
from mthydra.controller.distribution.sinks import DryRunDistributionSink


NOW = "2026-05-25T12:00:00Z"
LATER = "2026-05-25T13:00:00Z"


def _bootstrap_with_user_and_box(tmp_path, age_recipient):
    """Bootstrap controller; one user assigned to a shard with one live box."""
    from mthydra.controller.cli import run
    from mthydra.controller.state.credentials import issue_credential
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.ru_boxes import insert_box, mark_live
    from mthydra.controller.state.shards import assign_box_to_shard
    from mthydra.controller.state.user_channels import set_channels

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
    run(["user-add", "u1", "--out-of-band-channel", "email", "--db-path", str(db)])
    run(["shard-create", "s1", "--members", "u1",
         "--db-path", str(db), "--config", str(cfg)])

    conn = connect(db)
    conn.execute(
        "INSERT INTO ru_images (image_version, upstream_release, upstream_repo, "
        "binary_url, manifest_url, binary_sha256, binary_size_bytes, state, built_at) "
        "VALUES ('v1', 'r', 'r', 'u', 'm', 'sha', 1, 'candidate', ?)",
        (NOW,),
    )
    insert_box(conn, "b1", "p", "r", "10.0.0.1", "sni-b1.example",
               "v1", NOW)
    assign_box_to_shard(conn, box_id="b1", shard_id="s1", at=NOW)
    mark_live(conn, "b1", public_ip="10.0.0.1", at=NOW)
    issue_credential(conn, "b1", b"\x00\x01\x02", NOW, authority_generation=1)
    set_channels(conn, "u1", telegram_chat_id="12345",
                 email_addr="u1@example.org", at=NOW)
    conn.commit()
    conn.close()
    return db


def test_distribution_lifecycle_first_publish_then_dedupe(tmp_path, age_recipient):
    db = _bootstrap_with_user_and_box(tmp_path, age_recipient)
    tg = DryRunDistributionSink(label="telegram")
    em = DryRunDistributionSink(label="email")
    pub = DistributionPublisher(
        db_path=db,
        telegram_sink=tg, email_sink=em,
        sweep_interval_seconds=300,
        mode="production", clock=lambda: NOW,
    )
    res = pub.run_once()
    assert res["dispatched"] == 2
    assert len(tg.calls) == 1
    assert len(em.calls) == 1
    # Payload contains the live box.
    body = json.loads(tg.calls[0]["message"])
    assert body["user_id"] == "u1"
    assert [b["box_id"] for b in body["boxes"]] == ["b1"]

    # Re-run — same subset, deduped on both channels.
    pub._clock = lambda: LATER
    res2 = pub.run_once()
    assert res2["dispatched"] == 0
    assert res2["deduped"] == 2
    assert len(tg.calls) == 1
    assert len(em.calls) == 1


def test_distribution_lifecycle_compromise_reshuffles_then_republishes(tmp_path, age_recipient):
    """After ru-box-terminate --reason=compromise, the user's shard changes ->
    next publisher sweep delivers a new delta."""
    from mthydra.controller.cli import run
    from mthydra.controller.state.db import connect

    db = _bootstrap_with_user_and_box(tmp_path, age_recipient)
    # Seed cover_domain_pool so terminate's mark_burned can find the SNI.
    conn = connect(db)
    conn.execute(
        "INSERT INTO cover_domain_pool (domain, state, last_verified_at, "
        "verified_from_vantage, assigned_box_id, added_at, entered_in_use_at) "
        "VALUES ('sni-b1.example', 'in_use', ?, 'op', 'b1', ?, ?)",
        (NOW, NOW, NOW),
    )
    conn.commit()
    conn.close()

    tg = DryRunDistributionSink(label="telegram")
    em = DryRunDistributionSink(label="email")
    pub = DistributionPublisher(
        db_path=db,
        telegram_sink=tg, email_sink=em,
        sweep_interval_seconds=300,
        mode="production", clock=lambda: NOW,
    )
    pub.run_once()
    first_hash = json.loads(tg.calls[0]["message"])["subset_hash"]

    # Compromise terminate -> spec H reshuffles u1 to a new shard.
    rc = run(["ru-box-terminate", "b1", "--reason", "compromise",
              "--db-path", str(db)])
    assert rc == 0

    # New shard has no live box yet -> subset hash differs (empty box list now).
    pub._clock = lambda: LATER
    res = pub.run_once()
    assert res["dispatched"] >= 1
    new_payload = json.loads(tg.calls[-1]["message"])
    assert new_payload["subset_hash"] != first_hash
    assert new_payload["boxes"] == []


def test_distribution_lifecycle_unassigned_user_no_publish(tmp_path, age_recipient):
    """Unassigned user -> no publishing, no log rows, no anti-obligation."""
    from mthydra.controller.cli import run
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.user_channels import set_channels

    db = tmp_path / "state.sqlite"
    run([
        "init", "--db-path", str(db),
        "--age-recipient", age_recipient,
        "--provider-credential", "b2=id:secret",
    ])
    run(["user-add", "u1", "--out-of-band-channel", "email", "--db-path", str(db)])
    # u1 is registered but has no shard.
    conn = connect(db)
    set_channels(conn, "u1", telegram_chat_id="12345",
                 email_addr=None, at=NOW)
    conn.close()

    tg = DryRunDistributionSink(label="telegram")
    em = DryRunDistributionSink(label="email")
    pub = DistributionPublisher(
        db_path=db,
        telegram_sink=tg, email_sink=em,
        sweep_interval_seconds=300,
        mode="production", clock=lambda: NOW,
    )
    res = pub.run_once()
    assert res["dispatched"] == 0
    assert res["unregistered"] == 0
    assert tg.calls == []
    conn = connect(db)
    n = conn.execute("SELECT COUNT(*) FROM distribution_log").fetchone()[0]
    assert n == 0
    conn.close()
