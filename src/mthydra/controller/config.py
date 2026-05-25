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
class ImageCanaryConfig:
    """Spec D2 — canary promotion gate thresholds."""
    min_boxes: int
    min_cycles_per_box: int


@dataclass(frozen=True)
class ImageConfig:
    upstream_repo: str
    upstream_release_asset: str
    upstream_check_interval_seconds: int
    github_api_url: str
    build_tmp_dir: str
    canary: ImageCanaryConfig


@dataclass(frozen=True)
class DataExitConfig:
    listen_port: int
    sing_box_socket: str
    config_path: str
    reality_key_path: str
    telegram_dcs_v4: tuple[str, ...]
    telegram_dcs_v6: tuple[str, ...]
    cover_sni_default: str
    cover_sni_per_node: dict[str, str] = field(default_factory=dict)

    def cover_sni_for(self, node_id: str) -> str:
        return self.cover_sni_per_node.get(node_id, self.cover_sni_default)


@dataclass(frozen=True)
class ShardManagerConfig:
    target_size: int
    max_size: int
    reshuffle_interval_days: int
    reshuffle_sweep_interval_seconds: int


@dataclass(frozen=True)
class ProbeConfig:
    soft_fail_window_M: int
    soft_fail_threshold_N: int
    min_distinct_vantages: int
    coverage_window_seconds: int
    probe_vantage_ttl_days: int
    probe_audit_sweep_interval_seconds: int


@dataclass(frozen=True)
class TelegramConfig:
    bot_token: str
    chat_id: str


@dataclass(frozen=True)
class EmailConfig:
    smtp_host: str
    smtp_port: int
    from_addr: str
    to_addr: str
    username: str
    password: str


@dataclass(frozen=True)
class ObservabilityConfig:
    alerter_sweep_interval_seconds: int
    heartbeat_interval_seconds: int
    heartbeat_breach_threshold: int
    alert_dedupe_window_warn_seconds: int
    alert_dedupe_window_crit_seconds: int
    alert_dedupe_window_info_seconds: int
    telegram: TelegramConfig | None
    email: EmailConfig | None


@dataclass(frozen=True)
class DistributionTelegramConfig:
    bot_token: str


@dataclass(frozen=True)
class DistributionEmailConfig:
    smtp_host: str
    smtp_port: int
    from_addr: str
    username: str
    password: str


@dataclass(frozen=True)
class DistributionConfig:
    publish_sweep_interval_seconds: int
    user_heartbeat_interval_seconds: int
    heartbeat_breach_threshold: int
    telegram: DistributionTelegramConfig | None
    email: DistributionEmailConfig | None


@dataclass(frozen=True)
class Config:
    node: NodeConfig
    backup: BackupConfig
    gap_monitor: GapMonitorConfig
    obligations: ObligationsConfig
    descriptor: DescriptorConfig
    cover_pool: CoverPoolConfig
    standby: StandbyConfig
    image: ImageConfig
    shard_manager: ShardManagerConfig
    probe: ProbeConfig
    observability: ObservabilityConfig
    distribution: DistributionConfig
    data_exit: DataExitConfig | None = None


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


def _load_data_exit(data: dict) -> DataExitConfig | None:
    de_raw = data.get("data_exit")
    if de_raw is None:
        return None
    dcs = de_raw.get("telegram_dcs", {})
    cs = de_raw.get("cover_sni", {})
    cover_sni_default = cs.get("default")
    if cover_sni_default is None:
        raise ConfigError("[data_exit.cover_sni] requires a 'default' key")
    cover_sni_per_node = {k: v for k, v in cs.items() if k != "default"}
    try:
        return DataExitConfig(
            listen_port=int(de_raw.get("listen_port", 443)),
            sing_box_socket=str(de_raw["sing_box_socket"]),
            config_path=str(de_raw["config_path"]),
            reality_key_path=str(de_raw["reality_key_path"]),
            telegram_dcs_v4=tuple(str(x) for x in dcs.get("v4", [])),
            telegram_dcs_v6=tuple(str(x) for x in dcs.get("v6", [])),
            cover_sni_default=str(cover_sni_default),
            cover_sni_per_node={str(k): str(v) for k, v in cover_sni_per_node.items()},
        )
    except KeyError as e:
        raise ConfigError(f"[data_exit] missing required key: {e}") from e


def _load_shard_manager(data: dict) -> ShardManagerConfig:
    sec = data.get("shard_manager", {})
    target_size = _require_positive(
        "shard_manager.target_size", sec.get("target_size", 2)
    )
    max_size = _require_positive(
        "shard_manager.max_size", sec.get("max_size", 3)
    )
    if max_size < target_size:
        raise ConfigError(
            f"shard_manager.max_size ({max_size}) must be >= target_size ({target_size})"
        )
    return ShardManagerConfig(
        target_size=target_size,
        max_size=max_size,
        reshuffle_interval_days=_require_positive(
            "shard_manager.reshuffle_interval_days",
            sec.get("reshuffle_interval_days", 14),
        ),
        reshuffle_sweep_interval_seconds=_parse_interval_seconds(
            "shard_manager.reshuffle_sweep_interval",
            sec.get("reshuffle_sweep_interval", 3600),
        ),
    )


