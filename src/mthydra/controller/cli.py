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
from mthydra.controller.image.builder import BuildError, build_image
from mthydra.controller.image.upstream_tracker import UpstreamReleaseTracker
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
    init_p.add_argument(
        "--role",
        choices=["active", "standby"],
        default="active",
        help="initialise as active (default) or standby (skeleton DB)",
    )

    # startup-check
    sc_p = sub.add_parser("startup-check", help="run §10 self-checks and exit 0 on success")
    sc_p.add_argument("--db-path", default=DEFAULT_DB)
    sc_p.add_argument("--age-recipient", default=None)
    sc_p.add_argument("--age-recipient-file", default=DEFAULT_RECIPIENT_FILE)

    # backup-now
    bn_p = sub.add_parser("backup-now", help="run a manual backup immediately")
    bn_p.add_argument("--db-path", default=DEFAULT_DB)
    bn_p.add_argument("--config", default="/etc/mthydra/controller.toml")
    bn_p.add_argument("--tmp-dir", default="/var/lib/mthydra/tmp")
    bn_p.add_argument("--reason", default="manual",
                      help="advisory label stored in backup_log.trigger")

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
    op_p.add_argument("--config", default="/etc/mthydra/controller.toml")
    op_p.add_argument("--details", default=None)
    op_p.add_argument(
        "--next-due-hours",
        type=int,
        default=None,
        help=(
            "advance next_due_at by this many hours; "
            "defaults to the value in controller.toml [obligations.timers_hours], "
            "falling back to 720 if absent"
        ),
    )

    # rotate-provider-credential
    rpc_p = sub.add_parser(
        "rotate-provider-credential",
        help="update a provider credential (e.g. after B2 key rotation)",
    )
    rpc_p.add_argument("provider", help="provider name (e.g. aws, b2)")
    rpc_p.add_argument("--db-path", default=DEFAULT_DB)
    cred_g = rpc_p.add_mutually_exclusive_group(required=True)
    cred_g.add_argument("--credential", help="credential string")
    cred_g.add_argument("--credential-file", help="path to file containing the credential")

    # ----- spec B subcommands -----

    # descriptor-sign-now
    dsn = sub.add_parser("descriptor-sign-now", help="force-sign a new descriptor immediately")
    dsn.add_argument("--db-path", default=DEFAULT_DB)
    dsn.add_argument("--config", default="/etc/mthydra/controller.toml")

    # descriptor-show
    dsh = sub.add_parser("descriptor-show", help="print descriptor payload as pretty JSON")
    dsh.add_argument("--generation", type=int, default=None,
                     help="generation number (default: latest)")
    dsh.add_argument("--db-path", default=DEFAULT_DB)

    # descriptor-verify
    dvf = sub.add_parser("descriptor-verify",
                          help="verify a descriptor file against trusted keys in DB")
    dvf.add_argument("payload_file", help="path to canonical JSON payload bytes")
    dvf.add_argument("sig_file", help="path to raw 64-byte Ed25519 signature")
    dvf.add_argument("--db-path", default=DEFAULT_DB)
    dvf.add_argument("--now", default=None, help="ISO-8601 timestamp override (default: now)")

    # signing-key-rotate
    skr = sub.add_parser("signing-key-rotate",
                          help="generate new descriptor signing key, activate, sign (B-D11)")
    skr.add_argument("--db-path", default=DEFAULT_DB)
    skr.add_argument("--config", default="/etc/mthydra/controller.toml")

    # eu-add
    eua = sub.add_parser("eu-add", help="add an EU exit node to the descriptor exit set")
    eua.add_argument("fingerprint", help="hex SHA256 of EU node public key")
    eua.add_argument("endpoint", help="host:port or opaque transport address")
    eua.add_argument("--weight", type=int, default=1)
    eua.add_argument("--db-path", default=DEFAULT_DB)
    eua.add_argument("--config", default="/etc/mthydra/controller.toml")

    # eu-retire
    eur = sub.add_parser("eu-retire", help="retire an EU exit node from the descriptor exit set")
    eur.add_argument("fingerprint")
    eur.add_argument("--db-path", default=DEFAULT_DB)
    eur.add_argument("--config", default="/etc/mthydra/controller.toml")

    # descriptor-migrate-placeholder
    dmp = sub.add_parser(
        "descriptor-migrate-placeholder",
        help="one-shot: replace spec A placeholder signing key with real Ed25519",
    )
    dmp.add_argument("--db-path", default=DEFAULT_DB)
    dmp.add_argument("--config", default="/etc/mthydra/controller.toml")

    # ----- spec F subcommands -----

    pa = sub.add_parser("promote-active",
                         help="promote this standby to active via T2 §7 atomic state replacement")
    pa.add_argument("--backup-blob", required=True)
    pa.add_argument("--age-identity", required=True)
    pa.add_argument("--case", choices=["A", "B"], required=True)
    pa.add_argument("--db-path", default=DEFAULT_DB)
    pa.add_argument("--config", default="/etc/mthydra/controller.toml")
    pa.add_argument("--yes", action="store_true",
                     help="skip the interactive confirmation prompt")

    ar = sub.add_parser("authority-rotate",
                         help="rotate credential_authority — insert new generation, retire current")
    ar.add_argument("--db-path", default=DEFAULT_DB)
    ar.add_argument("--config", default="/etc/mthydra/controller.toml")

    amp = sub.add_parser("authority-migrate-placeholder",
                          help="replace PRIV-BOOTSTRAP-* authority rows with real Ed25519")
    amp.add_argument("--db-path", default=DEFAULT_DB)
    amp.add_argument("--config", default="/etc/mthydra/controller.toml")

    # serve — long-running daemon stub (spec F will expand this)
    srv_p = sub.add_parser(
        "serve",
        help="run the backup orchestrator daemon (stub; spec F will add full controller plane)",
    )
    srv_p.add_argument("--config", default="/etc/mthydra/controller.toml")
    srv_p.add_argument("--db-path", default=DEFAULT_DB)

    # ----- spec C subcommands -----

    ca = sub.add_parser("cover-add", help="add a candidate cover domain")
    ca.add_argument("domain")
    ca.add_argument("--db-path", default=DEFAULT_DB)
    ca.add_argument("--notes", default=None)

    cav = sub.add_parser("cover-attest-verified",
                          help="operator-attested Russia-vantage verification (spec C C-D1)")
    cav.add_argument("domain")
    cav.add_argument("--vantage", required=True)
    cav.add_argument("--evidence", default=None)
    cav.add_argument("--db-path", default=DEFAULT_DB)

    cl = sub.add_parser("cover-list", help="list cover_domain_pool rows")
    cl.add_argument("--db-path", default=DEFAULT_DB)
    cl.add_argument("--state",
                     choices=["candidate_unverified", "candidate_verified", "in_use"],
                     default=None)
    cl.add_argument("--json", action="store_true")

    cr = sub.add_parser("cover-rotate",
                         help="retire an in_use cover domain (in_use -> burned)")
    cr.add_argument("domain")
    cr.add_argument("--reason", default="manual_rotate")
    cr.add_argument("--db-path", default=DEFAULT_DB)

    cd = sub.add_parser("cover-due",
                         help="show due-for-rotation + stale-verified + pool health")
    cd.add_argument("--db-path", default=DEFAULT_DB)
    cd.add_argument("--config", default="/etc/mthydra/controller.toml")
    cd.add_argument("--json", action="store_true")

    cps = sub.add_parser("cover-pool-stats",
                          help="pool counts + rotation_frozen + oldest ages")
    cps.add_argument("--db-path", default=DEFAULT_DB)
    cps.add_argument("--config", default="/etc/mthydra/controller.toml")
    cps.add_argument("--json", action="store_true")

    # ----- spec F: eu-node inventory subcommands -----

    ena = sub.add_parser("eu-node-add",
                          help="add an EU node to the inventory (default role=standby)")
    ena.add_argument("node_id")
    ena.add_argument("--hostname", required=True)
    ena.add_argument("--provider", required=True)
    ena.add_argument("--region", required=True)
    ena.add_argument("--public-ip", default=None)
    ena.add_argument("--role", choices=["active", "standby"], default="standby")
    ena.add_argument("--notes", default=None)
    ena.add_argument("--db-path", default=DEFAULT_DB)

    enr = sub.add_parser("eu-node-retire",
                          help="retire an EU node (role -> retired)")
    enr.add_argument("node_id")
    enr.add_argument("--db-path", default=DEFAULT_DB)

    enl = sub.add_parser("eu-node-list", help="list eu_nodes inventory")
    enl.add_argument("--state", choices=["active", "standby", "retired"], default=None)
    enl.add_argument("--db-path", default=DEFAULT_DB)
    enl.add_argument("--json", action="store_true")

    # ----- spec D subcommands -----
    ib = sub.add_parser("image-build",
                         help="download upstream release, verify checksum, upload to B2, register candidate")
    ib.add_argument("--release", required=True)
    ib.add_argument("--asset", default=None,
                     help="override asset filename (defaults to cfg.image.upstream_release_asset)")
    ib.add_argument("--notes", default=None)
    ib.add_argument("--db-path", default=DEFAULT_DB)
    ib.add_argument("--config", default="/etc/mthydra/controller.toml")

    il = sub.add_parser("image-list", help="list ru_images catalog")
    il.add_argument("--state", choices=["candidate", "promoted", "retired"], default=None)
    il.add_argument("--db-path", default=DEFAULT_DB)
    il.add_argument("--config", default="/etc/mthydra/controller.toml")
    il.add_argument("--json", action="store_true")

    ip = sub.add_parser("image-promote",
                         help="atomic candidate -> promoted; prior promoted -> retired")
    ip.add_argument("image_version")
    ip.add_argument("--evidence", required=True,
                     help="evidence text (placeholder for D2 validation gate)")
    ip.add_argument("--db-path", default=DEFAULT_DB)

    ir = sub.add_parser("image-retire", help="mark a ru_images row as retired")
    ir.add_argument("image_version")
    ir.add_argument("--reason", required=True)
    ir.add_argument("--db-path", default=DEFAULT_DB)

    ic = sub.add_parser("image-current",
                         help="print the currently-promoted image_version (read-only)")
    ic.add_argument("--db-path", default=DEFAULT_DB)
    ic.add_argument("--config", default="/etc/mthydra/controller.toml")
    ic.add_argument("--json", action="store_true")

    uc = sub.add_parser("upstream-check",
                         help="force an immediate UpstreamReleaseTracker poll")
    uc.add_argument("--db-path", default=DEFAULT_DB)
    uc.add_argument("--config", default="/etc/mthydra/controller.toml")

    # ----- spec F: standby-drill-proven -----

    sdp = sub.add_parser("standby-drill-proven",
                          help="operator attests an end-to-end T2 §7 drill against a standby")
    sdp.add_argument("--node-id", required=True)
    sdp.add_argument("--case", choices=["A", "B"], required=True)
    sdp.add_argument("--notes", default=None)
    sdp.add_argument("--db-path", default=DEFAULT_DB)

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
                    "descriptor_signing_key_rotation": 8760,
                    "cover_pool_reverify_pass_proven": 60 * 24,
                    "cover_pool_replenishment_proven": 90 * 24,
                    "eu_standby_drill_proven": 30 * 24,
                    "t4_image_promoted":  30 * 24,
                } if args.role == "active" else {},
                now=_now(),
                role=args.role,
            )
            print(f"initialized {args.db_path} (role={args.role})")
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
        return _cmd_backup_now(args, mode)

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
        # Resolve next_due_hours: CLI flag > config > hardcoded default 720
        next_due_hours = args.next_due_hours
        if next_due_hours is None:
            from mthydra.controller.config import ConfigError, load_config
            try:
                cfg = load_config(args.config)
                next_due_hours = cfg.obligations.timers_hours.get(args.obligation_id, 720)
            except ConfigError:
                next_due_hours = 720

        now_dt = datetime.now(timezone.utc)
        next_due = (now_dt + timedelta(hours=next_due_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
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
        print(f"stamped {args.obligation_id} (next_due_hours={next_due_hours})")
        return 0

    if args.cmd == "rotate-provider-credential":
        cred = (
            args.credential
            if args.credential
            else Path(args.credential_file).read_text().strip()
        )
        from mthydra.controller.state.audit import log_event
        from mthydra.controller.state.tokens import set_provider_credential
        conn = connect(args.db_path)
        try:
            set_provider_credential(conn, provider=args.provider, credential=cred, at=_now())
            log_event(conn, ts=_now(), actor="operator",
                      action="rotate_provider_credential", target=args.provider,
                      details_json=None)
        finally:
            conn.close()
        print(f"rotated credential for {args.provider}")
        return 0

    if args.cmd == "descriptor-sign-now":
        return _cmd_descriptor_sign_now(args)

    if args.cmd == "descriptor-show":
        return _cmd_descriptor_show(args)

    if args.cmd == "descriptor-verify":
        return _cmd_descriptor_verify(args)

    if args.cmd == "signing-key-rotate":
        return _cmd_signing_key_rotate(args)

    if args.cmd == "eu-add":
        return _cmd_eu_add(args)

    if args.cmd == "eu-retire":
        return _cmd_eu_retire(args)

    if args.cmd == "descriptor-migrate-placeholder":
        return _cmd_descriptor_migrate_placeholder(args)

    if args.cmd == "serve":
        return _cmd_serve(args)

    if args.cmd == "cover-add":
        return _cmd_cover_add(args)

    if args.cmd == "cover-attest-verified":
        return _cmd_cover_attest_verified(args)

    if args.cmd == "cover-list":
        return _cmd_cover_list(args)

    if args.cmd == "cover-rotate":
        return _cmd_cover_rotate(args)

    if args.cmd == "cover-due":
        return _cmd_cover_due(args)

    if args.cmd == "cover-pool-stats":
        return _cmd_cover_pool_stats(args)

    if args.cmd == "authority-rotate":
        return _cmd_authority_rotate(args)

    if args.cmd == "authority-migrate-placeholder":
        return _cmd_authority_migrate_placeholder(args)

    if args.cmd == "promote-active":
        return _cmd_promote_active(args)

    if args.cmd == "eu-node-add":
        return _cmd_eu_node_add(args)

    if args.cmd == "eu-node-retire":
        return _cmd_eu_node_retire(args)

    if args.cmd == "eu-node-list":
        return _cmd_eu_node_list(args)

    if args.cmd == "standby-drill-proven":
        return _cmd_standby_drill_proven(args)

    if args.cmd == "image-build":
        return _cmd_image_build(args)
    if args.cmd == "image-list":
        return _cmd_image_list(args)
    if args.cmd == "image-promote":
        return _cmd_image_promote(args)
    if args.cmd == "image-retire":
        return _cmd_image_retire(args)
    if args.cmd == "image-current":
        return _cmd_image_current(args)
    if args.cmd == "upstream-check":
        return _cmd_upstream_check(args)

    return 1


def _cmd_backup_now(args, mode: str) -> int:
    """Run a manual backup synchronously (decision 2a — real pipeline call)."""
    from mthydra.controller.backup.pipeline import BackupPipeline
    from mthydra.controller.backup.s3_dest import S3Destination
    from mthydra.controller.config import ConfigError, load_config
    from mthydra.controller.state.backup_log import BackupTrigger
    from mthydra.controller.state.tokens import get_provider_credential

    if mode == "offline":
        print("backup-now refused: controller is in offline mode", file=sys.stderr)
        return 1

    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"backup-now: config error: {e}", file=sys.stderr)
        return 2

    try:
        recipient = _read_recipient(DEFAULT_RECIPIENT_FILE)
    except FileNotFoundError:
        print(f"backup-now: age recipient file not found: {DEFAULT_RECIPIENT_FILE}", file=sys.stderr)
        return 6

    conn = connect(args.db_path)
    try:
        try:
            secret = get_provider_credential(conn, "b2")
        except KeyError:
            print("backup-now: b2 credential not in DB; run init first", file=sys.stderr)
            return 7
    finally:
        conn.close()

    dest = _build_destination(cfg, secret, mode=mode, bucket_override=args.bucket_override)
    tmp_dir = Path(args.tmp_dir)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    pipeline = BackupPipeline(
        db_path=args.db_path,
        tmp_dir=tmp_dir,
        recipient=recipient,
        destination=dest,
        clock=_now,
        mode=mode,
    )
    try:
        gen = pipeline.do_backup(BackupTrigger.MANUAL)
        print(f"backup-now: pushed generation {gen}")
        return 0
    except Exception as e:
        print(f"backup-now: failed: {e}", file=sys.stderr)
        return 8


def _descriptor_valid_until(cfg_path: str, now_dt) -> str:
    """Compute valid_until from config, falling back to now+24h."""
    from datetime import timedelta
    from mthydra.controller.config import ConfigError, load_config
    try:
        cfg = load_config(cfg_path)
        hours = cfg.descriptor.validity_window_hours
    except ConfigError:
        hours = 24
    return (now_dt + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _cmd_descriptor_sign_now(args) -> int:
    from datetime import datetime, timezone
    from mthydra.descriptor.sign import SignError, sign_new_descriptor
    now = _now()
    now_dt = datetime.fromisoformat(now.replace("Z", "+00:00"))
    valid_until = _descriptor_valid_until(args.config, now_dt)
    conn = connect(args.db_path)
    try:
        gen, _, _ = sign_new_descriptor(conn, now_iso=now, valid_until_iso=valid_until)
        print(f"signed descriptor generation {gen}")
        return 0
    except Exception as e:
        print(f"descriptor-sign-now: {e}", file=sys.stderr)
        return 3
    finally:
        conn.close()


def _cmd_descriptor_show(args) -> int:
    import json as _json
    from mthydra.controller.state.descriptor import latest_descriptor_with_signature
    conn = connect(args.db_path)
    try:
        if args.generation is not None:
            row = conn.execute(
                "SELECT generation, payload, signature FROM descriptor_history WHERE generation=?",
                (args.generation,),
            ).fetchone()
            if row is None:
                print(f"generation {args.generation} not found", file=sys.stderr)
                return 3
            blob = row[1].encode("utf-8")
        else:
            result = latest_descriptor_with_signature(conn)
            if result is None:
                print("no descriptors in DB", file=sys.stderr)
                return 3
            _, blob, _ = result
        obj = _json.loads(blob)
        print(_json.dumps(obj, indent=2, sort_keys=True))
        return 0
    finally:
        conn.close()


def _cmd_descriptor_verify(args) -> int:
    from mthydra.descriptor.verify import TrustedKey, VerifyError, verify_descriptor
    blob = Path(args.payload_file).read_bytes()
    sig = Path(args.sig_file).read_bytes()
    now_str = args.now or _now()
    conn = connect(args.db_path)
    try:
        rows = conn.execute(
            "SELECT generation, pubkey FROM descriptor_signing_key "
            "WHERE retired_at IS NULL OR retired_at > ?",
            (now_str,),
        ).fetchall()
        trusted = [TrustedKey(generation=r[0], pubkey=bytes(r[1])) for r in rows]
    finally:
        conn.close()
    try:
        p = verify_descriptor(blob, sig, trusted, now_str)
        print(f"PASS  generation={p.generation} valid_until={p.valid_until}")
        return 0
    except VerifyError as e:
        print(f"FAIL  {e}", file=sys.stderr)
        return 1


def _cmd_signing_key_rotate(args) -> int:
    from datetime import datetime, timedelta, timezone
    from mthydra.controller.state.audit import log_event
    from mthydra.controller.state.obligations import prove
    from mthydra.descriptor.keys import generate_keypair
    from mthydra.descriptor.sign import SignError, sign_new_descriptor

    now = _now()
    now_dt = datetime.fromisoformat(now.replace("Z", "+00:00"))
    valid_until = _descriptor_valid_until(args.config, now_dt)

    conn = connect(args.db_path)
    try:
        # Get current active signing key
        row = conn.execute(
            "SELECT generation FROM descriptor_signing_key "
            "WHERE retired_at IS NULL ORDER BY generation DESC LIMIT 1"
        ).fetchone()
        if row is None:
            print("no active descriptor_signing_key — run init first", file=sys.stderr)
            return 3
        cur_gen = row[0]

        # Compute outgoing_retired_at = now + validity_window_hours
        from mthydra.controller.config import ConfigError, load_config
        try:
            cfg = load_config(args.config)
            vh = cfg.descriptor.validity_window_hours
        except ConfigError:
            vh = 24
        outgoing_at = (now_dt + timedelta(hours=vh)).strftime("%Y-%m-%dT%H:%M:%SZ")

        # Mark current key as outgoing (retired_at = now + validity_window)
        conn.execute(
            "UPDATE descriptor_signing_key SET retired_at=? WHERE generation=? AND retired_at IS NULL",
            (outgoing_at, cur_gen),
        )
        # Insert new key
        new_priv, new_pub = generate_keypair()
        new_gen = cur_gen + 1
        conn.execute(
            "INSERT INTO descriptor_signing_key (generation, privkey, pubkey, created_at) "
            "VALUES (?,?,?,?)",
            (new_gen, new_priv, new_pub, now),
        )
        conn.commit()

        # Sign under new key
        gen, _, _ = sign_new_descriptor(conn, now_iso=now, valid_until_iso=valid_until)
        log_event(conn, ts=now, actor="operator", action="signing_key_rotated",
                  target=str(new_gen), details_json=None)

        # Update obligation clock
        try:
            from mthydra.controller.config import load_config as lc
            cfg2 = lc(args.config)
            vh2 = cfg2.obligations.timers_hours.get("descriptor_signing_key_rotation", 8760)
        except Exception:
            vh2 = 8760
        nxt = (now_dt + timedelta(hours=vh2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            prove(conn, obligation_id="descriptor_signing_key_rotation",
                  proven_by="operator", at=now, next_due_at=nxt, details=None)
        except KeyError:
            pass  # obligation may not exist on old DBs

        print(f"rotated: new signing key gen={new_gen}, descriptor gen={gen}")
        return 0
    except Exception as e:
        print(f"signing-key-rotate: {e}", file=sys.stderr)
        return 4
    finally:
        conn.close()


def _cmd_eu_add(args) -> int:
    from datetime import datetime, timezone
    from mthydra.controller.state.audit import log_event
    from mthydra.controller.state.eu_exit_set import add_exit
    from mthydra.descriptor.sign import SignError, sign_new_descriptor

    now = _now()
    now_dt = datetime.fromisoformat(now.replace("Z", "+00:00"))
    valid_until = _descriptor_valid_until(args.config, now_dt)
    conn = connect(args.db_path)
    try:
        add_exit(conn, args.fingerprint, args.endpoint, args.weight, now)
        gen, _, _ = sign_new_descriptor(conn, now_iso=now, valid_until_iso=valid_until)
        log_event(conn, ts=now, actor="operator", action="eu_exit_added",
                  target=args.fingerprint, details_json=None)
        print(f"added {args.fingerprint} → descriptor gen={gen}")
        return 0
    except Exception as e:
        print(f"eu-add: {e}", file=sys.stderr)
        return 4
    finally:
        conn.close()


def _cmd_eu_retire(args) -> int:
    from datetime import datetime, timezone
    from mthydra.controller.state.audit import log_event
    from mthydra.controller.state.eu_exit_set import retire_exit
    from mthydra.descriptor.sign import SignError, sign_new_descriptor

    now = _now()
    now_dt = datetime.fromisoformat(now.replace("Z", "+00:00"))
    valid_until = _descriptor_valid_until(args.config, now_dt)
    conn = connect(args.db_path)
    try:
        retire_exit(conn, args.fingerprint, at=now)
        gen, _, _ = sign_new_descriptor(conn, now_iso=now, valid_until_iso=valid_until)
        log_event(conn, ts=now, actor="operator", action="eu_exit_retired",
                  target=args.fingerprint, details_json=None)
        print(f"retired {args.fingerprint} → descriptor gen={gen}")
        return 0
    except Exception as e:
        print(f"eu-retire: {e}", file=sys.stderr)
        return 4
    finally:
        conn.close()


def _cmd_descriptor_migrate_placeholder(args) -> int:
    from datetime import datetime, timezone
    from mthydra.controller.state.audit import log_event
    from mthydra.descriptor.keys import generate_keypair, is_placeholder
    from mthydra.descriptor.sign import SignError, sign_new_descriptor

    now = _now()
    now_dt = datetime.fromisoformat(now.replace("Z", "+00:00"))
    valid_until = _descriptor_valid_until(args.config, now_dt)

    conn = connect(args.db_path)
    try:
        row = conn.execute(
            "SELECT generation, privkey FROM descriptor_signing_key "
            "WHERE retired_at IS NULL ORDER BY generation DESC LIMIT 1"
        ).fetchone()
        if row is None:
            print("no active descriptor_signing_key — run init first", file=sys.stderr)
            return 3
        cur_gen, priv = row[0], bytes(row[1])
        if not is_placeholder(priv):
            print("active descriptor_signing_key is already a real Ed25519 key — nothing to do")
            return 0
        # Generate real key
        new_priv, new_pub = generate_keypair()
        new_gen = cur_gen + 1
        # Retire placeholder
        conn.execute(
            "UPDATE descriptor_signing_key SET retired_at=? WHERE generation=?", (now, cur_gen)
        )
        # Insert real key
        conn.execute(
            "INSERT INTO descriptor_signing_key (generation, privkey, pubkey, created_at) "
            "VALUES (?,?,?,?)",
            (new_gen, new_priv, new_pub, now),
        )
        conn.commit()
        # Sign first real descriptor
        gen_out, _, _ = sign_new_descriptor(conn, now_iso=now, valid_until_iso=valid_until)
        log_event(conn, ts=now, actor="operator",
                  action="descriptor_migrated_from_placeholder",
                  target=str(new_gen), details_json=None)
        print(f"migrated: new signing key gen={new_gen}, signed descriptor gen={gen_out}")
        return 0
    except Exception as e:
        print(f"descriptor-migrate-placeholder: {e}", file=sys.stderr)
        return 4
    finally:
        conn.close()


def _cmd_promote_active(args) -> int:
    from mthydra.controller.config import ConfigError, load_config
    from mthydra.controller.promote import PromotionError, promote_active

    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"promote-active: config error: {e}", file=sys.stderr)
        return 2

    if not args.yes:
        if not sys.stdin.isatty():
            print("promote-active: refusing — pass --yes or run on a TTY", file=sys.stderr)
            return 2
        confirm = input("Type 'PROMOTE' to proceed: ")
        if confirm != "PROMOTE":
            print("promote-active: aborted", file=sys.stderr)
            return 2

    node_id = cfg.standby.node_id or "unknown"

    try:
        checklist = promote_active(
            db_path=args.db_path,
            backup_blob=args.backup_blob,
            age_identity=args.age_identity,
            case=args.case,
            node_id=node_id,
            now=_now(),
        )
    except PromotionError as e:
        print(f"promote-active: {e}", file=sys.stderr)
        return 2

    print(f"promote-active: node {node_id} is now ACTIVE "
          f"(case {args.case}, ready to start systemd unit)")
    if checklist is not None:
        print(checklist)
    return 0


def _build_destination(cfg, secret: str, mode: str, bucket_override: str | None):
    """Build an S3Destination, honouring bucket_override in dryrun mode.

    In dryrun mode the destination bucket is replaced with bucket_override so
    backups go to a non-prod bucket.  In production mode bucket_override is
    ignored (the startup-check already rejected a matching override).
    """
    from mthydra.controller.backup.s3_dest import S3Destination

    bucket = (
        bucket_override
        if (mode == "dryrun" and bucket_override)
        else cfg.backup.bucket
    )
    return S3Destination(
        endpoint_url=cfg.backup.endpoint or None,
        bucket=bucket,
        access_key_id=cfg.backup.access_key_id,
        secret_access_key=secret,
        region=os.environ.get("MTHYDRA_BACKUP_REGION", "us-east-1"),
        object_lock_days=cfg.backup.retention.object_lock_days,
    )


def _cmd_serve(args) -> int:
    """Run the backup orchestrator loop (spec A stub; spec F will add the full controller plane)."""
    import signal
    import time

    from mthydra.controller.backup.pipeline import BackupPipeline
    from mthydra.controller.backup.triggers import BackupOrchestrator
    from mthydra.controller.config import load_config
    from mthydra.controller.state.tokens import get_provider_credential

    cfg = load_config(args.config)
    mode = args.mode

    from mthydra.controller.state.node_state import current_node_state
    _ns_conn = connect(args.db_path)
    try:
        ns = current_node_state(_ns_conn)
    finally:
        _ns_conn.close()

    if ns.role == "standby":
        return _serve_standby(args, cfg, mode)
    # else active — continue to existing active-path logic.

    recipient_path = DEFAULT_RECIPIENT_FILE
    try:
        recipient = _read_recipient(recipient_path)
    except FileNotFoundError:
        print(f"age recipient file not found: {recipient_path}", file=sys.stderr)
        return 6

    conn = connect(args.db_path)
    try:
        try:
            secret = get_provider_credential(conn, "b2")
        except KeyError:
            print("b2 provider credential not in DB; run init first", file=sys.stderr)
            return 7
    finally:
        conn.close()

    dest = _build_destination(cfg, secret, mode=mode, bucket_override=args.bucket_override)
    tmp_dir = Path("/var/lib/mthydra/tmp")
    tmp_dir.mkdir(parents=True, exist_ok=True)

    pipeline = BackupPipeline(
        db_path=args.db_path,
        tmp_dir=tmp_dir,
        recipient=recipient,
        destination=dest,
        clock=_now,
        mode=mode,
    )
    orch = BackupOrchestrator(
        pipeline=pipeline,
        debounce_seconds=cfg.backup.on_change_debounce_seconds,
        floor_interval_seconds=cfg.backup.floor_interval_hours * 3600,
        mode=mode,
    )

    # Enable audit-log file mirror (spec §4.7)
    from mthydra.controller.state.audit import set_audit_mirror
    from mthydra.descriptor.scheduler import DescriptorRotator
    from mthydra.controller.state.cover_pool_scheduler import (
        CoverPoolReverifySweep,
        CoverPoolRotationSweep,
    )
    from mthydra.controller.standby.heartbeat import StandbyHeartbeatPoller

    set_audit_mirror("/var/lib/mthydra/logs/audit.log")

    rotator = DescriptorRotator(
        db_path=args.db_path,
        rotation_interval_seconds=cfg.descriptor.rotation_interval_hours * 3600,
        validity_window_seconds=cfg.descriptor.validity_window_hours * 3600,
        mode=mode,
    )
    reverify_sweep = CoverPoolReverifySweep(
        db_path=args.db_path,
        reverify_after_days=cfg.cover_pool.reverify_after_days,
        sweep_interval_seconds=cfg.cover_pool.reverify_sweep_interval_seconds,
        mode=mode,
    )
    rotation_sweep = CoverPoolRotationSweep(
        db_path=args.db_path,
        rotation_ttl_days=cfg.cover_pool.rotation_ttl_days,
        freeze_threshold=cfg.cover_pool.freeze_threshold,
        sweep_interval_seconds=cfg.cover_pool.rotation_sweep_interval_seconds,
        mode=mode,
    )
    poller = StandbyHeartbeatPoller(
        db_path=args.db_path,
        b2_destination=dest,
        poll_interval_seconds=cfg.standby.heartbeat_poll_interval_seconds,
        staleness_alert_seconds=cfg.standby.staleness_alert_seconds,
        mode=mode,
    )
    tracker = UpstreamReleaseTracker(
        db_path=args.db_path,
        upstream_repo=cfg.image.upstream_repo,
        github_api_url=cfg.image.github_api_url,
        poll_interval_seconds=cfg.image.upstream_check_interval_seconds,
        mode=mode,
    )

    if mode != "offline":
        orch.arm()
        rotator.arm()
        reverify_sweep.arm()
        rotation_sweep.arm()
        poller.arm()
        tracker.arm()
        print("serve: backup orchestrator + descriptor rotator + cover-pool sweeps + standby poller + upstream tracker armed", flush=True)
    else:
        print("serve: offline mode — triggers not armed", flush=True)

    stop_event = _install_signal_handler()
    try:
        while not stop_event.is_set():
            stop_event.wait(timeout=60)
    finally:
        orch.disarm()
        rotator.disarm()
        reverify_sweep.disarm()
        rotation_sweep.disarm()
        poller.disarm()
        tracker.disarm()
        print("serve: stopped", flush=True)
    return 0


def _serve_standby(args, cfg, mode: str) -> int:
    """Spec F standby serve loop: heartbeat publisher only, no mutating schedulers."""
    from mthydra.controller.backup.s3_dest import S3Destination  # noqa: F401 (kept for symmetry)
    from mthydra.controller.standby.heartbeat import StandbyHeartbeatPublisher
    from mthydra.controller.state.audit import set_audit_mirror
    from mthydra.controller.state.tokens import get_provider_credential

    if not cfg.standby.node_id:
        print(
            "serve: refused — standby role requires [standby].node_id in controller.toml",
            file=sys.stderr,
        )
        return 2

    set_audit_mirror("/var/lib/mthydra/logs/audit.log")

    conn = connect(args.db_path)
    try:
        try:
            secret = get_provider_credential(conn, "b2")
        except KeyError:
            print(
                "serve: b2 provider credential not in DB; standby cannot publish heartbeat",
                file=sys.stderr,
            )
            return 7
    finally:
        conn.close()

    dest = _build_destination(cfg, secret, mode=mode, bucket_override=args.bucket_override)
    publisher = StandbyHeartbeatPublisher(
        node_id=cfg.standby.node_id,
        b2_destination=dest,
        interval_seconds=cfg.standby.heartbeat_interval_seconds,
        mode=mode,
    )
    if mode != "offline":
        publisher.arm()
        print(f"serve: standby heartbeat armed (node_id={cfg.standby.node_id})", flush=True)
    else:
        print("serve: standby in offline mode — heartbeat not armed", flush=True)

    stop_event = _install_signal_handler()
    try:
        while not stop_event.is_set():
            stop_event.wait(timeout=60)
    finally:
        publisher.disarm()
        print("serve: standby stopped", flush=True)
    return 0


def _install_signal_handler():
    import signal
    import threading

    stop_event = threading.Event()

    def _handler(sig, frame):
        stop_event.set()

    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)
    return stop_event


# ----- spec C handlers -----


def _add_hours_iso(iso: str, hours: int) -> str:
    from datetime import datetime, timedelta
    t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return (t + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _cmd_cover_add(args) -> int:
    import sqlite3

    from mthydra.controller.state.cover_pool import add_candidate
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.obligations import prove

    conn = connect(args.db_path)
    try:
        now = _now()
        try:
            add_candidate(conn, args.domain, added_at=now, notes=args.notes)
        except sqlite3.IntegrityError as e:
            print(f"cover-add: {e}", file=sys.stderr)
            return 2
        next_due = _add_hours_iso(now, 90 * 24)  # default replenishment_interval_days
        try:
            prove(conn, "cover_pool_replenishment_proven",
                  proven_by="operator", at=now,
                  next_due_at=next_due, details=args.domain)
        except KeyError:
            pass  # obligation may not be seeded in older DBs; non-fatal
        print(f"cover-add: {args.domain} added (candidate_unverified)")
        return 0
    finally:
        conn.close()


def _cmd_cover_attest_verified(args) -> int:
    from mthydra.controller.state.cover_pool import attest_verified
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.obligations import prove

    conn = connect(args.db_path)
    try:
        now = _now()
        try:
            attest_verified(
                conn, args.domain,
                from_vantage=args.vantage, at=now, evidence=args.evidence,
            )
        except ValueError as e:
            print(f"cover-attest-verified: {e}", file=sys.stderr)
            return 2
        next_due = _add_hours_iso(now, 60 * 24)
        try:
            prove(conn, "cover_pool_reverify_pass_proven",
                  proven_by="operator", at=now,
                  next_due_at=next_due, details=args.domain)
        except KeyError:
            pass
        print(f"cover-attest-verified: {args.domain} -> candidate_verified (vantage={args.vantage})")
        return 0
    finally:
        conn.close()


def _cmd_cover_list(args) -> int:
    import json
    from dataclasses import asdict

    from mthydra.controller.state.cover_pool import list_by_state
    from mthydra.controller.state.db import connect

    conn = connect(args.db_path)
    try:
        states = (
            [args.state] if args.state else
            ["candidate_unverified", "candidate_verified", "in_use"]
        )
        rows: list = []
        for s in states:
            rows.extend(list_by_state(conn, s))
        if args.json:
            print(json.dumps([asdict(r) for r in rows], indent=2))
        else:
            print(f"{'state':24} {'domain':40} added_at")
            for r in rows:
                print(f"{r.state:24} {r.domain:40} {r.added_at}")
        return 0
    finally:
        conn.close()


def _cmd_cover_rotate(args) -> int:
    from mthydra.controller.state.cover_pool import rotate_and_burn
    from mthydra.controller.state.db import connect

    conn = connect(args.db_path)
    try:
        row = conn.execute(
            "SELECT state, assigned_box_id FROM cover_domain_pool WHERE domain=?",
            (args.domain,),
        ).fetchone()
        if row is None:
            print(f"cover-rotate: {args.domain} not in cover_domain_pool", file=sys.stderr)
            return 2
        try:
            rotate_and_burn(
                conn, args.domain,
                reason=args.reason,
                last_box_id=row[1] or "",
                at=_now(),
            )
        except ValueError as e:
            print(f"cover-rotate: {e}", file=sys.stderr)
            return 2
        conn.execute(
            "DELETE FROM obligation_clocks WHERE obligation_id=?",
            (f"cover_pool_rotation_pending::{args.domain}",),
        )
        conn.commit()
        print(f"cover-rotate: {args.domain} -> burned (reason={args.reason})")
        return 0
    finally:
        conn.close()


def _cmd_cover_due(args) -> int:
    import json
    from dataclasses import asdict

    from mthydra.controller.config import ConfigError, load_config
    from mthydra.controller.state.cover_pool import (
        _iso_minus_days, list_due_for_rotation, pool_health,
    )
    from mthydra.controller.state.db import connect

    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"cover-due: config error: {e}", file=sys.stderr)
        return 2

    conn = connect(args.db_path)
    try:
        due = list_due_for_rotation(
            conn, now=_now(),
            rotation_ttl_days=cfg.cover_pool.rotation_ttl_days,
        )
        cutoff = _iso_minus_days(_now(), cfg.cover_pool.reverify_after_days)
        stale_rows = conn.execute(
            "SELECT domain, last_verified_at FROM cover_domain_pool "
            "WHERE state='candidate_verified' AND last_verified_at < ? "
            "ORDER BY last_verified_at",
            (cutoff,),
        ).fetchall()
        stale = [{"domain": d, "last_verified_at": v} for d, v in stale_rows]
        health = pool_health(conn, freeze_threshold=cfg.cover_pool.freeze_threshold)

        payload = {
            "due_for_rotation": [asdict(r) for r in due],
            "stale_verified": stale,
            "pool_health": asdict(health),
        }
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            print(f"pool: unverified={health.candidate_unverified} "
                  f"verified={health.candidate_verified} "
                  f"in_use={health.in_use} burned={health.burned}")
            print(f"rotation_frozen: {health.rotation_frozen}")
            if due:
                print("due for rotation:")
                for r in due:
                    print(f"  {r.domain}  entered_in_use_at={r.entered_in_use_at}")
            if stale:
                print("stale candidate_verified (will downgrade on next sweep):")
                for r in stale:
                    print(f"  {r['domain']}  last_verified_at={r['last_verified_at']}")
        return 0
    finally:
        conn.close()


def _cmd_authority_rotate(args) -> int:
    import json as _json

    from mthydra.controller.state.audit import log_event
    from mthydra.controller.state.authority import (
        current_authority, insert_authority, retire_authority,
    )
    from mthydra.controller.state.db import connect
    from mthydra.descriptor.authority import generate_authority_keypair

    conn = connect(args.db_path)
    try:
        rc = _require_active_role(conn, "authority-rotate")
        if rc is not None:
            return rc
        try:
            current = current_authority(conn)
        except LookupError:
            print("authority-rotate: no active credential_authority found", file=sys.stderr)
            return 2
        new_gen = current.generation + 1
        now = _now()
        priv, pub = generate_authority_keypair()
        insert_authority(conn, generation=new_gen, privkey_pem=priv,
                         pubkey_pem=pub, created_at=now)
        retire_authority(conn, current.generation, at=now)
        log_event(conn, ts=now, actor="operator", action="authority_rotated",
                  target=str(new_gen),
                  details_json=_json.dumps({"new_generation": new_gen,
                                             "retired_generation": current.generation}))
        print(f"authority-rotate: new generation {new_gen} active; "
              f"generation {current.generation} retired")
        return 0
    finally:
        conn.close()


def _cmd_authority_migrate_placeholder(args) -> int:
    import json as _json

    from mthydra.controller.state.audit import log_event
    from mthydra.controller.state.db import connect
    from mthydra.descriptor.authority import generate_authority_keypair

    conn = connect(args.db_path)
    try:
        rc = _require_active_role(conn, "authority-migrate-placeholder")
        if rc is not None:
            return rc
        rows = conn.execute(
            "SELECT generation, privkey_pem FROM credential_authority "
            "WHERE privkey_pem LIKE 'PRIV-BOOTSTRAP-%'"
        ).fetchall()
        if not rows:
            print("authority-migrate-placeholder: no placeholder rows; nothing to migrate")
            return 0
        now = _now()
        for gen, _old_priv in rows:
            priv_pem, pub_pem = generate_authority_keypair()
            conn.execute(
                "UPDATE credential_authority SET privkey_pem=?, pubkey_pem=? "
                "WHERE generation=?",
                (priv_pem, pub_pem, gen),
            )
            log_event(
                conn, ts=now, actor="operator",
                action="authority_migrated_placeholder",
                target=str(gen),
                details_json=_json.dumps({"old_prefix": "PRIV-BOOTSTRAP-"},
                                          separators=(",", ":")),
            )
        conn.commit()
        print(f"authority-migrate-placeholder: migrated {len(rows)} row(s) to real Ed25519")
        return 0
    finally:
        conn.close()


def _cmd_cover_pool_stats(args) -> int:
    import json
    from dataclasses import asdict

    from mthydra.controller.config import ConfigError, load_config
    from mthydra.controller.state.cover_pool import pool_health
    from mthydra.controller.state.db import connect

    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"cover-pool-stats: config error: {e}", file=sys.stderr)
        return 2

    conn = connect(args.db_path)
    try:
        h = pool_health(conn, freeze_threshold=cfg.cover_pool.freeze_threshold)
        if args.json:
            print(json.dumps(asdict(h), indent=2))
        else:
            print(f"candidate_unverified : {h.candidate_unverified}")
            print(f"candidate_verified   : {h.candidate_verified}")
            print(f"in_use               : {h.in_use}")
            print(f"burned               : {h.burned}")
            print(f"rotation_frozen      : {h.rotation_frozen}")
            print(f"oldest_in_use_at     : {h.oldest_in_use_at}")
            print(f"oldest_unverified_at : {h.oldest_unverified_at}")
            print(f"last_attest_at       : {h.last_attest_at}")
        return 0
    finally:
        conn.close()


# ----- spec F: eu-node inventory + standby-drill handlers -----


def _require_active_role(conn, cmd_name: str) -> int | None:
    """Returns exit code 2 if standby; None if active (continue)."""
    from mthydra.controller.state.node_state import current_node_state
    ns = current_node_state(conn)
    if ns.role != "active":
        print(f"{cmd_name}: refused — active-only command", file=sys.stderr)
        return 2
    return None


def _cmd_eu_node_add(args) -> int:
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.eu_nodes import add_eu_node

    conn = connect(args.db_path)
    try:
        rc = _require_active_role(conn, "eu-node-add")
        if rc is not None:
            return rc
        try:
            add_eu_node(
                conn,
                node_id=args.node_id,
                hostname=args.hostname,
                provider=args.provider,
                region=args.region,
                public_ip=args.public_ip,
                role=args.role,
                added_at=_now(),
                notes=args.notes,
            )
        except ValueError as e:
            print(f"eu-node-add: {e}", file=sys.stderr)
            return 2
        print(f"eu-node-add: {args.node_id} added (role={args.role})")
        return 0
    finally:
        conn.close()


def _cmd_eu_node_retire(args) -> int:
    from mthydra.controller.config import ConfigError, load_config
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.eu_nodes import retire_eu_node
    from mthydra.controller.state.node_state import current_node_state

    conn = connect(args.db_path)
    try:
        rc = _require_active_role(conn, "eu-node-retire")
        if rc is not None:
            return rc
        ns = current_node_state(conn)
        try:
            cfg = load_config("/etc/mthydra/controller.toml")
            if cfg.standby.node_id == args.node_id and ns.role == "active":
                print(f"eu-node-retire: refusing to retire local active node "
                      f"{args.node_id}; promote a standby first", file=sys.stderr)
                return 2
        except (ConfigError, FileNotFoundError):
            pass
        try:
            retire_eu_node(conn, args.node_id, at=_now())
        except ValueError as e:
            print(f"eu-node-retire: {e}", file=sys.stderr)
            return 2
        print(f"eu-node-retire: {args.node_id} retired")
        return 0
    finally:
        conn.close()


def _cmd_eu_node_list(args) -> int:
    import json as _json
    from dataclasses import asdict

    from mthydra.controller.state.db import connect
    from mthydra.controller.state.eu_nodes import list_eu_nodes

    conn = connect(args.db_path)
    try:
        nodes = list_eu_nodes(conn, role=args.state)
        if args.json:
            print(_json.dumps([asdict(n) for n in nodes], indent=2))
        else:
            print(f"{'node_id':30} {'role':10} {'hostname':30} last_heartbeat_at")
            for n in nodes:
                print(f"{n.node_id:30} {n.role:10} {n.hostname:30} "
                      f"{n.last_heartbeat_at or '-'}")
        return 0
    finally:
        conn.close()


def _cmd_standby_drill_proven(args) -> int:
    import json as _json

    from mthydra.controller.state.audit import log_event
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.obligations import prove, set_obligation

    conn = connect(args.db_path)
    try:
        rc = _require_active_role(conn, "standby-drill-proven")
        if rc is not None:
            return rc
        now = _now()
        case = args.case
        try:
            prove(conn, f"t2_dryrun_case{case}",
                  proven_by="operator", at=now,
                  next_due_at=_add_hours_iso(now, 30 * 24),
                  details=args.notes)
        except KeyError:
            set_obligation(conn,
                           obligation_id=f"t2_dryrun_case{case}",
                           last_proven_at=now,
                           proven_by="operator",
                           next_due_at=_add_hours_iso(now, 30 * 24),
                           details=args.notes)
        set_obligation(conn,
                       obligation_id=f"eu_standby_drill_proven::{args.node_id}",
                       last_proven_at=now,
                       proven_by="operator",
                       next_due_at=_add_hours_iso(now, 30 * 24),
                       details=args.notes)
        log_event(conn, ts=now, actor="operator", action="eu_standby_drill_proven",
                  target=args.node_id,
                  details_json=_json.dumps({"case": case, "notes": args.notes}))
        print(f"standby-drill-proven: case {case} attested for {args.node_id}")
        return 0
    finally:
        conn.close()


# ----- spec D handlers -----


def _cmd_image_build(args) -> int:
    from pathlib import Path

    from mthydra.controller.config import ConfigError, load_config
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.tokens import get_provider_credential

    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"image-build: config error: {e}", file=sys.stderr)
        return 2

    conn = connect(args.db_path)
    try:
        rc = _require_active_role(conn, "image-build")
        if rc is not None:
            return rc
        try:
            secret = get_provider_credential(conn, "b2")
        except KeyError:
            print("image-build: b2 provider credential not in DB", file=sys.stderr)
            return 7
        dest = _build_destination(cfg, secret, mode="production",
                                   bucket_override=args.bucket_override)
        asset = args.asset or cfg.image.upstream_release_asset
        try:
            iv = build_image(
                conn=conn,
                b2_destination=dest,
                upstream_repo=cfg.image.upstream_repo,
                upstream_release=args.release,
                asset_filename=asset,
                github_api_url=cfg.image.github_api_url,
                tmp_dir=Path(cfg.image.build_tmp_dir),
                now=_now(),
            )
        except BuildError as e:
            msg = str(e).lower()
            if "sha256 mismatch" in msg:
                code = 3
            elif "github" in msg or "release" in msg or "asset" in msg or "checksum" in msg:
                code = 4
            elif "b2" in msg:
                code = 5
            else:
                code = 2
            print(f"image-build: {e}", file=sys.stderr)
            return code
        print(f"image-build: candidate {iv} registered (release={args.release})")
        return 0
    finally:
        conn.close()


