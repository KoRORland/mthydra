from pathlib import Path

import pytest

from mthydra.controller.config import ConfigError, load_config


def _write(path: Path, content: str) -> Path:
    path.write_text(content)
    return path


def test_load_valid_config(tmp_path):
    p = _write(
        tmp_path / "c.toml",
        """
        [node]
        role = "active"
        hostname = "controller-1"

        [backup]
        floor_interval_hours = 24
        on_change_debounce_seconds = 30
        endpoint = "https://s3.example"
        bucket = "mthydra-state"
        access_key_id = "AKID"

        [backup.retention]
        keep_daily = 30
        keep_monthly = 12
        object_lock_days = 365

        [gap_monitor]
        poll_interval_minutes = 30
        alarm_threshold_hours = 48
        recipient_email = "op@example.org"

        [obligations.timers_hours]
        t1_dormant_health = 168
        t2_dryrun_caseA = 720
        t2_dryrun_caseB = 720
        t3_vantage_revalidation = 168
        t3_profile_repin = 0
        t4_upstream_check = 168
        t5_pool_revalidation = 168
        t6_reshuffle = 168
        backup_restore_dryrun = 720
        """,
    )
    cfg = load_config(p)
    assert cfg.node.role == "active"
    assert cfg.backup.floor_interval_hours == 24
    assert cfg.backup.retention.object_lock_days == 365
    assert cfg.gap_monitor.alarm_threshold_hours == 48
    assert cfg.obligations.timers_hours["t2_dryrun_caseA"] == 720


def test_load_rejects_invalid_role(tmp_path):
    p = _write(
        tmp_path / "c.toml",
        """
        [node]
        role = "primary"
        hostname = "x"
        [backup]
        floor_interval_hours = 24
        on_change_debounce_seconds = 30
        endpoint = "https://x"
        bucket = "x"
        access_key_id = "x"
        [backup.retention]
        keep_daily = 1
        keep_monthly = 1
        object_lock_days = 1
        [gap_monitor]
        poll_interval_minutes = 30
        alarm_threshold_hours = 48
        recipient_email = "op@x"
        [obligations.timers_hours]
        """,
    )
    with pytest.raises(ConfigError, match="role"):
        load_config(p)


def test_load_rejects_negative_interval(tmp_path):
    p = _write(
        tmp_path / "c.toml",
        """
        [node]
        role = "active"
        hostname = "x"
        [backup]
        floor_interval_hours = -1
        on_change_debounce_seconds = 30
        endpoint = "https://x"
        bucket = "x"
        access_key_id = "x"
        [backup.retention]
        keep_daily = 1
        keep_monthly = 1
        object_lock_days = 1
        [gap_monitor]
        poll_interval_minutes = 30
        alarm_threshold_hours = 48
        recipient_email = "op@x"
        [obligations.timers_hours]
        """,
    )
    with pytest.raises(ConfigError, match="floor_interval_hours"):
        load_config(p)


def test_load_cover_pool_config(tmp_path):
    from mthydra.controller.config import load_config

    cfg_path = tmp_path / "controller.toml"
    cfg_path.write_text(
        "[node]\nrole='active'\nhostname='h'\n"
        "[backup]\nfloor_interval_hours=24\non_change_debounce_seconds=30\n"
        "endpoint='https://example'\nbucket='b'\naccess_key_id='k'\n"
        "[backup.retention]\nkeep_daily=30\nkeep_monthly=12\nobject_lock_days=365\n"
        "[gap_monitor]\npoll_interval_minutes=30\nalarm_threshold_hours=48\n"
        "recipient_email='op@example.org'\n"
        "[descriptor]\nrotation_interval_hours=1\nvalidity_window_hours=24\n"
        "[obligations]\n[obligations.timers_hours]\n"
        "[cover_pool]\n"
        "rotation_ttl_days=14\n"
        "reverify_after_days=30\n"
        "freeze_threshold=2\n"
        "reverify_sweep_interval='1h'\n"
        "rotation_sweep_interval='1h'\n"
        "replenishment_interval_days=90\n"
    )
    cfg = load_config(cfg_path)
    assert cfg.cover_pool.rotation_ttl_days == 14
    assert cfg.cover_pool.reverify_after_days == 30
    assert cfg.cover_pool.freeze_threshold == 2
    assert cfg.cover_pool.reverify_sweep_interval_seconds == 3600
    assert cfg.cover_pool.rotation_sweep_interval_seconds == 3600
    assert cfg.cover_pool.replenishment_interval_days == 90


def test_load_standby_config(tmp_path):
    from mthydra.controller.config import load_config

    cfg_path = tmp_path / "controller.toml"
    cfg_path.write_text(
        "[node]\nrole='standby'\nhostname='h'\n"
        "[backup]\nfloor_interval_hours=24\non_change_debounce_seconds=30\n"
        "endpoint='https://example'\nbucket='b'\naccess_key_id='k'\n"
        "[backup.retention]\nkeep_daily=30\nkeep_monthly=12\nobject_lock_days=365\n"
        "[gap_monitor]\npoll_interval_minutes=30\nalarm_threshold_hours=48\n"
        "recipient_email='op@example.org'\n"
        "[descriptor]\nrotation_interval_hours=1\nvalidity_window_hours=24\n"
        "[obligations]\n[obligations.timers_hours]\n"
        "[cover_pool]\n"
        "rotation_ttl_days=14\nreverify_after_days=30\nfreeze_threshold=2\n"
        "reverify_sweep_interval='1h'\nrotation_sweep_interval='1h'\n"
        "replenishment_interval_days=90\n"
        "[standby]\nnode_id='eu-standby-de-1'\n"
        "heartbeat_interval_seconds=60\n"
        "heartbeat_poll_interval='5m'\n"
        "staleness_alert_seconds=600\n"
    )
    cfg = load_config(cfg_path)
    assert cfg.standby.node_id == "eu-standby-de-1"
    assert cfg.standby.heartbeat_interval_seconds == 60
    assert cfg.standby.heartbeat_poll_interval_seconds == 300
    assert cfg.standby.staleness_alert_seconds == 600


