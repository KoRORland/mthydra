"""mthydra-ops — operator-side automation over mthydra-controller.

Wraps the runbook's most error-prone or repetitive procedures. Each
subcommand corresponds to a runbook section and:

  - prints the steps it is about to take
  - refuses to proceed if a prerequisite is missing
  - calls mthydra-controller via subprocess (decoupled from internal API)
  - returns non-zero on failure with the underlying command's error

Subcommands:

  setup-host            (runbook §1.1)   apt + user + dirs (root only)
  gen-age-key           (runbook §1.2)   operator laptop; prints pubkey
  bootstrap             (runbook §1.5–7) init DB + write controller.toml
  preflight             (runbook §1.8)   obs-alert-test crit + heartbeat
  daily-check           (runbook §2)     obligation + anti + alert summary
  monthly-compact       (runbook §11.3)  compact-logs --before 30d
  image-build-template  (runbook §3.2)   stdout a profile.json skeleton
  user-onboard          (runbook §5.2–4) user-add + channels-set + test
  rotate-vantage        (runbook §7.5)   burn + add + attest a new one
  alert-summary         (runbook §2.2)   obs-status + probe-due + shard-stats

Run `mthydra-ops <subcommand> --help` for details on each.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path


# Defaults — override via flags or env.
_DEFAULT_DB = os.environ.get("MTHYDRA_DB_PATH", "/var/lib/mthydra/state.sqlite")
_DEFAULT_CONFIG = os.environ.get(
    "MTHYDRA_CONFIG", "/etc/mthydra/controller.toml"
)
_CONTROLLER_BIN = os.environ.get("MTHYDRA_CONTROLLER", "mthydra-controller")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _say(msg: str) -> None:
    print(f"[mthydra-ops] {msg}", flush=True)


def _err(msg: str) -> None:
    print(f"[mthydra-ops] ERROR: {msg}", file=sys.stderr, flush=True)


def _run_controller(
    *args: str,
    check: bool = True,
    capture: bool = False,
    env: dict | None = None,
) -> subprocess.CompletedProcess:
    """Invoke mthydra-controller. Reraises CalledProcessError on non-zero.

    `env`, when given, replaces the child environment — used to pass secrets
    (e.g. the B2 app key) out of band so they never appear on argv / `ps`.
    """
    cmd = [_CONTROLLER_BIN, *args]
    _say(f"$ {' '.join(cmd)}")
    return subprocess.run(
        cmd, check=check,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        text=True,
        env=env,
    )


def _run_controller_capture_both(*args: str) -> subprocess.CompletedProcess:
    """Capture both stdout and stderr. ru-provision uses stderr to extract
    the box_id printed by provision-seed."""
    cmd = [_CONTROLLER_BIN, *args]
    _say(f"$ {' '.join(cmd)}")
    return subprocess.run(
        cmd, check=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


def _confirm(question: str, default_no: bool = True) -> bool:
    """Interactive y/N. In non-TTY context, refuse unless --yes was passed."""
    if not sys.stdin.isatty():
        return False
    suffix = " [y/N] " if default_no else " [Y/n] "
    ans = input(f"{question}{suffix}").strip().lower()
    if not ans:
        return not default_no
    return ans in ("y", "yes")


# ---------------------------------------------------------------------------
# setup-host  —  runbook §1.1
# ---------------------------------------------------------------------------


def cmd_setup_host(args: argparse.Namespace) -> int:
    """Idempotent apt install + user + dir layout. Must run as root."""
    if os.geteuid() != 0 and not args.dry_run:
        _err("setup-host must run as root (or pass --dry-run)")
        return 2

    pkgs = ["python3.12", "python3.12-venv", "python3-pip", "git", "age"]
    user = "mthydra"
    dirs = [
        ("/etc/mthydra", "root:root", "0755"),
        ("/var/lib/mthydra", f"{user}:{user}", "0700"),
        ("/var/log/mthydra", f"{user}:{user}", "0755"),
    ]

    # List-form (shell=False) so there is no shell to inject into. Each step is
    # (argv, allow_fail); adduser is allowed to fail (idempotent re-run when the
    # user already exists), replacing the old `|| true`.
    steps: list[tuple[list[str], bool]] = [
        (["apt", "update"], False),
        (["apt", "install", "-y", *pkgs], False),
        (["adduser", "--system", "--group", "--no-create-home", user], True),
    ]
    for path, owner, mode in dirs:
        steps.append((["mkdir", "-p", path], False))
        steps.append((["chown", owner, path], False))
        steps.append((["chmod", mode, path], False))

    if args.dry_run:
        _say("DRY-RUN. Steps that WOULD be executed:")
        for argv, _ in steps:
            print(f"  $ {' '.join(argv)}")
        return 0

    for argv, allow_fail in steps:
        _say(f"$ {' '.join(argv)}")
        rc = subprocess.run(argv).returncode
        if rc != 0 and not allow_fail:
            _err(f"step failed (rc={rc}): {' '.join(argv)}")
            return rc

    _say("setup-host: complete. Next: mthydra-ops gen-age-key (on laptop, not here).")
    return 0


# ---------------------------------------------------------------------------
# gen-age-key  —  runbook §1.2
# ---------------------------------------------------------------------------


def cmd_gen_age_key(args: argparse.Namespace) -> int:
    """Generate the operator's age key. STRONGLY warns against EU host."""
    if not shutil.which("age-keygen"):
        _err("age-keygen not on PATH; install `age` first")
        return 2

    out_path = Path(args.out).expanduser()
    if out_path.exists() and not args.force:
        _err(f"refusing to overwrite existing key at {out_path}; pass --force to override")
        return 2

    # Heuristic: a hostname containing dots is probably a server hostname; this
    # is a soft warning, not a refusal.
    hostname = os.uname().nodename
    if "." in hostname and not args.yes:
        _say(f"WARNING: hostname {hostname!r} looks server-like.")
        _say("The operator age key MUST live on your laptop, NOT on a deployed host.")
        if not _confirm("Continue anyway?", default_no=True):
            _err("aborted by operator")
            return 2

    out_path.parent.mkdir(parents=True, exist_ok=True)
    rc = subprocess.call(["age-keygen", "-o", str(out_path)])
    if rc != 0:
        return rc
    os.chmod(out_path, 0o600)

    pub = None
    for line in out_path.read_text().splitlines():
        if line.startswith("# public key:"):
            pub = line.split("# public key:", 1)[1].strip()
            break
    if pub:
        _say(f"public key: {pub}")
        _say("Copy this public key to the EU host as the --age-recipient on bootstrap.")
    else:
        _err("could not find public key in generated file")
        return 1
    _say(f"private key saved to {out_path} (mode 0600).")
    _say("Back up to TWO non-cloud locations per runbook §1.2.")
    return 0


