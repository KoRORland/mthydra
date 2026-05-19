"""Tests for the SMTP gap-alarm emailer."""
from unittest.mock import MagicMock, patch

from mthydra_backup_monitor.emailer import EmailConfig, send_gap_alarm


def test_send_gap_alarm_uses_smtp_ssl():
    cfg = EmailConfig(
        host="smtp.example.com",
        port=465,
        username="alerter@example.com",
        app_password="apppw",
        from_addr="alerter@example.com",
        to_addr="op@example.org",
    )
    with patch("smtplib.SMTP_SSL") as smtp_cls:
        instance = MagicMock()
        smtp_cls.return_value.__enter__.return_value = instance
        send_gap_alarm(
            cfg,
            highest_gen=7,
            stuck_since="2026-05-18T01:00:00Z",
            now_iso="2026-05-20T02:00:00Z",
        )
    instance.login.assert_called_once_with("alerter@example.com", "apppw")
    instance.send_message.assert_called_once()
    msg = instance.send_message.call_args[0][0]
    assert "highest_gen=7" in msg["Subject"]
    assert "stuck since 2026-05-18T01:00:00Z" in msg["Subject"]
    body = msg.get_content()
    assert "2026-05-18T01:00:00Z" in body