def _load_distribution(data: dict) -> DistributionConfig:
    sec = data.get("distribution", {})
    tg = sec.get("telegram", {})
    em = sec.get("email", {})

    def _tg() -> DistributionTelegramConfig | None:
        token = str(tg.get("bot_token", ""))
        if not token:
            return None
        return DistributionTelegramConfig(bot_token=token)

    def _em() -> DistributionEmailConfig | None:
        host = str(em.get("smtp_host", ""))
        port = int(em.get("smtp_port", 587))
        from_addr = str(em.get("from_addr", ""))
        username = str(em.get("username", ""))
        password = str(em.get("password", ""))
        if not (host and from_addr and username and password):
            return None
        return DistributionEmailConfig(
            smtp_host=host, smtp_port=port, from_addr=from_addr,
            username=username, password=password,
        )

    return DistributionConfig(
        publish_sweep_interval_seconds=_parse_interval_seconds(
            "distribution.publish_sweep_interval",
            sec.get("publish_sweep_interval", 300),
        ),
        user_heartbeat_interval_seconds=_parse_interval_seconds(
            "distribution.user_heartbeat_interval",
            sec.get("user_heartbeat_interval", 86400),
        ),
        heartbeat_breach_threshold=_require_positive(
            "distribution.heartbeat_breach_threshold",
            sec.get("heartbeat_breach_threshold", 3),
        ),
        telegram=_tg(),
        email=_em(),
    )


def _load_observability(data: dict) -> ObservabilityConfig:
    sec = data.get("observability", {})
    tg = sec.get("telegram", {})
    em = sec.get("email", {})

    def _tg() -> TelegramConfig | None:
        token = str(tg.get("bot_token", ""))
        chat = str(tg.get("chat_id", ""))
        if not token or not chat:
            return None
        return TelegramConfig(bot_token=token, chat_id=chat)

    def _em() -> EmailConfig | None:
        host = str(em.get("smtp_host", ""))
        port = int(em.get("smtp_port", 587))
        from_addr = str(em.get("from_addr", ""))
        to_addr = str(em.get("to_addr", ""))
        username = str(em.get("username", ""))
        password = str(em.get("password", ""))
        if not (host and from_addr and to_addr and username and password):
            return None
        return EmailConfig(
            smtp_host=host, smtp_port=port, from_addr=from_addr,
            to_addr=to_addr, username=username, password=password,
        )

    return ObservabilityConfig(
        alerter_sweep_interval_seconds=_parse_interval_seconds(
            "observability.alerter_sweep_interval",
            sec.get("alerter_sweep_interval", 120),
        ),
        heartbeat_interval_seconds=_parse_interval_seconds(
            "observability.heartbeat_interval",
            sec.get("heartbeat_interval", 3600),
        ),
        heartbeat_breach_threshold=_require_positive(
            "observability.heartbeat_breach_threshold",
            sec.get("heartbeat_breach_threshold", 3),
        ),
        alert_dedupe_window_warn_seconds=_require_positive(
            "observability.alert_dedupe_window_warn_seconds",
            sec.get("alert_dedupe_window_warn_seconds", 3600),
        ),
        alert_dedupe_window_crit_seconds=_require_positive(
            "observability.alert_dedupe_window_crit_seconds",
            sec.get("alert_dedupe_window_crit_seconds", 900),
        ),
        alert_dedupe_window_info_seconds=_require_positive(
            "observability.alert_dedupe_window_info_seconds",
            sec.get("alert_dedupe_window_info_seconds", 21600),
        ),
        telegram=_tg(),
        email=_em(),
    )


def _load_probe(data: dict) -> ProbeConfig:
    sec = data.get("probe", {})
    M = _require_positive("probe.soft_fail_window_M", sec.get("soft_fail_window_M", 4))
    N = _require_positive("probe.soft_fail_threshold_N", sec.get("soft_fail_threshold_N", 3))
    if N > M:
        raise ConfigError(
            f"probe.soft_fail_threshold_N ({N}) must be <= soft_fail_window_M ({M})"
        )
    return ProbeConfig(
        soft_fail_window_M=M,
        soft_fail_threshold_N=N,
        min_distinct_vantages=_require_positive(
            "probe.min_distinct_vantages", sec.get("min_distinct_vantages", 2)
        ),
        coverage_window_seconds=_parse_interval_seconds(
            "probe.coverage_window", sec.get("coverage_window", 3600),
        ) if isinstance(sec.get("coverage_window"), str) else _require_positive(
            "probe.coverage_window_seconds", sec.get("coverage_window_seconds", 3600)
        ),
        probe_vantage_ttl_days=_require_positive(
            "probe.probe_vantage_ttl_days", sec.get("probe_vantage_ttl_days", 14)
        ),
        probe_audit_sweep_interval_seconds=_parse_interval_seconds(
            "probe.probe_audit_sweep_interval",
            sec.get("probe_audit_sweep_interval", 300),
        ),
    )


def _load_image(data: dict) -> ImageConfig:
    sec = data.get("image", {})
    canary_sec = sec.get("canary", {})
    canary = ImageCanaryConfig(
        min_boxes=_require_positive(
            "image.canary.min_boxes", canary_sec.get("min_boxes", 1)
        ),
        min_cycles_per_box=_require_positive(
            "image.canary.min_cycles_per_box",
            canary_sec.get("min_cycles_per_box", 4),
        ),
    )
    return ImageConfig(
        upstream_repo=str(sec.get("upstream_repo", "9seconds/mtg")),
        upstream_release_asset=str(sec.get("upstream_release_asset", "mtg-linux-amd64")),
        upstream_check_interval_seconds=_parse_interval_seconds(
            "image.upstream_check_interval",
            sec.get("upstream_check_interval", "168h"),
        ),
        github_api_url=str(sec.get("github_api_url", "https://api.github.com")),
        build_tmp_dir=str(sec.get("build_tmp_dir", "/var/lib/mthydra/tmp")),
        canary=canary,
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
        image=_load_image(raw),
        shard_manager=_load_shard_manager(raw),
        probe=_load_probe(raw),
        observability=_load_observability(raw),
        distribution=_load_distribution(raw),
        data_exit=_load_data_exit(raw),
    )
