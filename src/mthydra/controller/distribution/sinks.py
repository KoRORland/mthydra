"""Distribution sinks — spec K §6. Separate from observability sinks.

Two production sinks plus a DryRun for offline/test. Reuses the
generic SinkResult dataclass from observability.sinks but keeps
distinct callable classes so the type system catches accidental
cross-wiring with operator-alert sinks.

stdlib only: smtplib + email.message.EmailMessage + urllib.request.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from email.message import EmailMessage

from mthydra.controller.observability.sinks import SinkResult


class TelegramDistributionSink:
    """POST sendMessage with a per-user chat_id.

    http_post(url, body_dict) -> (status_code, body_text).
    """

    def __init__(
        self,
        bot_token: str,
        http_post: Callable[[str, dict], tuple[int, str]] | None = None,
    ) -> None:
        self._bot_token = bot_token
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

    def __call__(self, *, chat_id: str, message: str) -> SinkResult:
        url = f"https://api.telegram.org/bot{self._bot_token}/sendMessage"
        body = {"chat_id": chat_id, "text": message}
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


class EmailDistributionSink:
    """SMTP+STARTTLS send via app password; per-user `to_addr`.

    smtp_factory(host, port) -> object with starttls(), login(user, pw),
    send_message(msg), quit().
    """

    def __init__(
        self,
        smtp_host: str,
        smtp_port: int,
        from_addr: str,
        username: str,
        password: str,
        smtp_factory: Callable | None = None,
    ) -> None:
        self._host = smtp_host
        self._port = smtp_port
        self._from = from_addr
        self._user = username
        self._pw = password
        self._smtp_factory = smtp_factory or self._default_smtp_factory

    @staticmethod
    def _default_smtp_factory(host: str, port: int):
        import smtplib

        return smtplib.SMTP(host, port, timeout=15)

    def __call__(self, *, to_addr: str, subject: str, body: str) -> SinkResult:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = self._from
        msg["To"] = to_addr
        msg["X-Mthydra-Distribution"] = "1"
        msg.set_content(body)
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


class DryRunDistributionSink:
    """Records every call. Always reports success."""

    def __init__(self, label: str = "dryrun") -> None:
        self._label = label
        self.calls: list[dict] = []

    def __call__(self, **kwargs) -> SinkResult:
        self.calls.append(dict(kwargs))
        return SinkResult(sink=self._label, success=True, error=None)