# ---------------------------------------------------------------------------
# bootstrap  —  runbook §1.5–§1.7
# ---------------------------------------------------------------------------


_TOML_TEMPLATE = """\
# Generated by mthydra-ops bootstrap on {timestamp}
[node]
role     = "active"
hostname = "{hostname}"

[backup]
floor_interval_hours        = 24
on_change_debounce_seconds  = 30
endpoint                    = "{b2_endpoint}"
bucket                      = "{b2_bucket}"
access_key_id               = "{b2_key_id}"

[backup.retention]
keep_daily       = 30
keep_monthly     = 12
object_lock_days = 30

[gap_monitor]
poll_interval_minutes   = 30
alarm_threshold_hours   = 48
recipient_email         = "{operator_email}"

[descriptor]
rotation_interval_hours = 1
validity_window_hours   = 24

[obligations]
[obligations.timers_hours]

[cover_pool]
rotation_ttl_days            = 14
reverify_after_days          = 30
freeze_threshold             = 2
reverify_sweep_interval      = "1h"
rotation_sweep_interval      = "1h"
replenishment_interval_days  = 90

[standby]
node_id                       = ""
heartbeat_interval_seconds    = 60
heartbeat_poll_interval       = "5m"
staleness_alert_seconds       = 600

[image]
upstream_repo            = "9seconds/mtg"
upstream_release_asset   = "mtg-linux-amd64"
upstream_check_interval  = "168h"
github_api_url           = "https://api.github.com"
build_tmp_dir            = "/var/lib/mthydra/tmp"

[image.canary]
min_boxes          = 1
min_cycles_per_box = 4

[shard_manager]
target_size               = 2
max_size                  = 3
reshuffle_interval_days   = 14
reshuffle_sweep_interval  = "1h"

[probe]
soft_fail_window_M           = 4
soft_fail_threshold_N        = 3
min_distinct_vantages        = 2
coverage_window_seconds      = 3600
probe_vantage_ttl_days       = 14
probe_audit_sweep_interval   = "5m"

[observability]
alerter_sweep_interval              = "2m"
heartbeat_interval                  = "1h"
heartbeat_breach_threshold          = 3
alert_dedupe_window_warn_seconds    = 3600
alert_dedupe_window_crit_seconds    = 900
alert_dedupe_window_info_seconds    = 21600

[observability.telegram]
bot_token = "{obs_tg_bot_token}"
chat_id   = "{obs_tg_chat_id}"

[observability.email]
smtp_host = "{obs_smtp_host}"
smtp_port = {obs_smtp_port}
from_addr = "{obs_smtp_from}"
to_addr   = "{obs_smtp_to}"
username  = "{obs_smtp_user}"
password  = "{obs_smtp_pass}"

[distribution]
publish_sweep_interval     = "5m"
user_heartbeat_interval    = "24h"
heartbeat_breach_threshold = 3

[distribution.telegram]
bot_token = "{dist_tg_bot_token}"

[distribution.email]
smtp_host = "{dist_smtp_host}"
smtp_port = {dist_smtp_port}
from_addr = "{dist_smtp_from}"
username  = "{dist_smtp_user}"
password  = "{dist_smtp_pass}"
"""


