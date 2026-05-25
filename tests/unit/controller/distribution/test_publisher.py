"""Tests for distribution.publisher — delta-only per-user dispatch."""
from __future__ import annotations

import json

import pytest

from mthydra.controller.distribution.publisher import DistributionPublisher
from mthydra.controller.distribution.sinks import DryRunDistributionSink
from mthydra.controller.observability.sinks import SinkResult
from mthydra.controller.state.db import connect
from mthydra.controller.state.schema import apply_schema
from mthydra.controller.state.user_channels import set_channels


NOW = "2026-05-25T12:00:00Z"
LATER = "2026-05-25T13:00:00Z"


def _seed_user_with_box(conn, *, user_id="u1", box_id="b1",
                         shard_id="s1", public_ip="10.0.0.1"):
    import json as _json
    conn.execute(
        "INSERT OR IGNORE INTO credential_authority (generation, privkey_pem, "
        "pubkey_pem, created_at) VALUES (1, 'priv', 'pub', ?)",
        (NOW,),
    )
    conn.execute(
        "INSERT OR IGNORE INTO ru_images (image_version, upstream_release, upstream_repo, "
        "binary_url, manifest_url, binary_sha256, binary_size_bytes, state, built_at) "
        "VALUES ('v1', 'r', 'r', 'u', 'm', 'sha', 1, 'candidate', ?)",
        (NOW,),
    )
    conn.execute(
        "INSERT OR IGNORE INTO shards (shard_id, members_json, target_size, "
        "last_reshuffled_at, created_at) VALUES (?, ?, 2, ?, ?)",
        (shard_id, _json.dumps([user_id]), NOW, NOW),
    )
    conn.execute(
        "INSERT OR IGNORE INTO users (user_id, display_name, out_of_band_channel, "
        "current_shard_id, added_at) "
        "VALUES (?, NULL, 'email', ?, ?)",
        (user_id, shard_id, NOW),
    )
    conn.execute(
        "INSERT OR IGNORE INTO ru_boxes (box_id, provider, region, public_ip, sni, "
        "shard_id, state, image_version, created_at) "
        "VALUES (?, 'p', 'r', ?, ?, ?, 'live', 'v1', ?)",
        (box_id, public_ip, f"sni-{box_id}", shard_id, NOW),
    )
    conn.execute(
        "INSERT OR IGNORE INTO onward_credentials (cred_id, box_id, credential, "
        "issued_at, authority_generation) VALUES (?, ?, ?, ?, 1)",
        (f"c-{box_id}", box_id, b"\x00\x01\x02", NOW),
    )
    conn.commit()


@pytest.fixture
def db(tmp_path):
    p = tmp_path / "state.sqlite"
    c = connect(p)
    apply_schema(c)
    c.close()
    return p


def _pub(db, tg, em, clock=NOW, mode="production"):
    return DistributionPublisher(
        db_path=db,
        telegram_sink=tg, email_sink=em,
        sweep_interval_seconds=300,
        mode=mode, clock=lambda: clock,
    )


def test_first_tick_dispatches_to_both_channels(db):
    conn = connect(db)
    _seed_user_with_box(conn)
    set_channels(conn, "u1", telegram_chat_id="12345",
                 email_addr="u1@example.org", at=NOW)
    conn.close()
    tg = DryRunDistributionSink(label="telegram")
    em = DryRunDistributionSink(label="email")
    pub = _pub(db, tg=tg, em=em)
    res = pub.run_once()
    assert res["dispatched"] == 2
    assert res["deduped"] == 0
    assert len(tg.calls) == 1
    assert tg.calls[0]["chat_id"] == "12345"
    assert len(em.calls) == 1
    assert em.calls[0]["to_addr"] == "u1@example.org"


def test_second_tick_deduped_same_subset(db):
    conn = connect(db)
    _seed_user_with_box(conn)
    set_channels(conn, "u1", telegram_chat_id="12345",
                 email_addr="u1@example.org", at=NOW)
    conn.close()
    tg = DryRunDistributionSink(label="telegram")
    em = DryRunDistributionSink(label="email")
    pub = _pub(db, tg=tg, em=em)
    pub.run_once()
    pub._clock = lambda: LATER
    res = pub.run_once()
    assert res["dispatched"] == 0
    assert res["deduped"] == 2
    assert len(tg.calls) == 1
    assert len(em.calls) == 1


def test_changed_subset_re_dispatches(db):
    conn = connect(db)
    _seed_user_with_box(conn, box_id="b1")
    set_channels(conn, "u1", telegram_chat_id="12345",
                 email_addr=None, at=NOW)
    conn.close()
    tg = DryRunDistributionSink(label="telegram")
    em = DryRunDistributionSink(label="email")
    pub = _pub(db, tg=tg, em=em)
    pub.run_once()
    # Change the subset: terminate b1; add b2 to the same shard.
    conn = connect(db)
    conn.execute("UPDATE ru_boxes SET state='terminated', "
                 "terminated_at=? WHERE box_id='b1'", (LATER,))
    _seed_user_with_box(conn, user_id="u1", box_id="b2",
                       shard_id="s1", public_ip="10.0.0.2")
    conn.close()
    pub._clock = lambda: LATER
    res = pub.run_once()
    assert res["dispatched"] == 1
    assert len(tg.calls) == 2
    # Last payload mentions b2.
    last_payload = json.loads(tg.calls[-1]["message"])
    box_ids = [b["box_id"] for b in last_payload["boxes"]]
    assert "b2" in box_ids


