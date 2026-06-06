from __future__ import annotations

from mthydra.controller.data_exit.exit_observer import EuExitObserver
from mthydra.controller.state.db import connect
from mthydra.controller.state.schema import apply_schema


def _db(tmp_path):
    c = connect(tmp_path / "s.sqlite")
    apply_schema(c)
    return c


def _add_live_box(c, box_id):
    c.execute(
        "INSERT INTO ru_boxes (box_id, provider, region, sni, state, "
        "image_version, created_at, reality_uuid) "
        "VALUES (?, 'prov', 'reg', ?, 'live', 'img1', "
        "'2026-06-01T00:00:00Z', ?)",
        (box_id, f"sni-{box_id}", f"uuid-{box_id}"),
    )
    c.commit()


def _has_unseen(c, box_id):
    return c.execute(
        "SELECT COUNT(*) FROM obligation_clocks WHERE obligation_id=?",
        (f"box_eu_tunnel_unseen::{box_id}",),
    ).fetchone()[0] == 1


def _obs(tmp_path, sessions, *, now, threshold=900):
    return EuExitObserver(
        db_path=tmp_path / "s.sqlite",
        clash_api_url="http://127.0.0.1:9090",
        poll_fn=lambda url, timeout=5.0: set(sessions),
        clock=lambda: now,
        unseen_threshold_seconds=threshold,
        mode="offline",
    )


def test_seen_box_is_recorded_and_not_flagged(tmp_path):
    c = _db(tmp_path); _add_live_box(c, "box-1"); c.close()
    _obs(tmp_path, {"box-1"}, now="2026-06-06T10:00:00Z").tick()
    c = connect(tmp_path / "s.sqlite")
    assert not _has_unseen(c, "box-1")


def test_never_seen_live_box_is_flagged(tmp_path):
    c = _db(tmp_path); _add_live_box(c, "box-1"); c.close()
    _obs(tmp_path, set(), now="2026-06-06T10:00:00Z").tick()
    c = connect(tmp_path / "s.sqlite")
    assert _has_unseen(c, "box-1")


def test_stale_then_seen_clears_flag(tmp_path):
    c = _db(tmp_path); _add_live_box(c, "box-1"); c.close()
    _obs(tmp_path, set(), now="2026-06-06T10:00:00Z").tick()
    c = connect(tmp_path / "s.sqlite"); assert _has_unseen(c, "box-1"); c.close()
    _obs(tmp_path, {"box-1"}, now="2026-06-06T10:10:00Z").tick()
    c = connect(tmp_path / "s.sqlite"); assert not _has_unseen(c, "box-1")


def test_stale_last_seen_beyond_threshold_is_flagged(tmp_path):
    c = _db(tmp_path); _add_live_box(c, "box-1"); c.close()
    # seen at 10:00
    _obs(tmp_path, {"box-1"}, now="2026-06-06T10:00:00Z").tick()
    # next tick 30 min later with a 15-min threshold, exit sees nobody -> stale
    _obs(tmp_path, set(), now="2026-06-06T10:30:00Z", threshold=900).tick()
    c = connect(tmp_path / "s.sqlite")
    assert _has_unseen(c, "box-1")


def test_poll_error_does_not_flag_recently_seen_box(tmp_path):
    c = _db(tmp_path); _add_live_box(c, "box-1"); c.close()
    _obs(tmp_path, {"box-1"}, now="2026-06-06T10:00:00Z").tick()

    def boom(url, timeout=5.0):
        raise OSError("connection refused")

    EuExitObserver(
        db_path=tmp_path / "s.sqlite", clash_api_url="http://127.0.0.1:9090",
        poll_fn=boom, clock=lambda: "2026-06-06T10:05:00Z",
        unseen_threshold_seconds=900, mode="offline").tick()  # must not raise
    c = connect(tmp_path / "s.sqlite")
    assert not _has_unseen(c, "box-1")  # still fresh, not flagged


def test_terminated_box_not_flagged(tmp_path):
    c = _db(tmp_path)
    c.execute(
        "INSERT INTO ru_boxes (box_id, provider, region, sni, state, "
        "image_version, created_at, reality_uuid) VALUES "
        "('box-dead', 'p', 'r', 'sni-dead', 'terminated', 'img1', "
        "'2026-06-01T00:00:00Z', 'uuid-dead')")
    c.commit(); c.close()
    _obs(tmp_path, set(), now="2026-06-06T10:00:00Z").tick()
    c = connect(tmp_path / "s.sqlite")
    assert not _has_unseen(c, "box-dead")
