"""mthydra-backup-monitor CLI — polls index.json and emails on generation gap.

Standalone: no imports from the controller wheel (plan §16.1 G1 independence).
TOML config is parsed inline using stdlib tomllib.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import tomllib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import boto3

from mthydra_backup_monitor.emailer import EmailConfig, send_gap_alarm
from mthydra_backup_monitor.poller import GapMonitorState, evaluate_gap


STATE_FILE_DEFAULT = "/var/lib/mthydra/backup-monitor-state.json"


# ---------------------------------------------------------------------------
# Minimal config loader (mirrors the relevant subset of controller.toml)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _MonitorConfig:
    endpoint: str
    bucket: str
    access_key_id: str
    object_lock_days: int
    poll_interval_minutes: int
    alarm_threshold_hours: int
    recipient_email: str


def _load_config(path: str) -> _MonitorConfig:
    with open(path, "rb") as fh:
        raw = tomllib.load(fh)
    backup = raw["backup"]
    retention = backup.get("retention", {})
    gap = raw.get("gap_monitor", {})
    return _MonitorConfig(
        endpoint=backup.get("endpoint", ""),
        bucket=backup["bucket"],
        access_key_id=backup.get("access_key_id", ""),
        object_lock_days=retention.get("object_lock_days", 365),
        poll_interval_minutes=gap.get("poll_interval_minutes", 30),
        alarm_threshold_hours=gap.get("alarm_threshold_hours", 48),
        recipient_email=gap.get("recipient_email", ""),
    )


# ---------------------------------------------------------------------------
# Persistent state helpers
# ---------------------------------------------------------------------------

def _load_state(path: Path) -> GapMonitorState:
    if not path.exists():
        return GapMonitorState(None, None, None)
    raw = json.loads(path.read_text())
    return GapMonitorState(
        last_seen_gen=raw.get("last_seen_gen"),
        first_observed_at=raw.get("first_observed_at"),
        last_alarm_at=raw.get("last_alarm_at"),
    )


def _save_state(path: Path, state: GapMonitorState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({
            "last_seen_gen": state.last_seen_gen,
            "first_observed_at": state.first_observed_at,
            "last_alarm_at": state.last_alarm_at,
        })
    )


# ---------------------------------------------------------------------------
# S3 helper (inlined — no shared code with controller)
# ---------------------------------------------------------------------------

def _head_index(dest_client, bucket: str) -> dict | None:
    from botocore.exceptions import ClientError
    try:
        obj = dest_client.get_object(Bucket=bucket, Key="index.json")
        return json.loads(obj["Body"].read())
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
            return None
        raise


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def main() -> None:
    parser = argparse.ArgumentParser(prog="mthydra-backup-monitor")
    parser.add_argument("--config", default="/etc/mthydra/controller.toml")
    parser.add_argument("--state-file", default=STATE_FILE_DEFAULT)
    parser.add_argument(
        "--once", action="store_true", help="poll one time and exit (for tests)"
    )
    args = parser.parse_args()

    cfg = _load_config(args.config)
    secret = os.environ.get("MTHYDRA_B2_SECRET", "")
    if not secret:
        print("MTHYDRA_B2_SECRET env var required", file=sys.stderr)
        sys.exit(2)

    region = os.environ.get("MTHYDRA_BACKUP_REGION", "us-east-1")
    s3 = boto3.client(
        "s3",
        endpoint_url=cfg.endpoint or None,
        aws_access_key_id=cfg.access_key_id,
        aws_secret_access_key=secret,
        region_name=region,
    )

    email_cfg = EmailConfig(
        host=os.environ.get("MTHYDRA_SMTP_HOST", ""),
        port=int(os.environ.get("MTHYDRA_SMTP_PORT", "465")),
        username=os.environ.get("MTHYDRA_SMTP_USER", ""),
        app_password=os.environ.get("MTHYDRA_SMTP_PASS", ""),
        from_addr=os.environ.get("MTHYDRA_SMTP_FROM", os.environ.get("MTHYDRA_SMTP_USER", "")),
        to_addr=cfg.recipient_email,
    )

    state_path = Path(args.state_file)
    poll_interval_s = cfg.poll_interval_minutes * 60

    while True:
        state = _load_state(state_path)
        try:
            index = _head_index(s3, cfg.bucket)
        except Exception as e:
            print(f"poll: failed to fetch index: {e}", file=sys.stderr)
            if args.once:
                return
            time.sleep(poll_interval_s)
            continue

        new_state, should_alarm = evaluate_gap(
            index=index,
            state=state,
            now_iso=_now(),
            alarm_threshold_hours=cfg.alarm_threshold_hours,
            alarm_repeat_hours=24,
        )
        _save_state(state_path, new_state)

        if should_alarm:
            try:
                send_gap_alarm(
                    email_cfg,
                    highest_gen=new_state.last_seen_gen or 0,
                    stuck_since=new_state.first_observed_at or "?",
                    now_iso=_now(),
                )
            except Exception as e:
                print(f"alarm: failed to send email: {e}", file=sys.stderr)

        if args.once:
            return
        time.sleep(poll_interval_s)
