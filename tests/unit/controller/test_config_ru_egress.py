"""Tests for [ru_egress] parsing in controller.toml (V5 Task 6a)."""
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


def test_ru_egress_ja3_reference_path_parsed(tmp_path):
    """[ru_egress] ja3_reference_path is parsed onto the config."""
    p = _write(
        tmp_path,
        """
[ru_egress]
ja3_reference_path = "/etc/mthydra/ja3_reference.json"
""",
    )
    cfg = load_config(p)
    assert cfg.ru_egress is not None
    assert cfg.ru_egress.ja3_reference_path == "/etc/mthydra/ja3_reference.json"


def test_ru_egress_absent_section_is_none(tmp_path):
    """When [ru_egress] is absent, cfg.ru_egress is None."""
    p = _write(tmp_path)
    cfg = load_config(p)
    assert cfg.ru_egress is None
