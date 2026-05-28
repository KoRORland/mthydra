# EU Host Installer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace runbook §1 (and the §10.2 promotion path) with two idempotent one-shot orchestrators — `mthydra-ops install` (first EU active node) and `mthydra-ops install-standby [--promote]` (warm substitute) — that bring a naked Ubuntu 24.04 root shell to a running, self-monitoring controller.

**Architecture:** A dumb POSIX `scripts/install.sh` does apt prereqs + `git clone` + venv build, then `exec`s a tested Python orchestrator (`src/mthydra/ops/install.py`). The orchestrator is a list of idempotent **phases** — each probes the live system (`is_satisfied`) and skips or completes only the missing piece. Existing `mthydra-ops` step logic (`setup_host`, `bootstrap`, `preflight`) is refactored into injectable **cores** that both the standalone subcommands and the install phases call, so secrets stay off `argv` (passed via child `env`) and the install log is redacted.

**Tech Stack:** Python 3.12 stdlib (`argparse`, `configparser`, `dataclasses`, `getpass`, `subprocess`, `re`), pytest, systemd timers, POSIX sh, `shellcheck`.

**Spec:** `doc/specs/2026-05-28-N-eu-host-installer.md` (decisions N-D1…N-D10).

**Naming contract (used across all tasks — keep consistent):**
- Module: `src/mthydra/ops/install.py`. Tests: `tests/unit/ops/test_install.py` (split per-area is fine).
- `Config` frozen dataclass fields: `role, promote, git_url, git_ref, src_dir, venv_dir, scheduler, assume_sinks, hostname, age_recipient, b2_endpoint, b2_bucket, b2_key_id, b2_application_key, obs_tg_bot_token, obs_tg_chat_id, obs_smtp_host, obs_smtp_port, obs_smtp_from, obs_smtp_to, obs_smtp_user, obs_smtp_pass, dist_tg_bot_token, dist_smtp_host, dist_smtp_port, dist_smtp_from, dist_smtp_user, dist_smtp_pass, db_path, config_path, log_dir`.
- `SECRET_FIELDS = {"b2_application_key", "obs_tg_bot_token", "obs_smtp_pass", "dist_tg_bot_token", "dist_smtp_pass"}`.
- `Phase(name: str, is_satisfied: Callable[[Ctx], bool], run: Callable[[Ctx], None])`.
- `Ctx` attrs: `config, log, dry_run, quiet`; methods `say(msg)`, `err(msg)`, `run_controller(*args, env=None, capture=False)`.
- Probe names: `host_prepared, controller_installed, db_initialized, authority_is_real, controller_toml_present, age_recipient_file_present, service_active, descriptor_signed, timer_enabled(name)`. (No `standby-heartbeat-check` CLI exists in this build — standby readiness uses `startup-check`; see Task 9.)
- Refactored cores in `main.py`: `setup_host_core(run_step, *, dry_run)`, `bootstrap_core(run, say, *, db_path, config_path, age_recipient, b2_application_key, hostname, role, **toml_fields)`, `preflight_core(run, say, *, db_path, config_path)`.

---

## Task 1: `RedactingLog` — secret-safe install log

**Files:**
- Create: `src/mthydra/ops/install.py`
- Test: `tests/unit/ops/test_install.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/ops/test_install.py
from __future__ import annotations

from mthydra.ops import install


def test_redacting_log_masks_known_secret_value(tmp_path):
    log_path = tmp_path / "install.log"
    log = install.RedactingLog(
        log_path, secrets={"b2_application_key": "K00super-secret-key"}
    )
    log.write("running with key K00super-secret-key now\n")
    log.close()
    text = log_path.read_text()
    assert "K00super-secret-key" not in text
    assert "***REDACTED:b2_application_key***" in text


def test_redacting_log_masks_age_secret_and_bot_token(tmp_path):
    log = install.RedactingLog(tmp_path / "l.log", secrets={})
    log.write("AGE-SECRET-KEY-1QQPQYU8H4ENHEER9CA2W7XX7XXXXXXXXXXXXXXXXXXXXXXXXXSPLAB\n")
    log.write("token 123456789:ABCdefGHIjklMNOpqrSTUvwxYZ0123456789\n")
    log.close()
    text = (tmp_path / "l.log").read_text()
    assert "AGE-SECRET-KEY-1" not in text
    assert "123456789:ABC" not in text
    assert text.count("***REDACTED:") == 2


def test_redacting_log_passes_non_secret_text(tmp_path):
    log = install.RedactingLog(tmp_path / "l.log", secrets={"x": "sekret"})
    log.write("hello world\n")
    log.close()
    assert (tmp_path / "l.log").read_text() == "hello world\n"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/ops/test_install.py -k redacting -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mthydra.ops.install'`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/mthydra/ops/install.py
"""mthydra-ops install / install-standby — one-shot EU host orchestrators.

See doc/specs/2026-05-28-N-eu-host-installer.md.
"""
from __future__ import annotations

import re
from pathlib import Path

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
        self._fh = open(path, "a", encoding="utf-8")
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/ops/test_install.py -k redacting -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add src/mthydra/ops/install.py tests/unit/ops/test_install.py
git commit -m "feat(install): RedactingLog — secret-safe install log (spec N-D8)"
```

---

## Task 2: `Config` dataclass + ini parsing (no prompting yet)

**Files:**
- Modify: `src/mthydra/ops/install.py`
- Test: `tests/unit/ops/test_install.py`

- [ ] **Step 1: Write the failing test**

```python
# add to tests/unit/ops/test_install.py
import textwrap


def _write_ini(tmp_path, body: str):
    p = tmp_path / "install.ini"
    p.write_text(textwrap.dedent(body))
    return p


_FULL_INI = """\
    [install]
    git_url = https://example/mthydra.git
    git_ref = v1.0.0
    [node]
    hostname = eu1.example.com
    [age]
    recipient = age1qqp0000000000000000000000000000000000000000000000000q
    [backup]
    endpoint = https://s3.example.com
    bucket = mthydra-prod
    key_id = 0012abc
    application_key = B2SECRET
    [observability.telegram]
    bot_token = 111:AAA
    chat_id = 9999
    [observability.email]
    smtp_host = smtp.example.com
    smtp_port = 587
    from_addr = alerts@example.com
    to_addr = op@example.com
    username = alerts@example.com
    password = OBSPASS
    [distribution.telegram]
    bot_token = 222:BBB
    [distribution.email]
    smtp_host = smtp.example.com
    smtp_port = 587
    from_addr = dist@example.com
    username = dist@example.com
    password = DISTPASS
    """


def test_load_config_parses_full_ini(tmp_path):
    ini = _write_ini(tmp_path, _FULL_INI)
    cfg = install.load_config(ini, role="active", promote=False,
                              interactive=False, env={})
    assert cfg.hostname == "eu1.example.com"
    assert cfg.b2_application_key == "B2SECRET"
    assert cfg.obs_smtp_port == 587
    assert cfg.git_ref == "v1.0.0"


def test_b2_application_key_env_wins_over_ini(tmp_path):
    ini = _write_ini(tmp_path, _FULL_INI)
    cfg = install.load_config(ini, role="active", promote=False,
                              interactive=False,
                              env={"B2_APPLICATION_KEY": "FROM_ENV"})
    assert cfg.b2_application_key == "FROM_ENV"


