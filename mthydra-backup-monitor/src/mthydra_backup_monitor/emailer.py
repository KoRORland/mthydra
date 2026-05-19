"""SMTP-app-password emailer for gap alarms (spec A §8)."""
from __future__ import annotations

import smtplib
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
    with smtplib.SMTP_SSL(cfg.host, cfg.port) as smtp:
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