def cmd_bootstrap(args: argparse.Namespace) -> int:
    """Init DB + write controller.toml + migrate authority. Runs as mthydra user."""
    from datetime import datetime, timezone

    db_path = Path(args.db_path)
    cfg_path = Path(args.config)

    if db_path.exists() and not args.force:
        _err(f"DB already exists at {db_path}; pass --force only if you know what that does")
        return 2

    # B2 app key is a secret: read from the environment so it never lands on
    # argv (where `ps` would expose it) — neither ours nor the controller's.
    b2_app_key = os.environ.get("B2_APPLICATION_KEY")
    if not b2_app_key:
        _err("B2_APPLICATION_KEY must be set in the environment "
             "(kept off argv to avoid `ps` credential leak)")
        return 2

    # Step 1: init. The credential is handed to the controller via the child
    # environment, not argv, using --provider-credential-env.
    _say("step 1/3: init controller state")
    child_env = {**os.environ, "MTHYDRA_INIT_B2_CREDENTIAL": f"{args.b2_key_id}:{b2_app_key}"}
    init_args = [
        "init",
        "--db-path", str(db_path),
        "--age-recipient", args.age_recipient,
        "--provider-credential-env", "b2=MTHYDRA_INIT_B2_CREDENTIAL",
    ]
    try:
        _run_controller(*init_args, env=child_env)
    except subprocess.CalledProcessError as e:
        _err(f"init failed: {e}")
        return e.returncode

    # Step 2: migrate authority off placeholder
    _say("step 2/3: migrate credential authority off placeholder")
    try:
        _run_controller("authority-migrate-placeholder",
                          "--db-path", str(db_path))
    except subprocess.CalledProcessError as e:
        _err(f"authority-migrate-placeholder failed: {e}")
        return e.returncode

    # Step 3: write controller.toml
    _say(f"step 3/3: write {cfg_path}")
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    toml = _TOML_TEMPLATE.format(
        timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        hostname=args.hostname,
        b2_endpoint=args.b2_endpoint,
        b2_bucket=args.b2_bucket,
        b2_key_id=args.b2_key_id,
        operator_email=args.operator_email,
        obs_tg_bot_token=args.obs_tg_bot_token,
        obs_tg_chat_id=args.obs_tg_chat_id,
        obs_smtp_host=args.obs_smtp_host,
        obs_smtp_port=args.obs_smtp_port,
        obs_smtp_from=args.obs_smtp_from,
        obs_smtp_to=args.obs_smtp_to,
        obs_smtp_user=args.obs_smtp_user,
        obs_smtp_pass=args.obs_smtp_pass,
        dist_tg_bot_token=args.dist_tg_bot_token,
        dist_smtp_host=args.dist_smtp_host,
        dist_smtp_port=args.dist_smtp_port,
        dist_smtp_from=args.dist_smtp_from,
        dist_smtp_user=args.dist_smtp_user,
        dist_smtp_pass=args.dist_smtp_pass,
    )
    cfg_path.write_text(toml)
    os.chmod(cfg_path, 0o600)
    _say(f"wrote {cfg_path} (0600).")
    _say("Next: mthydra-ops preflight  (validates both alert sinks)")
    return 0