def _cmd_image_list(args) -> int:
    import json as _json
    from dataclasses import asdict

    from mthydra.controller.config import ConfigError, load_config
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.ru_images import list_images
    from mthydra.controller.state.tokens import get_provider_credential

    b2 = None
    try:
        cfg = load_config(args.config)
        conn = connect(args.db_path)
        try:
            secret = get_provider_credential(conn, "b2")
            b2 = _build_destination(cfg, secret, mode="production",
                                     bucket_override=args.bucket_override)
        finally:
            conn.close()
    except (ConfigError, KeyError, FileNotFoundError):
        pass

    conn = connect(args.db_path)
    try:
        images = list_images(conn, state=args.state)
        rows = []
        for im in images:
            d = asdict(im)
            if b2 is not None:
                try:
                    d["b2_present"] = b2.head_image(image_version=im.image_version) is not None
                except Exception:
                    d["b2_present"] = None
            else:
                d["b2_present"] = None
            rows.append(d)
        if args.json:
            print(_json.dumps(rows, indent=2))
        else:
            print(f"{'state':10} {'image_version':16} {'upstream_release':20} built_at")
            for r in rows:
                print(f"{r['state']:10} {r['image_version'][:16]:16} "
                      f"{r['upstream_release']:20} {r['built_at']}")
        return 0
    finally:
        conn.close()


