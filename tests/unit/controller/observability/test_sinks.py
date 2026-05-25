"""Tests for observability.sinks — Telegram + email + dryrun."""
from __future__ import annotations

import pytest

from mthydra.controller.observability.sinks import (
    AlertPayload,
    DryRunSink,
    EmailAlertSink,
    TelegramAlertSink,
)


PAYLOAD = AlertPayload(
    severity="crit", kind="probe_kill_pending", target="b1",
    dedupe_key="probe_kill_pending::b1",
    subject="b1 needs termination",
    body="hard_fail observed from vk @ 2026-05-25T01:00:00Z",
)


# --- Telegram ---


def test_telegram_success():
    calls: list[tuple[str, dict]] = []

    def fake_post(url, body):
        calls.append((url, body))
        return 200, '{"ok":true}'

    sink = TelegramAlertSink(bot_token="t", chat_id="c", http_post=fake_post)
    res = sink(PAYLOAD)
    assert res.success
    assert res.sink == "telegram"
    assert calls and "sendMessage" in calls[0][0]
    assert calls[0][1]["chat_id"] == "c"
    assert "b1 needs termination" in calls[0][1]["text"]


def test_telegram_4xx_failure():
    def fake_post(url, body):
        return 400, '{"ok":false,"description":"bad request"}'

    sink = TelegramAlertSink(bot_token="t", chat_id="c", http_post=fake_post)
    res = sink(PAYLOAD)
    assert not res.success
    assert "http 400" in res.error


def test_telegram_network_zero_status():
    def fake_post(url, body):
        return 0, "connection refused"

    sink = TelegramAlertSink(bot_token="t", chat_id="c", http_post=fake_post)
    res = sink(PAYLOAD)
    assert not res.success


def test_telegram_raised_exception_handled():
    def fake_post(url, body):
        raise RuntimeError("boom")

    sink = TelegramAlertSink(bot_token="t", chat_id="c", http_post=fake_post)
    res = sink(PAYLOAD)
    assert not res.success
    assert "boom" in res.error


# --- Email ---


class _FakeSMTP:
    def __init__(self):
        self.starttls_called = False
        self.login_user = None
        self.login_pw = None
        self.sent_messages = []
        self.quit_called = False
        self.fail_on = None  # 'starttls' | 'login' | 'send_message' | None

    def starttls(self):
        self.starttls_called = True
        if self.fail_on == "starttls":
            raise RuntimeError("starttls fail")

    def login(self, user, pw):
        self.login_user = user
        self.login_pw = pw
        if self.fail_on == "login":
            raise RuntimeError("auth fail")

    def send_message(self, msg):
        self.sent_messages.append(msg)
        if self.fail_on == "send_message":
            raise RuntimeError("send fail")

    def quit(self):
        self.quit_called = True


def _factory(smtp):
    def _make(host, port):
        return smtp

    return _make


def test_email_success_calls_full_lifecycle():
    fake = _FakeSMTP()
    sink = EmailAlertSink(
        smtp_host="smtp.example.org", smtp_port=587,
        from_addr="ops@example.org", to_addr="op@example.org",
        username="ops@example.org", password="pw",
        smtp_factory=_factory(fake),
    )
    res = sink(PAYLOAD)
    assert res.success
    assert fake.starttls_called
    assert fake.login_user == "ops@example.org"
    assert fake.login_pw == "pw"
    assert len(fake.sent_messages) == 1
    msg = fake.sent_messages[0]
    assert msg["Subject"] == "b1 needs termination"
    assert msg["X-Mthydra-Severity"] == "crit"
    assert msg["X-Mthydra-Kind"] == "probe_kill_pending"
    assert msg["X-Mthydra-Dedupe-Key"] == "probe_kill_pending::b1"
    assert fake.quit_called


@pytest.mark.parametrize("fail_on", ["starttls", "login", "send_message"])
def test_email_failure_still_quits(fail_on):
    fake = _FakeSMTP()
    fake.fail_on = fail_on
    sink = EmailAlertSink(
        smtp_host="smtp.example.org", smtp_port=587,
        from_addr="a", to_addr="b", username="u", password="p",
        smtp_factory=_factory(fake),
    )
    res = sink(PAYLOAD)
    assert not res.success
    assert fake.quit_called


def test_email_factory_exception_handled():
    def boom_factory(host, port):
        raise OSError("connection refused")

    sink = EmailAlertSink(
        smtp_host="x", smtp_port=587, from_addr="a", to_addr="b",
        username="u", password="p", smtp_factory=boom_factory,
    )
    res = sink(PAYLOAD)
    assert not res.success
    assert "connection refused" in res.error


# --- DryRun ---


def test_dryrun_always_succeeds_and_records():
    sink = DryRunSink()
    res = sink(PAYLOAD)
    assert res.success
    assert res.sink == "dryrun"
    assert sink.calls == [PAYLOAD]


def test_dryrun_custom_label():
    sink = DryRunSink(label="dryrun-tg")
    res = sink(PAYLOAD)
    assert res.sink == "dryrun-tg"