def test_secret_values_returns_only_nonempty_secrets(tmp_path):
    ini = _write_ini(tmp_path, _FULL_INI)
    cfg = install.load_config(ini, role="active", promote=False,
                              interactive=False, env={})
    sv = cfg.secret_values()
    assert sv["b2_application_key"] == "B2SECRET"
    assert sv["obs_smtp_pass"] == "OBSPASS"
    assert set(sv) == install.SECRET_FIELDS
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/ops/test_install.py -k "config or secret_values or env_wins" -v`
Expected: FAIL — `AttributeError: module 'mthydra.ops.install' has no attribute 'load_config'`.

- [ ] **Step 3: Write minimal implementation**

```python
# add to src/mthydra/ops/install.py
import configparser
import os
from dataclasses import dataclass, field

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

    # env wins for the B2 application key (kept off ini/argv when possible).
    if env.get("B2_APPLICATION_KEY"):
        raw["b2_application_key"] = env["B2_APPLICATION_KEY"]

    # (prompting + validation added in Task 3 — for now, fill blanks as-is)
    return _build_config(raw, role=role, promote=promote)


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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/ops/test_install.py -k "config or secret_values or env_wins" -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add src/mthydra/ops/install.py tests/unit/ops/test_install.py
git commit -m "feat(install): Config dataclass + ini parsing"
```

---

## Task 3: Config prompting, validation, and role-based requirements

**Files:**
- Modify: `src/mthydra/ops/install.py` (`load_config`, add `_prompt`, `_validate`)
- Test: `tests/unit/ops/test_install.py`

- [ ] **Step 1: Write the failing test**

```python
# add to tests/unit/ops/test_install.py
import pytest


def test_missing_required_field_errors_when_non_interactive(tmp_path):
    ini = _write_ini(tmp_path, _FULL_INI.replace("hostname = eu1.example.com", "hostname ="))
    with pytest.raises(install.ConfigError, match="hostname"):
        install.load_config(ini, role="active", promote=False,
                            interactive=False, env={})


def test_interactive_prompt_fills_missing_field(tmp_path, monkeypatch):
    ini = _write_ini(tmp_path, _FULL_INI.replace("hostname = eu1.example.com", "hostname ="))
    monkeypatch.setattr("builtins.input", lambda prompt="": "typed.example.com")
    cfg = install.load_config(ini, role="active", promote=False,
                              interactive=True, env={})
    assert cfg.hostname == "typed.example.com"


def test_secret_prompt_uses_getpass(tmp_path, monkeypatch):
    ini = _write_ini(tmp_path, _FULL_INI.replace("application_key = B2SECRET", "application_key ="))
    called = {}
    def fake_getpass(prompt=""):
        called["yes"] = True
        return "TYPED_SECRET"
    monkeypatch.setattr(install.getpass, "getpass", fake_getpass)
    cfg = install.load_config(ini, role="active", promote=False,
                              interactive=True, env={})
    assert cfg.b2_application_key == "TYPED_SECRET"
    assert called.get("yes") is True


def test_refuses_age_secret_key(tmp_path):
    bad = _FULL_INI.replace(
        "recipient = age1qqp0000000000000000000000000000000000000000000000000q",
        "recipient = AGE-SECRET-KEY-1QQPQYU8H4ENHEER9CA2W7XXXXXXXXXXXXXXXXXXXXXXXXXSPLAB",
    )
    ini = _write_ini(tmp_path, bad)
    with pytest.raises(install.ConfigError, match="secret key"):
        install.load_config(ini, role="active", promote=False,
                            interactive=False, env={})


def test_passive_standby_allows_missing_sinks(tmp_path):
    minimal = """\
        [install]
        git_url = https://example/mthydra.git
        [node]
        hostname = standby.example.com
        [age]
        recipient = age1qqp0000000000000000000000000000000000000000000000000q
        [backup]
        endpoint = https://s3.example.com
        bucket = mthydra-prod
        key_id = 0012abc
        application_key = B2SECRET
        """
    ini = _write_ini(tmp_path, minimal)
    cfg = install.load_config(ini, role="standby", promote=False,
                              interactive=False, env={})
    assert cfg.obs_tg_bot_token == ""  # not required, not prompted


def test_promote_standby_requires_sinks(tmp_path):
    minimal = """\
        [install]
        git_url = https://example/mthydra.git
        [node]
        hostname = standby.example.com
        [age]
        recipient = age1qqp0000000000000000000000000000000000000000000000000q
        [backup]
        endpoint = https://s3.example.com
        bucket = mthydra-prod
        key_id = 0012abc
        application_key = B2SECRET
        """
    ini = _write_ini(tmp_path, minimal)
    with pytest.raises(install.ConfigError, match="obs_tg_bot_token"):
        install.load_config(ini, role="standby", promote=True,
                            interactive=False, env={})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/ops/test_install.py -k "required or prompt or refuses or standby" -v`
Expected: FAIL — required fields are not yet enforced/validated.

- [ ] **Step 3: Write minimal implementation**

```python
# add near the top of src/mthydra/ops/install.py
import getpass

# replace the body of load_config (the part after computing `raw` and env-override):
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
    for fieldname in sorted(required):
        if raw.get(fieldname):
            continue
        if interactive:
            raw[fieldname] = _prompt(fieldname)
        if not raw.get(fieldname):
            raise ConfigError(f"required field missing: {fieldname}")

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
            "age.recipient is an age SECRET key — it must NEVER be on a host "
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/ops/test_install.py -v`
Expected: PASS (all install tests so far).

- [ ] **Step 5: Commit**

```bash
git add src/mthydra/ops/install.py tests/unit/ops/test_install.py
git commit -m "feat(install): config prompting, validation, role-based requirements (N-D6, age-secret refusal N-D8)"
```

---

## Task 4: `Phase` + `Ctx` + `Runner` framework

**Files:**
- Modify: `src/mthydra/ops/install.py`
- Test: `tests/unit/ops/test_install.py`

- [ ] **Step 1: Write the failing test**

```python
# add to tests/unit/ops/test_install.py
def _ctx(tmp_path, dry_run=False):
    ini = _write_ini(tmp_path, _FULL_INI)
    cfg = install.load_config(ini, role="active", promote=False,
                              interactive=False, env={})
    log = install.RedactingLog(tmp_path / "i.log", cfg.secret_values())
    return install.Ctx(config=cfg, log=log, dry_run=dry_run, quiet=True)


def test_runner_skips_satisfied_phases(tmp_path):
    ctx = _ctx(tmp_path)
    ran = []
    phases = [
        install.Phase("a", lambda c: True, lambda c: ran.append("a")),
        install.Phase("b", lambda c: False, lambda c: ran.append("b")),
    ]
    rc = install.Runner(phases, ctx).execute()
    assert rc == 0
    assert ran == ["b"]  # 'a' skipped


def test_runner_dry_run_executes_nothing(tmp_path):
    ctx = _ctx(tmp_path, dry_run=True)
    ran = []
    phases = [install.Phase("b", lambda c: False, lambda c: ran.append("b"))]
    rc = install.Runner(phases, ctx).execute()
    assert rc == 0
    assert ran == []


