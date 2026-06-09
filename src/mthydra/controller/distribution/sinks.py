"""Distribution sinks — spec K §6. Separate from observability sinks.

Two production sinks plus a DryRun for offline/test. Reuses the
generic SinkResult dataclass from observability.sinks but keeps
distinct callable classes so the type system catches accidental
cross-wiring with operator-alert sinks.

stdlib only: smtplib + email.message.EmailMessage + urllib.request.
"""
from __future__ import annotations

import contextlib
import json
import ssl
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
        http_get: Callable[[str, dict], tuple[int, str]] | None = None,
        http_post_photo: Callable[[str, dict, bytes], tuple[int, str]] | None = None,
    ) -> None:
        self._bot_token = bot_token
        self._http_post = http_post or self._default_http_post
        self._http_get = http_get or self._default_http_get
        self._http_post_photo = http_post_photo or self._default_http_post_photo

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

    @staticmethod
    def _default_http_get(url: str, params: dict) -> tuple[int, str]:
        import urllib.error
        import urllib.parse
        import urllib.request

        full = url + ("?" + urllib.parse.urlencode(params) if params else "")
        try:
            with urllib.request.urlopen(full, timeout=35) as resp:
                return int(resp.status), resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            return int(e.code), e.read().decode("utf-8", errors="replace")
        except Exception as e:
            return 0, str(e)

    @staticmethod
    def _default_http_post_photo(url: str, fields: dict, png: bytes) -> tuple[int, str]:
        import urllib.error
        import urllib.request
        import uuid as _uuid

        boundary = _uuid.uuid4().hex
        parts: list[bytes] = []
        for k, v in fields.items():
            parts.append(
                (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\""
                 f"\r\n\r\n{v}\r\n").encode())
        parts.append(
            (f"--{boundary}\r\nContent-Disposition: form-data; name=\"photo\"; "
             f"filename=\"proxy.png\"\r\nContent-Type: image/png\r\n\r\n").encode())
        parts.append(png)
        parts.append(f"\r\n--{boundary}--\r\n".encode())
        body = b"".join(parts)
        req = urllib.request.Request(
            url, data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST")
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return int(resp.status), resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            return int(e.code), e.read().decode("utf-8", errors="replace")
        except Exception as e:
            return 0, str(e)

    def send_photo(self, *, chat_id: str, png: bytes, caption: str) -> SinkResult:
        url = f"https://api.telegram.org/bot{self._bot_token}/sendPhoto"
        try:
            status, text = self._http_post_photo(
                url, {"chat_id": chat_id, "caption": caption}, png)
        except Exception as e:
            return SinkResult(sink="telegram", success=False, error=repr(e))
        if 200 <= status < 300:
            return SinkResult(sink="telegram", success=True, error=None)
        return SinkResult(sink="telegram", success=False,
                          error=f"http {status}: {text[:200]}")

    def get_me(self) -> str | None:
        url = f"https://api.telegram.org/bot{self._bot_token}/getMe"
        status, text = self._http_get(url, {})
        if not (200 <= status < 300):
            return None
        data = json.loads(text)
        if not data.get("ok"):
            return None
        return data["result"].get("username")

    def get_updates(self, *, offset: int) -> list[dict]:
        """Return normalised message updates: {update_id, chat_id, text}.

        Only plain `message` updates are returned (edited/callbacks ignored).
        """
        url = f"https://api.telegram.org/bot{self._bot_token}/getUpdates"
        status, text = self._http_get(url, {"offset": offset, "timeout": 0})
        if not (200 <= status < 300):
            return []
        data = json.loads(text)
        if not data.get("ok"):
            return []
        out: list[dict] = []
        for u in data.get("result", []):
            msg = u.get("message")
            if not msg:
                continue
            chat = msg.get("chat", {})
            out.append({
                "update_id": u["update_id"],
                "chat_id": str(chat.get("id")),
                "text": msg.get("text", ""),
            })
        return out

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
            # Explicit verifying context: starttls(context=None) uses an
            # unverified stdlib context (no cert/hostname check), exposing the
            # app password to active MITM.
            smtp.starttls(context=ssl.create_default_context())
            smtp.login(self._user, self._pw)
            smtp.send_message(msg)
        except Exception as e:
            return SinkResult(sink="email", success=False, error=repr(e))
        finally:
            if smtp is not None:
                with contextlib.suppress(Exception):
                    smtp.quit()
        return SinkResult(sink="email", success=True, error=None)


class DryRunDistributionSink:
    """Records every call. Always reports success."""

    def __init__(self, label: str = "dryrun") -> None:
        self._label = label
        self.calls: list[dict] = []
        self.photo_calls: list[dict] = []

    def __call__(self, **kwargs) -> SinkResult:
        self.calls.append(dict(kwargs))
        return SinkResult(sink=self._label, success=True, error=None)

    def send_photo(self, *, chat_id: str, png: bytes, caption: str) -> SinkResult:
        self.photo_calls.append({"chat_id": chat_id, "png": png, "caption": caption})
        return SinkResult(sink=self._label, success=True, error=None)