def _cmd_image_promote(args) -> int:
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.ru_images import promote

    conn = connect(args.db_path)
    try:
        rc = _require_active_role(conn, "image-promote")
        if rc is not None:
            return rc
        try:
            promote(conn, args.image_version, at=_now(), evidence=args.evidence)
        except (ValueError, LookupError) as e:
            print(f"image-promote: {e}", file=sys.stderr)
            return 2
        print(f"image-promote: {args.image_version} -> promoted")
        return 0
    finally:
        conn.close()


def _cmd_image_retire(args) -> int:
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.ru_images import current_promoted, get_image, retire

    conn = connect(args.db_path)
    try:
        rc = _require_active_role(conn, "image-retire")
        if rc is not None:
            return rc
        try:
            target = get_image(conn, args.image_version)
        except LookupError as e:
            print(f"image-retire: {e}", file=sys.stderr)
            return 2
        was_promoted = target.state == "promoted"
        try:
            retire(conn, args.image_version, at=_now(), reason=args.reason)
        except ValueError as e:
            print(f"image-retire: {e}", file=sys.stderr)
            return 2
        msg = f"image-retire: {args.image_version} -> retired"
        if was_promoted and current_promoted(conn) is None:
            msg += "  (WARNING: no promoted image; fleet has no default — promote a candidate)"
        print(msg)
        return 0
    finally:
        conn.close()


