from unittest import mock

from mthydra.controller import cli, debug_flag


def test_debug_enable_writes_flag_and_restarts(tmp_path):
    flag = tmp_path / "debug.flag"
    with mock.patch.object(cli, "DEFAULT_DEBUG_FLAG", str(flag)), \
         mock.patch("subprocess.run") as run:
        rc = cli.run(["debug", "enable", "--ttl-hours", "6"])
    assert rc == 0
    f = debug_flag.read_flag(flag)
    assert f is not None and f.ttl_hours == 6.0
    run.assert_called_once()
    assert run.call_args.args[0] == ["systemctl", "restart", "mthydra-controller"]


def test_debug_enable_no_restart_skips_systemctl(tmp_path):
    flag = tmp_path / "debug.flag"
    with mock.patch.object(cli, "DEFAULT_DEBUG_FLAG", str(flag)), \
         mock.patch("subprocess.run") as run:
        rc = cli.run(["debug", "enable", "--no-restart"])
    assert rc == 0
    assert debug_flag.read_flag(flag) is not None
    run.assert_not_called()


def test_debug_disable_clears_flag(tmp_path):
    flag = tmp_path / "debug.flag"
    debug_flag.write_flag(flag, now=0.0)
    with mock.patch.object(cli, "DEFAULT_DEBUG_FLAG", str(flag)), \
         mock.patch("subprocess.run"):
        rc = cli.run(["debug", "disable", "--no-restart"])
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
