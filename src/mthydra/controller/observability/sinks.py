"""Alert sinks — spec J §5.

Pluggable callables: AlertSink = Callable[[AlertPayload], SinkResult].
Production: TelegramAlertSink + EmailAlertSink. Tests inject fakes.
Offline mode wires DryRunSink.

stdlib only: smtplib + email.message.EmailMessage + urllib.request.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from email.message import EmailMessage


@dataclass(frozen=True)
class AlertPayload:
    severity: str             # 'info' | 'warn' | 'crit' | 'heartbeat'
    kind: str
    target: str | None
    dedupe_key: str
    subject: str
    body: str


@dataclass(frozen=True)
class SinkResult:
    sink: str                 # 'telegram' | 'email' | 'dryrun'
    success: bool
    error: str | None


class TelegramAlertSink:
    """POST sendMessage on https://api.telegram.org/bot<token>/sendMessage.

    http_post(url, body_dict) -> (status_code, body_text). Tests inject a fake.
    """

    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        http_post: Callable[[str, dict], tuple[int, str]] | None = None,
    ) -> None:
        self._bot_token = bot_token
        self._chat_id = chat_id
        self._http_post = http_post or self._default_http_post

    @staticmethod
    def _default_http_post(url: str, body: dict) -> tuple[int, str]:
        import urllib.error
        import urllib.request

        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return int(resp.status), resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            return int(e.code), e.read().decode("utf-8", errors="replace")
        except Exception as e:
            return 0, str(e)

    def __call__(self, payload: AlertPayload) -> SinkResult:
        url = f"https://api.telegram.org/bot{self._bot_token}/sendMessage"
        text = f"*{payload.subject}*\n\n{payload.body}"
        body = {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": "Markdown",
        }
        try:
            status, response_text = self._http_post(url, body)
        except Exception as e:
            return SinkResult(sink="telegram", success=False, error=repr(e))
        if 200 <= status < 300:
            return SinkResult(sink="telegram", success=True, error=None)
        return SinkResult(
            sink="telegram", success=False,
            error=f"http {status}: {response_text[:200]}",
        )


class EmailAlertSink:
    """SMTP+STARTTLS send via app password.

    smtp_factory(host, port) -> object with starttls(), login(user, pw),
    send_message(msg), quit(). Tests inject a fake.
    """

    def __init__(
        self,
        smtp_host: str,
        smtp_port: int,
        from_addr: str,
        to_addr: str,
        username: str,
        password: str,
        smtp_factory: Callable | None = None,
    ) -> None:
        self._host = smtp_host
        self._port = smtp_port
        self._from = from_addr
        self._to = to_addr
        self._user = username
        self._pw = password
        self._smtp_factory = smtp_factory or self._default_smtp_factory

    @staticmethod
    def _default_smtp_factory(host: str, port: int):
        import smtplib

        return smtplib.SMTP(host, port, timeout=15)

    def __call__(self, payload: AlertPayload) -> SinkResult:
        msg = EmailMessage()
        msg["Subject"] = payload.subject
        msg["From"] = self._from
        msg["To"] = self._to
        msg["X-Mthydra-Severity"] = payload.severity
        msg["X-Mthydra-Kind"] = payload.kind
        msg["X-Mthydra-Dedupe-Key"] = payload.dedupe_key
        msg.set_content(payload.body)
        smtp = None
        try:
            smtp = self._smtp_factory(self._host, self._port)
            smtp.starttls()
            smtp.login(self._user, self._pw)
            smtp.send_message(msg)
        except Exception as e:
            return SinkResult(sink="email", success=False, error=repr(e))
        finally:
            if smtp is not None:
                try:
                    smtp.quit()
                except Exception:
                    pass
        return SinkResult(sink="email", success=True, error=None)


class DryRunSink:
    """Records the call but never sends. Always reports success."""

    def __init__(self, label: str = "dryrun") -> None:
        self._label = label
        self.calls: list[AlertPayload] = []

    def __call__(self, payload: AlertPayload) -> SinkResult:
        self.calls.append(payload)
        return SinkResult(sink=self._label, success=True, error=None)
