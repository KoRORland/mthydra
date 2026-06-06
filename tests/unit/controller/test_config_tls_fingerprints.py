"""Tests for [descriptor.tls_fingerprints] parsing in controller.toml (V1 Task 6)."""
from pathlib import Path

from mthydra.controller.config import load_config

_MINIMAL_TOML = """
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
    p = tmp_path / "c.toml"
    p.write_text(_MINIMAL_TOML + extra)
    return p


def test_descriptor_tls_fingerprints_parsed(tmp_path):
    """[descriptor.tls_fingerprints] subtable is parsed into sorted tuple of pairs."""
    p = _write(
        tmp_path,
        """
[descriptor.tls_fingerprints]
chrome = 60
firefox = 40
""",
    )
    cfg = load_config(p)
    assert cfg.descriptor.tls_fingerprints == (("chrome", 60), ("firefox", 40))


def test_descriptor_no_tls_fingerprints_defaults_empty(tmp_path):
    """When [descriptor.tls_fingerprints] is absent, field defaults to empty tuple."""
    p = _write(tmp_path)
    cfg = load_config(p)
    assert cfg.descriptor.tls_fingerprints == ()
