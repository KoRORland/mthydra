"""mthydra-ops install / install-standby — one-shot EU host orchestrators.

See doc/specs/2026-05-28-N-eu-host-installer.md.
"""
from __future__ import annotations

import configparser
import getpass
import os
import re
from dataclasses import dataclass

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


SECRET_FIELDS = {
    "b2_application_key", "obs_tg_bot_token", "obs_smtp_pass",
    "dist_tg_bot_token", "dist_smtp_pass",
}

# field name -> (ini section, ini key). Drives both parsing and prompting.
_FIELD_MAP: dict[str, tuple[str, str]] = {
    "git_url": ("install", "git_url"),
    "git_ref": ("install", "git_ref"),
    "src_dir": ("install", "src_dir"),
    "venv_dir": ("install", "venv_dir"),
    "scheduler": ("install", "scheduler"),
    "assume_sinks": ("install", "assume_sinks"),
    "hostname": ("node", "hostname"),
    "age_recipient": ("age", "recipient"),
    "b2_endpoint": ("backup", "endpoint"),
    "b2_bucket": ("backup", "bucket"),
    "b2_key_id": ("backup", "key_id"),
    "b2_application_key": ("backup", "application_key"),
    "obs_tg_bot_token": ("observability.telegram", "bot_token"),
    "obs_tg_chat_id": ("observability.telegram", "chat_id"),
    "obs_smtp_host": ("observability.email", "smtp_host"),
    "obs_smtp_port": ("observability.email", "smtp_port"),
    "obs_smtp_from": ("observability.email", "from_addr"),
    "obs_smtp_to": ("observability.email", "to_addr"),
    "obs_smtp_user": ("observability.email", "username"),
    "obs_smtp_pass": ("observability.email", "password"),
    "dist_tg_bot_token": ("distribution.telegram", "bot_token"),
    "dist_smtp_host": ("distribution.email", "smtp_host"),
    "dist_smtp_port": ("distribution.email", "smtp_port"),
    "dist_smtp_from": ("distribution.email", "from_addr"),
    "dist_smtp_user": ("distribution.email", "username"),
    "dist_smtp_pass": ("distribution.email", "password"),
}

# Sink fields required for active (and for a promoted standby — N-D6).
_SINK_FIELDS = {
    "obs_tg_bot_token", "obs_tg_chat_id", "obs_smtp_host", "obs_smtp_from",
    "obs_smtp_to", "obs_smtp_user", "obs_smtp_pass", "dist_tg_bot_token",
    "dist_smtp_host", "dist_smtp_from", "dist_smtp_user", "dist_smtp_pass",
}
# Always required regardless of role.
_BASE_REQUIRED = {
    "hostname", "age_recipient", "b2_endpoint", "b2_bucket", "b2_key_id",
    "b2_application_key",
}
# Sensible defaults so the operator need not supply them.
_DEFAULTS = {
    "git_ref": "main", "src_dir": "/opt/mthydra/src",
    "venv_dir": "/opt/mthydra/venv", "scheduler": "systemd",
    "assume_sinks": "false", "obs_smtp_port": "587", "dist_smtp_port": "587",
}


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Config:
    role: str
    promote: bool
    git_url: str
    git_ref: str
    src_dir: str
    venv_dir: str
    scheduler: str
    assume_sinks: bool
    hostname: str
    age_recipient: str
    b2_endpoint: str
    b2_bucket: str
    b2_key_id: str
    b2_application_key: str
    obs_tg_bot_token: str
    obs_tg_chat_id: str
    obs_smtp_host: str
    obs_smtp_port: int
    obs_smtp_from: str
    obs_smtp_to: str
    obs_smtp_user: str
    obs_smtp_pass: str
    dist_tg_bot_token: str
    dist_smtp_host: str
    dist_smtp_port: int
    dist_smtp_from: str
    dist_smtp_user: str
    dist_smtp_pass: str
    db_path: str = "/var/lib/mthydra/state.sqlite"
    config_path: str = "/etc/mthydra/controller.toml"
    log_dir: str = "/var/log/mthydra"

    def secret_values(self) -> dict[str, str]:
        return {f: getattr(self, f) for f in SECRET_FIELDS if getattr(self, f)}


