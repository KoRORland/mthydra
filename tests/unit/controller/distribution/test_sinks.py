"""Tests for distribution.sinks — pluggable Telegram + email + dryrun."""
from __future__ import annotations

import pytest

from mthydra.controller.distribution.sinks import (
    DryRunDistributionSink,
    EmailDistributionSink,
    TelegramDistributionSink,
)


# --- Telegram ---


def test_telegram_success():
    calls = []

    def fake_post(url, body):
        calls.append((url, body))
        return 200, '{"ok":true}'

    sink = TelegramDistributionSink(bot_token="t", http_post=fake_post)
    res = sink(chat_id="12345", message="hello")
    assert res.success
    assert calls and "sendMessage" in calls[0][0]
    assert calls[0][1]["chat_id"] == "12345"
    assert calls[0][1]["text"] == "hello"


def test_telegram_4xx_failure():
    def fake_post(url, body):
        return 401, '{"description":"unauthorized"}'

    sink = TelegramDistributionSink(bot_token="t", http_post=fake_post)
    res = sink(chat_id="12345", message="x")
    assert not res.success
    assert "http 401" in res.error


def test_telegram_exception_handled():
    def fake_post(url, body):
        raise RuntimeError("net down")

    sink = TelegramDistributionSink(bot_token="t", http_post=fake_post)
    res = sink(chat_id="12345", message="x")
    assert not res.success
    assert "net down" in res.error


# --- Email ---


class _FakeSMTP:
    def __init__(self):
        self.starttls_called = False
        self.login_user = None
        self.sent = []
        self.quit_called = False
        self.fail_on = None
        self.starttls_context = None

    def starttls(self, context=None):
        self.starttls_called = True
        self.starttls_context = context
        if self.fail_on == "starttls":
            raise RuntimeError("tls fail")

    def login(self, user, pw):
        self.login_user = user
        if self.fail_on == "login":
            raise RuntimeError("auth fail")

    def send_message(self, msg):
        self.sent.append(msg)
        if self.fail_on == "send_message":
            raise RuntimeError("send fail")

    def quit(self):
        self.quit_called = True


def _factory(smtp):
    def _make(host, port):
        return smtp

    return _make


def test_email_success_full_lifecycle():
    fake = _FakeSMTP()
    sink = EmailDistributionSink(
        smtp_host="smtp.example.org", smtp_port=587,
        from_addr="dist@example.org",
        username="dist@example.org", password="pw",
        smtp_factory=_factory(fake),
    )
    res = sink(to_addr="alice@example.org",
               subject="new subset",
               body="payload")
    assert res.success
    assert fake.starttls_called
    # H1: STARTTLS must use a verifying context (cert + hostname checked).
    import ssl
    assert isinstance(fake.starttls_context, ssl.SSLContext)
    assert fake.starttls_context.check_hostname is True
    assert fake.starttls_context.verify_mode == ssl.CERT_REQUIRED
    assert fake.login_user == "dist@example.org"
    assert len(fake.sent) == 1
    msg = fake.sent[0]
    assert msg["Subject"] == "new subset"
    assert msg["To"] == "alice@example.org"
    assert msg["X-Mthydra-Distribution"] == "1"
    assert fake.quit_called


@pytest.mark.parametrize("fail_on", ["starttls", "login", "send_message"])
def test_email_failure_quits(fail_on):
    fake = _FakeSMTP()
    fake.fail_on = fail_on
    sink = EmailDistributionSink(
        smtp_host="x", smtp_port=587, from_addr="a", username="u", password="p",
        smtp_factory=_factory(fake),
    )
    res = sink(to_addr="b", subject="s", body="b")
    assert not res.success
    assert fake.quit_called


def test_email_factory_exception_handled():
    def boom(host, port):
        raise OSError("connection refused")

    sink = EmailDistributionSink(
        smtp_host="x", smtp_port=587, from_addr="a",
        username="u", password="p", smtp_factory=boom,
    )
    res = sink(to_addr="b", subject="s", body="b")
    assert not res.success
    assert "connection refused" in res.error


# --- DryRun ---


def test_dryrun_records_and_succeeds():
    sink = DryRunDistributionSink()
    res = sink(chat_id="12", message="hi")
    assert res.success
    assert sink.calls == [{"chat_id": "12", "message": "hi"}]


def test_dryrun_custom_label():
    sink = DryRunDistributionSink(label="dryrun-tg")
    res = sink(to_addr="x", subject="s", body="b")
    assert res.sink == "dryrun-tg"
