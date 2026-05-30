"""Controller CLI — subcommands: init, startup-check, backup-now, restore,
adopt-restored-state, obligation-proven.

Global flag --mode {production|dryrun|offline} per plan §16.2.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mthydra.controller.bootstrap import BootstrapError, init_state
from mthydra.controller.image.builder import BuildError, build_image
from mthydra.controller.image.upstream_tracker import UpstreamReleaseTracker
from mthydra.controller.provisioning.seed import ProvisionError, provision_box
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


def _parse_kv_env(items: list[str]) -> dict[str, str]:
    """Like _parse_kv but the value names an env var holding the secret.

    Keeps provider secrets off argv (which is world-readable via `ps`).
    Each item is PROVIDER=ENVVAR; the credential is read from os.environ.
    """
    out: dict[str, str] = {}
    for raw in (items or []):
        if "=" not in raw:
            raise ValueError(f"expected PROVIDER=ENVVAR, got {raw!r}")
        provider, envvar = raw.split("=", 1)
        if envvar not in os.environ:
            raise ValueError(
                f"--provider-credential-env {raw!r}: ${envvar} is not set in the environment"
            )
        out[provider] = os.environ[envvar]
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
        help="provider credential (repeatable). WARNING: visible in `ps`; "
             "prefer --provider-credential-env for secrets",
    )
    init_p.add_argument(
        "--provider-credential-env",
        action="append",
        default=[],
        metavar="PROVIDER=ENVVAR",
        help="provider credential read from the named env var, keeping the "
             "secret off argv (repeatable)",
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
    ib.add_argument("--profile-json", required=True, dest="profile_json",
                     help="path to known-good profile JSON file, or '-' to read stdin (spec D2 atomic pin)")
    ib.add_argument("--profile-recorded-by", default="operator", dest="profile_recorded_by",
                     help="who pinned the profile (default 'operator')")
    ib.add_argument("--db-path", default=DEFAULT_DB)
    ib.add_argument("--config", default="/etc/mthydra/controller.toml")

    il = sub.add_parser("image-list", help="list ru_images catalog")
    il.add_argument("--state", choices=["candidate", "promoted", "retired"], default=None)
    il.add_argument("--db-path", default=DEFAULT_DB)
    il.add_argument("--config", default="/etc/mthydra/controller.toml")
    il.add_argument("--json", action="store_true")

    ip = sub.add_parser("image-promote",
                         help="atomic candidate -> promoted; prior promoted -> retired (spec D2 gated)")
    ip.add_argument("image_version")
    ip.add_argument("--evidence", required=True,
                     help="operator-attested evidence text (recorded in audit; not validated)")
    ip.add_argument("--db-path", default=DEFAULT_DB)
    ip.add_argument("--config", default="/etc/mthydra/controller.toml")

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

    # ----- spec G: provisioning -----

    rbl = sub.add_parser("ru-box-list", help="list ru_boxes inventory")
    rbl.add_argument("--state", choices=["provisioning", "live", "terminated"], default=None)
    rbl.add_argument("--db-path", default=DEFAULT_DB)
    rbl.add_argument("--json", action="store_true")

    rbml = sub.add_parser("ru-box-mark-live", help="state: provisioning -> live")
    rbml.add_argument("box_id")
    rbml.add_argument("--public-ip", required=True)
    rbml.add_argument("--db-path", default=DEFAULT_DB)

    rbt = sub.add_parser("ru-box-terminate",
                          help="terminate a box (revokes credentials, burns SNI)")
    rbt.add_argument("box_id")
    rbt.add_argument("--reason", required=True)
    rbt.add_argument("--db-path", default=DEFAULT_DB)

    ps = sub.add_parser("provision-seed",
                         help="atomic provisioning: claim cover domain + image + credential, emit seed")
    ps.add_argument("--provider", required=True)
    ps.add_argument("--region", required=True)
    ps.add_argument("--format", choices=["cloud-init", "json"], default="cloud-init")
    ps.add_argument("--ttl-seconds", type=int, default=3600)
    ps.add_argument("--db-path", default=DEFAULT_DB)
    ps.add_argument("--config", default="/etc/mthydra/controller.toml")
    ps.add_argument("--agent-source-url", required=True)
    ps.add_argument("--agent-source-sha256", required=True)
    ps.add_argument("--descriptor-refresh-url", required=True)
    ps.add_argument("--canary", action="store_true", dest="is_canary",
                     help="mark the resulting ru_box as is_canary=1 (spec D2 soak)")

    des = sub.add_parser("data-exit-status",
                          help="show sing-box wheel status for an EU node")
    des.add_argument("--node-id", required=True)
    des.add_argument("--db-path", default=DEFAULT_DB)
    des.add_argument("--config", default="/etc/mthydra/controller.toml")

    der = sub.add_parser("data-exit-rewrite",
                          help="force a wheel tick: regenerate sing-box config now")
    der.add_argument("--node-id", required=True)
    der.add_argument("--db-path", default=DEFAULT_DB)
    der.add_argument("--config", default="/etc/mthydra/controller.toml")

    decs = sub.add_parser("data-exit-config-show",
                           help="print the rendered sing-box.json to stdout")
    decs.add_argument("--node-id", required=True)
    decs.add_argument("--db-path", default=DEFAULT_DB)
    decs.add_argument("--config", default="/etc/mthydra/controller.toml")

    derk = sub.add_parser("data-exit-reality-keygen",
                           help="generate the initial Reality keypair for an EU node")
    derk.add_argument("--node-id", required=True)
    derk.add_argument("--evidence", required=True,
                       help="operator-attested rationale (logged to audit)")
    derk.add_argument("--db-path", default=DEFAULT_DB)
    derk.add_argument("--config", default="/etc/mthydra/controller.toml")

    # ----- spec H: shard manager subcommands -----

    ua = sub.add_parser("user-add", help="add a user to the circle")
    ua.add_argument("user_id")
    ua.add_argument("--out-of-band-channel", required=True,
                     help="how to reach the user without Telegram (e.g. signal:+1)")
    ua.add_argument("--display-name", default=None)
    ua.add_argument("--db-path", default=DEFAULT_DB)

    ul = sub.add_parser("user-list", help="list users + current_shard_id")
    ul.add_argument("--db-path", default=DEFAULT_DB)
    ul.add_argument("--json", action="store_true")

    sc = sub.add_parser("shard-create",
                         help="explicit-membership shard create (bootstrap path)")
    sc.add_argument("shard_id")
    sc.add_argument("--members", required=True,
                     help="comma-separated user_ids")
    sc.add_argument("--db-path", default=DEFAULT_DB)
    sc.add_argument("--config", default="/etc/mthydra/controller.toml")

    sl = sub.add_parser("shard-list", help="list shards")
    sl.add_argument("--include-retired", action="store_true")
    sl.add_argument("--db-path", default=DEFAULT_DB)
    sl.add_argument("--json", action="store_true")

    ss = sub.add_parser("shard-show", help="full detail of one shard")
    ss.add_argument("shard_id")
    ss.add_argument("--db-path", default=DEFAULT_DB)
    ss.add_argument("--json", action="store_true")

    sab = sub.add_parser("shard-assign-box",
                          help="set ru_boxes.shard_id (provisioning state only)")
    sab.add_argument("box_id")
    sab.add_argument("--shard", required=True, dest="shard_id")
    sab.add_argument("--db-path", default=DEFAULT_DB)

    sr = sub.add_parser("shard-reshuffle",
                         help="operator-driven out-of-band reshuffle of one shard")
    sr.add_argument("shard_id")
    sr.add_argument("--reason", default="operator_manual")
    sr.add_argument("--db-path", default=DEFAULT_DB)
    sr.add_argument("--config", default="/etc/mthydra/controller.toml")

    sst = sub.add_parser("shard-stats",
                          help="shard health summary (overdue + unassigned + ages)")
    sst.add_argument("--db-path", default=DEFAULT_DB)
    sst.add_argument("--config", default="/etc/mthydra/controller.toml")
    sst.add_argument("--json", action="store_true")

    # ----- spec I: probe vantage harness subcommands -----

    va = sub.add_parser("vantage-add", help="add a candidate probe vantage")
    va.add_argument("vantage_id")
    va.add_argument("--label", required=True)
    va.add_argument("--source-kind", required=True)
    va.add_argument("--region-hint", default=None)
    va.add_argument("--notes", default=None)
    va.add_argument("--db-path", default=DEFAULT_DB)

    vaa = sub.add_parser("vantage-attest-active",
                          help="candidate -> active (operator confirms RU-approximating)")
    vaa.add_argument("vantage_id")
    vaa.add_argument("--evidence", default=None)
    vaa.add_argument("--db-path", default=DEFAULT_DB)

    vl = sub.add_parser("vantage-list", help="list probe vantages")
    vl.add_argument("--state",
                     choices=["candidate", "active", "retired", "burned"],
                     default=None)
    vl.add_argument("--db-path", default=DEFAULT_DB)
    vl.add_argument("--json", action="store_true")

    vr = sub.add_parser("vantage-retire", help="active -> retired")
    vr.add_argument("vantage_id")
    vr.add_argument("--reason", default=None)
    vr.add_argument("--db-path", default=DEFAULT_DB)

    vb = sub.add_parser("vantage-burn",
                          help="-> burned (monotonic; no undo)")
    vb.add_argument("vantage_id")
    vb.add_argument("--reason", required=True)
    vb.add_argument("--db-path", default=DEFAULT_DB)

    pp = sub.add_parser("profile-pin",
                          help="pin a known-good profile for an image_version")
    pp.add_argument("image_version")
    pp.add_argument("--profile-json", required=True,
                     help="path to JSON file, or '-' to read stdin")
    pp.add_argument("--recorded-by", required=True)
    pp.add_argument("--notes", default=None)
    pp.add_argument("--db-path", default=DEFAULT_DB)

    ps_show = sub.add_parser("profile-show",
                          help="print the pinned profile for an image_version")
    ps_show.add_argument("image_version")
    ps_show.add_argument("--db-path", default=DEFAULT_DB)
    ps_show.add_argument("--json", action="store_true")

    prr = sub.add_parser("probe-record",
                          help="append one probe_results row (operator submits)")
    prr.add_argument("--box-id", required=True)
    prr.add_argument("--vantage", required=True, dest="vantage_id")
    prr.add_argument("--check", required=True, dest="check_type",
                      choices=["tls_fall_through", "cover_domain_consistency",
                                "surface_scan", "valid_path_liveness",
                                "latency_loss", "behavioural_identity"])
    prr.add_argument("--status", required=True,
                      choices=["pass", "soft_fail", "hard_fail"])
    prr.add_argument("--cycle-at", required=True)
    prr.add_argument("--evidence", default=None)
    prr.add_argument("--image-version", default=None,
                      help="defaults to the box's current image_version")
    prr.add_argument("--db-path", default=DEFAULT_DB)

    pe = sub.add_parser("probe-evaluate",
                          help="run evaluate_box and print the verdict")
    pe.add_argument("--box-id", required=True)
    pe.add_argument("--config", default="/etc/mthydra/controller.toml")
    pe.add_argument("--db-path", default=DEFAULT_DB)
    pe.add_argument("--json", action="store_true")

    pd = sub.add_parser("probe-due",
                          help="show kill_pending + coverage_pending + rotation_pending")
    pd.add_argument("--db-path", default=DEFAULT_DB)
    pd.add_argument("--json", action="store_true")

    # ----- spec J: observability subcommands -----

    obs_s = sub.add_parser("obs-status",
                            help="one-screen snapshot dump (counts, obligations, anti-obligations)")
    obs_s.add_argument("--db-path", default=DEFAULT_DB)
    obs_s.add_argument("--json", action="store_true")

    obs_ar = sub.add_parser("obs-alerts-recent",
                             help="tail of alert_log; most-recent first")
    obs_ar.add_argument("--limit", type=int, default=50)
    obs_ar.add_argument("--severity",
                         choices=["info", "warn", "crit", "heartbeat"],
                         default=None)
    obs_ar.add_argument("--db-path", default=DEFAULT_DB)
    obs_ar.add_argument("--json", action="store_true")

    obs_at = sub.add_parser("obs-alert-test",
                             help="synthetic alert through configured sinks (deploy-time check)")
    obs_at.add_argument("--severity", choices=["info", "warn", "crit"],
                         default="warn")
    obs_at.add_argument("--message", default=None)
    obs_at.add_argument("--db-path", default=DEFAULT_DB)
    obs_at.add_argument("--config", default="/etc/mthydra/controller.toml")

    obs_hb = sub.add_parser("obs-heartbeat-now",
                             help="force one immediate heartbeat email (debug SMTP)")
    obs_hb.add_argument("--db-path", default=DEFAULT_DB)
    obs_hb.add_argument("--config", default="/etc/mthydra/controller.toml")

    # ----- spec J2: operator alert acks -----

    oaa = sub.add_parser("obs-alert-ack",
                           help="suppress alert dispatch for one dedupe_key until --expires-in")
    oaa.add_argument("dedupe_key")
    oaa.add_argument("--evidence", required=True,
                      help="operator-attested reason for the ack (recorded in audit + alert_acks)")
    oaa.add_argument("--expires-in", default="24h",
                      dest="expires_in",
                      help="duration; max 7d (default 24h). Format: <N>{s,m,h,d}")
    oaa.add_argument("--acked-by", default="operator", dest="acked_by")
    oaa.add_argument("--db-path", default=DEFAULT_DB)

    oaal = sub.add_parser("obs-alert-ack-list",
                            help="list alert acks; default shows only active ones")
    oaal.add_argument("--include-expired", action="store_true")
    oaal.add_argument("--db-path", default=DEFAULT_DB)
    oaal.add_argument("--json", action="store_true")

    # ----- spec D2: image canary + rollback subcommands -----

    ipst = sub.add_parser("image-promote-status",
                            help="read-only gate evaluation (does NOT promote; always exit 0)")
    ipst.add_argument("image_version")
    ipst.add_argument("--db-path", default=DEFAULT_DB)
    ipst.add_argument("--config", default="/etc/mthydra/controller.toml")
    ipst.add_argument("--json", action="store_true")

    irb = sub.add_parser("image-rollback",
                           help="retire <version>, re-promote --to <target>, flag live boxes")
    irb.add_argument("image_version")
    irb.add_argument("--to", required=True, dest="target_version")
    irb.add_argument("--evidence", required=True)
    irb.add_argument("--db-path", default=DEFAULT_DB)

    rbcc = sub.add_parser("ru-box-canary-clear",
                            help="demote a canary box to regular fleet")
    rbcc.add_argument("box_id")
    rbcc.add_argument("--reason", required=True)
    rbcc.add_argument("--db-path", default=DEFAULT_DB)

    # ----- spec I2: per-(box, vantage) probe credentials -----

    pci = sub.add_parser("probe-credential-issue",
                          help="issue a probe credential for a (box, vantage) pair")
    pci.add_argument("--box", required=True, dest="box_id")
    pci.add_argument("--vantage", required=True, dest="vantage_id")
    pci.add_argument("--evidence", default=None,
                      help="operator-attested rationale (recorded in audit)")
    pci.add_argument("--db-path", default=DEFAULT_DB)

    pcl = sub.add_parser("probe-credential-list",
                          help="list probe credentials")
    pcl.add_argument("--box", default=None, dest="box_id")
    pcl.add_argument("--vantage", default=None, dest="vantage_id")
    pcl.add_argument("--include-revoked", action="store_true")
    pcl.add_argument("--db-path", default=DEFAULT_DB)
    pcl.add_argument("--json", action="store_true")

    pcr = sub.add_parser("probe-credential-revoke",
                          help="revoke a probe credential by id")
    pcr.add_argument("cred_id")
    pcr.add_argument("--reason", required=True)
    pcr.add_argument("--db-path", default=DEFAULT_DB)

    # ----- spec M: append-only log compaction -----

    cl = sub.add_parser("compact-logs",
                          help="delete rows older than --before from append-only logs")
    cl.add_argument("--table", required=True,
                     choices=["alert_log", "probe_results", "distribution_log",
                              "alert_acks", "all"])
    cl.add_argument("--before", required=True,
                     help="ISO timestamp; rows with the table's natural ts column "
                          "strictly less than this are deleted")
    cl.add_argument("--no-dry-run", action="store_true",
                     help="actually delete rows (default is dry-run COUNT only)")
    cl.add_argument("--evidence", default=None,
                     help="operator-attested rationale (recorded in audit_log)")
    cl.add_argument("--db-path", default=DEFAULT_DB)

    # ----- spec K: distribution channel subcommands -----

    uc_set = sub.add_parser("user-channels-set",
                             help="register/update per-user Telegram + email contact points")
    uc_set.add_argument("user_id")
    uc_set.add_argument("--telegram", dest="telegram_chat_id", default=None)
    uc_set.add_argument("--email", dest="email_addr", default=None)
    uc_set.add_argument("--db-path", default=DEFAULT_DB)

    uc_show = sub.add_parser("user-channels-show",
                              help="show one user's registered channels")
    uc_show.add_argument("user_id")
    uc_show.add_argument("--db-path", default=DEFAULT_DB)
    uc_show.add_argument("--json", action="store_true")

    uc_list = sub.add_parser("user-channels-list",
                              help="list all registered user channels")
    uc_list.add_argument("--db-path", default=DEFAULT_DB)
    uc_list.add_argument("--json", action="store_true")

    ds = sub.add_parser("dist-status",
                          help="per-user shard + current subset + last-delivery summary")
    ds.add_argument("--db-path", default=DEFAULT_DB)
    ds.add_argument("--json", action="store_true")

    dpn = sub.add_parser("dist-publish-now",
                           help="force immediate publish for one user")
    dpn.add_argument("--user-id", required=True)
    dpn.add_argument("--db-path", default=DEFAULT_DB)
    dpn.add_argument("--config", default="/etc/mthydra/controller.toml")

    dt = sub.add_parser("dist-test",
                         help="send a synthetic test message to one user (deploy check)")
    dt.add_argument("--user-id", required=True)
    dt.add_argument("--db-path", default=DEFAULT_DB)
    dt.add_argument("--config", default="/etc/mthydra/controller.toml")

    dlr = sub.add_parser("dist-log-recent",
                          help="tail of distribution_log; most-recent first")
    dlr.add_argument("--user-id", default=None)
    dlr.add_argument("--limit", type=int, default=50)
    dlr.add_argument("--db-path", default=DEFAULT_DB)
    dlr.add_argument("--json", action="store_true")

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
            provider_credentials = _parse_kv(args.provider_credential)
            provider_credentials.update(_parse_kv_env(args.provider_credential_env))
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        try:
            init_state(
                db_path=args.db_path,
                age_recipient=recipient,
                provider_credentials=provider_credentials,
                obligation_timer_hours={
                    "backup_restore_dryrun": 720,
                    "t2_dryrun_caseA": 720,
                    "t2_dryrun_caseB": 720,
                    "t1_dormant_health": 168,
                    "t3_vantage_revalidation": 168,
                    "t3_profile_repin": 0,
                    "t4_upstream_check": 168,
                    "t5_pool_revalidation": 168,
                    # Spec H: replaces the placeholder t6_reshuffle. Cadence
                    # is reshuffle_interval_days x 2 (default 14d x 2 = 28d).
                    "shard_reshuffle_proven": 28 * 24,
                    "shard_reshuffle_sweep_ran": 1,
                    "shard_disjointness_check_proven": 24,
                    # Spec I — probe vantage harness obligations.
                    "probe_audit_sweep_ran": 1,
                    "probe_coverage_proven": 2,
                    "probe_vantage_rotation_proven": 28 * 24,
                    # Spec J — observability obligations.
                    "obs_alerter_sweep_ran": 1,
                    "obs_heartbeat_proven": 2,
                    # Spec K — distribution obligations.
                    "dist_publish_sweep_ran": 1,
                    "descriptor_signing_key_rotation": 8760,
                    "cover_pool_reverify_pass_proven": 60 * 24,
                    "cover_pool_replenishment_proven": 90 * 24,
                    "eu_standby_drill_proven": 30 * 24,
                    "t4_image_promoted":  30 * 24,
                    "g_provision_drill_proven":  90 * 24,
                    "e_ru_agent_provision_replace_drill_proven": 30 * 24,
                    "e_data_exit_drill_proven":                  30 * 24,
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

    if args.cmd == "provision-seed":
        return _cmd_provision_seed(args)

    if args.cmd == "ru-box-list":
        return _cmd_ru_box_list(args)
    if args.cmd == "ru-box-mark-live":
        return _cmd_ru_box_mark_live(args)
    if args.cmd == "ru-box-terminate":
        return _cmd_ru_box_terminate(args)

    if args.cmd == "data-exit-status":
        return _cmd_data_exit_status(args)
    if args.cmd == "data-exit-rewrite":
        return _cmd_data_exit_rewrite(args)
    if args.cmd == "data-exit-config-show":
        return _cmd_data_exit_config_show(args)
    if args.cmd == "data-exit-reality-keygen":
        return _cmd_data_exit_reality_keygen(args)

    if args.cmd == "user-add":
        return _cmd_user_add(args)
    if args.cmd == "user-list":
        return _cmd_user_list(args)
    if args.cmd == "shard-create":
        return _cmd_shard_create(args)
    if args.cmd == "shard-list":
        return _cmd_shard_list(args)
    if args.cmd == "shard-show":
        return _cmd_shard_show(args)
    if args.cmd == "shard-assign-box":
        return _cmd_shard_assign_box(args)
    if args.cmd == "shard-reshuffle":
        return _cmd_shard_reshuffle(args)
    if args.cmd == "shard-stats":
        return _cmd_shard_stats(args)

    if args.cmd == "vantage-add":
        return _cmd_vantage_add(args)
    if args.cmd == "vantage-attest-active":
        return _cmd_vantage_attest_active(args)
    if args.cmd == "vantage-list":
        return _cmd_vantage_list(args)
    if args.cmd == "vantage-retire":
        return _cmd_vantage_retire(args)
    if args.cmd == "vantage-burn":
        return _cmd_vantage_burn(args)
    if args.cmd == "profile-pin":
        return _cmd_profile_pin(args)
    if args.cmd == "profile-show":
        return _cmd_profile_show(args)
    if args.cmd == "probe-record":
        return _cmd_probe_record(args)
    if args.cmd == "probe-evaluate":
        return _cmd_probe_evaluate(args)
    if args.cmd == "probe-due":
        return _cmd_probe_due(args)

    if args.cmd == "obs-status":
        return _cmd_obs_status(args)
    if args.cmd == "obs-alerts-recent":
        return _cmd_obs_alerts_recent(args)
    if args.cmd == "obs-alert-test":
        return _cmd_obs_alert_test(args, mode)
    if args.cmd == "obs-heartbeat-now":
        return _cmd_obs_heartbeat_now(args, mode)
    if args.cmd == "obs-alert-ack":
        return _cmd_obs_alert_ack(args)
    if args.cmd == "obs-alert-ack-list":
        return _cmd_obs_alert_ack_list(args)

    if args.cmd == "image-promote-status":
        return _cmd_image_promote_status(args)
    if args.cmd == "image-rollback":
        return _cmd_image_rollback(args)
    if args.cmd == "ru-box-canary-clear":
        return _cmd_ru_box_canary_clear(args)
    if args.cmd == "compact-logs":
        return _cmd_compact_logs(args)
    if args.cmd == "probe-credential-issue":
        return _cmd_probe_credential_issue(args)
    if args.cmd == "probe-credential-list":
        return _cmd_probe_credential_list(args)
    if args.cmd == "probe-credential-revoke":
        return _cmd_probe_credential_revoke(args)

    if args.cmd == "user-channels-set":
        return _cmd_user_channels_set(args)
    if args.cmd == "user-channels-show":
        return _cmd_user_channels_show(args)
    if args.cmd == "user-channels-list":
        return _cmd_user_channels_list(args)
    if args.cmd == "dist-status":
        return _cmd_dist_status(args)
    if args.cmd == "dist-publish-now":
        return _cmd_dist_publish_now(args, mode)
    if args.cmd == "dist-test":
        return _cmd_dist_test(args, mode)
    if args.cmd == "dist-log-recent":
        return _cmd_dist_log_recent(args)

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

    # Spec J J-D1: active mode refuses to start without both sink credentials.
    if mode != "offline":
        if cfg.observability.telegram is None or cfg.observability.email is None:
            print(
                "serve: refusing — [observability.telegram] and "
                "[observability.email] are required for active mode",
                file=sys.stderr,
            )
            return 2
        # Spec K K-D10: same discipline for distribution sinks.
        if cfg.distribution.telegram is None or cfg.distribution.email is None:
            print(
                "serve: refusing — [distribution.telegram] and "
                "[distribution.email] are required for active mode",
                file=sys.stderr,
            )
            return 2

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

    # M12: validate LOCAL system state before arming any wheel — schema
    # version, SQL invariants, age recipient/binary. The documented operator
    # flow runs `startup-check` via preflight, but serve must not silently arm
    # against a broken DB. destination=None deliberately skips the network
    # reachability probe: a transient B2 outage must not stop the controller
    # from starting (the backup pipeline records such failures at runtime).
    # Skipped entirely in offline mode (used by tests).
    if mode != "offline":
        sc = run_startup_checks(
            db_path=args.db_path,
            age_recipient=recipient,
            mode=mode,
            bucket_override=args.bucket_override,
            destination=None,
        )
        if not sc.ok:
            print(
                f"serve: refusing — startup check failed "
                f"[{sc.failed_check}]: {sc.message}",
                file=sys.stderr,
            )
            return 10

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
    from mthydra.controller.shard_manager.wheel import ShardReshuffleWheel
    from mthydra.controller.probe.audit_wheel import ProbeAuditWheel
    from mthydra.controller.probe.evaluator import ProbeConfigView
    from mthydra.controller.observability.alerter import AlertSweep
    from mthydra.controller.observability.heartbeat import ObsHeartbeatPublisher
    from mthydra.controller.distribution.publisher import DistributionPublisher
    from mthydra.controller.distribution.user_heartbeat import (
        DistUserHeartbeatPublisher,
    )

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
    shard_wheel = ShardReshuffleWheel(
        db_path=args.db_path,
        target_size=cfg.shard_manager.target_size,
        max_size=cfg.shard_manager.max_size,
        reshuffle_interval_days=cfg.shard_manager.reshuffle_interval_days,
        sweep_interval_seconds=cfg.shard_manager.reshuffle_sweep_interval_seconds,
        mode=mode,
    )
    probe_wheel = ProbeAuditWheel(
        db_path=args.db_path,
        cfg=ProbeConfigView(
            soft_fail_window_M=cfg.probe.soft_fail_window_M,
            soft_fail_threshold_N=cfg.probe.soft_fail_threshold_N,
            min_distinct_vantages=cfg.probe.min_distinct_vantages,
        ),
        coverage_window_seconds=cfg.probe.coverage_window_seconds,
        probe_vantage_ttl_days=cfg.probe.probe_vantage_ttl_days,
        sweep_interval_seconds=cfg.probe.probe_audit_sweep_interval_seconds,
        mode=mode,
    )
    tg_sink, em_sink = _build_alert_sinks(cfg, mode)
    alerter = AlertSweep(
        db_path=args.db_path,
        telegram_sink=tg_sink,
        email_sink=em_sink,
        sweep_interval_seconds=cfg.observability.alerter_sweep_interval_seconds,
        dedupe_window_seconds={
            "warn": cfg.observability.alert_dedupe_window_warn_seconds,
            "crit": cfg.observability.alert_dedupe_window_crit_seconds,
            "info": cfg.observability.alert_dedupe_window_info_seconds,
        },
        staleness_alert_seconds=cfg.standby.staleness_alert_seconds,
        mode=mode,
    )
    obs_heartbeat = ObsHeartbeatPublisher(
        db_path=args.db_path,
        email_sink=em_sink,
        interval_seconds=cfg.observability.heartbeat_interval_seconds,
        breach_threshold=cfg.observability.heartbeat_breach_threshold,
        mode=mode,
    )
    dist_tg_sink, dist_em_sink = _build_dist_sinks(cfg, mode)
    dist_publisher = DistributionPublisher(
        db_path=args.db_path,
        telegram_sink=dist_tg_sink, email_sink=dist_em_sink,
        sweep_interval_seconds=cfg.distribution.publish_sweep_interval_seconds,
        mode=mode,
    )
    dist_user_heartbeat = DistUserHeartbeatPublisher(
        db_path=args.db_path,
        telegram_sink=dist_tg_sink,
        interval_seconds=cfg.distribution.user_heartbeat_interval_seconds,
        breach_threshold=cfg.distribution.heartbeat_breach_threshold,
        mode=mode,
    )

    if mode != "offline":
        orch.arm()
        rotator.arm()
        reverify_sweep.arm()
        rotation_sweep.arm()
        poller.arm()
        tracker.arm()
        shard_wheel.arm()
        probe_wheel.arm()
        alerter.arm()
        obs_heartbeat.arm()
        dist_publisher.arm()
        dist_user_heartbeat.arm()
        print("serve: backup orchestrator + descriptor rotator + cover-pool sweeps + standby poller + upstream tracker + shard wheel + probe audit wheel + alerter + obs heartbeat + dist publisher + dist user heartbeat armed", flush=True)
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
        shard_wheel.disarm()
        probe_wheel.disarm()
        alerter.disarm()
        obs_heartbeat.disarm()
        dist_publisher.disarm()
        dist_user_heartbeat.disarm()
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
    from mthydra.controller.state.image_profiles import pin as pin_profile
    from mthydra.controller.state.tokens import get_provider_credential

    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"image-build: config error: {e}", file=sys.stderr)
        return 2

    # Spec D2: read the candidate profile JSON before doing any network work so
    # we fail fast on a missing or unreadable file.
    try:
        if args.profile_json == "-":
            profile_text = sys.stdin.read()
        else:
            profile_text = Path(args.profile_json).read_text()
    except OSError as e:
        print(f"image-build: cannot read --profile-json: {e}", file=sys.stderr)
        return 2
    if not profile_text.strip():
        print("image-build: --profile-json must be non-empty", file=sys.stderr)
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
        # Spec D2: atomically pin the known-good profile for this image.
        pin_profile(
            conn,
            image_version=iv,
            profile_json=profile_text,
            recorded_by=args.profile_recorded_by,
            at=_now(),
            notes="auto-pinned at image-build time",
        )
        print(f"image-build: candidate {iv} registered with pinned profile "
              f"(release={args.release}, profile_recorded_by={args.profile_recorded_by!r})")
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
    import json as _json

    from mthydra.controller.config import ConfigError, load_config
    from mthydra.controller.image.gate import (
        GateConfigView, evaluate_promotion_gate,
    )
    from mthydra.controller.state.audit import log_event
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.ru_images import promote

    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"image-promote: config error: {e}", file=sys.stderr)
        return 2

    gate_cfg = GateConfigView(
        min_canary_boxes=cfg.image.canary.min_boxes,
        min_cycles_per_box=cfg.image.canary.min_cycles_per_box,
        min_distinct_vantages=cfg.probe.min_distinct_vantages,
    )

    conn = connect(args.db_path)
    try:
        rc = _require_active_role(conn, "image-promote")
        if rc is not None:
            return rc
        # Spec D2: gate before promoting.
        res = evaluate_promotion_gate(conn, args.image_version, cfg=gate_cfg)
        if not res.passed:
            for reason in res.reasons:
                print(f"image-promote: gate: {reason}", file=sys.stderr)
            log_event(
                conn, ts=_now(), actor="operator",
                action="image_promote_refused",
                target=args.image_version,
                details_json=_json.dumps({"reasons": list(res.reasons)}),
            )
            return 2
        try:
            promote(conn, args.image_version, at=_now(), evidence=args.evidence)
        except (ValueError, LookupError) as e:
            print(f"image-promote: {e}", file=sys.stderr)
            return 2
        print(f"image-promote: {args.image_version} -> promoted "
              f"(gate passed: {res.canary_probe_rows} probe rows across "
              f"{res.canary_distinct_vantages} distinct vantages)")
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


# ----- spec G: provisioning handlers -----


def _cmd_provision_seed(args) -> int:
    from mthydra.controller.config import ConfigError, load_config
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.tokens import get_provider_credential

    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"provision-seed: config error: {e}", file=sys.stderr)
        return 2

    if cfg.data_exit is None:
        print(
            "provision-seed: [data_exit] section is required in controller.toml "
            "(telegram_dcs_v4/v6 needed for seed bundle v2)",
            file=sys.stderr,
        )
        return 2

    conn = connect(args.db_path)
    try:
        rc = _require_active_role(conn, "provision-seed")
        if rc is not None:
            return rc
        try:
            secret = get_provider_credential(conn, "b2")
        except KeyError:
            print("provision-seed: b2 provider credential not in DB", file=sys.stderr)
            return 7
        dest = _build_destination(cfg, secret, mode="production",
                                   bucket_override=args.bucket_override)
        try:
            seed = provision_box(
                conn=conn, b2_destination=dest,
                provider=args.provider, region=args.region,
                image_signed_url_ttl_seconds=args.ttl_seconds,
                now=_now(),
                descriptor_refresh_url=args.descriptor_refresh_url,
                agent_source_url=args.agent_source_url,
                agent_source_sha256=args.agent_source_sha256,
                telegram_dcs_v4=cfg.data_exit.telegram_dcs_v4,
                telegram_dcs_v6=cfg.data_exit.telegram_dcs_v6,
                is_canary=args.is_canary,
            )
        except ProvisionError as e:
            print(f"provision-seed: {e}", file=sys.stderr)
            return 3
        except Exception as e:
            print(f"provision-seed: B2 URL minting failed: {e}", file=sys.stderr)
            return 5

        if args.format == "json":
            print(seed.to_json_pretty().decode("utf-8"))
        else:
            print(seed.to_cloud_init().decode("utf-8"))
        # Print box_id to stderr so callers (mthydra-ops ru-provision) can
        # parse it without parsing the cloud-init YAML or racing on
        # ru-box-list. Stdout still carries the chosen format unchanged.
        print(f"provision-seed: created box_id={seed.box_id}", file=sys.stderr)
        return 0
    finally:
        conn.close()


def _cmd_ru_box_list(args) -> int:
    import json as _json
    from dataclasses import asdict
    from mthydra.controller.state.db import connect

    conn = connect(args.db_path)
    try:
        rc = _require_active_role(conn, "ru-box-list")
        if rc is not None:
            return rc
        if args.state is None:
            rows = conn.execute(
                "SELECT box_id, provider, region, public_ip, sni, shard_id, state, "
                "image_version, created_at, went_live_at, terminated_at, termination_reason "
                "FROM ru_boxes ORDER BY created_at DESC"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT box_id, provider, region, public_ip, sni, shard_id, state, "
                "image_version, created_at, went_live_at, terminated_at, termination_reason "
                "FROM ru_boxes WHERE state=? ORDER BY created_at DESC", (args.state,)
            ).fetchall()
        cols = ("box_id", "provider", "region", "public_ip", "sni", "shard_id",
                "state", "image_version", "created_at", "went_live_at",
                "terminated_at", "termination_reason")
        out = [dict(zip(cols, r)) for r in rows]
        if args.json:
            print(_json.dumps(out, indent=2))
        else:
            print(f"{'state':14} {'box_id':38} {'sni':40} created_at")
            for r in out:
                print(f"{r['state']:14} {r['box_id']:38} {r['sni']:40} "
                      f"{r['created_at']}")
        return 0
    finally:
        conn.close()


def _cmd_ru_box_mark_live(args) -> int:
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.ru_boxes import mark_live

    conn = connect(args.db_path)
    try:
        rc = _require_active_role(conn, "ru-box-mark-live")
        if rc is not None:
            return rc
        row = conn.execute(
            "SELECT 1 FROM ru_boxes WHERE box_id=?", (args.box_id,)
        ).fetchone()
        if row is None:
            print(f"ru-box-mark-live: box {args.box_id!r} not found", file=sys.stderr)
            return 2
        try:
            mark_live(conn, args.box_id, public_ip=args.public_ip, at=_now())
        except ValueError as e:
            print(f"ru-box-mark-live: {e}", file=sys.stderr)
            return 2
        from mthydra.controller.state.audit import log_event
        log_event(
            conn, ts=_now(), actor="operator", action="ru_box_live",
            target=args.box_id, details_json=None,
        )
        print(f"ru-box-mark-live: {args.box_id} -> live (public_ip={args.public_ip})")
        return 0
    finally:
        conn.close()


def _cmd_ru_box_terminate(args) -> int:
    import json as _json
    import uuid as _uuid

    from mthydra.controller.shard_manager.picker import pick_new_rosters
    from mthydra.controller.state import shards as _shards
    from mthydra.controller.state.audit import log_event
    from mthydra.controller.state.burned import mark_burned
    from mthydra.controller.state.credentials import active_for_box, revoke_credential
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.ru_boxes import mark_terminated

    conn = connect(args.db_path)
    try:
        rc = _require_active_role(conn, "ru-box-terminate")
        if rc is not None:
            return rc
        row = conn.execute(
            "SELECT sni, state, shard_id FROM ru_boxes WHERE box_id=?", (args.box_id,)
        ).fetchone()
        if row is None:
            print(f"ru-box-terminate: box {args.box_id!r} not found", file=sys.stderr)
            return 2
        sni, prior_state, shard_id = row
        if prior_state == "terminated":
            print(f"ru-box-terminate: box {args.box_id!r} already terminated",
                  file=sys.stderr)
            return 2
        now = _now()
        # Note: cannot use BEGIN/COMMIT explicitly because the helpers call
        # conn.commit() internally. Run the steps in order; each is atomic
        # at the row level, and any failure leaves a clean partial state.
        for c in active_for_box(conn, args.box_id):
            revoke_credential(conn, c.cred_id, at=now)
        mark_burned(conn, sni, args.reason, args.box_id, now, None)
        mark_terminated(conn, args.box_id, reason=args.reason, at=now)
        log_event(
            conn, ts=now, actor="operator", action="ru_box_terminated",
            target=args.box_id,
            details_json=_json.dumps({"reason": args.reason, "prior_state": prior_state},
                                     separators=(",", ":")),
        )

        # Spec H H-D5: compromise terminate triggers immediate reshuffle of the
        # affected shard. The shard_id was retained by the H-D2 trigger.
        compromise_msg = ""
        if args.reason == "compromise" and shard_id:
            try:
                shard = _shards.get_shard(conn, shard_id)
            except LookupError:
                shard = None
            if shard is not None and shard.retired_at is None:
                members = _json.loads(shard.members_json)
                target_size = shard.target_size or 2
                rosters = pick_new_rosters(
                    current_members=members,
                    unassigned=[],
                    target_size=target_size,
                )
                if rosters:
                    primary = rosters[0]
                    new_sid = str(_uuid.uuid4())
                    _shards.reshuffle(
                        conn, shard_id,
                        now=now,
                        target_size=target_size,
                        new_shard_id=new_sid,
                        new_members=primary,
                        reason="compromise",
                    )
                    for leftover in rosters[1:]:
                        extra_sid = str(_uuid.uuid4())
                        _shards.create_shard(
                            conn, shard_id=extra_sid, members=leftover,
                            target_size=target_size, at=now,
                        )
                        for u in leftover:
                            conn.execute(
                                "UPDATE users SET current_shard_id=? WHERE user_id=?",
                                (extra_sid, u),
                            )
                        conn.commit()
                    compromise_msg = f"; shard {shard_id} -> {new_sid} (compromise reshuffle)"
                else:
                    # Empty shard: retire it (covers the unlikely edge case).
                    _shards.retire_shard(conn, shard_id, at=now)
                    compromise_msg = f"; shard {shard_id} retired (empty)"

        print(f"ru-box-terminate: {args.box_id} -> terminated; sni {sni!r} burned{compromise_msg}")
        return 0
    finally:
        conn.close()


def _cmd_data_exit_status(args) -> int:
    from mthydra.controller.config import ConfigError, load_config
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.eu_nodes import get_node

    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"data-exit-status: {e}", file=sys.stderr)
        return 2
    if cfg.data_exit is None:
        print("data-exit-status: [data_exit] section missing", file=sys.stderr)
        return 2
    conn = connect(args.db_path)
    try:
        node = get_node(conn, args.node_id)
        if node is None:
            print(f"data-exit-status: node {args.node_id!r} not found",
                  file=sys.stderr)
            return 2
        n_active = conn.execute(
            "SELECT COUNT(*) FROM ru_boxes rb JOIN onward_credentials oc "
            "ON oc.box_id = rb.box_id WHERE rb.state='live' "
            "AND rb.reality_uuid IS NOT NULL AND oc.revoked_at IS NULL"
        ).fetchone()[0]
        n_exit_rows = conn.execute(
            "SELECT COUNT(*) FROM eu_exit_set WHERE retired_at IS NULL"
        ).fetchone()[0]
        path = Path(cfg.data_exit.config_path)
        if path.exists():
            mtime_ts = os.path.getmtime(path)
            mtime = datetime.fromtimestamp(mtime_ts, tz=timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ")
        else:
            mtime = "(file not present)"
        print(f"node_id:            {args.node_id}")
        print(f"data_exit_state:    {node['data_exit_state']}")
        print(f"data_exit_started:  {node['data_exit_started_at']}")
        print(f"cover_sni:          {node['cover_sni']}")
        print(f"reality_pubkey:     {(node['reality_pubkey'] or '')[:32]}...")
        print(f"config_path:        {cfg.data_exit.config_path}")
        print(f"config_mtime:       {mtime}")
        print(f"users_allowlist:    {n_active}")
        print(f"eu_exit_set_rows:   {n_exit_rows}")
        return 0
    finally:
        conn.close()


def _cmd_data_exit_rewrite(args) -> int:
    from mthydra.controller.config import ConfigError, load_config
    from mthydra.controller.data_exit.wheel import DataExitWheel

    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"data-exit-rewrite: {e}", file=sys.stderr)
        return 2
    if cfg.data_exit is None:
        print("data-exit-rewrite: [data_exit] section missing", file=sys.stderr)
        return 2
    wheel = DataExitWheel(
        db_path=args.db_path, cfg=cfg.data_exit, node_id=args.node_id,
        mode="offline",  # do not start the scheduler; tick once.
    )
    try:
        wheel.tick()
    except Exception as e:
        print(f"data-exit-rewrite: tick failed: {e}", file=sys.stderr)
        return 5
    print(f"data-exit-rewrite: {args.node_id} config regenerated")
    return 0


def _cmd_data_exit_config_show(args) -> int:
    from mthydra.controller.config import ConfigError, load_config
    from mthydra.controller.data_exit.config_writer import render_sing_box_config
    from mthydra.controller.state.db import connect

    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"data-exit-config-show: {e}", file=sys.stderr)
        return 2
    if cfg.data_exit is None:
        print("data-exit-config-show: [data_exit] section missing", file=sys.stderr)
        return 2
    try:
        reality_pk = Path(cfg.data_exit.reality_key_path).read_text().strip()
    except FileNotFoundError:
        print("data-exit-config-show: reality key not present at "
              f"{cfg.data_exit.reality_key_path}", file=sys.stderr)
        return 4
    conn = connect(args.db_path)
    try:
        cover_sni = cfg.data_exit.cover_sni_for(args.node_id)
        content = render_sing_box_config(
            conn, cfg.data_exit, node_id=args.node_id,
            cover_sni=cover_sni, reality_private_key=reality_pk,
        )
        print(content.decode("utf-8"))
        return 0
    finally:
        conn.close()


def _cmd_data_exit_reality_keygen(args) -> int:
    import subprocess

    from mthydra.controller.config import ConfigError, load_config
    from mthydra.controller.state.audit import log_event
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.eu_nodes import (
        get_node, set_data_exit_identity,
    )

    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"data-exit-reality-keygen: {e}", file=sys.stderr)
        return 2
    if cfg.data_exit is None:
        print("data-exit-reality-keygen: [data_exit] section missing",
              file=sys.stderr)
        return 2
    conn = connect(args.db_path)
    try:
        node = get_node(conn, args.node_id)
        if node is None:
            print(f"data-exit-reality-keygen: node {args.node_id!r} not found",
                  file=sys.stderr)
            return 2
        if node["reality_pubkey"]:
            print("data-exit-reality-keygen: already has reality_pubkey; "
                  "rotation deferred to a future spec", file=sys.stderr)
            return 3
        result = subprocess.run(
            ["sing-box", "generate", "reality-keypair"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            print(f"data-exit-reality-keygen: sing-box failed: {result.stderr}",
                  file=sys.stderr)
            return 5
        priv = None
        pub = None
        for line in result.stdout.splitlines():
            line = line.strip()
            if line.startswith("PrivateKey:"):
                priv = line.split(":", 1)[1].strip()
            elif line.startswith("PublicKey:"):
                pub = line.split(":", 1)[1].strip()
        if priv is None or pub is None:
            print("data-exit-reality-keygen: could not parse keypair output",
                  file=sys.stderr)
            return 5
        key_path = Path(cfg.data_exit.reality_key_path)
        key_path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write: tempfile in same dir → 0600 → fsync → rename → fsync
        # parent dir. write_text() left a partial file on crash and the next
        # boot would read a truncated private key (M16).
        fd, tmp_path = tempfile.mkstemp(
            prefix=key_path.name + ".", suffix=".tmp",
            dir=str(key_path.parent),
        )
        try:
            os.chmod(tmp_path, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(priv + "\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, key_path)
            dir_fd = os.open(str(key_path.parent), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            except OSError:
                pass    # best-effort on filesystems that reject dir fsync
            finally:
                os.close(dir_fd)
        except Exception:
            try:
                os.unlink(tmp_path)
            except FileNotFoundError:
                pass
            raise
        cover_sni = cfg.data_exit.cover_sni_for(args.node_id)
        set_data_exit_identity(
            conn, args.node_id, cover_sni=cover_sni, reality_pubkey=pub,
        )
        log_event(
            conn, ts=_now(), actor="operator", action="data_exit_reality_keygen",
            target=args.node_id, details_json=f'{{"evidence":{args.evidence!r}}}',
        )
        print(f"data-exit-reality-keygen: {args.node_id} key generated "
              f"(pubkey={pub[:32]}...)")
        return 0
    finally:
        conn.close()


# ----- spec H: shard manager subcommands -----


def _cmd_user_add(args) -> int:
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.users_shards import add_user
    conn = connect(args.db_path)
    try:
        try:
            add_user(
                conn,
                user_id=args.user_id,
                display_name=args.display_name,
                out_of_band_channel=args.out_of_band_channel,
                at=_now(),
            )
        except sqlite3.IntegrityError as e:
            print(f"user-add: {e}", file=sys.stderr)
            return 2
        print(f"user-add: {args.user_id} added")
        return 0
    finally:
        conn.close()


def _cmd_user_list(args) -> int:
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.users_shards import list_users
    conn = connect(args.db_path)
    try:
        users = list_users(conn)
        if args.json:
            import json as _json
            print(_json.dumps(
                [{
                    "user_id": u.user_id,
                    "display_name": u.display_name,
                    "out_of_band_channel": u.out_of_band_channel,
                    "current_shard_id": u.current_shard_id,
                    "added_at": u.added_at,
                } for u in users],
                sort_keys=True,
            ))
        else:
            for u in users:
                shard = u.current_shard_id or "<unassigned>"
                name = u.display_name or "-"
                print(f"{u.user_id}\t{name}\t{u.out_of_band_channel}\t{shard}")
        return 0
    finally:
        conn.close()


def _cmd_shard_create(args) -> int:
    from mthydra.controller.config import ConfigError, load_config
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.shards import create_shard
    from mthydra.controller.state.users_shards import set_user_shard
    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"shard-create: config error: {e}", file=sys.stderr)
        return 2
    members = [m.strip() for m in args.members.split(",") if m.strip()]
    if not members:
        print("shard-create: --members must list at least one user", file=sys.stderr)
        return 2
    conn = connect(args.db_path)
    try:
        # Refuse if any listed user already belongs to an active shard.
        for u in members:
            row = conn.execute(
                "SELECT u.current_shard_id FROM users u "
                "LEFT JOIN shards s ON s.shard_id = u.current_shard_id "
                "WHERE u.user_id=? AND s.retired_at IS NULL",
                (u,),
            ).fetchone()
            if row is not None and row[0]:
                print(
                    f"shard-create: user {u!r} already in active shard {row[0]!r}",
                    file=sys.stderr,
                )
                return 2
        try:
            create_shard(
                conn,
                shard_id=args.shard_id,
                members=members,
                target_size=cfg.shard_manager.target_size,
                at=_now(),
            )
        except sqlite3.IntegrityError as e:
            print(f"shard-create: {e}", file=sys.stderr)
            return 2
        for u in members:
            set_user_shard(conn, u, args.shard_id)
        print(f"shard-create: {args.shard_id} created with members {members}")
        return 0
    finally:
        conn.close()


def _cmd_shard_list(args) -> int:
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.shards import list_active, list_all
    conn = connect(args.db_path)
    try:
        shards = list_all(conn) if args.include_retired else list_active(conn)
        if args.json:
            import json as _json
            print(_json.dumps(
                [{
                    "shard_id": s.shard_id,
                    "members": _json.loads(s.members_json),
                    "target_size": s.target_size,
                    "last_reshuffled_at": s.last_reshuffled_at,
                    "created_at": s.created_at,
                    "retired_at": s.retired_at,
                } for s in shards],
                sort_keys=True,
            ))
        else:
            import json as _json
            for s in shards:
                mem = _json.loads(s.members_json)
                status = "retired" if s.retired_at else "active"
                print(f"{s.shard_id}\t{status}\t{len(mem)}\t{s.last_reshuffled_at}")
        return 0
    finally:
        conn.close()


def _cmd_shard_show(args) -> int:
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.shards import get_shard, list_shard_boxes
    conn = connect(args.db_path)
    try:
        try:
            shard = get_shard(conn, args.shard_id)
        except LookupError as e:
            print(f"shard-show: {e}", file=sys.stderr)
            return 2
        boxes = list_shard_boxes(conn, args.shard_id, include_terminated=True)
        import json as _json
        members = _json.loads(shard.members_json)
        out = {
            "shard_id": shard.shard_id,
            "members": members,
            "target_size": shard.target_size,
            "last_reshuffled_at": shard.last_reshuffled_at,
            "created_at": shard.created_at,
            "retired_at": shard.retired_at,
            "boxes": boxes,
        }
        if args.json:
            print(_json.dumps(out, sort_keys=True))
        else:
            print(f"shard_id: {shard.shard_id}")
            print(f"  members: {members}")
            print(f"  target_size: {shard.target_size}")
            print(f"  last_reshuffled_at: {shard.last_reshuffled_at}")
            print(f"  created_at: {shard.created_at}")
            print(f"  retired_at: {shard.retired_at or '<active>'}")
            print(f"  boxes: {boxes}")
        return 0
    finally:
        conn.close()


def _cmd_shard_assign_box(args) -> int:
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.shards import assign_box_to_shard
    conn = connect(args.db_path)
    try:
        try:
            assign_box_to_shard(
                conn, box_id=args.box_id, shard_id=args.shard_id, at=_now(),
            )
        except LookupError as e:
            print(f"shard-assign-box: {e}", file=sys.stderr)
            return 2
        except sqlite3.IntegrityError as e:
            print(f"shard-assign-box: {e}", file=sys.stderr)
            return 2
        print(f"shard-assign-box: box {args.box_id} -> shard {args.shard_id}")
        return 0
    finally:
        conn.close()


def _cmd_shard_reshuffle(args) -> int:
    import json as _json
    import uuid as _uuid
    from mthydra.controller.config import ConfigError, load_config
    from mthydra.controller.shard_manager.picker import pick_new_rosters
    from mthydra.controller.state import shards as _shards
    from mthydra.controller.state.db import connect
    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"shard-reshuffle: config error: {e}", file=sys.stderr)
        return 2
    conn = connect(args.db_path)
    try:
        try:
            old = _shards.get_shard(conn, args.shard_id)
        except LookupError as e:
            print(f"shard-reshuffle: {e}", file=sys.stderr)
            return 2
        if old.retired_at is not None:
            print(f"shard-reshuffle: {args.shard_id} already retired",
                  file=sys.stderr)
            return 2
        rosters = pick_new_rosters(
            current_members=_json.loads(old.members_json),
            unassigned=[],
            target_size=cfg.shard_manager.target_size,
        )
        if not rosters:
            print(f"shard-reshuffle: {args.shard_id} is empty (nothing to reshuffle)",
                  file=sys.stderr)
            return 2
        primary = rosters[0]
        new_sid = str(_uuid.uuid4())
        _shards.reshuffle(
            conn, args.shard_id,
            now=_now(),
            target_size=cfg.shard_manager.target_size,
            new_shard_id=new_sid,
            new_members=primary,
            reason=args.reason,
        )
        for leftover in rosters[1:]:
            extra_sid = str(_uuid.uuid4())
            _shards.create_shard(
                conn, shard_id=extra_sid, members=leftover,
                target_size=cfg.shard_manager.target_size, at=_now(),
            )
            for u in leftover:
                conn.execute(
                    "UPDATE users SET current_shard_id=? WHERE user_id=?",
                    (extra_sid, u),
                )
            conn.commit()
        print(f"shard-reshuffle: {args.shard_id} -> {new_sid} (reason={args.reason!r})")
        return 0
    finally:
        conn.close()


def _cmd_shard_stats(args) -> int:
    from mthydra.controller.config import ConfigError, load_config
    from mthydra.controller.state import shards as _shards
    from mthydra.controller.state.db import connect
    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"shard-stats: config error: {e}", file=sys.stderr)
        return 2
    conn = connect(args.db_path)
    try:
        h = _shards.health(
            conn, now=_now(),
            reshuffle_interval_seconds=cfg.shard_manager.reshuffle_interval_days * 86400,
        )
        last_sweep = conn.execute(
            "SELECT last_proven_at FROM obligation_clocks "
            "WHERE obligation_id='shard_reshuffle_sweep_ran'"
        ).fetchone()
        out = {
            "total_active": h.total_active,
            "total_retired": h.total_retired,
            "oldest_active_age_seconds": h.oldest_active_age_seconds,
            "overdue_for_reshuffle": h.overdue_for_reshuffle,
            "unassigned_users": h.unassigned_users,
            "last_sweep_at": last_sweep[0] if last_sweep else None,
        }
        if args.json:
            import json as _json
            print(_json.dumps(out, sort_keys=True))
        else:
            print(f"active:    {h.total_active}")
            print(f"retired:   {h.total_retired}")
            print(f"oldest age (s): {h.oldest_active_age_seconds}")
            print(f"overdue:   {h.overdue_for_reshuffle}")
            print(f"unassigned: {h.unassigned_users}")
            print(f"last sweep: {out['last_sweep_at']}")
        return 0
    finally:
        conn.close()


# ----- spec I: probe vantage harness subcommands -----


def _cmd_vantage_add(args) -> int:
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.probe_vantages import add_candidate
    conn = connect(args.db_path)
    try:
        try:
            add_candidate(
                conn,
                vantage_id=args.vantage_id,
                label=args.label,
                source_kind=args.source_kind,
                at=_now(),
                region_hint=args.region_hint,
                notes=args.notes,
            )
        except sqlite3.IntegrityError as e:
            print(f"vantage-add: {e}", file=sys.stderr)
            return 2
        print(f"vantage-add: {args.vantage_id} added (candidate)")
        return 0
    finally:
        conn.close()


def _cmd_vantage_attest_active(args) -> int:
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.probe_vantages import attest_active
    conn = connect(args.db_path)
    try:
        try:
            attest_active(conn, args.vantage_id, at=_now(), evidence=args.evidence)
        except (LookupError, ValueError) as e:
            print(f"vantage-attest-active: {e}", file=sys.stderr)
            return 2
        print(f"vantage-attest-active: {args.vantage_id} -> active")
        return 0
    finally:
        conn.close()


def _cmd_vantage_list(args) -> int:
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.probe_vantages import list_by_state
    conn = connect(args.db_path)
    try:
        rows = list_by_state(conn, args.state)
        if args.json:
            import json as _json
            print(_json.dumps(
                [{
                    "vantage_id": v.vantage_id,
                    "label": v.label,
                    "source_kind": v.source_kind,
                    "region_hint": v.region_hint,
                    "state": v.state,
                    "added_at": v.added_at,
                    "attested_at": v.attested_at,
                    "last_used_at": v.last_used_at,
                    "retired_at": v.retired_at,
                    "burned_at": v.burned_at,
                    "burn_reason": v.burn_reason,
                } for v in rows],
                sort_keys=True,
            ))
        else:
            for v in rows:
                print(f"{v.vantage_id}\t{v.state}\t{v.label}\t{v.source_kind}"
                      f"\t{v.region_hint or '-'}")
        return 0
    finally:
        conn.close()


def _cmd_vantage_retire(args) -> int:
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.probe_vantages import retire
    conn = connect(args.db_path)
    try:
        try:
            retire(conn, args.vantage_id, at=_now(), reason=args.reason)
        except (LookupError, ValueError) as e:
            print(f"vantage-retire: {e}", file=sys.stderr)
            return 2
        print(f"vantage-retire: {args.vantage_id} -> retired")
        return 0
    finally:
        conn.close()


def _cmd_vantage_burn(args) -> int:
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.probe_vantages import burn
    conn = connect(args.db_path)
    try:
        try:
            burn(conn, args.vantage_id, at=_now(), reason=args.reason)
        except (LookupError, ValueError) as e:
            print(f"vantage-burn: {e}", file=sys.stderr)
            return 2
        print(f"vantage-burn: {args.vantage_id} -> burned (label permanently poisoned)")
        return 0
    finally:
        conn.close()


def _read_profile_json(arg: str) -> str:
    if arg == "-":
        return sys.stdin.read()
    p = Path(arg)
    return p.read_text()


def _cmd_profile_pin(args) -> int:
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.image_profiles import pin
    try:
        profile_json = _read_profile_json(args.profile_json)
    except OSError as e:
        print(f"profile-pin: cannot read profile: {e}", file=sys.stderr)
        return 2
    conn = connect(args.db_path)
    try:
        try:
            pin(
                conn,
                image_version=args.image_version,
                profile_json=profile_json,
                recorded_by=args.recorded_by,
                at=_now(),
                notes=args.notes,
            )
        except (LookupError, ValueError) as e:
            print(f"profile-pin: {e}", file=sys.stderr)
            return 2
        print(f"profile-pin: {args.image_version} profile pinned by {args.recorded_by}")
        return 0
    finally:
        conn.close()


def _cmd_profile_show(args) -> int:
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.image_profiles import get_profile
    conn = connect(args.db_path)
    try:
        p = get_profile(conn, args.image_version)
        if p is None:
            print(f"profile-show: no profile for {args.image_version!r}",
                  file=sys.stderr)
            return 2
        if args.json:
            import json as _json
            print(_json.dumps({
                "image_version": p.image_version,
                "profile_json": p.profile_json,
                "recorded_at": p.recorded_at,
                "recorded_by": p.recorded_by,
                "notes": p.notes,
            }, sort_keys=True))
        else:
            print(f"image_version: {p.image_version}")
            print(f"recorded_by: {p.recorded_by}")
            print(f"recorded_at: {p.recorded_at}")
            print(f"notes: {p.notes or '-'}")
            print("profile_json:")
            print(p.profile_json)
        return 0
    finally:
        conn.close()


def _cmd_probe_record(args) -> int:
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.probe_results import record
    evidence = None
    if args.evidence:
        # Treat --evidence as text by default; if it's a path that exists, read it.
        p = Path(args.evidence)
        if p.exists():
            try:
                evidence = p.read_text()
            except OSError:
                evidence = args.evidence
        else:
            evidence = args.evidence
    conn = connect(args.db_path)
    try:
        image_version = args.image_version
        if image_version is None:
            row = conn.execute(
                "SELECT image_version FROM ru_boxes WHERE box_id=?", (args.box_id,)
            ).fetchone()
            if row is None:
                print(f"probe-record: unknown box {args.box_id!r}", file=sys.stderr)
                return 2
            image_version = row[0]
        try:
            rid = record(
                conn,
                box_id=args.box_id,
                vantage_id=args.vantage_id,
                cycle_at=args.cycle_at,
                check_type=args.check_type,
                status=args.status,
                evidence_json=evidence,
                image_version=image_version,
                recorded_at=_now(),
            )
        except (LookupError, ValueError) as e:
            print(f"probe-record: {e}", file=sys.stderr)
            return 2
        print(f"probe-record: id={rid} box={args.box_id} vantage={args.vantage_id} "
              f"check={args.check_type} status={args.status}")
        return 0
    finally:
        conn.close()


def _cmd_probe_evaluate(args) -> int:
    from mthydra.controller.config import ConfigError, load_config
    from mthydra.controller.probe.evaluator import (
        EvaluationError, ProbeConfigView, evaluate_box,
    )
    from mthydra.controller.state.db import connect
    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"probe-evaluate: config error: {e}", file=sys.stderr)
        return 2
    view = ProbeConfigView(
        soft_fail_window_M=cfg.probe.soft_fail_window_M,
        soft_fail_threshold_N=cfg.probe.soft_fail_threshold_N,
        min_distinct_vantages=cfg.probe.min_distinct_vantages,
    )
    conn = connect(args.db_path)
    try:
        try:
            res = evaluate_box(conn, box_id=args.box_id, cfg=view, now=_now())
        except EvaluationError as e:
            print(f"probe-evaluate: {e}", file=sys.stderr)
            return 2
        if args.json:
            import json as _json
            print(_json.dumps({
                "box_id": res.box_id,
                "verdict": res.verdict,
                "offending_checks": list(res.offending_checks),
                "distinct_vantages_consulted": res.distinct_vantages_consulted,
                "evidence_pointer": list(res.evidence_pointer),
            }, sort_keys=True))
        else:
            print(f"box: {res.box_id}")
            print(f"verdict: {res.verdict}")
            print(f"distinct_vantages: {res.distinct_vantages_consulted}")
            if res.offending_checks:
                print(f"offending: {list(res.offending_checks)}")
            if res.evidence_pointer:
                print(f"evidence_ids: {list(res.evidence_pointer)}")
        return 0
    finally:
        conn.close()


def _cmd_probe_due(args) -> int:
    from mthydra.controller.state.db import connect
    conn = connect(args.db_path)
    try:
        kill = [
            r[0] for r in conn.execute(
                "SELECT obligation_id FROM obligation_clocks "
                "WHERE obligation_id LIKE 'probe_kill_pending::%' ORDER BY obligation_id"
            ).fetchall()
        ]
        coverage = [
            r[0] for r in conn.execute(
                "SELECT obligation_id FROM obligation_clocks "
                "WHERE obligation_id LIKE 'probe_coverage_pending::%' ORDER BY obligation_id"
            ).fetchall()
        ]
        rotation = [
            r[0] for r in conn.execute(
                "SELECT obligation_id FROM obligation_clocks "
                "WHERE obligation_id LIKE 'probe_vantage_rotation_pending::%' ORDER BY obligation_id"
            ).fetchall()
        ]
        blocked = [
            r[0] for r in conn.execute(
                "SELECT obligation_id FROM obligation_clocks "
                "WHERE obligation_id LIKE 'probe_evaluate_blocked::%' ORDER BY obligation_id"
            ).fetchall()
        ]
        out = {
            "kill_pending": kill,
            "coverage_pending": coverage,
            "rotation_pending": rotation,
            "evaluate_blocked": blocked,
        }
        if args.json:
            import json as _json
            print(_json.dumps(out, sort_keys=True))
        else:
            print(f"kill_pending:     {kill}")
            print(f"coverage_pending: {coverage}")
            print(f"rotation_pending: {rotation}")
            print(f"evaluate_blocked: {blocked}")
        return 0
    finally:
        conn.close()


# ----- spec J: observability subcommands -----


def _build_alert_sinks(cfg, mode: str):
    """Return (telegram_sink, email_sink). Both are DryRunSinks in offline mode
    OR when the corresponding credential section is missing."""
    from mthydra.controller.observability.sinks import (
        DryRunSink, EmailAlertSink, TelegramAlertSink,
    )
    if mode == "offline":
        return DryRunSink(label="telegram"), DryRunSink(label="email")
    tg = cfg.observability.telegram
    em = cfg.observability.email
    tg_sink = (
        TelegramAlertSink(bot_token=tg.bot_token, chat_id=tg.chat_id)
        if tg is not None else DryRunSink(label="telegram")
    )
    em_sink = (
        EmailAlertSink(
            smtp_host=em.smtp_host, smtp_port=em.smtp_port,
            from_addr=em.from_addr, to_addr=em.to_addr,
            username=em.username, password=em.password,
        )
        if em is not None else DryRunSink(label="email")
    )
    return tg_sink, em_sink


def _cmd_obs_status(args) -> int:
    from mthydra.controller.observability.snapshot import collect_snapshot
    from mthydra.controller.state.db import connect
    conn = connect(args.db_path)
    try:
        snap = collect_snapshot(conn, now=_now())
        if args.json:
            import json as _json
            print(_json.dumps({
                "collected_at": snap.collected_at,
                "summary_line": snap.summary_line,
                "obligations_healthy": [
                    {"obligation_id": o.obligation_id, "severity": o.severity}
                    for o in snap.obligations_healthy
                ],
                "obligations_overdue": [
                    {"obligation_id": o.obligation_id,
                     "overdue_seconds": o.overdue_seconds,
                     "severity": o.severity}
                    for o in snap.obligations_overdue
                ],
                "anti_obligations": [
                    {"obligation_id": a.obligation_id, "kind": a.kind,
                     "target": a.target, "severity": a.severity}
                    for a in snap.anti_obligations
                ],
                "eu_nodes": [
                    {"node_id": n.node_id, "role": n.role,
                     "heartbeat_age_seconds": n.heartbeat_age_seconds,
                     "severity": n.severity}
                    for n in snap.eu_nodes
                ],
                "counts": {
                    "boxes_provisioning": snap.counts.boxes_provisioning,
                    "boxes_live": snap.counts.boxes_live,
                    "boxes_terminated": snap.counts.boxes_terminated,
                    "cover_domains_in_use": snap.counts.cover_domains_in_use,
                    "cover_domains_burned": snap.counts.cover_domains_burned,
                    "active_vantages": snap.counts.active_vantages,
                    "active_shards": snap.counts.active_shards,
                },
            }, sort_keys=True))
        else:
            print(f"collected: {snap.collected_at}")
            print(snap.summary_line)
            if snap.obligations_overdue:
                print("OVERDUE:")
                for o in snap.obligations_overdue:
                    print(f"  [{o.severity}] {o.obligation_id} "
                          f"overdue={o.overdue_seconds}s")
            if snap.anti_obligations:
                print("ANTI-OBLIGATIONS:")
                for a in snap.anti_obligations:
                    print(f"  [{a.severity}] {a.obligation_id}")
            if snap.eu_nodes:
                print("EU NODES:")
                for n in snap.eu_nodes:
                    print(f"  {n.node_id} role={n.role} "
                          f"age={n.heartbeat_age_seconds}s "
                          f"sev={n.severity}")
        return 0
    finally:
        conn.close()


def _cmd_obs_alerts_recent(args) -> int:
    from mthydra.controller.state import alert_log as _al
    from mthydra.controller.state.db import connect
    conn = connect(args.db_path)
    try:
        rows = _al.recent(conn, limit=args.limit, severity=args.severity)
        if args.json:
            import json as _json
            print(_json.dumps([
                {
                    "id": r.id,
                    "attempted_at": r.attempted_at,
                    "delivered_at": r.delivered_at,
                    "sink": r.sink, "severity": r.severity,
                    "kind": r.kind, "target": r.target,
                    "dedupe_key": r.dedupe_key,
                    "error": r.error,
                } for r in rows
            ], sort_keys=True))
        else:
            for r in rows:
                status = "OK" if r.delivered_at else "FAIL"
                err = f" err={r.error!r}" if r.error else ""
                print(f"#{r.id} {r.attempted_at} [{r.severity}] "
                      f"{r.sink} {status} {r.kind} {r.target or ''}{err}")
        return 0
    finally:
        conn.close()


def _cmd_obs_alert_test(args, mode: str) -> int:
    from mthydra.controller.config import ConfigError, load_config
    from mthydra.controller.observability.sinks import AlertPayload
    from mthydra.controller.state import alert_log as _al
    from mthydra.controller.state.db import connect
    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"obs-alert-test: config error: {e}", file=sys.stderr)
        return 2
    tg_sink, em_sink = _build_alert_sinks(cfg, mode)
    severity = args.severity
    routing = {"crit": ("telegram", "email"),
               "warn": ("telegram",), "info": ()}.get(severity, ())
    if not routing:
        print(f"obs-alert-test: severity={severity!r} does not route to any sink",
              file=sys.stderr)
        return 2
    now = _now()
    body = args.message or f"synthetic alert at {now}"
    payload = AlertPayload(
        severity=severity, kind="operator_test", target=None,
        dedupe_key=f"operator_test::{now}",
        subject=f"mthydra alert test [{severity}]",
        body=body,
    )
    conn = connect(args.db_path)
    try:
        for label in routing:
            sink = tg_sink if label == "telegram" else em_sink
            try:
                res = sink(payload)
                success = bool(getattr(res, "success", False))
                err = getattr(res, "error", None)
            except Exception as e:
                success = False
                err = repr(e)
            _al.append(
                conn, attempted_at=now,
                delivered_at=now if success else None,
                sink=label, severity=severity, kind="operator_test",
                target=None, dedupe_key=payload.dedupe_key,
                payload=f"{payload.subject}\n\n{payload.body}",
                error=err,
            )
            print(f"obs-alert-test: {label} -> "
                  f"{'OK' if success else 'FAIL ' + (err or '')}")
        return 0
    finally:
        conn.close()


def _cmd_obs_heartbeat_now(args, mode: str) -> int:
    from mthydra.controller.config import ConfigError, load_config
    from mthydra.controller.observability.heartbeat import ObsHeartbeatPublisher
    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"obs-heartbeat-now: config error: {e}", file=sys.stderr)
        return 2
    _, em_sink = _build_alert_sinks(cfg, mode)
    pub = ObsHeartbeatPublisher(
        db_path=args.db_path,
        email_sink=em_sink,
        interval_seconds=cfg.observability.heartbeat_interval_seconds,
        breach_threshold=cfg.observability.heartbeat_breach_threshold,
        mode=mode,
        clock=_now,
    )
    res = pub.run_once()
    print(f"obs-heartbeat-now: "
          f"{'OK' if res['success'] else 'FAIL'} "
          f"consecutive_failures={res['consecutive_failures']}")
    return 0 if res["success"] else 2


# ----- spec K: distribution channel subcommands -----


def _build_dist_sinks(cfg, mode: str):
    """Return (telegram_sink, email_sink). DryRun in offline OR when creds missing."""
    from mthydra.controller.distribution.sinks import (
        DryRunDistributionSink, EmailDistributionSink, TelegramDistributionSink,
    )
    if mode == "offline":
        return (DryRunDistributionSink(label="telegram"),
                DryRunDistributionSink(label="email"))
    tg = cfg.distribution.telegram
    em = cfg.distribution.email
    tg_sink = (
        TelegramDistributionSink(bot_token=tg.bot_token)
        if tg is not None else DryRunDistributionSink(label="telegram")
    )
    em_sink = (
        EmailDistributionSink(
            smtp_host=em.smtp_host, smtp_port=em.smtp_port,
            from_addr=em.from_addr,
            username=em.username, password=em.password,
        )
        if em is not None else DryRunDistributionSink(label="email")
    )
    return tg_sink, em_sink


def _cmd_user_channels_set(args) -> int:
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.user_channels import set_channels
    if not args.telegram_chat_id and not args.email_addr:
        print("user-channels-set: at least one of --telegram or --email required",
              file=sys.stderr)
        return 2
    conn = connect(args.db_path)
    try:
        try:
            set_channels(
                conn, args.user_id,
                telegram_chat_id=args.telegram_chat_id,
                email_addr=args.email_addr, at=_now(),
            )
        except sqlite3.IntegrityError as e:
            print(f"user-channels-set: {e}", file=sys.stderr)
            return 2
        print(f"user-channels-set: {args.user_id} registered")
        return 0
    finally:
        conn.close()


def _cmd_user_channels_show(args) -> int:
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.user_channels import get_channels
    conn = connect(args.db_path)
    try:
        row = get_channels(conn, args.user_id)
        if row is None:
            print(f"user-channels-show: no channels for {args.user_id!r}",
                  file=sys.stderr)
            return 2
        if args.json:
            import json as _json
            print(_json.dumps({
                "user_id": row.user_id,
                "telegram_chat_id": row.telegram_chat_id,
                "email_addr": row.email_addr,
                "registered_at": row.registered_at,
                "updated_at": row.updated_at,
            }, sort_keys=True))
        else:
            print(f"user_id: {row.user_id}")
            print(f"telegram: {row.telegram_chat_id or '-'}")
            print(f"email:    {row.email_addr or '-'}")
            print(f"registered: {row.registered_at}")
            print(f"updated:    {row.updated_at}")
        return 0
    finally:
        conn.close()


def _cmd_user_channels_list(args) -> int:
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.user_channels import list_channels
    conn = connect(args.db_path)
    try:
        rows = list_channels(conn)
        if args.json:
            import json as _json
            print(_json.dumps([
                {
                    "user_id": r.user_id,
                    "telegram_chat_id": r.telegram_chat_id,
                    "email_addr": r.email_addr,
                } for r in rows
            ], sort_keys=True))
        else:
            for r in rows:
                tg = r.telegram_chat_id or "-"
                em = r.email_addr or "-"
                print(f"{r.user_id}\tTG:{tg}\tEM:{em}")
        return 0
    finally:
        conn.close()


def _cmd_dist_status(args) -> int:
    from mthydra.controller.distribution.payload import build_subset
    from mthydra.controller.state import distribution_log as _dl
    from mthydra.controller.state.db import connect
    conn = connect(args.db_path)
    try:
        users = [
            r[0] for r in conn.execute(
                "SELECT user_id FROM users WHERE current_shard_id IS NOT NULL "
                "ORDER BY user_id"
            ).fetchall()
        ]
        records = []
        for u in users:
            payload = build_subset(conn, u, now=_now())
            shard = payload.shard_id if payload else None
            box_count = len(payload.boxes) if payload else 0
            tg_last = _dl.last_subset_hash(conn, u, "telegram")
            em_last = _dl.last_subset_hash(conn, u, "email")
            records.append({
                "user_id": u, "shard_id": shard,
                "current_box_count": box_count,
                "last_subset_hash_telegram": tg_last,
                "last_subset_hash_email": em_last,
            })
        if args.json:
            import json as _json
            print(_json.dumps(records, sort_keys=True))
        else:
            for r in records:
                print(f"{r['user_id']}\tshard={r['shard_id']}\t"
                      f"boxes={r['current_box_count']}\t"
                      f"tg={r['last_subset_hash_telegram'] or '-'[:8]}\t"
                      f"em={r['last_subset_hash_email'] or '-'[:8]}")
        return 0
    finally:
        conn.close()


def _cmd_dist_publish_now(args, mode: str) -> int:
    from mthydra.controller.config import ConfigError, load_config
    from mthydra.controller.distribution.publisher import DistributionPublisher
    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"dist-publish-now: config error: {e}", file=sys.stderr)
        return 2
    tg_sink, em_sink = _build_dist_sinks(cfg, mode)
    pub = DistributionPublisher(
        db_path=args.db_path,
        telegram_sink=tg_sink, email_sink=em_sink,
        sweep_interval_seconds=cfg.distribution.publish_sweep_interval_seconds,
        mode=mode, clock=_now,
    )
    res = pub.run_once()
    print(f"dist-publish-now: dispatched={res['dispatched']} "
          f"deduped={res['deduped']} unregistered={res['unregistered']}")
    return 0


def _cmd_dist_test(args, mode: str) -> int:
    from mthydra.controller.config import ConfigError, load_config
    from mthydra.controller.state import distribution_log as _dl
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.user_channels import get_channels
    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"dist-test: config error: {e}", file=sys.stderr)
        return 2
    tg_sink, em_sink = _build_dist_sinks(cfg, mode)
    conn = connect(args.db_path)
    try:
        row = get_channels(conn, args.user_id)
        if row is None:
            print(f"dist-test: no channels for {args.user_id!r}", file=sys.stderr)
            return 2
        now = _now()
        body = f"mthydra distribution test @ {now}"
        for label, configured, sink in (
            ("telegram", row.telegram_chat_id, tg_sink),
            ("email", row.email_addr, em_sink),
        ):
            if not configured:
                continue
            try:
                if label == "telegram":
                    res = sink(chat_id=configured, message=body)
                else:
                    res = sink(to_addr=configured,
                                subject="mthydra distribution test",
                                body=body)
                success = bool(getattr(res, "success", False))
                err = getattr(res, "error", None)
            except Exception as e:
                success = False
                err = repr(e)
            _dl.append(
                conn, user_id=args.user_id, channel=label, kind="test",
                attempted_at=now,
                delivered_at=now if success else None,
                subset_hash=None, payload_json=body, error=err,
            )
            print(f"dist-test: {label} -> "
                  f"{'OK' if success else 'FAIL ' + (err or '')}")
        return 0
    finally:
        conn.close()


def _cmd_dist_log_recent(args) -> int:
    from mthydra.controller.state import distribution_log as _dl
    from mthydra.controller.state.db import connect
    conn = connect(args.db_path)
    try:
        rows = _dl.recent(conn, user_id=args.user_id, limit=args.limit)
        if args.json:
            import json as _json
            print(_json.dumps([
                {
                    "id": r.id,
                    "user_id": r.user_id,
                    "channel": r.channel,
                    "kind": r.kind,
                    "attempted_at": r.attempted_at,
                    "delivered_at": r.delivered_at,
                    "subset_hash": r.subset_hash,
                    "error": r.error,
                } for r in rows
            ], sort_keys=True))
        else:
            for r in rows:
                status = "OK" if r.delivered_at else "FAIL"
                err = f" err={r.error!r}" if r.error else ""
                print(f"#{r.id} {r.attempted_at} {r.user_id} {r.channel} "
                      f"{r.kind} {status}{err}")
        return 0
    finally:
        conn.close()


# ----- spec D2: canary + rollback subcommands -----


def _cmd_image_promote_status(args) -> int:
    from mthydra.controller.config import ConfigError, load_config
    from mthydra.controller.image.gate import (
        GateConfigView, evaluate_promotion_gate,
    )
    from mthydra.controller.state.db import connect

    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"image-promote-status: config error: {e}", file=sys.stderr)
        return 2

    gate_cfg = GateConfigView(
        min_canary_boxes=cfg.image.canary.min_boxes,
        min_cycles_per_box=cfg.image.canary.min_cycles_per_box,
        min_distinct_vantages=cfg.probe.min_distinct_vantages,
    )
    conn = connect(args.db_path)
    try:
        res = evaluate_promotion_gate(conn, args.image_version, cfg=gate_cfg)
        if args.json:
            import json as _json
            print(_json.dumps({
                "image_version": res.image_version,
                "passed": res.passed,
                "reasons": list(res.reasons),
                "canary_box_ids": list(res.canary_box_ids),
                "canary_probe_rows": res.canary_probe_rows,
                "canary_distinct_vantages": res.canary_distinct_vantages,
                "pending_kills": list(res.pending_kills),
            }, sort_keys=True))
        else:
            verdict = "PASSED" if res.passed else "FAILED"
            print(f"image-promote-status: {args.image_version} -> {verdict}")
            print(f"  canary cohort: {list(res.canary_box_ids)}")
            print(f"  probe rows: {res.canary_probe_rows}")
            print(f"  distinct vantages: {res.canary_distinct_vantages}")
            if res.pending_kills:
                print(f"  pending kills: {list(res.pending_kills)}")
            for reason in res.reasons:
                print(f"  reason: {reason}")
        return 0
    finally:
        conn.close()


def _cmd_image_rollback(args) -> int:
    import json as _json

    from mthydra.controller.state.audit import log_event
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.obligations import set_obligation
    from mthydra.controller.state.ru_images import (
        get_image, list_live_boxes_for_image, retire,
    )

    if args.image_version == args.target_version:
        print("image-rollback: --to must differ from the rolled-back version",
              file=sys.stderr)
        return 2

    conn = connect(args.db_path)
    try:
        rc = _require_active_role(conn, "image-rollback")
        if rc is not None:
            return rc
        try:
            src = get_image(conn, args.image_version)
        except LookupError as e:
            print(f"image-rollback: {e}", file=sys.stderr)
            return 2
        try:
            target = get_image(conn, args.target_version)
        except LookupError as e:
            print(f"image-rollback: {e}", file=sys.stderr)
            return 2
        # Target must have been promoted at some point (i.e. promoted_at IS NOT NULL).
        if target.promoted_at is None:
            print(
                f"image-rollback: target {args.target_version!r} was never promoted; "
                "use image-promote for new candidates",
                file=sys.stderr,
            )
            return 2
        # Source must currently be the promoted one (we're rolling IT back).
        if src.state != "promoted":
            print(
                f"image-rollback: {args.image_version!r} is not in 'promoted' state "
                f"(current state={src.state!r})",
                file=sys.stderr,
            )
            return 2

        now = _now()
        # Step 1: retire the bad image.
        retire(conn, args.image_version, at=now, reason=f"rollback: {args.evidence}")
        # Step 2: re-promote the target (it's currently 'retired').
        cur = conn.execute(
            "UPDATE ru_images SET state='promoted', promoted_at=?, retired_at=NULL "
            "WHERE image_version=? AND state='retired'",
            (now, args.target_version),
        )
        if cur.rowcount == 0:
            print(
                f"image-rollback: target {args.target_version!r} is not "
                "currently retired (rollback only re-promotes previously retired images)",
                file=sys.stderr,
            )
            return 2
        # Step 3: emit per-box rollback_pending obligations.
        live_box_ids = list_live_boxes_for_image(conn, args.image_version)
        for box_id in live_box_ids:
            set_obligation(
                conn,
                obligation_id=f"image_rollback_pending::{box_id}",
                last_proven_at=now, proven_by="operator",
                next_due_at=now,
                details=_json.dumps({
                    "rolled_back_image": args.image_version,
                    "restored_image": args.target_version,
                    "evidence": args.evidence,
                }),
            )
        log_event(
            conn, ts=now, actor="operator", action="image_rollback",
            target=args.image_version,
            details_json=_json.dumps({
                "to": args.target_version,
                "evidence": args.evidence,
                "live_boxes_flagged": live_box_ids,
            }),
        )
        conn.commit()
        print(
            f"image-rollback: {args.image_version} -> retired; "
            f"{args.target_version} -> promoted; "
            f"flagged {len(live_box_ids)} live box(es) for replacement"
        )
        return 0
    finally:
        conn.close()


def _cmd_ru_box_canary_clear(args) -> int:
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.ru_boxes import clear_canary_flag

    conn = connect(args.db_path)
    try:
        rc = _require_active_role(conn, "ru-box-canary-clear")
        if rc is not None:
            return rc
        try:
            clear_canary_flag(conn, args.box_id, at=_now(), reason=args.reason)
        except (LookupError, ValueError) as e:
            print(f"ru-box-canary-clear: {e}", file=sys.stderr)
            return 2
        print(f"ru-box-canary-clear: {args.box_id} -> regular fleet")
        return 0
    finally:
        conn.close()


# ----- spec M: log compaction -----


def _cmd_compact_logs(args) -> int:
    from mthydra.controller.state.compactor import (
        compact_alert_acks, compact_alert_log, compact_distribution_log,
        compact_probe_results,
    )
    from mthydra.controller.state.db import connect

    dry_run = not args.no_dry_run
    if not dry_run and not args.evidence:
        print("compact-logs: --evidence required when running without --dry-run",
              file=sys.stderr)
        return 2

    fns = {
        "alert_log": compact_alert_log,
        "probe_results": compact_probe_results,
        "distribution_log": compact_distribution_log,
        "alert_acks": compact_alert_acks,
    }
    targets = (
        list(fns.keys()) if args.table == "all" else [args.table]
    )
    conn = connect(args.db_path)
    try:
        rc = _require_active_role(conn, "compact-logs")
        if rc is not None:
            return rc
        for t in targets:
            try:
                res = fns[t](
                    conn, before=args.before, dry_run=dry_run,
                    actor=args.evidence or "operator (dry-run)",
                )
            except RuntimeError as e:
                print(f"compact-logs: {e}", file=sys.stderr)
                return 2
            verb = "would delete" if res.dry_run else "deleted"
            print(f"compact-logs: {t} {verb} {res.deleted} row(s) "
                  f"older than {args.before}")
        return 0
    finally:
        conn.close()


# ----- spec J2: operator alert acks -----


_DURATION_MAX_SECONDS = 7 * 86400


def _parse_duration_seconds(spec: str) -> int:
    """Parse e.g. '24h', '7d', '300s'. Refuses > 7d. Refuses unknown suffix."""
    if not spec or spec[-1] not in {"s", "m", "h", "d"}:
        raise ValueError(f"duration must end with s/m/h/d (got {spec!r})")
    try:
        n = int(spec[:-1])
    except ValueError as e:
        raise ValueError(f"duration: invalid number {spec!r}") from e
    if n <= 0:
        raise ValueError(f"duration must be positive (got {spec!r})")
    mult = {"s": 1, "m": 60, "h": 3600, "d": 86400}[spec[-1]]
    secs = n * mult
    if secs > _DURATION_MAX_SECONDS:
        raise ValueError(
            f"duration {spec!r} exceeds 7d cap"
        )
    return secs


def _cmd_obs_alert_ack(args) -> int:
    from datetime import datetime, timedelta, timezone

    from mthydra.controller.state.alert_acks import ack
    from mthydra.controller.state.db import connect

    try:
        secs = _parse_duration_seconds(args.expires_in)
    except ValueError as e:
        print(f"obs-alert-ack: {e}", file=sys.stderr)
        return 2
    now = _now()
    expires = (
        datetime.fromisoformat(now.replace("Z", "+00:00"))
        + timedelta(seconds=secs)
    ).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = connect(args.db_path)
    try:
        ack(
            conn, dedupe_key=args.dedupe_key, acked_by=args.acked_by,
            evidence=args.evidence, at=now, expires_at=expires,
        )
        print(f"obs-alert-ack: {args.dedupe_key} acked until {expires}")
        return 0
    finally:
        conn.close()


def _cmd_obs_alert_ack_list(args) -> int:
    from mthydra.controller.state.alert_acks import list_active, list_all
    from mthydra.controller.state.db import connect

    conn = connect(args.db_path)
    try:
        rows = list_all(conn) if args.include_expired else list_active(conn, now=_now())
        if args.json:
            import json as _json
            print(_json.dumps([
                {
                    "id": r.id,
                    "dedupe_key": r.dedupe_key,
                    "acked_at": r.acked_at,
                    "acked_by": r.acked_by,
                    "expires_at": r.expires_at,
                    "evidence": r.evidence,
                } for r in rows
            ], sort_keys=True))
        else:
            for r in rows:
                print(f"#{r.id} {r.dedupe_key} by={r.acked_by} "
                      f"expires={r.expires_at} ev={r.evidence!r}")
        return 0
    finally:
        conn.close()


# ----- spec I2: probe credential subcommands -----


def _cmd_probe_credential_issue(args) -> int:
    import uuid as _uuid

    from mthydra.descriptor.authority import sign_onward_credential
    from mthydra.controller.state.authority import current_authority
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.probe_credentials import issue
    conn = connect(args.db_path)
    try:
        rc = _require_active_role(conn, "probe-credential-issue")
        if rc is not None:
            return rc
        # Authority must be the same one used for onward credentials.
        try:
            auth = current_authority(conn)
        except LookupError as e:
            print(f"probe-credential-issue: {e}", file=sys.stderr)
            return 2
        # Validate the box + vantage exist.
        if conn.execute(
            "SELECT 1 FROM ru_boxes WHERE box_id=?", (args.box_id,)
        ).fetchone() is None:
            print(f"probe-credential-issue: unknown box {args.box_id!r}",
                  file=sys.stderr)
            return 2
        vrow = conn.execute(
            "SELECT state FROM probe_vantages WHERE vantage_id=?",
            (args.vantage_id,),
        ).fetchone()
        if vrow is None:
            print(f"probe-credential-issue: unknown vantage {args.vantage_id!r}",
                  file=sys.stderr)
            return 2
        if vrow[0] != "active":
            print(
                f"probe-credential-issue: vantage {args.vantage_id!r} is in "
                f"state={vrow[0]!r}; only 'active' vantages may hold credentials",
                file=sys.stderr,
            )
            return 2
        cred_id = str(_uuid.uuid4())
        now = _now()
        blob = sign_onward_credential(
            auth.privkey_pem,
            box_id=f"{args.box_id}|probe|{args.vantage_id}",
            issued_at=now,
            authority_generation=auth.generation,
        )
        try:
            issue(
                conn, cred_id=cred_id, box_id=args.box_id,
                vantage_id=args.vantage_id,
                authority_generation=auth.generation,
                credential=blob, issued_at=now,
                evidence=args.evidence,
            )
        except sqlite3.IntegrityError as e:
            print(f"probe-credential-issue: {e}", file=sys.stderr)
            return 2
        print(f"probe-credential-issue: {cred_id} issued "
              f"(box={args.box_id}, vantage={args.vantage_id})")
        return 0
    finally:
        conn.close()


def _cmd_probe_credential_list(args) -> int:
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.probe_credentials import list_all
    conn = connect(args.db_path)
    try:
        rows = list_all(
            conn, box_id=args.box_id, vantage_id=args.vantage_id,
            include_revoked=args.include_revoked,
        )
        if args.json:
            import base64 as _b64
            import json as _json
            print(_json.dumps([
                {
                    "cred_id": r.cred_id,
                    "box_id": r.box_id,
                    "vantage_id": r.vantage_id,
                    "credential_b64": _b64.b64encode(r.credential).decode("ascii"),
                    "issued_at": r.issued_at,
                    "revoked_at": r.revoked_at,
                    "authority_generation": r.authority_generation,
                } for r in rows
            ], sort_keys=True))
        else:
            for r in rows:
                status = "REVOKED" if r.revoked_at else "active"
                print(f"{r.cred_id}\t{status}\tbox={r.box_id}\t"
                      f"vantage={r.vantage_id}\tauth_gen={r.authority_generation}\t"
                      f"issued={r.issued_at}")
        return 0
    finally:
        conn.close()


def _cmd_probe_credential_revoke(args) -> int:
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.probe_credentials import revoke
    conn = connect(args.db_path)
    try:
        rc = _require_active_role(conn, "probe-credential-revoke")
        if rc is not None:
            return rc
        try:
            revoke(conn, args.cred_id, at=_now(), reason=args.reason)
        except (LookupError, ValueError) as e:
            print(f"probe-credential-revoke: {e}", file=sys.stderr)
            return 2
        print(f"probe-credential-revoke: {args.cred_id} -> revoked")
        return 0
    finally:
        conn.close()