# ---------------------------------------------------------------------------
# preflight  —  runbook §1.8
# ---------------------------------------------------------------------------


def cmd_preflight(args: argparse.Namespace) -> int:
    """Validate both alert sinks AND heartbeat. Refuse to declare ready otherwise."""
    msg = f"deploy-time crit test from {os.uname().nodename}"
    _say("step 1/3: alert sinks (Telegram + email)")
    try:
        _run_controller(
            "obs-alert-test",
            "--severity", "crit",
            "--message", msg,
            "--db-path", args.db_path,
            "--config", args.config,
        )
    except subprocess.CalledProcessError as e:
        _err(f"obs-alert-test failed: {e}")
        return e.returncode

    _say("step 2/3: heartbeat email")
    try:
        _run_controller(
            "obs-heartbeat-now",
            "--db-path", args.db_path,
            "--config", args.config,
        )
    except subprocess.CalledProcessError as e:
        _err(f"obs-heartbeat-now failed: {e}")
        return e.returncode

    _say("step 3/3: startup-check")
    try:
        _run_controller(
            "startup-check",
            "--db-path", args.db_path,
        )
    except subprocess.CalledProcessError as e:
        _err(f"startup-check failed: {e}")
        return e.returncode

    _say("preflight PASSED.")
    _say(
        "Now CONFIRM out-of-band that the test message arrived in BOTH:\n"
        "  - your operator-alert Telegram chat\n"
        "  - your operator email inbox (check spam folder; whitelist From if needed)\n"
        "If either is silent, fix [observability.X] in controller.toml and re-run preflight."
    )
    return 0


# ---------------------------------------------------------------------------
# daily-check  —  runbook §2
# ---------------------------------------------------------------------------


def cmd_daily_check(args: argparse.Namespace) -> int:
    """Quick summary of obligations + anti-obligations + recent alerts."""
    try:
        res = _run_controller(
            "obs-status", "--json", "--db-path", args.db_path,
            capture=True,
        )
    except subprocess.CalledProcessError as e:
        _err(f"obs-status failed: {e}")
        return e.returncode

    snap = json.loads(res.stdout)
    overdue = snap.get("obligations_overdue", [])
    anti = snap.get("anti_obligations", [])
    counts = snap.get("counts", {})

    print(f"\n== mthydra daily check @ {snap.get('collected_at')} ==\n")
    print(f"  summary: {snap.get('summary_line')}")
    print(f"  boxes: provisioning={counts.get('boxes_provisioning')} "
          f"live={counts.get('boxes_live')} "
          f"terminated={counts.get('boxes_terminated')}")
    print(f"  cover domains: in_use={counts.get('cover_domains_in_use')} "
          f"burned={counts.get('cover_domains_burned')}")
    print(f"  active vantages: {counts.get('active_vantages')}")
    print(f"  active shards: {counts.get('active_shards')}")

    if overdue:
        print("\n  OVERDUE OBLIGATIONS (act on these):")
        for o in overdue:
            print(f"    [{o['severity']}] {o['obligation_id']} "
                  f"overdue {o['overdue_seconds']}s")

    if anti:
        print("\n  ANTI-OBLIGATIONS (currently broken):")
        for a in anti:
            target = f" :: {a['target']}" if a.get('target') else ""
            print(f"    [{a['severity']}] {a['kind']}{target}")

    if not overdue and not anti:
        print("\n  ALL GREEN.")

    # Recent failed alert log rows.
    try:
        res2 = _run_controller(
            "obs-alerts-recent", "--limit", "30", "--json",
            "--db-path", args.db_path, capture=True,
        )
        rows = json.loads(res2.stdout)
        failed = [r for r in rows if r.get("delivered_at") is None]
        if failed:
            print("\n  RECENT SILENT-DELIVERY EVENTS:")
            for r in failed[:10]:
                print(f"    #{r['id']} {r['attempted_at']} sink={r['sink']} "
                      f"kind={r['kind']} err={r.get('error')!r}")
    except subprocess.CalledProcessError:
        pass  # non-fatal

    print()
    return 0 if not overdue and not any(a.get("severity") == "crit" for a in anti) else 1


