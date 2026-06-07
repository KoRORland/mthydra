"""Tests for the `fingerprint-staleness-show` CLI subcommand (V5 Task 6b)."""
from __future__ import annotations

from pathlib import Path

import pytest

from mthydra.controller.cli import run
from mthydra.controller.state.db import connect
from mthydra.controller.state.descriptor import insert_signing_key
from mthydra.controller.state.obligations import set_obligation
from mthydra.controller.state.schema import apply_schema
from mthydra.descriptor.keys import generate_keypair
from mthydra.descriptor.sign import sign_new_descriptor

_NOW = "2026-06-06T00:00:00Z"

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


def _write_toml(tmp_path: Path, extra: str = "") -> Path:
    p = tmp_path / "controller.toml"
    p.write_text(_MIN_TOML + extra)
    return p


def _seeded_db(tmp_path: Path) -> Path:
    db = tmp_path / "state.sqlite"
    conn = connect(db)
    apply_schema(conn)
    priv, pub = generate_keypair()
    insert_signing_key(conn, 1, priv, pub, _NOW)
    sign_new_descriptor(
        conn, now_iso=_NOW, valid_until_iso="2026-06-07T00:00:00Z",
        tls_fingerprints=(("chrome", 60),),
    )
    conn.commit()
    conn.close()
    return db


@pytest.fixture
def seeded_db(tmp_path):
    return _seeded_db(tmp_path)


def test_fingerprint_staleness_show_reports_stale(tmp_path, seeded_db, capsys):
    cfg_path = _write_toml(tmp_path)
    conn = connect(seeded_db)
    set_obligation(
        conn, obligation_id="tls_fingerprint_stale::chrome",
        last_proven_at=_NOW, proven_by="reality_observer", next_due_at=_NOW,
        details='{"observed_ja3": "deadbeef"}',
    )
    conn.commit()
    conn.close()

    rc = run([
        "fingerprint-staleness-show",
        "--config", str(cfg_path),
        "--db-path", str(seeded_db),
    ])
    out = capsys.readouterr().out
    assert rc == 0
    assert "STALE" in out
    assert "chrome" in out


def test_fingerprint_staleness_show_reports_ok_when_no_stale_row(tmp_path, seeded_db, capsys):
    cfg_path = _write_toml(tmp_path)

    rc = run([
        "fingerprint-staleness-show",
        "--config", str(cfg_path),
        "--db-path", str(seeded_db),
    ])
    out = capsys.readouterr().out
    assert rc == 0
    assert "chrome: OK" in out
    assert "STALE" not in out


def test_fingerprint_staleness_show_no_descriptor(tmp_path, capsys):
    db = tmp_path / "state.sqlite"
    conn = connect(db)
    apply_schema(conn)
    conn.commit()
    conn.close()
    cfg_path = _write_toml(tmp_path)

    rc = run([
        "fingerprint-staleness-show",
        "--config", str(cfg_path),
        "--db-path", str(db),
    ])
    out = capsys.readouterr().out
    assert rc == 0
    assert "no descriptor / no fingerprints configured" in out


def test_fingerprint_staleness_show_config_error(tmp_path, seeded_db, capsys):
    bad_cfg = tmp_path / "bad.toml"
    bad_cfg.write_text("not valid toml [[[")

    rc = run([
        "fingerprint-staleness-show",
        "--config", str(bad_cfg),
        "--db-path", str(seeded_db),
    ])
    err = capsys.readouterr().err
    assert rc == 2
    assert "fingerprint-staleness-show" in err