def test_runner_aborts_on_phase_exception(tmp_path):
    ctx = _ctx(tmp_path)
    ran = []
    def boom(c):
        raise RuntimeError("kaboom")
    phases = [
        install.Phase("a", lambda c: False, boom),
        install.Phase("b", lambda c: False, lambda c: ran.append("b")),
    ]
    rc = install.Runner(phases, ctx).execute()
    assert rc == 1
    assert ran == []  # pipeline stopped before 'b'
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/ops/test_install.py -k runner -v`
Expected: FAIL — `Ctx`/`Phase`/`Runner` undefined.

- [ ] **Step 3: Write minimal implementation**

```python
# add to src/mthydra/ops/install.py
import subprocess
import sys
from typing import Callable

_CONTROLLER_BIN = os.environ.get("MTHYDRA_CONTROLLER", "mthydra-controller")


@dataclass
class Ctx:
    config: "Config"
    log: "RedactingLog"
    dry_run: bool = False
    quiet: bool = False

    def say(self, msg: str) -> None:
        line = f"[mthydra-install] {msg}"
        self.log.write(line + "\n")
        if not self.quiet:
            print(line, flush=True)

    def err(self, msg: str) -> None:
        line = f"[mthydra-install] ERROR: {msg}"
        self.log.write(line + "\n")
        print(line, file=sys.stderr, flush=True)

    def run_controller(self, *args, env=None, capture=False):
        cmd = [_CONTROLLER_BIN, *args]
        self.log.write("$ " + " ".join(cmd) + "\n")
        if self.dry_run:
            self.say("DRY-RUN, would run: " + " ".join(cmd))
            return subprocess.CompletedProcess(cmd, 0, "", "")
        res = subprocess.run(
            cmd, check=True, text=True, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        if res.stdout:
            self.log.write(res.stdout)
        return res


@dataclass
class Phase:
    name: str
    is_satisfied: Callable[[Ctx], bool]
    run: Callable[[Ctx], None]


class Runner:
    def __init__(self, phases: list[Phase], ctx: Ctx):
        self.phases = phases
        self.ctx = ctx

    def execute(self) -> int:
        n = len(self.phases)
        for i, ph in enumerate(self.phases, 1):
            self.ctx.say(f"[{i}/{n}] {ph.name} …")
            if ph.is_satisfied(self.ctx):
                self.ctx.say(f"[{i}/{n}] {ph.name}: already satisfied → skip")
                continue
            if self.ctx.dry_run:
                self.ctx.say(f"[{i}/{n}] {ph.name}: WOULD run")
                continue
            try:
                ph.run(self.ctx)
            except Exception as e:  # noqa: BLE001 — top-level orchestrator boundary
                self.ctx.err(f"phase '{ph.name}' failed: {e}")
                return 1
        return 0
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/ops/test_install.py -k runner -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add src/mthydra/ops/install.py tests/unit/ops/test_install.py
git commit -m "feat(install): Phase/Ctx/Runner framework with idempotent skip + dry-run (N-D4, N-D10)"
```

---

## Task 5: `is_satisfied` probes

**Files:**
- Modify: `src/mthydra/ops/install.py`
- Test: `tests/unit/ops/test_install.py`

- [ ] **Step 1: Write the failing test**

```python
# add to tests/unit/ops/test_install.py
import json as _json


def test_service_active_probe(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)
    calls = []
    def fake_run(argv, **kw):
        calls.append(argv)
        rc = 0 if argv[:2] == ["systemctl", "is-active"] else 1
        return subprocess.CompletedProcess(argv, rc, "active\n", "")
    monkeypatch.setattr(install.subprocess, "run", fake_run)
    assert install.service_active(ctx) is True
    assert ["systemctl", "is-active", "mthydra-controller"] in calls


def test_db_initialized_probe_false_when_missing(tmp_path):
    ctx = _ctx(tmp_path)
    object.__setattr__(ctx.config, "db_path", str(tmp_path / "absent.sqlite"))
    assert install.db_initialized(ctx) is False


def test_descriptor_signed_probe_reads_generation(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)
    monkeypatch.setattr(
        install.Ctx, "run_controller",
        lambda self, *a, **k: subprocess.CompletedProcess(
            list(a), 0, _json.dumps({"generation": 3}), ""),
    )
    assert install.descriptor_signed(ctx) is True


def test_timer_enabled_probe(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)
    monkeypatch.setattr(
        install.subprocess, "run",
        lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "enabled\n", ""),
    )
    assert install.timer_enabled(ctx, "mthydra-daily-check") is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/ops/test_install.py -k "probe" -v`
Expected: FAIL — probe functions undefined.

- [ ] **Step 3: Write minimal implementation**

```python
# add to src/mthydra/ops/install.py
import grp
import pwd
import stat as _stat


def _systemctl_ok(*args: str) -> bool:
    return subprocess.run(
        ["systemctl", *args], capture_output=True, text=True
    ).returncode == 0


def host_prepared(ctx: Ctx) -> bool:
    try:
        pwd.getpwnam("mthydra")
    except KeyError:
        return False
    needed = {
        "/etc/mthydra": (0o755, "root"),
        "/var/lib/mthydra": (0o700, "mthydra"),
        "/var/log/mthydra": (0o755, "mthydra"),
    }
    for path, (mode, owner) in needed.items():
        p = Path(path)
        if not p.is_dir():
            return False
        st = p.stat()
        if _stat.S_IMODE(st.st_mode) != mode:
            return False
        if pwd.getpwuid(st.st_uid).pw_name != owner:
            return False
    return True


def controller_installed(ctx: Ctx) -> bool:
    return subprocess.run(
        [_CONTROLLER_BIN, "--help"], capture_output=True, text=True
    ).returncode == 0


def db_initialized(ctx: Ctx) -> bool:
    if not Path(ctx.config.db_path).exists():
        return False
    return subprocess.run(
        [_CONTROLLER_BIN, "startup-check", "--db-path", ctx.config.db_path],
        capture_output=True, text=True,
    ).returncode == 0


def authority_is_real(ctx: Ctx) -> bool:
    """True if credential_authority holds a real (non-placeholder) key.

    Uses sqlite3 to read the key length (runbook §1.6 verifies the same way).
    If sqlite3 is unavailable, conservatively assume real so we never re-run
    authority-migrate-placeholder on an existing DB (which would mint gen 2).
    """
    import shutil as _sh
    if _sh.which("sqlite3") is None:
        return True
    out = subprocess.run(
        ["sqlite3", ctx.config.db_path,
         "SELECT length(privkey_pem) FROM credential_authority "
         "ORDER BY generation DESC LIMIT 1;"],
        capture_output=True, text=True,
    )
    return out.returncode == 0 and out.stdout.strip().isdigit() and int(out.stdout) > 200


def controller_toml_present(ctx: Ctx) -> bool:
    return Path(ctx.config.config_path).exists()


def age_recipient_file_present(ctx: Ctx) -> bool:
    return Path("/etc/mthydra/age-recipient.txt").exists()


def service_active(ctx: Ctx) -> bool:
    return _systemctl_ok("is-active", "mthydra-controller")


