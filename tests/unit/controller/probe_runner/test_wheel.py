from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import pytest

from mthydra.controller.probe_runner import wheel as wheel_mod
from mthydra.controller.state import schema


@pytest.fixture
def seeded_db(tmp_path):
    db = tmp_path / "state.sqlite"
    conn = sqlite3.connect(str(db))
    schema.apply_schema(conn)
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute(
        "INSERT INTO ru_boxes (box_id, provider, region, public_ip, sni,"
        " state, image_version, created_at) VALUES (?, ?, ?, ?, ?, 'live', ?, ?)",
        ("b-1", "timeweb", "ru-msk-1", "203.0.113.10",
         "www.cloudflare.com", "iv-v2.2.8", now))
    conn.execute(
        "INSERT INTO probe_vantages (vantage_id, label, source_kind, state,"
        " added_at, attested_at, ssh_host, ssh_port, ssh_user, ssh_key_path,"
        " ssh_known_hosts_path) VALUES (?, ?, 'cloud-cis', 'active', ?, ?,"
        " ?, ?, ?, ?, ?)",
        ("ru-msk-1", "ru-msk-1", now, now,
         "203.0.113.5", 22, "probe", "/k", "/kh"))
    conn.commit()
    conn.close()
    return db


def test_wheel_tick_dispatches_probers_and_ingests(monkeypatch, seeded_db):
    monkeypatch.setattr(wheel_mod, "ssh_cmd",
                        lambda v, *c, **kw: None)
    monkeypatch.setattr(wheel_mod.probers, "probe_tls_fall_through",
                        lambda fn, ip, sni: ("pass", "tls evidence"))
    monkeypatch.setattr(wheel_mod.probers, "probe_cover_consistency",
                        lambda fn, ip, sni: ("pass", "cover evidence"))
    monkeypatch.setattr(wheel_mod.probers, "probe_surface_scan",
                        lambda fn, ip: ("pass", "surface evidence"))
    recorded = []
    monkeypatch.setattr(wheel_mod, "_record_probe",
        lambda **kw: recorded.append(kw))

    w = wheel_mod.ProbeRunnerWheel(
        db_path=str(seeded_db), interval_seconds=1800, max_concurrent=2,
        mode="offline",
    )
    w.tick()
    assert len(recorded) == 3
    checks = sorted(r["check_type"] for r in recorded)
    assert checks == ["cover_domain_consistency", "surface_scan",
                      "tls_fall_through"]
    assert all(r["box_id"] == "b-1" for r in recorded)
    assert all(r["vantage_id"] == "ru-msk-1" for r in recorded)
    assert all(r["status"] == "pass" for r in recorded)


def test_wheel_tick_skips_vantage_without_ssh(monkeypatch, seeded_db):
    conn = sqlite3.connect(str(seeded_db))
    conn.execute("UPDATE probe_vantages SET ssh_host=NULL WHERE vantage_id=?",
                 ("ru-msk-1",))
    conn.commit()
    conn.close()
    recorded = []
    monkeypatch.setattr(wheel_mod, "_record_probe",
                        lambda **kw: recorded.append(kw))
    w = wheel_mod.ProbeRunnerWheel(db_path=str(seeded_db),
                                   interval_seconds=1800,
                                   max_concurrent=2, mode="offline")
    w.tick()
    assert recorded == []
