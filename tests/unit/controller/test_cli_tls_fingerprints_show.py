"""Tests for the `tls-fingerprints-show` CLI subcommand (V1 Task 8)."""
from pathlib import Path

from mthydra.controller.cli import run

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
object_lock_days = 30
[gap_monitor]
poll_interval_minutes = 30
alarm_threshold_hours = 48
recipient_email = "op@example.org"
"""


def _write(tmp_path: Path, extra: str = "") -> Path:
    p = tmp_path / "controller.toml"
    p.write_text(_MIN_TOML + extra)
    return p


def test_tls_fingerprints_show_prints_pool_and_percentages(tmp_path, capsys):
    cfg_path = _write(
        tmp_path,
        """
[descriptor.tls_fingerprints]
chrome = 60
firefox = 40
""",
    )
    rc = run(["tls-fingerprints-show", "--config", str(cfg_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "chrome" in out
    assert "firefox" in out
    assert "weight=60" in out
    assert "(~60%)" in out
    assert "weight=40" in out
    assert "(~40%)" in out


def test_tls_fingerprints_show_none_configured(tmp_path, capsys):
    cfg_path = _write(tmp_path)
    rc = run(["tls-fingerprints-show", "--config", str(cfg_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "tls_fingerprints: (none configured" in out
    assert "chrome" in out  # fallback name mentioned in the message
