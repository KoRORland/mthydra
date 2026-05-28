"""mthydra-ops install / install-standby — one-shot EU host orchestrators.

See doc/specs/2026-05-28-N-eu-host-installer.md.
"""
from __future__ import annotations

import configparser
import getpass
import json
import os
import pwd
import re
import stat as _stat
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import main as _main

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


_CONTROLLER_BIN = os.environ.get("MTHYDRA_CONTROLLER", "mthydra-controller")


@dataclass
class Ctx:
    config: Config
    log: RedactingLog
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


# ---------------------------------------------------------------------------
# State-derived is_satisfied probe functions (N-D4)
# ---------------------------------------------------------------------------

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
    return out.returncode == 0 and out.stdout.strip().isdigit() and int(out.stdout.strip()) > 200


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


# ---------------------------------------------------------------------------
# systemd unit generation + enable helpers (N-D9)
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Active-host 9-phase orchestrator  (§1.1-1.10)
# ---------------------------------------------------------------------------


def _precondition_check(ctx: Ctx) -> None:
    if os.geteuid() != 0 and not ctx.dry_run:
        raise RuntimeError("install must run as root")
    ctx.say(
        f"role={ctx.config.role} promote={ctx.config.promote} "
        f"host={ctx.config.hostname}"
    )  # config already validated at load


def _phase_setup_host(ctx: Ctx) -> None:
    def run_step(argv: list[str], allow_fail: bool) -> int:
        ctx.log.write("$ " + " ".join(argv) + "\n")
        rc = subprocess.run(argv).returncode
        if rc != 0 and not allow_fail:
            raise RuntimeError(f"step failed (rc={rc}): {' '.join(argv)}")
        return rc

    _main.setup_host_core(run_step, dry_run=ctx.dry_run)


def _phase_bootstrap(ctx: Ctx) -> None:
    c = ctx.config
    _main.bootstrap_core(
        ctx.run_controller,
        ctx.say,
        db_path=c.db_path,
        config_path=c.config_path,
        age_recipient=c.age_recipient,
        b2_application_key=c.b2_application_key,
        hostname=c.hostname,
        role=c.role,
        operator_email=c.obs_smtp_to,
        b2_endpoint=c.b2_endpoint,
        b2_bucket=c.b2_bucket,
        b2_key_id=c.b2_key_id,
        obs_tg_bot_token=c.obs_tg_bot_token,
        obs_tg_chat_id=c.obs_tg_chat_id,
        obs_smtp_host=c.obs_smtp_host,
        obs_smtp_port=c.obs_smtp_port,
        obs_smtp_from=c.obs_smtp_from,
        obs_smtp_to=c.obs_smtp_to,
        obs_smtp_user=c.obs_smtp_user,
        obs_smtp_pass=c.obs_smtp_pass,
        dist_tg_bot_token=c.dist_tg_bot_token,
        dist_smtp_host=c.dist_smtp_host,
        dist_smtp_port=c.dist_smtp_port,
        dist_smtp_from=c.dist_smtp_from,
        dist_smtp_user=c.dist_smtp_user,
        dist_smtp_pass=c.dist_smtp_pass,
    )


def _phase_preflight(ctx: Ctx) -> None:
    c = ctx.config
    rc = _main.preflight_core(
        ctx.run_controller, ctx.say, db_path=c.db_path, config_path=c.config_path
    )
    if rc != 0:
        raise RuntimeError(
            "preflight failed — fix [observability.*] in controller.toml and re-run"
        )
    if c.assume_sinks:
        ctx.say("assume_sinks=true → skipping the §1.8 human gate")
        return
    if ctx.dry_run:
        return
    ans = input("Did the crit test arrive in BOTH Telegram AND email? [y/N] ")
    if ans.strip().lower() not in ("y", "yes"):
        raise RuntimeError(
            "§1.8 gate not confirmed — fix [observability.*] and re-run install"
        )


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


# ---------------------------------------------------------------------------
# Warm-standby orchestrator with optional promotion (§1.11, §10.2, N-D5/D6)
# ---------------------------------------------------------------------------


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


def cmd_install(args) -> int:
    cfg = load_config(args.config, role="active", promote=False,
                      interactive=not args.non_interactive)
    Path(cfg.log_dir).mkdir(parents=True, exist_ok=True)
    from datetime import UTC, datetime
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
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
    from datetime import UTC, datetime
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    log = RedactingLog(Path(cfg.log_dir) / f"install-{stamp}.log",
                       cfg.secret_values(), echo=args.verbose)
    ctx = Ctx(config=cfg, log=log, dry_run=args.dry_run, quiet=args.quiet)
    try:
        return Runner(
            build_standby_phases(ctx, promote=args.promote, case=args.case),
            ctx).execute()
    finally:
        log.close()


def build_active_phases(ctx: Ctx) -> list[Phase]:
    return [
        Phase("preconditions", lambda c: False, _precondition_check),
        Phase("setup-host", host_prepared, _phase_setup_host),
        Phase(
            "verify-install",
            controller_installed,
            lambda c: (_ for _ in ()).throw(
                RuntimeError(
                    "mthydra-controller not on PATH — build broken (§1.4)"
                )
            ),
        ),
        Phase(
            "bootstrap",
            lambda c: db_initialized(c)
            and authority_is_real(c)
            and controller_toml_present(c)
            and age_recipient_file_present(c),
            _phase_bootstrap,
        ),
        Phase("preflight", lambda c: False, _phase_preflight),
        Phase("service", service_active, install_controller_service),
        Phase(
            "first-descriptor",
            descriptor_signed,
            lambda c: c.run_controller(
                "descriptor-sign-now", "--db-path", c.config.db_path
            ),
        ),
        Phase(
            "maintenance-timers",
            lambda c: all(
                timer_enabled(c, n)
                for n in (
                    "mthydra-daily-check",
                    "mthydra-weekly-scan",
                    "mthydra-monthly-compact",
                )
            ),
            install_maintenance_timers,
        ),
        Phase("summary", lambda c: False, _phase_summary),
    ]
