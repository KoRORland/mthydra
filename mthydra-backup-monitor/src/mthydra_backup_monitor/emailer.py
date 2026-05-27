"""SMTP-app-password emailer for gap alarms (spec A §8)."""
from __future__ import annotations

import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage


@dataclass(frozen=True)
class EmailConfig:
    host: str
    port: int
    username: str
    app_password: str
    from_addr: str
    to_addr: str


def _send(cfg: EmailConfig, subject: str, body: str) -> None:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg.from_addr
    msg["To"] = cfg.to_addr
    msg.set_content(body)
    # Explicit verifying context: SMTP_SSL(context=None) falls back to
    # ssl._create_stdlib_context() which does NOT validate the cert or
    # hostname, leaving SMTP AUTH (the app password) open to active MITM.
    with smtplib.SMTP_SSL(cfg.host, cfg.port, context=ssl.create_default_context()) as smtp:
        smtp.login(cfg.username, cfg.app_password)
        smtp.send_message(msg)


def send_gap_alarm(cfg: EmailConfig, highest_gen: int, stuck_since: str, now_iso: str) -> None:
    """Send a single gap-alarm email."""
    subject = f"mthydra: backup gap (highest_gen={highest_gen} stuck since {stuck_since})"
    body = (
        f"Backup generation has not advanced.\n\n"
        f"Highest generation observed: {highest_gen}\n"
        f"Stuck since:                 {stuck_since}\n"
        f"Now:                         {now_iso}\n\n"
        f"Investigate per T2 runbook. "
        f"If the active controller is dead, promote the warm standby.\n"
    )
    _send(cfg, subject, body)
