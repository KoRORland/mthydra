"""Tests for user-onboard CLI subcommand (Task 8)."""
from __future__ import annotations

from mthydra.controller.state.db import connect
from mthydra.controller.state.schema import apply_schema

# Minimal TOML that load_config accepts; has shard_manager + distribution defaults.
_MIN_TOML = """\
[node]
role = "active"
hostname = "h"
[backup]
floor_interval_hours = 24
on_change_debounce_seconds = 30
endpoint = "https://example"
bucket = "b"
access_key_id = "k"
[backup.retention]
keep_daily = 30
keep_monthly = 12
object_lock_days = 365
[gap_monitor]
poll_interval_minutes = 30
alarm_threshold_hours = 48
recipient_email = "op@example.org"
[descriptor]
rotation_interval_hours = 1
validity_window_hours = 24
[obligations]
[obligations.timers_hours]
[cover_pool]
rotation_ttl_days = 14
reverify_after_days = 30
freeze_threshold = 2
reverify_sweep_interval = "1h"
rotation_sweep_interval = "1h"
replenishment_interval_days = 90
[data_exit]
listen_port = 443
sing_box_socket = "/run/mthydra/sing-box.sock"
config_path = "/etc/mthydra/sing-box.json"
reality_key_path = "/etc/mthydra/reality.key"
[data_exit.telegram_dcs]
v4 = ["149.154.160.0/20"]
v6 = ["2001:b28:f23d::/48"]
[data_exit.cover_sni]
default = "www.example-cover-domain.invalid"
[observability.telegram]
bot_token = "test-token"
chat_id = "12345"
[observability.email]
smtp_host = "smtp.example.org"
smtp_port = 587
from_addr = "ops@example.org"
to_addr = "op@example.org"
username = "ops@example.org"
password = "app-pw"
[distribution.telegram]
bot_token = "dist-token"
[distribution.email]
smtp_host = "smtp.example.org"
smtp_port = 587
from_addr = "dist@example.org"
username = "dist@example.org"
password = "app-pw"
"""


def test_user_onboard_creates_user_assigns_default_shard_mints_token(tmp_path, capsys):
    db = str(tmp_path / "s.sqlite")
    c = connect(db)
    apply_schema(c)
    c.close()

    cfg_path = tmp_path / "controller.toml"
    cfg_path.write_text(_MIN_TOML)

    from mthydra.controller.cli import run

    rc = run(["user-onboard", "granny", "--display-name", "Granny",
              "--db-path", db, "--config", str(cfg_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "?start=" in out  # token/deep-link printed

    c = connect(db)
    assert c.execute(
        "SELECT current_shard_id FROM users WHERE user_id='granny'"
    ).fetchone()[0] == "default_shard"
    assert c.execute(
        "SELECT COUNT(*) FROM pending_enrollments WHERE user_id='granny'"
    ).fetchone()[0] == 1
    c.close()


def test_user_onboard_idempotent_if_user_exists(tmp_path, capsys):
    """Re-running user-onboard for an existing user should succeed (rc=0)."""
    db = str(tmp_path / "s.sqlite")
    c = connect(db)
    apply_schema(c)
    c.close()

    cfg_path = tmp_path / "controller.toml"
    cfg_path.write_text(_MIN_TOML)

    from mthydra.controller.cli import run

    rc1 = run(["user-onboard", "granny", "--db-path", db, "--config", str(cfg_path)])
    capsys.readouterr()
    rc2 = run(["user-onboard", "granny", "--db-path", db, "--config", str(cfg_path)])
    assert rc1 == 0
    assert rc2 == 0


def test_user_onboard_bad_config_returns_2(tmp_path, capsys):
    db = str(tmp_path / "s.sqlite")
    c = connect(db)
    apply_schema(c)
    c.close()

    from mthydra.controller.cli import run

    rc = run(["user-onboard", "granny", "--db-path", db,
              "--config", str(tmp_path / "nonexistent.toml")])
    assert rc == 2