def descriptor_signed(ctx: Ctx) -> bool:
    try:
        res = ctx.run_controller(
            "descriptor-show", "--db-path", ctx.config.db_path, "--json",
            capture=True)
    except subprocess.CalledProcessError:
        return False
    try:
        return int(json.loads(res.stdout).get("generation", 0)) >= 1
    except (ValueError, json.JSONDecodeError):
        return False


def timer_enabled(ctx: Ctx, name: str) -> bool:
    return _systemctl_ok("is-enabled", f"{name}.timer")
```

Add `import json` at the top of the module if not already present.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/ops/test_install.py -k probe -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add src/mthydra/ops/install.py tests/unit/ops/test_install.py
git commit -m "feat(install): state-derived is_satisfied probes (N-D4)"
```

---

## Task 6: Refactor `main.py` step logic into injectable cores

**Files:**
- Modify: `src/mthydra/ops/main.py` (extract `setup_host_core`, `bootstrap_core`, `preflight_core`; `cmd_setup_host`/`cmd_bootstrap`/`cmd_preflight` call them)
- Test: `tests/unit/ops/test_main.py` (existing tests must still pass; add direct-call tests)

- [ ] **Step 1: Write the failing test**

```python
# add to tests/unit/ops/test_main.py
def test_bootstrap_core_passes_secret_via_env_not_argv():
    from mthydra.ops import main as m
    calls, envs = [], []
    def fake_run(*args, check=True, capture=False, env=None):
        calls.append(list(args)); envs.append(env)
        return subprocess.CompletedProcess(args, 0, "", "")
    said = []
    m.bootstrap_core(
        fake_run, said.append,
        db_path="/tmp/x.sqlite", config_path="/tmp/c.toml",
        age_recipient="age1abc", b2_application_key="SECRET",
        hostname="h", role="active",
        b2_endpoint="e", b2_bucket="b", b2_key_id="k",
        obs_tg_bot_token="t", obs_tg_chat_id="1", obs_smtp_host="s",
        obs_smtp_port=587, obs_smtp_from="a@b", obs_smtp_to="c@d",
        obs_smtp_user="u", obs_smtp_pass="p", dist_tg_bot_token="t2",
        dist_smtp_host="s", dist_smtp_port=587, dist_smtp_from="e@f",
        dist_smtp_user="u", dist_smtp_pass="p",
    )
    # SECRET must never appear on any argv
    for argv in calls:
        assert "SECRET" not in " ".join(argv)
    # but it must be handed to the init call via env
    init_env = next(e for c, e in zip(calls, envs) if c and c[0] == "init")
    assert "SECRET" in " ".join(init_env.values())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/ops/test_main.py -k bootstrap_core -v`
Expected: FAIL — `bootstrap_core` undefined.

- [ ] **Step 3: Write minimal implementation**

Extract the body of `cmd_bootstrap` (lines ~340–390 in `main.py`) into a core that takes injected `run`/`say` and explicit fields. `cmd_bootstrap` then reads argv/env and delegates. Sketch:

```python
# src/mthydra/ops/main.py
def bootstrap_core(run, say, *, db_path, config_path, age_recipient,
                   b2_application_key, hostname, role, force=False, **toml):
    """init (if absent) + authority-migrate (if placeholder) + write toml +
    write age-recipient.txt. `run` matches _run_controller's signature; all
    secrets travel via the child env, never argv. Idempotent per sub-step."""
    from datetime import datetime, timezone
    db, cfg = Path(db_path), Path(config_path)

    if not db.exists():
        say("step 1/4: init controller state")
        child_env = {**os.environ,
                     "MTHYDRA_INIT_B2_CREDENTIAL": f"{toml['b2_key_id']}:{b2_application_key}"}
        run("init", "--db-path", str(db), "--age-recipient", age_recipient,
            "--provider-credential-env", "b2=MTHYDRA_INIT_B2_CREDENTIAL",
            "--role", role, env=child_env)
        say("step 2/4: migrate credential authority off placeholder")
        run("authority-migrate-placeholder", "--db-path", str(db))
    else:
        say("DB exists → skip init + authority-migrate")

    if not cfg.exists():
        say(f"step 3/4: write {cfg}")
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(_TOML_TEMPLATE.format(
            timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            hostname=hostname, **toml))
        os.chmod(cfg, 0o600)
    else:
        say(f"{cfg} exists → skip")

    rec = Path("/etc/mthydra/age-recipient.txt")
    if not rec.exists():
        say("step 4/4: write /etc/mthydra/age-recipient.txt")
        rec.write_text(age_recipient + "\n")
        os.chmod(rec, 0o600)
    return 0
```

