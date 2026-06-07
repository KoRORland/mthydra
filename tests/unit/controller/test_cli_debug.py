from unittest import mock

from mthydra.controller import cli, debug_flag
from mthydra.controller.observability.sinks import DryRunSink

_MIN_TOML = """
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
object_lock_days = 30
[gap_monitor]
poll_interval_minutes = 30
alarm_threshold_hours = 48
recipient_email = "op@example.org"
"""


def test_debug_enable_writes_flag_and_restarts(tmp_path):
    flag = tmp_path / "debug.flag"
    with mock.patch.object(cli, "DEFAULT_DEBUG_FLAG", str(flag)), \
         mock.patch("subprocess.run") as run:
        rc = cli.run(["debug", "enable", "--ttl-hours", "6", "--no-alert"])
    assert rc == 0
    f = debug_flag.read_flag(flag)
    assert f is not None and f.ttl_hours == 6.0
    run.assert_called_once()
    assert run.call_args.args[0] == ["systemctl", "restart", "mthydra-controller"]


def test_debug_enable_no_restart_skips_systemctl(tmp_path):
    flag = tmp_path / "debug.flag"
    with mock.patch.object(cli, "DEFAULT_DEBUG_FLAG", str(flag)), \
         mock.patch("subprocess.run") as run:
        rc = cli.run(["debug", "enable", "--no-restart", "--no-alert"])
    assert rc == 0
    assert debug_flag.read_flag(flag) is not None
    run.assert_not_called()


def test_debug_disable_clears_flag(tmp_path):
    flag = tmp_path / "debug.flag"
    debug_flag.write_flag(flag, now=0.0)
    with mock.patch.object(cli, "DEFAULT_DEBUG_FLAG", str(flag)), \
         mock.patch("subprocess.run"):
        rc = cli.run(["debug", "disable", "--no-restart", "--no-alert"])
    assert rc == 0
    assert not flag.exists()


def test_debug_status_reports_off_then_on(tmp_path, capsys):
    flag = tmp_path / "debug.flag"
    with mock.patch.object(cli, "DEFAULT_DEBUG_FLAG", str(flag)):
        assert cli.run(["debug", "status"]) == 0
        assert "OFF" in capsys.readouterr().out
        debug_flag.write_flag(flag, ttl_hours=24)
        assert cli.run(["debug", "status"]) == 0
        assert "ON" in capsys.readouterr().out


# --- alerting ---------------------------------------------------------------

def test_debug_alert_payload_enabled_is_crit_and_warns():
    p = cli._debug_alert_payload(enabled=True, ttl_hours=24, now="T", host="eu-1")
    assert p.severity == "crit"
    assert p.kind == "debug_mode_enabled"
    assert "UNREDACTED" in p.body
    assert "eu-1" in p.body and "24h" in p.body


def test_debug_alert_payload_disabled():
    p = cli._debug_alert_payload(enabled=False, ttl_hours=None, now="T", host="eu-1")
    assert p.kind == "debug_mode_disabled"
    assert "DISABLED" in p.subject


def test_dispatch_debug_alert_hits_both_sinks():
    tg, em = DryRunSink(label="telegram"), DryRunSink(label="email")
    payload = cli._debug_alert_payload(enabled=True, ttl_hours=2, now="T", host="h")
    results = cli._dispatch_debug_alert(tg, em, None, payload=payload, now="T")
    assert results["telegram"][0] is True
    assert results["email"][0] is True
    assert len(tg.calls) == 1 and len(em.calls) == 1


def test_debug_enable_sends_alert_to_both_channels(tmp_path):
    flag = tmp_path / "debug.flag"
    cfg_path = tmp_path / "controller.toml"
    cfg_path.write_text(_MIN_TOML)
    tg, em = DryRunSink(label="telegram"), DryRunSink(label="email")
    with mock.patch.object(cli, "DEFAULT_DEBUG_FLAG", str(flag)), \
         mock.patch.object(cli, "_build_alert_sinks", return_value=(tg, em)), \
         mock.patch("subprocess.run"):
        rc = cli.run([
            "debug", "enable", "--no-restart",
            "--config", str(cfg_path), "--db-path", str(tmp_path / "state.sqlite"),
        ])
    assert rc == 0
    assert len(tg.calls) == 1 and len(em.calls) == 1
    assert tg.calls[0].kind == "debug_mode_enabled"
