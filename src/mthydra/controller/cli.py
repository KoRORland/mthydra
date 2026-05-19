"""Controller CLI — subcommands: init, startup-check, backup-now, restore,
adopt-restored-state, obligation-proven.

Global flag --mode {production|dryrun|offline} per plan §16.2.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mthydra.controller.bootstrap import BootstrapError, init_state
from mthydra.controller.restore.adopt import AdoptError, adopt_restored_state
from mthydra.controller.restore.decrypt import DecryptError, decrypt_blob
from mthydra.controller.restore.summary import summarize_db
from mthydra.controller.startup import run_startup_checks
from mthydra.controller.state.db import connect
from mthydra.controller.state.obligations import prove


DEFAULT_DB = "/var/lib/mthydra/state.sqlite"
DEFAULT_RECIPIENT_FILE = "/etc/mthydra/age-recipient.txt"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_recipient(path: str) -> str:
    return Path(path).read_text().strip().splitlines()[0]


def _parse_kv(items: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in (items or []):
        if "=" not in raw:
            raise ValueError(f"expected KEY=VALUE, got {raw!r}")
        k, v = raw.split("=", 1)
        out[k] = v
    return out


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="mthydra-controller")
    p.add_argument(
        "--mode",
        choices=["production", "dryrun", "offline"],
        default=os.environ.get("MTHYDRA_MODE", "production"),
        help="operating mode (default: production; env: MTHYDRA_MODE)",
    )
    p.add_argument(
        "--bucket-override",
        default=os.environ.get("MTHYDRA_BUCKET_OVERRIDE"),
        help="S3 bucket override for dryrun mode (env: MTHYDRA_BUCKET_OVERRIDE)",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    # init
    init_p = sub.add_parser("init", help="initialize a fresh controller DB")
    init_p.add_argument("--db-path", default=DEFAULT_DB)
    g = init_p.add_mutually_exclusive_group(required=True)
    g.add_argument("--age-recipient", help="age public key as a string")
    g.add_argument("--age-recipient-file", help="path to file containing the age public key")
    init_p.add_argument(
        "--provider-credential",
        action="append",
        default=[],
        metavar="PROVIDER=CREDENTIAL",
        help="provider credential (repeatable)",
    )

    # startup-check
    sc_p = sub.add_parser("startup-check", help="run §10 self-checks and exit 0 on success")
    sc_p.add_argument("--db-path", default=DEFAULT_DB)
    sc_p.add_argument("--age-recipient", default=None)
    sc_p.add_argument("--age-recipient-file", default=DEFAULT_RECIPIENT_FILE)

    # backup-now
    bn_p = sub.add_parser("backup-now", help="trigger a manual backup")
    bn_p.add_argument("--db-path", default=DEFAULT_DB)
    bn_p.add_argument("--reason", default="manual")

    # restore
    rst_p = sub.add_parser("restore", help="decrypt + summarize a backup blob")
    rst_p.add_argument("--from", dest="src", required=True, help="encrypted .age blob path")
    rst_p.add_argument("--identity", required=True, help="operator age identity file")
    rst_p.add_argument("--into", required=True, help="output plaintext sqlite path")
    rst_p.add_argument("--summary-only", action="store_true")

    # adopt-restored-state
    adp_p = sub.add_parser("adopt-restored-state", help="install a restored DB as live state")
    adp_p.add_argument("restored_path")
    adp_p.add_argument("--live-path", default=DEFAULT_DB)
    adp_p.add_argument("--case", choices=["A", "B"])
    adp_p.add_argument("--rotate-published-subset", action="store_true")

    # obligation-proven
    op_p = sub.add_parser("obligation-proven", help="stamp an obligation clock as proven now")
    op_p.add_argument("obligation_id")
    op_p.add_argument("--db-path", default=DEFAULT_DB)
    op_p.add_argument("--details", default=None)
    op_p.add_argument(
        "--next-due-hours",
        type=int,
        default=720,
        help="advance next_due_at by this many hours (default 720)",
    )

    return p


def run(argv: list[str]) -> int:
    """Parse argv and dispatch to subcommand handler. Returns exit code."""
    args = build_parser().parse_args(argv)
    mode = args.mode

    # Validate --mode/--bucket-override combination
    if mode == "dryrun" and not args.bucket_override:
        print(
            "error: --mode dryrun requires --bucket-override (or MTHYDRA_BUCKET_OVERRIDE)",
            file=sys.stderr,
        )
        return 1

    if args.cmd == "init":
        recipient = args.age_recipient or _read_recipient(args.age_recipient_file)
        try:
            init_state(
                db_path=args.db_path,
                age_recipient=recipient,
                provider_credentials=_parse_kv(args.provider_credential),
                obligation_timer_hours={
                    "backup_restore_dryrun": 720,
                    "t2_dryrun_caseA": 720,
                    "t2_dryrun_caseB": 720,
                    "t1_dormant_health": 168,
                    "t3_vantage_revalidation": 168,
                    "t3_profile_repin": 0,
                    "t4_upstream_check": 168,
                    "t5_pool_revalidation": 168,
                    "t6_reshuffle": 168,
                },
                now=_now(),
            )
            print(f"initialized {args.db_path}")
            return 0
        except BootstrapError as e:
            print(f"bootstrap error: {e}", file=sys.stderr)
            return 2

    if args.cmd == "startup-check":
        recipient = args.age_recipient or (
            _read_recipient(args.age_recipient_file)
            if Path(args.age_recipient_file).exists()
            else ""
        )
        result = run_startup_checks(
            db_path=args.db_path,
            age_recipient=recipient,
            mode=mode,
            bucket_override=args.bucket_override,
        )
        if result.ok:
            if mode != "production":
                print(
                    f"STARTUP WARNING: running in {mode.upper()} mode — backups are NOT production",
                    file=sys.stderr,
                )
            print("startup-check: OK")
            return 0
        print(
            f"startup-check FAILED [{result.failed_check}]: {result.message}", file=sys.stderr
        )
        return 10

    if args.cmd == "backup-now":
        # Until spec J ships, this CLI notifies via stdout only.
        # Spec J will add a real IPC signal to the running daemon.
        print(
            f"backup-now: signalled (trigger=manual, reason={args.reason!r}). "
            "The running daemon handles the S3 push; this CLI does not invoke S3 directly.",
        )
        return 0

    if args.cmd == "restore":
        plain = Path(args.into)
        try:
            decrypt_blob(args.src, identity_path=args.identity, out=plain)
        except DecryptError as e:
            print(f"restore: {e}", file=sys.stderr)
            return 3
        summary = summarize_db(plain)
        print(json.dumps(summary, indent=2, default=str))
        if args.summary_only:
            plain.unlink(missing_ok=True)
        return 0

    if args.cmd == "adopt-restored-state":
        try:
            adopt_restored_state(
                live_path=args.live_path,
                restored_path=args.restored_path,
                case=args.case,
                rotate_published_subset=args.rotate_published_subset,
                at=_now(),
            )
            print("adopted")
            return 0
        except AdoptError as e:
            print(f"adopt: {e}", file=sys.stderr)
            return 4

    if args.cmd == "obligation-proven":
        now_dt = datetime.now(timezone.utc)
        next_due = (now_dt + timedelta(hours=args.next_due_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn = connect(args.db_path)
        try:
            try:
                prove(
                    conn,
                    obligation_id=args.obligation_id,
                    proven_by="operator",
                    at=now_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    next_due_at=next_due,
                    details=args.details,
                )
            except KeyError as e:
                print(f"unknown obligation: {e}", file=sys.stderr)
                return 5
        finally:
            conn.close()
        print(f"stamped {args.obligation_id}")
        return 0

    return 1
