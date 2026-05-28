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
from datetime import UTC, datetime
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


@dataclass(frozen=True)
class CanaryTarget:
    provider: str
    region: str


_say = _main._say
_err = _main._err


def _prompt_public_ip() -> str | None:
    try:
        ans = input("Public IP when VM is up (Ctrl-C to defer): ").strip()
    except (KeyboardInterrupt, EOFError):
        return None
    return ans or None


def cmd_ru_bringup(args) -> int:
    # Phase 1: mint (skipped on --box-id resume).
    if args.box_id:
        box_id = args.box_id
        _say(f"resume: using existing box {box_id}; skipping mint")
    else:
        _say(f"mint: provision-seed for {args.provider}/{args.region}"
             + (" (canary)" if args.canary else ""))
        box_id = mint_seed(
            args.provider, args.region, canary=args.canary,
            agent_source_url=args.agent_source_url,
            agent_source_sha256=args.agent_source_sha256,
            descriptor_refresh_url=args.descriptor_refresh_url,
            cloud_init_out=args.cloud_init_out,
        )
        _say(f"minted box_id={box_id}; cloud-init at {args.cloud_init_out}")

    # Phase 2: boot-handoff. Get public IP.
    public_ip = args.public_ip
    if not public_ip:
        if args.non_interactive:
            _err("--public-ip required in non-interactive mode")
            return 2
        _say("Paste the cloud-init file as user-data in your provider's "
             "console, boot the VM, then come back with the public IP.")
        public_ip = _prompt_public_ip()
        if not public_ip:
            _say(f"deferred. Resume with: mthydra-ops ru-bringup "
                 f"--box-id {box_id} --public-ip <ip>")
            return 0

    # Phase 3: reachability (skip if box already live).
    info = box_info(box_id)
    if info is None:
        _err(f"box {box_id} not found in ru-box-list")
        return 3
    if info.get("state") == "live":
        _say("box already live — skipping reachability + mark-live")
    else:
        sni = info.get("sni") or ""
        _say(f"reachability: waiting for TLS handshake on {public_ip}:443 "
             f"(sni={sni!r}, timeout={args.reach_timeout}s)")
        ok = wait_for_reachable(public_ip, 443, sni,
                                timeout_s=args.reach_timeout,
                                on_progress=lambda e: None)
        if not ok:
            _err(f"box {public_ip}:443 not reachable within "
                 f"{args.reach_timeout}s — check provider firewall + "
                 f"cloud-init logs on the VM")
            return 4
        # Phase 4: mark-live.
        _say(f"mark-live: {box_id} @ {public_ip}")
        mark_live(box_id, public_ip)

    # Phase 5: summary.
    canary_note = ("CANARY — next: §3.4 soak (submit probe-record from "
                   "each registered vantage)" if args.canary
                   else "in rotation")
    _say(f"done: box {box_id} live @ {public_ip}; {canary_note}")
    return 0


def parse_cohort(*, flags: list[str] | None, file_path,
                 expected_count: int) -> list[CanaryTarget]:
    """Accept repeated `provider=X,region=Y` flags OR a file with one such
    line per target. Validates count matches --canaries N.

    Spec O-D9 mentions YAML; we use a stdlib-only "key=value,key=value\\n"
    line format to avoid pulling in PyYAML. The file format is line-oriented
    and human-editable; a future task can add real YAML if needed."""
    raw: list[str] = []
    if flags:
        raw.extend(flags)
    if file_path is not None:
        raw.extend(line.strip() for line in Path(file_path).read_text().splitlines()
                   if line.strip() and not line.lstrip().startswith("#"))
    if len(raw) != expected_count:
        raise ValueError(
            f"cohort size {len(raw)} != canaries={expected_count}")
    targets = []
    for spec in raw:
        kv = dict(part.split("=", 1) for part in spec.split(","))
        targets.append(CanaryTarget(provider=kv["provider"].strip(),
                                    region=kv["region"].strip()))
    return targets


