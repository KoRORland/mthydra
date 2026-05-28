"""mthydra-ops install / install-standby — one-shot EU host orchestrators.

See doc/specs/2026-05-28-N-eu-host-installer.md.
"""
from __future__ import annotations

import re

_AGE_SECRET_RE = re.compile(r"AGE-SECRET-KEY-1[0-9A-Z]+")
_BOT_TOKEN_RE = re.compile(r"\d{8,10}:[A-Za-z0-9_-]{35}")


class RedactingLog:
    """Append-only log writer that masks secret values before they hit disk.

    `secrets` maps field-name -> secret value; every occurrence of a value is
    replaced with ***REDACTED:<field>***. Two regex fallbacks catch age secret
    keys and Telegram bot tokens that may appear in subprocess output even when
    not in the known-secrets set.
    """

    def __init__(self, path, secrets: dict[str, str], echo: bool = False):
        self._fh = open(path, "a", encoding="utf-8")  # noqa: SIM115
        # value -> field, skipping empty secrets
        self._secrets = {v: k for k, v in secrets.items() if v}
        self._echo = echo

    def _redact(self, text: str) -> str:
        for value, field in self._secrets.items():
            text = text.replace(value, f"***REDACTED:{field}***")
        text = _AGE_SECRET_RE.sub("***REDACTED:age-secret***", text)
        text = _BOT_TOKEN_RE.sub("***REDACTED:bot-token***", text)
        return text

    def write(self, text: str) -> None:
        red = self._redact(text)
        self._fh.write(red)
        self._fh.flush()
        if self._echo:
            import sys

            sys.stdout.write(red)

    def close(self) -> None:
        self._fh.close()