Refactor `cmd_bootstrap` to call `bootstrap_core(_run_controller, _say, ...)` reading args/env as today (preserving its `--force`/refuse-second-bootstrap guard before delegating). Do the analogous extraction for `setup_host_core(run_step, *, dry_run)` (from `cmd_setup_host`'s step loop) and `preflight_core(run, say, *, db_path, config_path)` (from `cmd_preflight`). Keep `_TOML_TEMPLATE` where it is.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/ops/test_main.py -v`
Expected: PASS — the new `bootstrap_core` test AND every pre-existing `cmd_bootstrap`/`cmd_setup_host`/`cmd_preflight` test (no behaviour change).

- [ ] **Step 5: Commit**

```bash
git add src/mthydra/ops/main.py tests/unit/ops/test_main.py
git commit -m "refactor(ops): extract setup_host/bootstrap/preflight cores (injectable run+say)"
```

---

## Task 7: systemd unit generation + enable helper

**Files:**
- Modify: `src/mthydra/ops/install.py` (unit templates + `write_and_enable_unit`, `install_maintenance_timers`, `install_controller_service`)
- Test: `tests/unit/ops/test_install.py`

- [ ] **Step 1: Write the failing test**

```python
# add to tests/unit/ops/test_install.py
def test_write_and_enable_unit_writes_file_and_enables(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)
    units = tmp_path / "systemd"
    units.mkdir()
    monkeypatch.setattr(install, "_UNIT_DIR", units)
    sysctl = []
    monkeypatch.setattr(install.subprocess, "run",
        lambda argv, **kw: sysctl.append(argv) or subprocess.CompletedProcess(argv, 0, "", ""))
    install.write_and_enable_unit(ctx, "mthydra-daily-check.timer",
                                  "[Unit]\nDescription=x\n", enable=True)
    assert (units / "mthydra-daily-check.timer").read_text().startswith("[Unit]")
    assert ["systemctl", "daemon-reload"] in sysctl
    assert any(a[:3] == ["systemctl", "enable", "--now"] for a in sysctl)


def test_maintenance_timers_use_configured_venv(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)
    written = {}
    monkeypatch.setattr(install, "write_and_enable_unit",
        lambda c, name, body, enable=True: written.__setitem__(name, body))
    install.install_maintenance_timers(ctx)
    svc = written["mthydra-daily-check.service"]
    assert f"{ctx.config.venv_dir}/bin/mthydra-ops daily-check" in svc
    assert "mthydra-monthly-compact.timer" in written
    assert "mthydra-weekly-scan.timer" in written
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/ops/test_install.py -k "unit or timers" -v`
Expected: FAIL — helpers undefined.

- [ ] **Step 3: Write minimal implementation**

```python
# add to src/mthydra/ops/install.py
_UNIT_DIR = Path("/etc/systemd/system")

_TIMER_TMPL = """\
[Unit]
Description={desc}

[Timer]
OnCalendar={oncalendar}
Persistent=true

[Install]
WantedBy=timers.target
"""

_SERVICE_TMPL = """\
[Unit]
Description={desc}

[Service]
Type=oneshot
User=mthydra
Group=mthydra
ExecStart={venv}/bin/mthydra-ops {subcommand}
StandardOutput=journal
StandardError=journal
"""


def write_and_enable_unit(ctx: Ctx, name: str, body: str, enable: bool = True) -> None:
    target = _UNIT_DIR / name
    ctx.say(f"writing {target}")
    if ctx.dry_run:
        return
    target.write_text(body)
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    if enable and name.endswith(".timer"):
        subprocess.run(["systemctl", "enable", "--now", name], check=True)


def install_maintenance_timers(ctx: Ctx) -> None:
    venv = ctx.config.venv_dir
    specs = [
        ("mthydra-daily-check", "daily obligation check", "daily-check", "*-*-* 06:17:00"),
        ("mthydra-weekly-scan", "weekly silent-failure scan",
         "alert-summary", "Mon *-*-* 07:00:00"),
        ("mthydra-monthly-compact", "monthly log compaction",
         "monthly-compact --no-dry-run --evidence scheduled", "*-*-01 03:00:00"),
    ]
    for base, desc, subcmd, oncal in specs:
        write_and_enable_unit(
            ctx, f"{base}.service",
            _SERVICE_TMPL.format(desc=desc, venv=venv, subcommand=subcmd),
            enable=False)
        write_and_enable_unit(
            ctx, f"{base}.timer",
            _TIMER_TMPL.format(desc=desc, oncalendar=oncal), enable=True)


def install_controller_service(ctx: Ctx) -> None:
    body = (
        "[Unit]\nDescription=mthydra controller\nAfter=network.target\n\n"
        "[Service]\nUser=mthydra\nGroup=mthydra\n"
        "WorkingDirectory=/var/lib/mthydra\n"
        f"ExecStart={ctx.config.venv_dir}/bin/mthydra-controller serve "
        f"--db-path {ctx.config.db_path} --config {ctx.config.config_path}\n"
        "Restart=on-failure\nRestartSec=5\n"
        "StandardOutput=journal\nStandardError=journal\n\n"
        "[Install]\nWantedBy=multi-user.target\n"
    )
    ctx.say("writing /etc/systemd/system/mthydra-controller.service")
    if ctx.dry_run:
        return
    (_UNIT_DIR / "mthydra-controller.service").write_text(body)
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "enable", "--now", "mthydra-controller"], check=True)
```

(A passive standby installs **no** maintenance timers — it emits nothing and has no standby-specific check command to schedule; systemd auto-restarts the `serve` loop. A *promoted* standby becomes active and gets the active `install_maintenance_timers` above. So no `install_standby_heartbeat_timer` helper is needed.)

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/ops/test_install.py -k "unit or timers" -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add src/mthydra/ops/install.py tests/unit/ops/test_install.py
git commit -m "feat(install): systemd controller service + maintenance timers (N-D9)"
```

---

## Task 8: `cmd_install` — active 9-phase orchestrator

**Files:**
- Modify: `src/mthydra/ops/install.py`
- Test: `tests/unit/ops/test_install.py`

- [ ] **Step 1: Write the failing test**

```python
# add to tests/unit/ops/test_install.py
def test_build_active_phases_order(tmp_path):
    ctx = _ctx(tmp_path)
    names = [p.name for p in install.build_active_phases(ctx)]
    assert names == [
        "preconditions", "setup-host", "verify-install", "bootstrap",
        "preflight", "service", "first-descriptor", "maintenance-timers",
        "summary",
    ]


def test_active_dry_run_executes_no_side_effects(tmp_path, monkeypatch):
    # every probe False so phases would "run", but dry_run must execute nothing
    for probe in ("host_prepared", "controller_installed", "db_initialized",
                  "service_active", "descriptor_signed"):
        monkeypatch.setattr(install, probe, lambda c: False)
    monkeypatch.setattr(install, "timer_enabled", lambda c, n: False)
    ran = {"systemctl": 0}
    monkeypatch.setattr(install.subprocess, "run",
        lambda *a, **k: ran.__setitem__("systemctl", ran["systemctl"] + 1)
        or subprocess.CompletedProcess(a, 0, "", ""))
    ctx = _ctx(tmp_path, dry_run=True)
    rc = install.Runner(install.build_active_phases(ctx), ctx).execute()
    assert rc == 0
    assert ran["systemctl"] == 0  # nothing executed in dry-run
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/ops/test_install.py -k "active" -v`
Expected: FAIL — `build_active_phases` undefined.

- [ ] **Step 3: Write minimal implementation**

```python
# add to src/mthydra/ops/install.py
from . import main as _main


def _precondition_check(ctx: Ctx) -> None:
    if os.geteuid() != 0 and not ctx.dry_run:
        raise RuntimeError("install must run as root")
    ctx.say(f"role={ctx.config.role} promote={ctx.config.promote} "
            f"host={ctx.config.hostname}")  # config already validated at load


def _phase_setup_host(ctx: Ctx) -> None:
    def run_step(argv, allow_fail):
        ctx.log.write("$ " + " ".join(argv) + "\n")
        rc = subprocess.run(argv).returncode
        if rc != 0 and not allow_fail:
            raise RuntimeError(f"step failed (rc={rc}): {' '.join(argv)}")
    _main.setup_host_core(run_step, dry_run=ctx.dry_run)


def _phase_bootstrap(ctx: Ctx) -> None:
    c = ctx.config
    _main.bootstrap_core(
        ctx.run_controller, ctx.say,
        db_path=c.db_path, config_path=c.config_path,
        age_recipient=c.age_recipient, b2_application_key=c.b2_application_key,
        hostname=c.hostname, role=c.role,
        b2_endpoint=c.b2_endpoint, b2_bucket=c.b2_bucket, b2_key_id=c.b2_key_id,
        operator_email=c.obs_smtp_to,
        obs_tg_bot_token=c.obs_tg_bot_token, obs_tg_chat_id=c.obs_tg_chat_id,
        obs_smtp_host=c.obs_smtp_host, obs_smtp_port=c.obs_smtp_port,
        obs_smtp_from=c.obs_smtp_from, obs_smtp_to=c.obs_smtp_to,
        obs_smtp_user=c.obs_smtp_user, obs_smtp_pass=c.obs_smtp_pass,
        dist_tg_bot_token=c.dist_tg_bot_token, dist_smtp_host=c.dist_smtp_host,
        dist_smtp_port=c.dist_smtp_port, dist_smtp_from=c.dist_smtp_from,
        dist_smtp_user=c.dist_smtp_user, dist_smtp_pass=c.dist_smtp_pass,
    )


def _phase_preflight(ctx: Ctx) -> None:
    c = ctx.config
    _main.preflight_core(ctx.run_controller, ctx.say,
                         db_path=c.db_path, config_path=c.config_path)
    if c.assume_sinks:
        ctx.say("assume_sinks=true → skipping the §1.8 human gate")
        return
    if ctx.dry_run:
        return
    ans = input("Did the crit test arrive in BOTH Telegram AND email? [y/N] ")
    if ans.strip().lower() not in ("y", "yes"):
        raise RuntimeError(
            "§1.8 gate not confirmed — fix [observability.*] and re-run install")


def _phase_summary(ctx: Ctx) -> None:
    ctx.say(
        "EU active host is live. Remaining OUT-OF-BAND steps:\n"
        "  1. Confirm §1.8 sinks if you skipped the gate.\n"
        "  2. Back up the operator age key to two non-cloud locations "
        "(§1.2) — it is NOT on this host and must never be.\n"
        "  3. Stand up a warm standby (mthydra-ops install-standby) and "
        "eu-node-add it from here (§1.11).\n"
        "  4. RU image build and RU-node provisioning are SEPARATE automation, "
        "not run by this installer."
    )


def build_active_phases(ctx: Ctx) -> list[Phase]:
    return [
        Phase("preconditions", lambda c: False, _precondition_check),
        Phase("setup-host", host_prepared, _phase_setup_host),
        Phase("verify-install", controller_installed,
              lambda c: (_ for _ in ()).throw(
                  RuntimeError("mthydra-controller not on PATH — build broken (§1.4)"))),
        Phase("bootstrap",
              lambda c: db_initialized(c) and authority_is_real(c)
              and controller_toml_present(c) and age_recipient_file_present(c),
              _phase_bootstrap),
        Phase("preflight", lambda c: False, _phase_preflight),
        Phase("service", service_active, install_controller_service),
        Phase("first-descriptor", descriptor_signed,
              lambda c: c.run_controller("descriptor-sign-now",
                                         "--db-path", c.config.db_path)),
        Phase("maintenance-timers",
              lambda c: all(timer_enabled(c, n) for n in
                            ("mthydra-daily-check", "mthydra-weekly-scan",
                             "mthydra-monthly-compact")),
              install_maintenance_timers),
        Phase("summary", lambda c: False, _phase_summary),
    ]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/ops/test_install.py -k active -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add src/mthydra/ops/install.py tests/unit/ops/test_install.py
git commit -m "feat(install): cmd_install active 9-phase orchestrator (§1.1-1.10)"
```

---

## Task 9: `cmd_install_standby` — standby + `--promote`/`--case`

**Files:**
- Modify: `src/mthydra/ops/install.py`
- Test: `tests/unit/ops/test_install.py`

- [ ] **Step 1: Write the failing test**

```python
# add to tests/unit/ops/test_install.py
def _standby_ctx(tmp_path, promote=False, case="A"):
    minimal = _FULL_INI if promote else """\
        [install]
        git_url = https://example/mthydra.git
        [node]
        hostname = standby.example.com
        [age]
        recipient = age1qqp0000000000000000000000000000000000000000000000000q
        [backup]
        endpoint = https://s3.example.com
        bucket = mthydra-prod
        key_id = 0012abc
        application_key = B2SECRET
        """
    ini = _write_ini(tmp_path, minimal)
    cfg = install.load_config(ini, role="standby", promote=promote,
                              interactive=False, env={})
    object.__setattr__(cfg, "promote_case", case)
    log = install.RedactingLog(tmp_path / "s.log", cfg.secret_values())
    return install.Ctx(config=cfg, log=log, dry_run=True, quiet=True)


def test_passive_standby_phase_order(tmp_path):
    ctx = _standby_ctx(tmp_path)
    names = [p.name for p in install.build_standby_phases(ctx, promote=False, case="A")]
    assert names == [
        "preconditions", "setup-host", "verify-install", "bootstrap",
        "standby-readiness", "service", "summary",
    ]


def test_promote_inserts_promote_phase_and_appends_active_timers(tmp_path):
    ctx = _standby_ctx(tmp_path, promote=True)
    names = [p.name for p in install.build_standby_phases(ctx, promote=True, case="B")]
    assert "promote" in names
    assert "maintenance-timers" in names         # active timers after promotion
    assert names[-1] == "summary"


def test_promote_case_b_runs_rotation(tmp_path, monkeypatch):
    ctx = _standby_ctx(tmp_path, promote=True, case="B")
    ctx.dry_run = False
    calls = []
    monkeypatch.setattr(install.Ctx, "run_controller",
        lambda self, *a, **k: calls.append(list(a))
        or subprocess.CompletedProcess(a, 0, "", ""))
    install._phase_promote(ctx)
    flat = [c[0] for c in calls]
    assert "promote-active" in flat
    assert "authority-rotate" in flat
    assert "signing-key-rotate" in flat
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/ops/test_install.py -k "standby or promote" -v`
Expected: FAIL — `build_standby_phases`/`_phase_promote` undefined.

- [ ] **Step 3: Write minimal implementation**

```python
# add to src/mthydra/ops/install.py
def _phase_standby_readiness(ctx: Ctx) -> None:
    # No `standby-heartbeat-check` CLI exists in this build; startup-check is the
    # available health gate. serve's own B2 polling (spec F-D5) tracks the active.
    ctx.run_controller("startup-check", "--db-path", ctx.config.db_path)
    ctx.say(
        "standby startup-check passed. The serve loop polls the active's B2 "
        "heartbeat automatically (spec F-D5); confirm liveness from the active "
        "via `mthydra-controller eu-node-list` after eu-node-add.")


def _phase_promote(ctx: Ctx) -> None:
    case = getattr(ctx.config, "promote_case", "A")
    ctx.say(f"promoting standby → active (case {case})")
    ctx.run_controller("promote-active", "--case", case,
                       "--evidence", f"install-standby --promote --case {case}")
    if case == "B":
        ctx.run_controller("authority-rotate", "--evidence", "post-Case-B install")
        ctx.run_controller("signing-key-rotate", "--evidence", "post-Case-B install")
        ctx.say(
            "CASE B manual rotations still required (NOT automated):\n"
            "  - rotate the B2 application key in the B2 UI + re-bootstrap cred\n"
            "  - rotate [observability.email]/[distribution.email] app passwords\n"
            "  - revoke + re-mint both Telegram bot tokens at @BotFather\n"
            "  then re-run obs-alert-test to confirm sinks.")


def build_standby_phases(ctx: Ctx, *, promote: bool, case: str) -> list[Phase]:
    phases = [
        Phase("preconditions", lambda c: False, _precondition_check),
        Phase("setup-host", host_prepared, _phase_setup_host),
        Phase("verify-install", controller_installed,
              lambda c: (_ for _ in ()).throw(
                  RuntimeError("mthydra-controller not on PATH — build broken (§1.4)"))),
        Phase("bootstrap",
              lambda c: db_initialized(c) and authority_is_real(c)
              and controller_toml_present(c) and age_recipient_file_present(c),
              _phase_bootstrap),
        Phase("standby-readiness", lambda c: False, _phase_standby_readiness),
        Phase("service", service_active, install_controller_service),
    ]
    if promote:
        object.__setattr__(ctx.config, "promote_case", case)
        phases.append(Phase("promote", lambda c: False, _phase_promote))
        phases.append(Phase("maintenance-timers",
              lambda c: all(timer_enabled(c, n) for n in
                            ("mthydra-daily-check", "mthydra-weekly-scan",
                             "mthydra-monthly-compact")),
              install_maintenance_timers))
    # A passive standby installs no maintenance timers (emits nothing; systemd
    # auto-restarts serve; no standby check command to schedule).
    phases.append(Phase("summary", lambda c: False, _phase_summary))
    return phases
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/ops/test_install.py -k "standby or promote" -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add src/mthydra/ops/install.py tests/unit/ops/test_install.py
git commit -m "feat(install): cmd_install_standby + --promote --case A|B (§1.11, §10.2, N-D5/D6)"
```

---

## Task 10: `install` / `install-standby` subparsers + dispatch

**Files:**
- Modify: `src/mthydra/ops/install.py` (`cmd_install`, `cmd_install_standby` entry fns)
- Modify: `src/mthydra/ops/main.py` (subparsers + `_DISPATCH`)
- Test: `tests/unit/ops/test_main.py`

- [ ] **Step 1: Write the failing test**

```python
# add to tests/unit/ops/test_main.py
def test_install_subcommands_parse():
    from mthydra.ops import main as m
    p = m.build_parser()
    a = p.parse_args(["install", "--config", "x.ini", "--verbose"])
    assert a.cmd == "install" and a.config == "x.ini" and a.verbose is True
    b = p.parse_args(["install-standby", "--config", "s.ini",
                      "--promote", "--case", "B"])
    assert b.cmd == "install-standby" and b.promote is True and b.case == "B"


def test_main_routes_install_to_cmd_install(monkeypatch):
    from mthydra.ops import main as m, install
    called = {}
    monkeypatch.setattr(install, "cmd_install", lambda args: called.setdefault("v", 0) or 0)
    rc = m.main(["install", "--config", "x.ini"])
    assert rc == 0 and "v" in called
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/ops/test_main.py -k "install_subcommands or routes_install" -v`
Expected: FAIL — subcommands not registered.

- [ ] **Step 3: Write minimal implementation**

```python
# add to src/mthydra/ops/install.py
def cmd_install(args) -> int:
    cfg = load_config(args.config, role="active", promote=False,
                      interactive=not args.non_interactive)
    Path(cfg.log_dir).mkdir(parents=True, exist_ok=True)
    from datetime import datetime, timezone
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log = RedactingLog(Path(cfg.log_dir) / f"install-{stamp}.log",
                       cfg.secret_values(), echo=args.verbose)
    ctx = Ctx(config=cfg, log=log, dry_run=args.dry_run, quiet=args.quiet)
    try:
        return Runner(build_active_phases(ctx), ctx).execute()
    finally:
        log.close()


def cmd_install_standby(args) -> int:
    cfg = load_config(args.config, role="standby", promote=args.promote,
                      interactive=not args.non_interactive)
    Path(cfg.log_dir).mkdir(parents=True, exist_ok=True)
    from datetime import datetime, timezone
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log = RedactingLog(Path(cfg.log_dir) / f"install-{stamp}.log",
                       cfg.secret_values(), echo=args.verbose)
    ctx = Ctx(config=cfg, log=log, dry_run=args.dry_run, quiet=args.quiet)
    try:
        return Runner(
            build_standby_phases(ctx, promote=args.promote, case=args.case),
            ctx).execute()
    finally:
        log.close()
```

```python
# src/mthydra/ops/main.py — in build_parser(), add:
from . import install as _install  # top of file

def _add_install_args(sp):
    sp.add_argument("--config", required=True, help="path to install.ini")
    sp.add_argument("--non-interactive", action="store_true",
                    help="never prompt; missing required field is an error")
    sp.add_argument("--verbose", action="store_true",
                    help="stream all subprocess output to the terminal")
    sp.add_argument("--quiet", action="store_true", help="errors only")
    sp.add_argument("--dry-run", action="store_true",
                    help="print the plan; execute nothing")

inst = sub.add_parser("install", help="one-shot first EU active node setup")
_add_install_args(inst)

st = sub.add_parser("install-standby", help="one-shot warm-standby setup")
_add_install_args(st)
st.add_argument("--promote", action="store_true",
                help="promote to active immediately after setup")
st.add_argument("--case", choices=["A", "B"], default="A",
                help="promotion case (B = active compromised → rotate creds)")

# and in _DISPATCH:
#   "install": _install.cmd_install,
#   "install-standby": _install.cmd_install_standby,
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/ops/test_main.py -v`
Expected: PASS (routing + parse tests, plus all prior).

- [ ] **Step 5: Commit**

```bash
git add src/mthydra/ops/install.py src/mthydra/ops/main.py tests/unit/ops/test_main.py
git commit -m "feat(install): wire install / install-standby subcommands + dispatch"
```

---

## Task 11: `scripts/install.sh` bootstrap + shellcheck

**Files:**
- Create: `scripts/install.sh`
- Test: `tests/integration/test_install_sh.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/integration/test_install_sh.py
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "install.sh"


def test_shellcheck_clean():
    if shutil.which("shellcheck") is None:
        pytest.skip("shellcheck not installed")
    r = subprocess.run(["shellcheck", str(SCRIPT)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


def test_execs_correct_subcommand_with_stub(tmp_path):
    # stub mthydra-ops that records its argv then exits 0
    binroot = tmp_path / "opt" / "mthydra" / "venv" / "bin"
    binroot.mkdir(parents=True)
    recorder = tmp_path / "argv.txt"
    stub = binroot / "mthydra-ops"
    stub.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > "{recorder}"\n')
    stub.chmod(0o755)
    env = {**os.environ, "MTHYDRA_SKIP_APT": "1", "MTHYDRA_SKIP_BUILD": "1",
           "MTHYDRA_VENV_DIR": str(tmp_path / "opt" / "mthydra" / "venv")}
    r = subprocess.run(
        ["sh", str(SCRIPT), "--standby", "--config", "/tmp/s.ini",
         "--promote", "--case", "B"],
        env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    forwarded = recorder.read_text().split("\n")
    assert "install-standby" in forwarded
    assert "--promote" in forwarded and "B" in forwarded
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/integration/test_install_sh.py -v`
Expected: FAIL — `scripts/install.sh` does not exist.

- [ ] **Step 3: Write minimal implementation**

```sh
#!/bin/sh
# scripts/install.sh — naked-24.04 bootstrap for the mthydra EU host installer.
# Does ONLY what must precede Python (apt prereqs, git clone, venv build), then
# execs the tested Python orchestrator. Holds no domain logic and no secrets.
set -eu

SUBCMD="install"
GIT_URL="${MTHYDRA_GIT_URL:-}"
GIT_REF="${MTHYDRA_GIT_REF:-main}"
SRC_DIR="${MTHYDRA_SRC_DIR:-/opt/mthydra/src}"
VENV_DIR="${MTHYDRA_VENV_DIR:-/opt/mthydra/venv}"
FWD=""   # forwarded args

while [ $# -gt 0 ]; do
  case "$1" in
    --standby) SUBCMD="install-standby" ;;
    --git-url) GIT_URL="$2"; shift ;;
    --git-ref) GIT_REF="$2"; shift ;;
    --src-dir) SRC_DIR="$2"; shift ;;
    --venv-dir) VENV_DIR="$2"; shift ;;
    *) FWD="$FWD $1" ;;
  esac
  shift