def compose_evidence(state: CycleState, soak_started: str, soak_ended: str) -> str:
    boxes = ", ".join(c["box_id"] for c in state.canaries)
    return (f"soak from {soak_started} to {soak_ended}; canaries: {boxes}; "
            f"cover-site behaviour: stable per probe_results; "
            f"latency baseline within profile bounds")


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def cmd_ru_image_cycle(args) -> int:  # noqa: C901
    state_dir = Path(args.state_dir) if getattr(args, "state_dir", None) \
                else CYCLE_STATE_DIR
    state_dir.mkdir(parents=True, exist_ok=True)
    state_path = state_dir / f"{args.release}.json"

    state = CycleState.load(state_path)  # always pick up prior state if present
    if state is None:
        state = CycleState(
            release=args.release,
            image_version=f"iv-{args.release}",
            profile_path=args.profile_json or "",
            image_built=False,
            canaries=[],
            started_at=_now_iso(),
        )
        state.save(state_path)

    # Phase 1: image-build (skip if already built).
    if not state.image_built:
        _say(f"[1/4] image-build: --release {args.release} "
             f"--profile-json {state.profile_path}")
        _run_controller("image-build", "--release", args.release,
                        "--profile-json", state.profile_path,
                        "--db-path", _DEFAULT_DB, "--config", _DEFAULT_CONFIG)
        state.image_built = True
        state.save(state_path)
    else:
        _say("[1/4] image-build: already built → skip")

    # Phase 2: canary cohort.
    targets = parse_cohort(flags=args.canary_target, file_path=args.cohort,
                           expected_count=args.canaries)
    done_count = sum(1 for c in state.canaries if c.get("marked_live_at"))
    _say(f"[2/4] canaries: {done_count}/{args.canaries} already live")
    for idx, target in enumerate(targets):
        if idx < len(state.canaries) and state.canaries[idx].get("marked_live_at"):
            continue
        # Mint + bring up this canary.
        cloud_init = str(state_dir / f"{args.release}-c{idx + 1}.yaml")
        box_id = mint_seed(target.provider, target.region, canary=True,
                           agent_source_url=args.agent_source_url,
                           agent_source_sha256=args.agent_source_sha256,
                           descriptor_refresh_url=args.descriptor_refresh_url,
                           cloud_init_out=cloud_init)
        # Cycle always prompts for canary IPs as VMs come up — there is no
        # CLI shape for pre-staging N IPs at invocation. `--non-interactive`
        # on the cycle is a no-op for IP collection.
        public_ip = _prompt_public_ip()
        if public_ip is None:
            _say(f"deferred at canary {box_id}. Resume with: "
                 f"mthydra-ops ru-image-cycle --release {args.release} --resume")
            return 0
        info = box_info(box_id) or {}
        if info.get("state") != "live":
            if not wait_for_reachable(public_ip, 443, info.get("sni") or "",
                                      timeout_s=600):
                _err(f"canary {box_id} unreachable; resume later")
                return 4
            mark_live(box_id, public_ip)
        entry = {"box_id": box_id, "provider": target.provider,
                 "region": target.region, "public_ip": public_ip,
                 "marked_live_at": _now_iso()}
        if idx < len(state.canaries):
            state.canaries[idx] = entry
        else:
            state.canaries.append(entry)
        state.save(state_path)

    # Phase 3: soak wait.
    soak_started = _now_iso()
    _say(f"[3/4] soak: polling image-promote-status every {args.soak_poll}s "
         f"(Ctrl-C to defer)")

    def _progress(reasons):
        for r in reasons:
            _say(f"  pending: {r}")

    try:
        result = wait_for_soak(state.image_version,
                               poll_interval_s=args.soak_poll,
                               on_progress=_progress,
                               state_writer=lambda: state.save(state_path))
    except KeyboardInterrupt:
        _say("deferred. Resume with: mthydra-ops ru-image-cycle "
             f"--release {args.release} --resume")
        return 0

    # Phase 4: promote (operator-confirmed unless --promote-yes in tests).
    soak_ended = _now_iso()
    evidence = args.evidence or compose_evidence(state, soak_started, soak_ended)
    if not getattr(args, "promote_yes", False) and not args.non_interactive:
        ans = input(f"soak passed in {result.duration_s}s. "
                    f"Promote {state.image_version}? [y/N] ").strip().lower()
        if ans not in ("y", "yes"):
            _say("promote declined. State preserved; rerun with --resume to retry")
            return 0
    _say(f"[4/4] promote: {state.image_version}")
    _run_controller("image-promote", state.image_version,
                    "--evidence", evidence,
                    "--db-path", _DEFAULT_DB, "--config", _DEFAULT_CONFIG)

    # Phase 5: summary + remove state file.
    _say(f"done: {state.image_version} promoted. "
         f"Existing boxes age out via §3.7 replace-on-burn; "
         f"use `mthydra-ops ru-bringup` for replacements.")
    state_path.unlink(missing_ok=True)
    return 0