def test_load_config_standby_section_defaults(tmp_path):
    """Missing [standby] section: load with safe defaults."""
    from mthydra.controller.config import load_config

    cfg_path = tmp_path / "controller.toml"
    cfg_path.write_text(
        "[node]\nrole='active'\nhostname='h'\n"
        "[backup]\nfloor_interval_hours=24\non_change_debounce_seconds=30\n"
        "endpoint='https://example'\nbucket='b'\naccess_key_id='k'\n"
        "[backup.retention]\nkeep_daily=30\nkeep_monthly=12\nobject_lock_days=365\n"
        "[gap_monitor]\npoll_interval_minutes=30\nalarm_threshold_hours=48\n"
        "recipient_email='op@example.org'\n"
        "[descriptor]\nrotation_interval_hours=1\nvalidity_window_hours=24\n"
        "[obligations]\n[obligations.timers_hours]\n"
        "[cover_pool]\nrotation_ttl_days=14\nreverify_after_days=30\n"
        "freeze_threshold=2\nreverify_sweep_interval='1h'\n"
        "rotation_sweep_interval='1h'\nreplenishment_interval_days=90\n"
    )
    cfg = load_config(cfg_path)
    assert cfg.standby.node_id == ""
    assert cfg.standby.heartbeat_interval_seconds == 60
    assert cfg.standby.heartbeat_poll_interval_seconds == 300
    assert cfg.standby.staleness_alert_seconds == 600


def test_load_image_config(tmp_path):
    from mthydra.controller.config import load_config

    cfg_path = tmp_path / "controller.toml"
    cfg_path.write_text(
        "[node]\nrole='active'\nhostname='h'\n"
        "[backup]\nfloor_interval_hours=24\non_change_debounce_seconds=30\n"
        "endpoint='https://example'\nbucket='b'\naccess_key_id='k'\n"
        "[backup.retention]\nkeep_daily=30\nkeep_monthly=12\nobject_lock_days=365\n"
        "[gap_monitor]\npoll_interval_minutes=30\nalarm_threshold_hours=48\n"
        "recipient_email='op@example.org'\n"
        "[descriptor]\nrotation_interval_hours=1\nvalidity_window_hours=24\n"
        "[obligations]\n[obligations.timers_hours]\n"
        "[cover_pool]\nrotation_ttl_days=14\nreverify_after_days=30\n"
        "freeze_threshold=2\nreverify_sweep_interval='1h'\n"
        "rotation_sweep_interval='1h'\nreplenishment_interval_days=90\n"
        "[image]\n"
        "upstream_repo = '9seconds/mtg'\n"
        "upstream_release_asset = 'mtg-linux-amd64'\n"
        "upstream_check_interval = '168h'\n"
        "github_api_url = 'https://api.github.com'\n"
        "build_tmp_dir = '/var/lib/mthydra/tmp'\n"
    )
    cfg = load_config(cfg_path)
    assert cfg.image.upstream_repo == "9seconds/mtg"
    assert cfg.image.upstream_release_asset == "mtg-linux-amd64"
    assert cfg.image.upstream_check_interval_seconds == 168 * 3600
    assert cfg.image.github_api_url == "https://api.github.com"
    assert cfg.image.build_tmp_dir == "/var/lib/mthydra/tmp"


def test_load_image_config_defaults(tmp_path):
    """Missing [image] section: load with safe defaults."""
    from mthydra.controller.config import load_config

    cfg_path = tmp_path / "controller.toml"
    cfg_path.write_text(
        "[node]\nrole='active'\nhostname='h'\n"
        "[backup]\nfloor_interval_hours=24\non_change_debounce_seconds=30\n"
        "endpoint='https://example'\nbucket='b'\naccess_key_id='k'\n"
        "[backup.retention]\nkeep_daily=30\nkeep_monthly=12\nobject_lock_days=365\n"
        "[gap_monitor]\npoll_interval_minutes=30\nalarm_threshold_hours=48\n"
        "recipient_email='op@example.org'\n"
        "[descriptor]\nrotation_interval_hours=1\nvalidity_window_hours=24\n"
        "[obligations]\n[obligations.timers_hours]\n"
        "[cover_pool]\nrotation_ttl_days=14\nreverify_after_days=30\n"
        "freeze_threshold=2\nreverify_sweep_interval='1h'\n"
        "rotation_sweep_interval='1h'\nreplenishment_interval_days=90\n"
    )
    cfg = load_config(cfg_path)
    assert cfg.image.upstream_repo == "9seconds/mtg"
    assert cfg.image.upstream_release_asset == "mtg-linux-amd64"
    assert cfg.image.upstream_check_interval_seconds == 168 * 3600
    assert cfg.image.github_api_url == "https://api.github.com"
    assert cfg.image.build_tmp_dir == "/var/lib/mthydra/tmp"