done

say() { printf '[install.sh] %s\n' "$1"; }

if [ "$(id -u)" -ne 0 ] && [ "${MTHYDRA_SKIP_APT:-0}" != "1" ]; then
  echo "install.sh must run as root" >&2; exit 1
fi

if [ -r /etc/os-release ]; then
  . /etc/os-release
  case "${VERSION_ID:-}" in
    24.04) : ;;
    *) say "WARNING: tested only on Ubuntu 24.04 (found ${PRETTY_NAME:-unknown})" ;;
  esac
fi

if [ "${MTHYDRA_SKIP_APT:-0}" != "1" ]; then
  say "installing build prerequisites via apt"
  apt-get update
  apt-get install -y python3.12 python3.12-venv git age build-essential
fi

if [ "${MTHYDRA_SKIP_BUILD:-0}" != "1" ]; then
  if [ -d "$SRC_DIR/.git" ]; then
    say "updating existing checkout at $SRC_DIR"
    git -C "$SRC_DIR" fetch --tags origin
    git -C "$SRC_DIR" checkout "$GIT_REF"
  else
    [ -n "$GIT_URL" ] || { echo "--git-url required for first run" >&2; exit 2; }
    say "cloning $GIT_URL@$GIT_REF → $SRC_DIR"
    git clone --branch "$GIT_REF" "$GIT_URL" "$SRC_DIR"
  fi
  say "building venv at $VENV_DIR"
  python3.12 -m venv "$VENV_DIR"
  "$VENV_DIR/bin/pip" install --upgrade pip
  "$VENV_DIR/bin/pip" install -e "$SRC_DIR"
