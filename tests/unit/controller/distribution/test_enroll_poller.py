from __future__ import annotations

import pytest

from mthydra.controller.distribution import enrollment
from mthydra.controller.distribution.enroll_poller import EnrollmentPoller
from mthydra.controller.state.db import connect
from mthydra.controller.state.schema import apply_schema
from mthydra.controller.state.user_channels import get_channels
from mthydra.controller.state.users_shards import add_user


class FakeReceive:
    def __init__(self, batches):
        self._batches = list(batches)
    def get_updates(self, *, offset):
        return self._batches.pop(0) if self._batches else []


@pytest.fixture
def db(tmp_path):
    p = tmp_path / "s.sqlite"
    c = connect(p)
    apply_schema(c)
    add_user(c, "granny", "Granny", "phone:+0", "2026-06-03T00:00:00Z")
    c.commit()
    c.close()
    return p


def _poller(db, recv, enrolled):
    return EnrollmentPoller(
        db_path=db, receive_client=recv,
        poll_interval_seconds=30, mode="offline",
        on_enrolled=lambda uid: enrolled.append(uid),
        clock=lambda: "2026-06-03T10:30:00Z",
    )


def test_valid_start_captures_chat_id_and_triggers_delivery(db):
    c = connect(db)
    tok = enrollment.mint(c, "granny", ttl_seconds=3600, now="2026-06-03T10:00:00Z")
    c.commit(); c.close()
    enrolled = []
    recv = FakeReceive([[{"update_id": 5, "chat_id": "12345",
                          "text": f"/start {tok}"}]])
    p = _poller(db, recv, enrolled)
    p.run_once()
    c = connect(db)
    ch = get_channels(c, "granny")
    assert ch is not None and ch.telegram_chat_id == "12345"
    assert c.execute("SELECT last_offset FROM bot_offsets WHERE bot_purpose='distribution'"
                     ).fetchone()[0] == 6
    c.close()
    assert enrolled == ["granny"]


def test_unknown_token_no_capture(db):
    enrolled = []
    recv = FakeReceive([[{"update_id": 9, "chat_id": "1", "text": "/start nope"}]])
    p = _poller(db, recv, enrolled)
    p.run_once()
    c = connect(db)
    assert get_channels(c, "granny") is None
    assert c.execute("SELECT last_offset FROM bot_offsets WHERE bot_purpose='distribution'"
                     ).fetchone()[0] == 10
    c.close()
    assert enrolled == []


def test_consumed_token_replay_ignored(db):
    c = connect(db)
    tok = enrollment.mint(c, "granny", ttl_seconds=3600, now="2026-06-03T10:00:00Z")
    c.commit(); c.close()
    enrolled = []
    recv = FakeReceive([
        [{"update_id": 1, "chat_id": "12345", "text": f"/start {tok}"}],
        [{"update_id": 2, "chat_id": "99999", "text": f"/start {tok}"}],
    ])
    p = _poller(db, recv, enrolled)
    p.run_once()
    p.run_once()
    c = connect(db)
    assert get_channels(c, "granny").telegram_chat_id == "12345"  # not overwritten
    c.close()
    assert enrolled == ["granny"]


def test_preserves_existing_email_on_capture(db):
    from mthydra.controller.state.user_channels import set_channels
    c = connect(db)
    set_channels(c, "granny", telegram_chat_id=None, email_addr="g@example.com",
                 at="2026-06-03T09:00:00Z")
    tok = enrollment.mint(c, "granny", ttl_seconds=3600, now="2026-06-03T10:00:00Z")
    c.commit(); c.close()
    recv = FakeReceive([[{"update_id": 3, "chat_id": "777", "text": f"/start {tok}"}]])
    p = _poller(db, recv, [])
    p.run_once()
    c = connect(db)
    ch = get_channels(c, "granny")
    assert ch.telegram_chat_id == "777"
    assert ch.email_addr == "g@example.com"  # email preserved
    c.close()


def test_offline_mode_does_not_arm(db):
    p = _poller(db, FakeReceive([]), [])
    p.arm()
    assert p._scheduler is None


class FakeReceiveCallable(FakeReceive):
    """FakeReceive that can also send (sink-style __call__)."""
    def __init__(self, batches):
        super().__init__(batches)
        self.sent = []

    def __call__(self, *, chat_id, message):
        self.sent.append({"chat_id": chat_id, "message": message})


def test_callback_failure_notifies_user(db):
    """If delivery throws, the user must be told — not left with a silent /start."""
    c = connect(db)
    tok = enrollment.mint(c, "granny", ttl_seconds=3600, now="2026-06-03T10:00:00Z")
    c.commit(); c.close()
    recv = FakeReceiveCallable([[{"update_id": 5, "chat_id": "12345",
                                  "text": f"/start {tok}"}]])

    def boom(uid):
        raise RuntimeError("delivery exploded")

    p = EnrollmentPoller(
        db_path=db, receive_client=recv,
        poll_interval_seconds=30, mode="offline",
        on_enrolled=boom, clock=lambda: "2026-06-03T10:30:00Z")
    enrolled = p.run_once()
    assert enrolled == ["granny"]                       # enrollment still saved
    assert len(recv.sent) == 1                          # user was notified
    assert recv.sent[0]["chat_id"] == "12345"
    assert "couldn't prepare" in recv.sent[0]["message"].lower()