def _cmd_image_current(args) -> int:
    """Read-only; works on both active and standby."""
    import json as _json
    from dataclasses import asdict

    from mthydra.controller.state.db import connect
    from mthydra.controller.state.ru_images import current_promoted

    conn = connect(args.db_path)
    try:
        n = current_promoted(conn)
        if args.json:
            print(_json.dumps(asdict(n) if n is not None else None, indent=2))
        else:
            if n is None:
                print("image-current: none promoted")
            else:
                print(f"image-current: {n.image_version} "
                      f"(release={n.upstream_release}, promoted_at={n.promoted_at})")
        return 0
    finally:
        conn.close()


def _cmd_upstream_check(args) -> int:
    from mthydra.controller.config import ConfigError, load_config

    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"upstream-check: config error: {e}", file=sys.stderr)
        return 2

    tracker = UpstreamReleaseTracker(
        db_path=args.db_path,
        upstream_repo=cfg.image.upstream_repo,
        github_api_url=cfg.image.github_api_url,
        poll_interval_seconds=cfg.image.upstream_check_interval_seconds,
        mode="offline",
    )
    latest = tracker.run_once()
    if latest is None:
        print("upstream-check: GitHub poll failed (see logs)", file=sys.stderr)
        return 4
    print(f"upstream-check: latest upstream tag = {latest}")
    return 0
