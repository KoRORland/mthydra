"""TOML config loader. Non-secret operator-authored policy, lives in git."""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path


class ConfigError(ValueError):
    """Raised when controller.toml is missing required fields or has invalid values."""


@dataclass(frozen=True)
class NodeConfig:
    role: str
    hostname: str


@dataclass(frozen=True)
class RetentionConfig:
    keep_daily: int
    keep_monthly: int
    object_lock_days: int


@dataclass(frozen=True)
class BackupConfig:
    floor_interval_hours: int
    on_change_debounce_seconds: int
    endpoint: str
    bucket: str
    access_key_id: str
    retention: RetentionConfig


@dataclass(frozen=True)
class GapMonitorConfig:
    poll_interval_minutes: int
    alarm_threshold_hours: int
    recipient_email: str


@dataclass(frozen=True)
class ObligationsConfig:
    timers_hours: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class Config:
    node: NodeConfig
    backup: BackupConfig
    gap_monitor: GapMonitorConfig
    obligations: ObligationsConfig


_VALID_ROLES = {"active", "standby"}


def _require_positive(name: str, value: int) -> int:
    if not isinstance(value, int) or value < 0:
        raise ConfigError(f"{name}: must be a non-negative integer (got {value!r})")
    return value


def load_config(path: Path | str) -> Config:
    path = Path(path)
    try:
        raw = tomllib.loads(path.read_text())
    except FileNotFoundError as e:
        raise ConfigError(f"config not found: {path}") from e
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"TOML parse error in {path}: {e}") from e

    try:
        node = raw["node"]
        backup = raw["backup"]
        retention = backup["retention"]
        gap = raw["gap_monitor"]
        obligations = raw.get("obligations", {}).get("timers_hours", {})
    except KeyError as e:
        raise ConfigError(f"missing required section/key: {e}") from e

    role = node.get("role")
    if role not in _VALID_ROLES:
        raise ConfigError(f"node.role must be one of {sorted(_VALID_ROLES)}, got {role!r}")

    return Config(
        node=NodeConfig(role=role, hostname=str(node["hostname"])),
        backup=BackupConfig(
            floor_interval_hours=_require_positive("backup.floor_interval_hours", backup["floor_interval_hours"]),
            on_change_debounce_seconds=_require_positive(
                "backup.on_change_debounce_seconds", backup["on_change_debounce_seconds"]
            ),
            endpoint=str(backup["endpoint"]),
            bucket=str(backup["bucket"]),
            access_key_id=str(backup["access_key_id"]),
            retention=RetentionConfig(
                keep_daily=_require_positive("backup.retention.keep_daily", retention["keep_daily"]),
                keep_monthly=_require_positive("backup.retention.keep_monthly", retention["keep_monthly"]),
                object_lock_days=_require_positive(
                    "backup.retention.object_lock_days", retention["object_lock_days"]
                ),
            ),
        ),
        gap_monitor=GapMonitorConfig(
            poll_interval_minutes=_require_positive("gap_monitor.poll_interval_minutes", gap["poll_interval_minutes"]),
            alarm_threshold_hours=_require_positive("gap_monitor.alarm_threshold_hours", gap["alarm_threshold_hours"]),
            recipient_email=str(gap["recipient_email"]),
        ),
        obligations=ObligationsConfig(timers_hours={str(k): int(v) for k, v in obligations.items()}),
    )