fi

OPS="$VENV_DIR/bin/mthydra-ops"
"$OPS" --help >/dev/null 2>&1 || { echo "mthydra-ops smoke failed" >&2; exit 3; }

say "handing off to: mthydra-ops $SUBCMD$FWD"
# shellcheck disable=SC2086  # intentional word-splitting of forwarded args
exec "$OPS" "$SUBCMD" $FWD
```

`chmod +x scripts/install.sh`.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/integration/test_install_sh.py -v`
Expected: PASS (shellcheck test skips if shellcheck absent; stub-exec test passes).

- [ ] **Step 5: Commit**

```bash
git add scripts/install.sh tests/integration/test_install_sh.py
chmod +x scripts/install.sh
git commit -m "feat(install): scripts/install.sh thin bootstrap (N-D1)"
```

---

## Task 12: ini examples, runbook pointer, Makefile note

**Files:**
- Create: `packaging/etc/mthydra/install.ini.example`, `packaging/etc/mthydra/install-standby.ini.example`
- Modify: `doc/runbook.md` (§1 callout), `Makefile` (manual-smoke note)
- Test: `tests/unit/ops/test_install.py` (examples load cleanly)

- [ ] **Step 1: Write the failing test**

```python
# add to tests/unit/ops/test_install.py
from pathlib import Path as _P


def test_active_example_ini_loads():
    ex = _P(__file__).resolve().parents[3] / "packaging/etc/mthydra/install.ini.example"
    cfg = install.load_config(ex, role="active", promote=False,
                              interactive=False,
                              env={"B2_APPLICATION_KEY": "x"})
    assert cfg.hostname  # non-empty placeholder present


def test_standby_example_loads_passive():
    ex = _P(__file__).resolve().parents[3] / "packaging/etc/mthydra/install-standby.ini.example"
    cfg = install.load_config(ex, role="standby", promote=False,
                              interactive=False,
                              env={"B2_APPLICATION_KEY": "x"})
    assert cfg.obs_tg_bot_token == ""  # sinks omitted for passive standby
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/ops/test_install.py -k example -v`
Expected: FAIL — example files do not exist.

