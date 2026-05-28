"""mthydra-ops ru-bringup / ru-image-cycle — RU node automation wizards.

See doc/specs/2026-05-28-O-ru-bringup-and-image-cycle.md.
"""
from __future__ import annotations

import contextlib
import json
import os
import socket
import ssl
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

# Import controller helpers from main. main does NOT import ru_bringup at top
# (lazy dispatch in a later task), so this is safe.
from . import main as _main

_run_controller = _main._run_controller                  # subprocess wrapper, env-safe
_run_controller_capture_both = _main._run_controller_capture_both
_extract_box_id = _main._extract_box_id                   # 'provision-seed: created box_id=...'

CYCLE_STATE_DIR = Path("/var/lib/mthydra/ru-cycle")

# Mirror main.py's env-derived defaults so MTHYDRA_DB_PATH / MTHYDRA_CONFIG work.
_DEFAULT_DB = os.environ.get("MTHYDRA_DB_PATH", "/var/lib/mthydra/state.sqlite")
_DEFAULT_CONFIG = os.environ.get("MTHYDRA_CONFIG", "/etc/mthydra/controller.toml")


@dataclass
class CycleState:
    release: str
    image_version: str
    profile_path: str
    image_built: bool
    canaries: list[dict] = field(default_factory=list)
    started_at: str = ""

    def save(self, path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2, sort_keys=True))
        tmp.replace(p)   # atomic rename

    @classmethod
    def load(cls, path) -> CycleState | None:
        p = Path(path)
        if not p.exists():
            return None
        return cls(**json.loads(p.read_text()))


def wait_for_reachable(host: str, port: int, sni: str, *,
                       timeout_s: int, poll_s: int = 10,
                       on_progress: Callable[[Exception], None] | None = None,
                       ) -> bool:
    """TCP+TLS handshake liveness check (no cert validation — Fake-TLS box).

    Returns True on the first successful handshake; False after timeout_s.
    `on_progress` is called with the exception on each failed attempt.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=5) as sock:
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE   # Fake-TLS — liveness only (O-D4)
                with ctx.wrap_socket(sock, server_hostname=sni) as tls:
                    tls.do_handshake()
                    return True
        except (OSError, ssl.SSLError) as e:
            if on_progress is not None:
                on_progress(e)
            time.sleep(poll_s)
    return False


def mint_seed(provider: str, region: str, *, canary: bool,
              agent_source_url: str, agent_source_sha256: str,
              descriptor_refresh_url: str, cloud_init_out: str,
              db_path: str = _DEFAULT_DB,
              config: str = _DEFAULT_CONFIG) -> str:
    """Run provision-seed; write the stdout cloud-init bundle to cloud_init_out
    (mode 0600); return the box_id parsed from stderr.

    provision-seed has NO --cloud-init-out flag — it prints the bundle to
    stdout. We capture both streams (stdout = bundle, stderr = box_id line)
    via _run_controller_capture_both."""
    argv = [
        "provision-seed",
        "--provider", provider, "--region", region,
        "--agent-source-url", agent_source_url,
        "--agent-source-sha256", agent_source_sha256,
        "--descriptor-refresh-url", descriptor_refresh_url,
        "--db-path", db_path, "--config", config,
    ]
    if canary:
        argv.append("--canary")
    res = _run_controller_capture_both(*argv)
    box_id = _extract_box_id(res.stderr or "")
    if not box_id:
        raise RuntimeError(
            "provision-seed succeeded but emitted no 'box_id=' line "
            "(controller version mismatch?)")
    out = Path(cloud_init_out)
    out.write_text(res.stdout or "")
    with contextlib.suppress(OSError):  # best-effort; tmpfs / non-owner cases
        out.chmod(0o600)
    return box_id


def mark_live(box_id: str, public_ip: str,
              *, db_path: str = _DEFAULT_DB) -> None:
    """Flip a provisioning box to live."""
    _run_controller("ru-box-mark-live", box_id,
                    "--public-ip", public_ip, "--db-path", db_path)


@dataclass(frozen=True)
class SoakResult:
    passed: bool
    reasons: list[str]
    duration_s: int


def wait_for_soak(image_version: str, *, poll_interval_s: int,
                  on_progress: Callable[[list[str]], None],
                  state_writer: Callable[[], None],
                  db_path: str = _DEFAULT_DB,
                  config: str = _DEFAULT_CONFIG) -> SoakResult:
    """Poll image-promote-status until passed=True. KeyboardInterrupt is
    propagated (caller catches and prints a resume hint); state_writer is
    called each tick so a Ctrl-C lands the latest progress on disk.

    image-promote-status takes image_version as a positional + --db-path +
    --config + --json."""
    started = time.monotonic()
    while True:
        res = _run_controller(
            "image-promote-status", image_version,
            "--db-path", db_path, "--config", config, "--json",
            capture=True)
        try:
            payload = json.loads(res.stdout or "{}")
        except json.JSONDecodeError:
            payload = {"passed": False, "reasons": ["malformed status JSON"]}
        reasons = list(payload.get("reasons") or [])
        on_progress(reasons)
        state_writer()
        if payload.get("passed"):
            return SoakResult(True, reasons,
                              int(time.monotonic() - started))
        time.sleep(poll_interval_s)


def box_info(box_id: str, *, db_path: str = _DEFAULT_DB) -> dict | None:
    """Return the ru_boxes row for `box_id` from ru-box-list, or None.
    Row schema (from controller/state/ru_boxes.py): box_id, provider, region,
    public_ip, sni, shard_id, state, image_version, ..."""
    res = _run_controller("ru-box-list", "--json",
                          "--db-path", db_path, capture=True)
    try:
        rows = json.loads(res.stdout or "[]")
    except json.JSONDecodeError:
        return None
    for row in rows:
        if row.get("box_id") == box_id:
            return row
    return None
