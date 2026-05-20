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
class DescriptorConfig:
    rotation_interval_hours: int
    validity_window_hours: int


@dataclass(frozen=True)
class CoverPoolConfig:
    rotation_ttl_days: int
    reverify_after_days: int
    freeze_threshold: int
    reverify_sweep_interval_seconds: int
    rotation_sweep_interval_seconds: int
    replenishment_interval_days: int


@dataclass(frozen=True)
class StandbyConfig:
    node_id: str
    heartbeat_interval_seconds: int
    heartbeat_poll_interval_seconds: int
    staleness_alert_seconds: int


@dataclass(frozen=True)
class Config:
    node: NodeConfig
    backup: BackupConfig
    gap_monitor: GapMonitorConfig
    obligations: ObligationsConfig
    descriptor: DescriptorConfig
    cover_pool: CoverPoolConfig
    standby: StandbyConfig


_VALID_ROLES = {"active", "standby"}
_INTERVAL_SUFFIXES = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def _require_positive(name: str, value: int) -> int:
    if not isinstance(value, int) or value < 0:
        raise ConfigError(f"{name}: must be a non-negative integer (got {value!r})")
    return value


def _parse_interval_seconds(name: str, value: object) -> int:
    if isinstance(value, int):
        return _require_positive(name, value)
    if isinstance(value, str) and len(value) >= 2 and value[-1] in _INTERVAL_SUFFIXES:
        try:
            n = int(value[:-1])
        except ValueError as e:
            raise ConfigError(f"{name}: invalid interval {value!r}") from e
        return _require_positive(name, n) * _INTERVAL_SUFFIXES[value[-1]]
    raise ConfigError(f"{name}: must be int or 'Nh'/'Nm'/'Nd'/'Ns' string (got {value!r})")


def _load_cover_pool(data: dict) -> CoverPoolConfig:
    sec = data.get("cover_pool", {})
    return CoverPoolConfig(
        rotation_ttl_days=_require_positive(
            "cover_pool.rotation_ttl_days", sec.get("rotation_ttl_days", 14)
        ),
        reverify_after_days=_require_positive(
            "cover_pool.reverify_after_days", sec.get("reverify_after_days", 30)
        ),
        freeze_threshold=_require_positive(
            "cover_pool.freeze_threshold", sec.get("freeze_threshold", 2)
        ),
        reverify_sweep_interval_seconds=_parse_interval_seconds(
            "cover_pool.reverify_sweep_interval",
            sec.get("reverify_sweep_interval", 3600),
        ),
        rotation_sweep_interval_seconds=_parse_interval_seconds(
            "cover_pool.rotation_sweep_interval",
            sec.get("rotation_sweep_interval", 3600),
        ),
        replenishment_interval_days=_require_positive(
            "cover_pool.replenishment_interval_days",
            sec.get("replenishment_interval_days", 90),
        ),
    )


def _load_standby(data: dict) -> StandbyConfig:
    sec = data.get("standby", {})
    return StandbyConfig(
        node_id=str(sec.get("node_id", "")),
        heartbeat_interval_seconds=_require_positive(
            "standby.heartbeat_interval_seconds",
            sec.get("heartbeat_interval_seconds", 60),
        ),
        heartbeat_poll_interval_seconds=_parse_interval_seconds(
            "standby.heartbeat_poll_interval",
            sec.get("heartbeat_poll_interval", 300),
        ),
        staleness_alert_seconds=_require_positive(
            "standby.staleness_alert_seconds",
            sec.get("staleness_alert_seconds", 600),
        ),
    )


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

    desc = raw.get("descriptor", {})

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
        descriptor=DescriptorConfig(
            rotation_interval_hours=int(desc.get("rotation_interval_hours", 1)),
            validity_window_hours=int(desc.get("validity_window_hours", 24)),
        ),
        cover_pool=_load_cover_pool(raw),
        standby=_load_standby(raw),
    )