# ---------------------------------------------------------------------------
# monthly-compact  —  runbook §11.3
# ---------------------------------------------------------------------------


def cmd_monthly_compact(args: argparse.Namespace) -> int:
    """Compact log rows older than --days (default 30). Dry-run first."""
    from datetime import datetime, timedelta, timezone

    cutoff = (datetime.now(timezone.utc) - timedelta(days=args.days)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    _say(f"step 1/2: dry-run compaction (cutoff={cutoff})")
    try:
        _run_controller(
            "compact-logs", "--table", args.table,
            "--before", cutoff,
            "--db-path", args.db_path,
        )
    except subprocess.CalledProcessError as e:
        _err(f"dry-run failed: {e}")
        return e.returncode

    if args.no_dry_run:
        evidence = args.evidence or f"monthly retention purge older than {args.days}d"
        _say(f"step 2/2: real compaction (evidence={evidence!r})")
        try:
            _run_controller(
                "compact-logs", "--table", args.table,
                "--before", cutoff, "--no-dry-run",
                "--evidence", evidence,
                "--db-path", args.db_path,
            )
        except subprocess.CalledProcessError as e:
            _err(f"compaction failed: {e}")
            return e.returncode
    else:
        _say("DRY-RUN only. Re-run with --no-dry-run --evidence '...' to actually delete.")
    return 0


# ---------------------------------------------------------------------------
# image-build-template  —  runbook §3.2
# ---------------------------------------------------------------------------


_PROFILE_SKELETON = {
    "image_version": "REPLACE_WITH_TAG",
    "transport_build_hash": "REPLACE_WITH_BINARY_SHA256",
    "tls_handshake": {
        "expected_cipher_order": [
            "TLS_AES_128_GCM_SHA256",
            "TLS_AES_256_GCM_SHA384",
            "TLS_CHACHA20_POLY1305_SHA256",
        ],
        "expected_extensions": [
            "server_name",
            "supported_versions",
            "key_share",
            "supported_groups",
        ],
    },
    "malformed_input_response": {
        "tcp_reset_within_ms": 250,
        "no_application_layer_response": True,
    },
    "expected_surface": [443],
    "baseline_latency_ms": {"p50": 50, "p95": 200},
    "notes": "REPLACE_WITH_CAPTURE_NOTES (date, vantage, what was probed)",
}


def cmd_image_build_template(args: argparse.Namespace) -> int:
    """Print a known-good profile JSON skeleton to stdout for the operator to edit."""
    print(json.dumps(_PROFILE_SKELETON, indent=2, sort_keys=True))
    return 0


# ---------------------------------------------------------------------------
# user-onboard  —  runbook §5.2 + §5.4
# ---------------------------------------------------------------------------


def cmd_user_onboard(args: argparse.Namespace) -> int:
    """user-add + user-channels-set + dist-test."""
    _say(f"step 1/3: register user {args.user_id!r}")
    add_args = ["user-add", args.user_id,
                "--out-of-band-channel", args.out_of_band,
                "--db-path", args.db_path]
    if args.display_name:
        add_args.extend(["--display-name", args.display_name])
    try:
        _run_controller(*add_args)
    except subprocess.CalledProcessError as e:
        _err(f"user-add failed: {e}")
        return e.returncode

    _say("step 2/3: register channels")
    cs_args = ["user-channels-set", args.user_id,
                "--db-path", args.db_path]
    if args.chat_id:
        cs_args.extend(["--telegram", args.chat_id])
    if args.email:
        cs_args.extend(["--email", args.email])
    try:
        _run_controller(*cs_args)
    except subprocess.CalledProcessError as e:
        _err(f"user-channels-set failed: {e}")
        return e.returncode

    _say("step 3/3: dist-test (send a synthetic message; confirm out-of-band)")
    try:
        _run_controller(
            "dist-test", "--user-id", args.user_id,
            "--db-path", args.db_path,
            "--config", args.config,
        )
    except subprocess.CalledProcessError as e:
        _err(f"dist-test failed: {e}")
        return e.returncode

    _say(
        "user-onboard: complete. Confirm OUT-OF-BAND that the user received the "
        "test in BOTH Telegram AND email."
    )
    return 0


# ---------------------------------------------------------------------------
# rotate-vantage  —  runbook §7.5
# ---------------------------------------------------------------------------


def cmd_rotate_vantage(args: argparse.Namespace) -> int:
    """Burn the old vantage; add + attest a new one. Atomic from the operator side."""
    _say(f"step 1/3: burn {args.old_vantage!r}")
    try:
        _run_controller(
            "vantage-burn", args.old_vantage,
            "--reason", args.burn_reason,
            "--db-path", args.db_path,
        )
    except subprocess.CalledProcessError as e:
        _err(f"vantage-burn failed: {e}")
        return e.returncode

    _say(f"step 2/3: add {args.new_vantage!r}")
    add_args = [
        "vantage-add", args.new_vantage,
        "--label", args.new_label,
        "--source-kind", args.source_kind,
        "--db-path", args.db_path,
    ]
    if args.region_hint:
        add_args.extend(["--region-hint", args.region_hint])
    try:
        _run_controller(*add_args)
    except subprocess.CalledProcessError as e:
        _err(f"vantage-add failed: {e}")
        return e.returncode

    _say(f"step 3/3: attest {args.new_vantage!r} as active")
    try:
        _run_controller(
            "vantage-attest-active", args.new_vantage,
            "--evidence", args.attest_evidence,
            "--db-path", args.db_path,
        )
    except subprocess.CalledProcessError as e:
        _err(f"vantage-attest-active failed: {e}")
        return e.returncode

    _say("rotate-vantage: complete.")
    return 0


# ---------------------------------------------------------------------------
# alert-summary  —  runbook §2.2
# ---------------------------------------------------------------------------


def cmd_alert_summary(args: argparse.Namespace) -> int:
    """Aggregate obs-status + probe-due + shard-stats into one operator view."""
    for cmd in ("obs-status", "probe-due", "shard-stats"):
        cmd_args = [cmd, "--json", "--db-path", args.db_path]
        if cmd in ("shard-stats",) and args.config:
            cmd_args.extend(["--config", args.config])
        print(f"\n=== {cmd} ===")
        try:
            res = _run_controller(*cmd_args, capture=True, check=False)
            if res.returncode != 0:
                print(f"  (skipped — rc={res.returncode})")
                continue
            obj = json.loads(res.stdout)
            print(textwrap.indent(json.dumps(obj, indent=2, sort_keys=True), "  "))
        except (json.JSONDecodeError, subprocess.CalledProcessError) as e:
            print(f"  (error: {e})")
    return 0


# ---------------------------------------------------------------------------
# ru-provision  —  runbook §3.3 + §5 (the unified one-shot RU node setup)
# ---------------------------------------------------------------------------


_BOX_ID_RE = None  # lazily compiled


def _extract_box_id(stderr_text: str) -> str | None:
    """Pull the box_id out of provision-seed's stderr line.

    Format: 'provision-seed: created box_id=<uuid>'
    """
    import re
    global _BOX_ID_RE
    if _BOX_ID_RE is None:
        _BOX_ID_RE = re.compile(r"provision-seed: created box_id=(\S+)")
    for line in stderr_text.splitlines():
        m = _BOX_ID_RE.search(line)
        if m:
            return m.group(1)
    return None


def cmd_ru_provision(args) -> int:
    """Mint a provision seed and print cloud-init + the mark-live command.

    RU boxes must run on hosters reachable from inside Russia's filtered
    network — i.e. Russian providers (Selectel, Timeweb, FirstVDS, VK
    Cloud, Reg.ru, ...) or CIS-adjacent ones. Each has its own API and
    most require Russian-side KYC, so this command stops at producing the
    cloud-init bundle; the operator boots the VM on their provider of
    choice, then runs the printed `ru-box-mark-live` command.

    --provider is a free-text tag stored in ru_boxes.provider for
    inventory (e.g. 'selectel', 'timeweb'). --region likewise.
    """
    provision_args = [
        "provision-seed",
        "--provider", args.provider,
        "--region", args.region,
        "--format", "cloud-init",
        "--ttl-seconds", str(args.ttl_seconds),
        "--db-path", args.db_path,
        "--config", args.config,
        "--agent-source-url", args.agent_source_url,
        "--agent-source-sha256", args.agent_source_sha256,
        "--descriptor-refresh-url", args.descriptor_refresh_url,
    ]
    if args.is_canary:
        provision_args.append("--canary")

    try:
        res = _run_controller_capture_both(*provision_args)
    except subprocess.CalledProcessError as e:
        _err(f"provision-seed failed: {e.stderr.strip() if e.stderr else e}")
        return e.returncode

    cloud_init = res.stdout
    box_id = _extract_box_id(res.stderr)
    if not box_id:
        _err(
            "provision-seed succeeded but stderr did not contain "
            "'provision-seed: created box_id=...' — older controller? "
            "Fall back to manual marking via ru-box-list."
        )
        return 4

    _say(f"box_id minted: {box_id}")

    if args.cloud_init_out:
        out_path = Path(args.cloud_init_out)
        out_path.write_text(cloud_init)
        try:
            out_path.chmod(0o600)
        except OSError:
            pass
        _say(f"cloud-init written to {out_path} (mode 0600)")
    else:
        print(cloud_init)

    _say(
        "\nNext steps:\n"
        f"  1. Boot a VM on your Russian/CIS hoster of choice, supplying "
        f"the cloud-init above as user-data.\n"
        f"     (Reminder: Hetzner/AWS/GCP do NOT operate in cordoned RU "
        f"environments — use Selectel, Timeweb, FirstVDS, VK Cloud, etc.)\n"
        f"  2. Once the VM has a public IPv4, mark it live:\n"
        f"       mthydra-controller ru-box-mark-live {box_id} "
        f"--public-ip <VM-IP> --db-path {args.db_path}\n"
        f"  3. The RU agent should call home within ~10 minutes; confirm with:\n"
        f"       mthydra-controller ru-box-list --json | jq '.[] | "
        f"select(.box_id==\"{box_id}\")'"
    )
    return 0


# ---------------------------------------------------------------------------
# main parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mthydra-ops",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("setup-host", help="install OS deps + create user + dirs (root)")
    sp.add_argument("--dry-run", action="store_true", help="print steps without executing")

    gak = sub.add_parser("gen-age-key", help="generate operator age key (laptop only)")
    gak.add_argument("--out", default="~/.config/mthydra/operator.age",
                      help="output path for the private key")
    gak.add_argument("--force", action="store_true",
                      help="overwrite if exists")
    gak.add_argument("--yes", action="store_true",
                      help="skip the laptop-hostname warning")

    bs = sub.add_parser("bootstrap", help="init DB + write controller.toml + migrate authority")
    bs.add_argument("--db-path", default=_DEFAULT_DB)
    bs.add_argument("--config", default=_DEFAULT_CONFIG)
    bs.add_argument("--force", action="store_true",
                     help="proceed even if DB exists (DANGEROUS)")
    # Credentials + addresses (all required)
    bs.add_argument("--age-recipient", required=True,
                     help="operator's age public key (age1...)")
    bs.add_argument("--hostname", required=True)
    bs.add_argument("--operator-email", required=True,
                     help="recipient for [gap_monitor].recipient_email")
    bs.add_argument("--b2-key-id", required=True,
                     help="B2 key id (identifier, not secret). The matching "
                          "application key (secret) is read from the "
                          "B2_APPLICATION_KEY environment variable.")
    bs.add_argument("--b2-bucket", required=True)
    bs.add_argument("--b2-endpoint", required=True)
    bs.add_argument("--obs-tg-bot-token", required=True)
    bs.add_argument("--obs-tg-chat-id", required=True)
    bs.add_argument("--obs-smtp-host", required=True)
    bs.add_argument("--obs-smtp-port", type=int, default=587)
    bs.add_argument("--obs-smtp-from", required=True)
    bs.add_argument("--obs-smtp-to", required=True)
    bs.add_argument("--obs-smtp-user", required=True)
    bs.add_argument("--obs-smtp-pass", required=True)
    bs.add_argument("--dist-tg-bot-token", required=True)
    bs.add_argument("--dist-smtp-host", required=True)
    bs.add_argument("--dist-smtp-port", type=int, default=587)
    bs.add_argument("--dist-smtp-from", required=True)
    bs.add_argument("--dist-smtp-user", required=True)
    bs.add_argument("--dist-smtp-pass", required=True)

    pf = sub.add_parser("preflight",
                          help="run obs-alert-test crit + heartbeat + startup-check")
    pf.add_argument("--db-path", default=_DEFAULT_DB)
    pf.add_argument("--config", default=_DEFAULT_CONFIG)

    dc = sub.add_parser("daily-check",
                          help="obligation/anti/recent-alert summary; exit 1 if any crit anti-obligation")
    dc.add_argument("--db-path", default=_DEFAULT_DB)

    mc = sub.add_parser("monthly-compact",
                          help="dry-run compact-logs; real run with --no-dry-run --evidence")
    mc.add_argument("--db-path", default=_DEFAULT_DB)
    mc.add_argument("--days", type=int, default=30,
                     help="delete rows older than this many days")
    mc.add_argument("--table", default="all",
                     choices=["alert_log", "probe_results", "distribution_log",
                              "alert_acks", "all"])
    mc.add_argument("--no-dry-run", action="store_true",
                     help="actually delete rows")
    mc.add_argument("--evidence", default=None)

    sub.add_parser("image-build-template",
                     help="print a known-good profile JSON skeleton to stdout")

    uo = sub.add_parser("user-onboard",
                          help="user-add + user-channels-set + dist-test")
    uo.add_argument("user_id")
    uo.add_argument("--out-of-band", required=True,
                     help="how to reach the user without Telegram (e.g. 'signal:+1...')")
    uo.add_argument("--display-name", default=None)
    uo.add_argument("--chat-id", default=None,
                     help="Telegram chat_id for the distribution bot")
    uo.add_argument("--email", default=None)
    uo.add_argument("--db-path", default=_DEFAULT_DB)
    uo.add_argument("--config", default=_DEFAULT_CONFIG)

    rv = sub.add_parser("rotate-vantage",
                          help="burn old vantage + add + attest new one")
    rv.add_argument("--old", required=True, dest="old_vantage")
    rv.add_argument("--new", required=True, dest="new_vantage")
    rv.add_argument("--new-label", required=True)
    rv.add_argument("--source-kind", default="cloud-cis")
    rv.add_argument("--region-hint", default=None)
    rv.add_argument("--burn-reason", required=True)
    rv.add_argument("--attest-evidence", required=True)
    rv.add_argument("--db-path", default=_DEFAULT_DB)

    al = sub.add_parser("alert-summary",
                          help="obs-status + probe-due + shard-stats, JSON, one go")
    al.add_argument("--db-path", default=_DEFAULT_DB)
    al.add_argument("--config", default=_DEFAULT_CONFIG)

    rp = sub.add_parser(
        "ru-provision",
        help="mint a provision seed and print cloud-init + mark-live recipe",
    )
    rp.add_argument("--provider", required=True,
                     help="free-text tag for ru_boxes.provider (e.g. selectel, "
                          "timeweb, firstvds, vk-cloud, regru)")
    rp.add_argument("--region", required=True,
                     help="value stored in ru_boxes.region (e.g. ru-moscow-1)")
    rp.add_argument("--canary", action="store_true", dest="is_canary",
                     help="mark the box as is_canary=1 (spec D2 soak cohort)")
    rp.add_argument("--ttl-seconds", type=int, default=3600,
                     help="image_signed_url_ttl_seconds (cloud-init download window)")
    rp.add_argument("--db-path", default=_DEFAULT_DB)
    rp.add_argument("--config", default=_DEFAULT_CONFIG)
    rp.add_argument("--agent-source-url", required=True)
    rp.add_argument("--agent-source-sha256", required=True)
    rp.add_argument("--descriptor-refresh-url", required=True)
    rp.add_argument("--cloud-init-out", default=None,
                     help="write cloud-init to this path (mode 0600) instead "
                          "of stdout — convenient for pasting into a provider "
                          "console via 'cat'")

    return p


_DISPATCH: dict[str, object] = {
    "setup-host": cmd_setup_host,
    "gen-age-key": cmd_gen_age_key,
    "bootstrap": cmd_bootstrap,
    "preflight": cmd_preflight,
    "daily-check": cmd_daily_check,
    "monthly-compact": cmd_monthly_compact,
    "image-build-template": cmd_image_build_template,
    "user-onboard": cmd_user_onboard,
    "rotate-vantage": cmd_rotate_vantage,
    "alert-summary": cmd_alert_summary,
    "ru-provision": cmd_ru_provision,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    fn = _DISPATCH[args.cmd]
    return int(fn(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