def _required_fields(role: str, promote: bool) -> set[str]:
    req = set(_BASE_REQUIRED)
    if role == "active" or promote:
        req |= _SINK_FIELDS
    return req


def load_config(ini_path, *, role, promote, interactive=True, env=None) -> Config:
    env = os.environ if env is None else env
    parser = configparser.ConfigParser()
    parser.read(ini_path)

    raw: dict[str, str] = {}
    for fieldname, (section, key) in _FIELD_MAP.items():
        if parser.has_option(section, key):
            raw[fieldname] = parser.get(section, key).strip()
        elif fieldname in _DEFAULTS:
            raw[fieldname] = _DEFAULTS[fieldname]
        else:
            raw[fieldname] = ""
    if env.get("B2_APPLICATION_KEY"):
        raw["b2_application_key"] = env["B2_APPLICATION_KEY"]

    required = _required_fields(role, promote)
    missing = []
    for fieldname in sorted(required):
        if raw.get(fieldname):
            continue
        if interactive:
            raw[fieldname] = _prompt(fieldname)
        if not raw.get(fieldname):
            missing.append(fieldname)
    if missing:
        raise ConfigError(f"required fields missing: {', '.join(missing)}")

    _validate(raw)
    return _build_config(raw, role=role, promote=promote)


def _prompt(fieldname: str) -> str:
    label = f"  {fieldname}: "
    if fieldname in SECRET_FIELDS:
        return getpass.getpass(label).strip()
    return input(label).strip()


def _validate(raw: dict[str, str]) -> None:
    rec = raw.get("age_recipient", "")
    if rec.startswith("AGE-SECRET-KEY-"):
        raise ConfigError(
            "age.recipient is an age secret key — it must NEVER be on a host "
            "(runbook §1.2). Supply the PUBLIC recipient (age1...)."
        )
    if rec and not rec.startswith("age1"):
        raise ConfigError("age.recipient must start with 'age1'")
    for f in ("obs_smtp_from", "obs_smtp_to", "dist_smtp_from"):
        v = raw.get(f, "")
        if v and "@" not in v:
            raise ConfigError(f"{f} does not look like an email address: {v!r}")
    for f in ("obs_smtp_port", "dist_smtp_port"):
        v = raw.get(f, "")
        if v and not v.isdigit():
            raise ConfigError(f"{f} must be an integer: {v!r}")


def _build_config(raw: dict[str, str], *, role: str, promote: bool) -> Config:
    return Config(
        role=role, promote=promote,
        git_url=raw["git_url"], git_ref=raw["git_ref"], src_dir=raw["src_dir"],
        venv_dir=raw["venv_dir"], scheduler=raw["scheduler"],
        assume_sinks=raw["assume_sinks"].lower() in ("1", "true", "yes"),
        hostname=raw["hostname"], age_recipient=raw["age_recipient"],
        b2_endpoint=raw["b2_endpoint"], b2_bucket=raw["b2_bucket"],
        b2_key_id=raw["b2_key_id"], b2_application_key=raw["b2_application_key"],
        obs_tg_bot_token=raw["obs_tg_bot_token"], obs_tg_chat_id=raw["obs_tg_chat_id"],
        obs_smtp_host=raw["obs_smtp_host"],
        obs_smtp_port=int(raw["obs_smtp_port"]) if raw["obs_smtp_port"] else 587,
        obs_smtp_from=raw["obs_smtp_from"], obs_smtp_to=raw["obs_smtp_to"],
        obs_smtp_user=raw["obs_smtp_user"], obs_smtp_pass=raw["obs_smtp_pass"],
        dist_tg_bot_token=raw["dist_tg_bot_token"], dist_smtp_host=raw["dist_smtp_host"],
        dist_smtp_port=int(raw["dist_smtp_port"]) if raw["dist_smtp_port"] else 587,
        dist_smtp_from=raw["dist_smtp_from"], dist_smtp_user=raw["dist_smtp_user"],
        dist_smtp_pass=raw["dist_smtp_pass"],
    )