- [ ] **Step 3: Write the example files + docs**

`packaging/etc/mthydra/install.ini.example` — the full schema from spec §5 with placeholder values (real `age1…`-shaped placeholder so validation passes; `application_key` left blank with a comment to use `B2_APPLICATION_KEY`).

`packaging/etc/mthydra/install-standby.ini.example` — `[install] [node] [age] [backup]` only, with a header comment: "Passive standby needs no sink sections. `--promote` makes this node active and then requires the [observability.*] / [distribution.*] sections — copy them from install.ini.example."

`doc/runbook.md` — add a callout under the `## §1 — Initial deployment` heading:

```markdown
> **One-shot:** `scripts/install.sh --config install.ini` (or `--standby`) runs
> §1.1–§1.10 end-to-end, idempotently. See spec
> `doc/specs/2026-05-28-N-eu-host-installer.md`. The long-form steps below remain
> authoritative for non-default setups. RU image build / RU-node provisioning
> are separate automation, not part of the installer.
```

`Makefile` — add a `smoke-install` target echoing the manual live end-to-end procedure (real B2/SMTP/host), mirroring the existing `make smoke` style.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/ops/test_install.py -k example -v && make lint`
Expected: PASS; ruff clean.

- [ ] **Step 5: Commit**

```bash
git add packaging/etc/mthydra/install*.ini.example doc/runbook.md Makefile tests/unit/ops/test_install.py
git commit -m "docs(install): ini examples, runbook §1 one-shot callout, smoke-install target"
```

---

## Task 13: Full-suite green + lint

**Files:** none (verification gate)

- [ ] **Step 1: Run the full controller suite**

Run: `pytest tests/ -q`
Expected: PASS (all, including the pre-existing suite — no regressions from the Task 6 refactor).

- [ ] **Step 2: Lint**

Run: `make lint`
Expected: ruff check + format clean on `src/` and `tests/`.

- [ ] **Step 3: Coverage sanity on the new module**

Run: `pytest tests/unit/ops/test_install.py --cov=mthydra.ops.install --cov-report=term-missing`
Expected: install.py well covered; eyeball the `--cov-report` for any untested branch and add a test if a meaningful path is bare.

- [ ] **Step 4: Commit any test top-ups**

```bash
git add -A && git commit -m "test(install): coverage top-ups for installer"
```

---

## Self-review notes (for the implementer)

- **Spec coverage:** N-D1 → Tasks 11 + 6/8/9; N-D2 → summary text (Task 8/9) + runbook callout (Task 12); N-D3 → Tasks 8/9 + parser (Task 10); N-D4 → Tasks 4/5 + every phase's `is_satisfied`; N-D5 → Task 9 `_phase_promote`; N-D6 → Tasks 3/9; N-D7 → Task 8 `_phase_preflight` gate; N-D8 → Tasks 1/6; N-D9 → Task 7; N-D10 → Tasks 4/8.
- **Resolved (verified against the installed binary 2026-05-28):** `mthydra-controller init --role {active,standby}` exists — Task 6 keeps the `--role` flag. `promote-active`, `authority-rotate`, `signing-key-rotate`, `startup-check`, `descriptor-sign-now`, `descriptor-show`, `obs-alert-test`, `obs-heartbeat-now`, `authority-migrate-placeholder` all exist.
- **Resolved:** there is **no** `standby-heartbeat-check` subcommand in this build (the runbook §10.2 reference predates it). The standby readiness phase therefore uses `startup-check` (Task 9 `_phase_standby_readiness`), and a passive standby installs no maintenance timer — serve's own B2 polling (spec F-D5) tracks the active. Spec §4.2 updated to match.