def test_unassigned_user_skipped(db):
    conn = connect(db)
    conn.execute(
        "INSERT INTO users (user_id, display_name, out_of_band_channel, added_at) "
        "VALUES ('u1', NULL, 'email', ?)", (NOW,),
    )
    conn.commit()
    conn.close()
    pub = _pub(
        db,
        tg=DryRunDistributionSink(label="telegram"),
        em=DryRunDistributionSink(label="email"),
    )
    res = pub.run_once()
    assert res["dispatched"] == 0
    conn = connect(db)
    n = conn.execute("SELECT COUNT(*) FROM distribution_log").fetchone()[0]
    assert n == 0
    conn.close()


def test_assigned_user_with_no_channels_emits_unregistered_anti(db):
    conn = connect(db)
    _seed_user_with_box(conn)
    # NO set_channels call.
    conn.close()
    pub = _pub(
        db,
        tg=DryRunDistributionSink(label="telegram"),
        em=DryRunDistributionSink(label="email"),
    )
    res = pub.run_once()
    assert res["unregistered"] == 1
    conn = connect(db)
    row = conn.execute(
        "SELECT obligation_id FROM obligation_clocks "
        "WHERE obligation_id='dist_user_unregistered::u1'"
    ).fetchone()
    assert row is not None
    conn.close()


def test_unregistered_anti_clears_after_channels_set(db):
    conn = connect(db)
    _seed_user_with_box(conn)
    conn.close()
    pub = _pub(
        db,
        tg=DryRunDistributionSink(label="telegram"),
        em=DryRunDistributionSink(label="email"),
    )
    pub.run_once()  # emits unregistered
    conn = connect(db)
    set_channels(conn, "u1", telegram_chat_id="t", email_addr=None, at=NOW)
    conn.close()
    pub._clock = lambda: LATER
    pub.run_once()
    conn = connect(db)
    n = conn.execute(
        "SELECT COUNT(*) FROM obligation_clocks "
        "WHERE obligation_id='dist_user_unregistered::u1'"
    ).fetchone()[0]
    assert n == 0
    conn.close()


def test_sink_failure_recorded_in_log(db):
    conn = connect(db)
    _seed_user_with_box(conn)
    set_channels(conn, "u1", telegram_chat_id="12345",
                 email_addr=None, at=NOW)
    conn.close()

    class _FailingSink:
        def __call__(self, **kwargs):
            return SinkResult(sink="telegram", success=False, error="http 401")

    pub = _pub(
        db,
        tg=_FailingSink(),
        em=DryRunDistributionSink(label="email"),
    )
    res = pub.run_once()
    assert res["dispatched"] == 0
    conn = connect(db)
    row = conn.execute(
        "SELECT delivered_at, error FROM distribution_log "
        "WHERE user_id='u1' AND channel='telegram' "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row[0] is None
    assert "http 401" in row[1]
    conn.close()


def test_heartbeat_obligation_proven_each_tick(db):
    pub = _pub(
        db,
        tg=DryRunDistributionSink(label="telegram"),
        em=DryRunDistributionSink(label="email"),
    )
    pub.run_once()
    conn = connect(db)
    row = conn.execute(
        "SELECT last_proven_at FROM obligation_clocks "
        "WHERE obligation_id='dist_publish_sweep_ran'"
    ).fetchone()
    assert row[0] == NOW
    conn.close()


def test_offline_mode_uses_offline_sink(db):
    conn = connect(db)
    _seed_user_with_box(conn)
    set_channels(conn, "u1", telegram_chat_id="12345",
                 email_addr="u1@example.org", at=NOW)
    conn.close()
    pub = _pub(
        db,
        tg=DryRunDistributionSink(label="telegram"),
        em=DryRunDistributionSink(label="email"),
        mode="offline",
    )
    res = pub.run_once()
    assert res["dispatched"] == 2
    conn = connect(db)
    rows = conn.execute(
        "SELECT channel FROM distribution_log ORDER BY id"
    ).fetchall()
    assert {r[0] for r in rows} == {"telegram", "email"}
    conn.close()


def test_arm_and_disarm_production(db):
    pub = DistributionPublisher(
        db_path=db,
        telegram_sink=DryRunDistributionSink(label="telegram"),
        email_sink=DryRunDistributionSink(label="email"),
        sweep_interval_seconds=86400, mode="production",
        clock=lambda: NOW,
    )
    pub.arm()
    assert pub._scheduler is not None
    pub.disarm()
    assert pub._scheduler is None


def test_offline_mode_does_not_arm(db):
    pub = DistributionPublisher(
        db_path=db,
        telegram_sink=DryRunDistributionSink(label="telegram"),
        email_sink=DryRunDistributionSink(label="email"),
        sweep_interval_seconds=300, mode="offline",
        clock=lambda: NOW,
    )
    pub.arm()
    assert pub._scheduler is None
    pub.disarm()
