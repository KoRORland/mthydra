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
        "shard_id, state, image_version, reality_uuid, created_at) "
        "VALUES (?, 'p', 'r', ?, ?, ?, 'live', 'v1', ?, ?)",
        (box_id, public_ip, f"sni-{box_id}", shard_id, f"reality-{box_id}", NOW),
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


def test_orphaned_unregistered_obligation_is_cleared(db):
    """A dist-unregistered alert for a user who is no longer assigned (deleted
    or unassigned, e.g. after manual shard cleanup) is reconciled away — it must
    not orphan forever (only iterated users get the per-user clear)."""
    conn = connect(db)
    # Stale obligation for a user that doesn't exist / isn't assigned.
    conn.execute(
        "INSERT INTO obligation_clocks (obligation_id, last_proven_at, proven_by, "
        "details, next_due_at) VALUES "
        "('dist_user_unregistered::ghost', ?, 'test', NULL, ?)",
        (NOW, NOW),
    )
    conn.commit()
    conn.close()
    pub = _pub(db, tg=DryRunDistributionSink(label="telegram"),
               em=DryRunDistributionSink(label="email"))
    pub.run_once()
    conn = connect(db)
    n = conn.execute(
        "SELECT COUNT(*) FROM obligation_clocks "
        "WHERE obligation_id='dist_user_unregistered::ghost'").fetchone()[0]
    conn.close()
    assert n == 0


def test_force_user_ids_bypasses_dedupe(db):
    """An explicit request (/start, dist-publish-now) must redeliver even when
    the subset is unchanged — dedupe is only for the background sweep."""
    conn = connect(db)
    _seed_user_with_box(conn)
    set_channels(conn, "u1", telegram_chat_id="12345",
                 email_addr="u1@example.org", at=NOW)
    conn.close()
    tg = DryRunDistributionSink(label="telegram")
    em = DryRunDistributionSink(label="email")
    pub = _pub(db, tg=tg, em=em)
    pub.run_once()                       # first delivery
    pub._clock = lambda: LATER
    res = pub.run_once(force_user_ids={"u1"})   # same subset, but forced
    assert res["dispatched"] == 2
    assert res["deduped"] == 0
    assert len(tg.calls) == 2            # redelivered, not skipped
    assert len(em.calls) == 2


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
    # Last message is rendered text (link), not JSON.
    last_msg = tg.calls[-1]["message"]
    assert not last_msg.startswith("{"), "message must be rendered text, not JSON"
    assert "https://t.me/proxy" in last_msg


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


class _TelegramFakeWithPhoto:
    """Fake telegram sink that records both text messages and send_photo calls."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.photo_calls: list[dict] = []

    def __call__(self, **kwargs) -> SinkResult:
        self.calls.append(dict(kwargs))
        return SinkResult(sink="telegram", success=True, error=None)

    def send_photo(self, *, chat_id: str, png: bytes, caption: str) -> SinkResult:
        self.photo_calls.append({"chat_id": chat_id, "png": png, "caption": caption})
        return SinkResult(sink="telegram", success=True, error=None)


def _seed_user_with_reality_box(conn, *, user_id="u2", box_id="b9",
                                 shard_id="s9", public_ip="10.9.9.1"):
    """Like _seed_user_with_box but sets reality_uuid so build_subset can form a proxy_url."""
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
        "shard_id, state, image_version, reality_uuid, created_at) "
        "VALUES (?, 'p', 'r', ?, ?, ?, 'live', 'v1', ?, ?)",
        (box_id, public_ip, f"sni-{box_id}", shard_id,
         "aaaabbbbccccdddd", NOW),
    )
    conn.execute(
        "INSERT OR IGNORE INTO onward_credentials (cred_id, box_id, credential, "
        "issued_at, authority_generation) VALUES (?, ?, ?, ?, 1)",
        (f"c-{box_id}", box_id, b"\x00\x01\x02", NOW),
    )
    conn.commit()


def test_publisher_sends_rendered_link_not_json(db):
    """Publisher must deliver rendered link text + QR photo, not raw JSON."""
    conn = connect(db)
    _seed_user_with_reality_box(conn)
    set_channels(conn, "u2", telegram_chat_id="99999", email_addr=None, at=NOW)
    conn.close()

    tg = _TelegramFakeWithPhoto()
    em = DryRunDistributionSink(label="email")
    pub = _pub(db, tg=tg, em=em)
    res = pub.run_once()

    assert res["dispatched"] == 1

    # Text message must be rendered link text, not JSON.
    assert len(tg.calls) == 1
    msg = tg.calls[0]["message"]
    assert not msg.startswith("{"), "message must not be raw JSON"
    assert "https://t.me/proxy?server=" in msg

    # One box → one QR photo sent.
    assert len(tg.photo_calls) == 1, f"expected 1 send_photo call, got {len(tg.photo_calls)}"

    # distribution_log must still store JSON for audit.
    conn = connect(db)
    row = conn.execute(
        "SELECT payload_json FROM distribution_log "
        "WHERE user_id='u2' AND channel='telegram' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()
    assert row is not None
    stored = row[0]
    assert stored.startswith("{"), "stored payload_json must be raw JSON"
    assert "proxy_url" in stored
